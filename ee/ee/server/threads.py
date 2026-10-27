"""线程 = 一个 Session + 显示用条目（items）。把内核事件翻译成 item/started|delta|updated 通知。

条目类型（与桌面端 src/types.ts 一致）：
  user        {text, queued?, queue?: steer|follow}（queued = 运行中排队、还没送入；送入后去掉 queued、保留 queue 作标签）
  assistant   {text, reasoning, streaming}
  tool        {call_id, name, args, summary, status: running|ok|error|denied|interrupted, output, details}
              （details = 只给界面的结构化数据，见 ee/tools/details.py；diff 在 details.diff。旧会话的 meta/diff 读取时迁移）
  approval    {call_id, tool, title, detail, prefix, status: pending|once|always|deny|cancelled}
  plan        {items: [{content, status}]}
  notice      {text, level}
  error       {message}
  turn_end    {reason, duration_ms, usage}
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Callable

from ee.config import load_config
from ee.core.events import (ApprovalDecision, ApprovalRequest, AssistantMessage, Error, Notice, PlanUpdate,
                            ReasoningDelta, Status, StepStart, TextDelta, ToolEnd, ToolOutput, ToolStart, TurnDone,
                            TurnStart, Usage)
from ee.core.events import QuestionRequest, QueueDelivered, QueueReturned, SubagentEnd, SubagentStart, SubagentUpdate
from ee.core import queue as Q
from ee.core.permissions import MODES
from ee.core.subagent import apply_event as apply_subagent_event
from ee.core.session import Session
from ee.core.store import SessionStore, _title

log = logging.getLogger("ee.thread")

UI_OUTPUT_LIMIT = 30_000          # 发给界面的单个工具输出上限（模型侧另有截断规则）


def _clip(s: str, n: int = UI_OUTPUT_LIMIT) -> str:
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half] + f"\n\n…（界面省略了中间 {len(s) - n} 个字符）…\n\n" + s[-half:]


def _migrate_tool_item(it: dict) -> None:
    """旧版条目 {meta, diff} → {details}（diff 并入 details.diff）。"""
    if "meta" not in it and "diff" not in it:
        return
    d = dict(it.pop("meta", None) or {})
    d.update(it.get("details") or {})
    diff = it.pop("diff", "") or ""
    if diff and not d.get("diff"):
        d["diff"] = diff
    it["details"] = d


def clean_title(t: str) -> str:
    """模型给的标题：取第一行、去引号/句号/“标题：”前缀，最多 40 字。"""
    t = (t or "").strip().splitlines()[0].strip() if (t or "").strip() else ""
    for p in ("标题：", "标题:", "Title:", "title:"):
        if t.startswith(p):
            t = t[len(p):].strip()
    t = t.strip("\"'“”‘’《》`*#。.！!").strip()
    return t[:40]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Thread:
    def __init__(self, tid: str, home: Path, notify: Callable[[str, dict], None], meta: dict,
                 messages: list[dict] | None = None, items: list[dict] | None = None,
                 client_factory: Callable | None = None, persist: bool = True,
                 configure: Callable | None = None):
        self.id = tid
        self.home = home
        self.notify = notify
        self.meta = dict(meta)
        self.items: list[dict] = list(items or [])
        for it in self.items:          # 排队条目只在送入后才落盘；旧版本落盘的 queued 标记其实都已送入
            if it.get("type") == "user":
                it.pop("queued", None)
            elif it.get("type") == "tool":
                _migrate_tool_item(it)
        self._by_id = {it["id"]: it for it in self.items}
        self._seq = max([int(it.get("seq", 0)) for it in self.items] + [len(self.items)]) + 1
        self.client_factory = client_factory
        self.persist = persist
        self.configure = configure     # 建会话前改配置的钩子（ee exec 用：关 ask_user、步数上限、额外规则）
        self.session: Session | None = None
        self._pending_messages = messages or []
        self.task: asyncio.Task | None = None
        self.approvals: dict[str, tuple[asyncio.Future, dict]] = {}
        self.cur_assistant: dict | None = None
        self.plan_item: dict | None = None
        self.turn_started_at = 0.0
        self.usage: dict = {}
        self.context_left: float | None = None
        self.compacting = False
        self.status_text = ""          # 临时状态（正在连接模型…），不落盘

    # ------------------------------------------------------------ 内核会话（懒加载）
    @property
    def cwd(self) -> Path:
        return Path(self.meta["cwd"])

    def ensure_session(self) -> Session:
        if self.session is not None:
            return self.session
        cfg = load_config(self.cwd)
        if self.configure is not None:
            self.configure(cfg)
        model = self.meta.get("model") or None
        if model and model not in cfg.model_names() and "/" not in model:
            model = None
        mode = self.meta.get("mode") if self.meta.get("mode") in MODES else None
        # 精简模式（profile）跟着对话走：记在 meta 里，设置页切换只影响新对话；功能上线前的旧对话按 full
        agent = self.meta.get("profile") or ("full" if self._pending_messages else None)
        s = Session(cfg, model=model, cwd=self.cwd, mode=mode, client_factory=self.client_factory,
                    persist=self.persist, home=self.home, agent=agent)
        s.store = SessionStore(self.home, session_id=self.id, enabled=self.persist)
        if self._pending_messages:
            s.history = list(self._pending_messages)
            s.turns = sum(1 for it in self.items if it.get("type") == "user")
            self._pending_messages = []
            # 重启后恢复回合起点（编辑重发 / 回退用）：只认能对上原文的
            s.turn_starts = sorted({int(it["hist"]) for it in self.items
                                    if it.get("type") == "user" and isinstance(it.get("hist"), int)
                                    and s.user_text_at(int(it["hist"])) is not None})
        # 磁盘快照（D4-1）：从用户条目重建每回合前后 tree，重启后 /undo、编辑重发照样能恢复文件
        s.turn_trees = [{"before": it["tree"], "after": it.get("tree_after"), "undone": bool(it.get("undone"))}
                        for it in self.items if it.get("type") == "user" and it.get("tree")]
        self.meta["model"] = s.spec.alias
        self.meta["mode"] = s.policy.mode
        self.meta["profile"] = s.agent_profile
        # D5-9：会话级推理强度 / 回答风格（存在 meta 里，重启后照旧）；重启前已花的钱计入会话上限
        if "effort" in self.meta:
            try:
                s.set_effort(self.meta.get("effort") or "", remember=False)
            except ValueError:
                pass
        if self.meta.get("style"):
            try:
                s.set_style(self.meta["style"])
            except ValueError:
                pass
        last = next((it for it in reversed(self.items) if it.get("type") == "turn_end"), None)
        u = (last or {}).get("usage") or {}
        if u.get("cost"):
            s.spent_before = {str(u.get("currency") or "¥"): float(u["cost"])}
        s.bg.on_change = lambda info: self.notify("shell/updated", {"thread_id": self.id, "proc": info})
        self.session = s
        for n in s.notices:
            self._add({"type": "notice", "text": n, "level": "warn"}, persist=False)
        s.notices.clear()
        return s

    async def ensure_session_async(self) -> Session:
        """在工作线程里建会话（Session 初始化会跑 git status 等子进程），不阻塞事件循环；并发调用只建一次。"""
        if self.session is not None:
            return self.session
        lock = getattr(self, "_ensure_lock", None)
        if lock is None:
            lock = self._ensure_lock = asyncio.Lock()
        async with lock:
            if self.session is None:
                await asyncio.to_thread(self.ensure_session)
        return self.session

    def reload_config(self) -> None:
        if self.session is None or self.session.running:
            return
        cfg = load_config(self.cwd)
        self.session.config = cfg
        alias = self.session.spec.alias
        try:
            self.session.spec = cfg.resolve_model(alias)   # 例如价格 / key 改了
            self.session._client = None
        except ValueError:
            self.session.set_model(cfg.default_model)
            self.meta["model"] = self.session.spec.alias

    # ------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def status(self) -> str:
        if self.compacting:
            return "compacting"
        if not self.running:
            return "idle"
        return "waiting_approval" if self.approvals else "running"

    def summary(self) -> dict:
        return {"id": self.id, "title": self.meta.get("title") or "新对话", "cwd": self.meta.get("cwd", ""),
                "model": self.meta.get("model", ""), "mode": self.meta.get("mode", ""),
                "created": self.meta.get("created"), "updated": self.meta.get("updated"), "status": self.status,
                "parent_id": self.meta.get("parent_id"), "source": self.meta.get("source"),
                "worktree": self.meta.get("worktree"),          # D5-1：在 worktree 里的对话 {path, branch, repo, name}
                "effort": self.session.effort if self.session else self.meta.get("effort", ""),   # D5-9
                "effort_support": self.session.effort_support() if self.session else None,
                "style": self.session.style if self.session else (self.meta.get("style") or "default"),
                "workspace": (self.meta.get("worktree") or {}).get("repo") or self.meta.get("cwd", "")}

    def detail(self) -> dict:
        s = self.session
        git = None
        if s is not None:
            git = {"is_repo": s.git.is_repo, "branch": s.git.branch, "changed": s.git.changed,
                   "untracked": s.git.untracked}
        return {**self.summary(), "items": self.items, "usage": self.usage, "context_left": self.context_left,
                "shell": (s.shell.display if s and s.shell else None), "git": git,
                "instructions": self.instructions(), "status_text": self.status_text}

    def instructions(self) -> list[str]:
        """本会话加载的 AGENTS.md（绝对路径）。"""
        s = self.session
        return list(getattr(s, "instruction_sources", None) or []) if s is not None else []

    def save_meta(self) -> None:
        self.meta["updated"] = time.time()
        if self.persist:
            SessionStore(self.home, session_id=self.id).meta(self.meta)

    # ------------------------------------------------------------ 条目
    def _store_item(self, it: dict) -> None:
        if self.persist:
            (self.session.store if self.session else SessionStore(self.home, session_id=self.id)).item(it)

    def _add(self, it: dict, persist: bool = True) -> dict:
        it.setdefault("id", _new_id(it["type"][:2]))
        it.setdefault("ts", time.time())
        it["seq"] = self._seq          # 显示顺序（有的条目结束时才落盘，恢复时按它排序）
        self._seq += 1
        self.items.append(it)
        self._by_id[it["id"]] = it
        self.notify("item/started", {"thread_id": self.id, "item": it})
        if persist:
            self._store_item(it)
        return it

    def _update(self, it: dict, persist: bool = True) -> None:
        self.notify("item/updated", {"thread_id": self.id, "item": it})
        if persist:
            self._store_item(it)

    def _delta(self, it: dict, field: str, text: str) -> None:
        it[field] = (it.get(field) or "") + text
        self.notify("item/delta", {"thread_id": self.id, "item_id": it["id"], "field": field, "delta": text})

    # ------------------------------------------------------------ 回合
    def set_options(self, effort=None, style: str | None = None) -> dict:
        """会话级选项（D5-9）：推理强度（同时按模型记住）、回答风格。None = 不变。"""
        s = self.ensure_session()
        msgs = []
        if effort is not None and effort != s.effort:
            msgs.append(s.set_effort(effort))
            self.meta["effort"] = s.effort
        if style is not None and style != s.style:
            msgs.append(s.set_style(style))
            self.meta["style"] = s.style
        if msgs:
            self.save_meta()
            self.notify("thread/updated", {"thread": self.summary()})
        return {"effort": s.effort, "style": s.style, "effort_support": s.effort_support(), "messages": msgs}

    def start_turn(self, text: str, model: str | None = None, mode: str | None = None,
                   images: list[str] | None = None, attachments: list[dict] | None = None,
                   display: str | None = None, readonly: bool = False, kind: str | None = None,
                   effort=None, style: str | None = None, queue: str | None = None) -> dict:
        """images：已保存的附件路径；attachments：对应的界面元数据（D3-D）。
        display：界面上显示的用户消息（模型收到的仍是 text，例如 /review 拼好的长提示）；
        readonly：这一回合临时只读，回合结束恢复原模式（/review）；kind：用户条目的类型标记（review）。
        queue：运行中发送时排进哪条队列：steer 插话（默认）/ follow 完成后追加；空闲时忽略。"""
        s = self.ensure_session()
        if images and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再发图片")
        if (readonly or display) and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再开始审查")
        if model and model != s.spec.alias:
            s.set_model(model)
            self.meta["model"] = s.spec.alias
        if mode and mode != s.policy.mode:
            s.set_mode(mode)
            self.meta["mode"] = mode
        if effort is not None or style is not None:
            self.set_options(effort, style)
        user_item = {"type": "user", "text": display or text}
        if kind:
            user_item["kind"] = kind
        if attachments:
            user_item["attachments"] = attachments
        if self.running:
            if queue not in (None, "", *Q.KINDS):
                raise ValueError(f"未知的排队类型：{queue}")
            qkind = queue or Q.STEER
            user_item.update(queued=True, queue=qkind)
            user = self._add(user_item, persist=False)      # 送入时才落盘（撤回 / 退回的不留痕）
            s.queue_input(text, qkind, qid=user["id"])
            return {"queued": True, "item_id": user["id"], "queue": qkind}
        user = self._add(user_item)
        if not any(it.get("type") == "user" for it in self.items[:-1]) or self.meta.get("title") in (None, "", "新对话"):
            self.meta["title"] = _title(display or text)
        self.save_meta()
        if readonly and s.policy.mode != "read-only":
            self._restore_mode = s.policy.mode
            s.set_mode("read-only")
        s._turn_role = "review" if kind == "review" else None      # 审查回合按 roles.review 取模型（D5-9）
        self.notify("thread/updated", {"thread": self.summary()})
        self.task = asyncio.create_task(self._consume(text, images))
        self.notify("thread/updated", {"thread": self.summary()})
        return {"queued": False, "item_id": user["id"]}

    def interrupt(self) -> bool:
        if self.session is None:
            return False
        return self.session.interrupt()

    def withdraw(self, item_id: str) -> dict:
        """撤回还没送入的排队消息，返回原文（界面放回输入框）。"""
        s = self.session
        q = s.withdraw_input(item_id) if s is not None else None
        if q is None:
            raise ValueError("这条消息已经送给 ee 了（或已退回），不能撤回")
        self._drop_item(item_id)
        return {"ok": True, "text": q.text, "queue": q.kind}

    def _drop_item(self, item_id: str) -> dict | None:
        it = self._by_id.pop(item_id, None)
        if it is not None and it in self.items:
            self.items.remove(it)
            self.notify("item/removed", {"thread_id": self.id, "item_id": item_id})
        return it

    def answer(self, call_id: str, answer: str) -> dict:
        """回答 ask_user 的提问（D4-8）。"""
        entry = self.approvals.get(call_id)
        if entry is None or entry[1].get("type") != "question":
            raise ValueError("这个提问已经回答过或已失效")
        answer = str(answer or "").strip()
        if not answer:
            raise ValueError("回答不能为空")
        self.approvals.pop(call_id, None)
        fut, it = entry
        it["status"] = "answered"
        it["answer"] = answer[:4000]
        self._update(it)
        if not fut.done():
            fut.set_result(answer)
        self.notify("thread/updated", {"thread": self.summary()})
        return {"ok": True}

    def respond(self, call_id: str, choice: str, reason: str = "") -> dict:
        if choice not in ("once", "always", "forever", "deny"):
            raise ValueError(f"未知审批选项：{choice}")
        entry = self.approvals.get(call_id)
        if entry is None or entry[1].get("type") != "approval":
            raise ValueError("这个审批已经处理过或已失效")
        self.approvals.pop(call_id, None)
        fut, it = entry
        it["status"] = choice
        if reason:
            it["reason"] = reason
        self._update(it)
        if not fut.done():
            fut.set_result(ApprovalDecision("deny_with_reason" if (choice == "deny" and reason) else choice, reason))
        self.notify("thread/updated", {"thread": self.summary()})
        return {"ok": True}

    async def _consume(self, text: str, images: list[str] | None = None) -> None:
        s = self.session
        assert s is not None
        self.turn_started_at = time.monotonic()
        self.cur_assistant = None
        self.plan_item = None
        reason = "error"
        try:
            async for ev in s.run_turn(text, images):
                if isinstance(ev, TurnDone):
                    reason = ev.reason
                    if ev.tree_after:
                        self._mark_tree_after(ev.tree_after)
                self._on_event(ev)
        except Exception as e:  # noqa: BLE001
            log.error("回合异常（会话 %s）", self.id, exc_info=True)
            self._add({"type": "error", "message": f"内部错误：{type(e).__name__}: {e}"})
        finally:
            restore = getattr(self, "_restore_mode", None)
            if restore:
                self._restore_mode = None
                s.set_mode(restore)
            s._turn_role = None
            try:
                self._finish(reason)
            except Exception:  # noqa: BLE001
                log.error("回合收尾异常（会话 %s）", self.id, exc_info=True)
                self.notify("turn/completed", {"thread_id": self.id, "reason": "error", "duration_ms": 0,
                                               "usage": self.usage, "context_left": self.context_left})

    def _finish(self, reason: str) -> None:
        if self.status_text:
            self.status_text = ""
            self.notify("turn/status", {"thread_id": self.id, "text": ""})
        log.info("回合结束（会话 %s）：%s，用时 %.1fs", self.id, reason, time.monotonic() - self.turn_started_at)
        if self.cur_assistant is not None:
            self.cur_assistant["streaming"] = False
            self._update(self.cur_assistant)
            self.cur_assistant = None
        for it in self.items:
            if it.get("type") == "tool" and it.get("status") == "running":
                it["status"] = "interrupted"
                self._update(it)
        for cid, (fut, it) in list(self.approvals.items()):
            it["status"] = "cancelled"
            self._update(it)
            if not fut.done():
                fut.cancel()
        self.approvals.clear()
        dur = int((time.monotonic() - self.turn_started_at) * 1000)
        self._add({"type": "turn_end", "reason": reason, "duration_ms": dur, "usage": dict(self.usage)})
        self.save_meta()
        self.notify("turn/completed", {"thread_id": self.id, "reason": reason, "duration_ms": dur,
                                       "usage": self.usage, "context_left": self.context_left,
                                       "instructions": self.instructions()})
        self.notify("thread/updated", {"thread": {**self.summary(), "status": "idle"}})
        if reason == "completed":
            self._maybe_ai_title()

    # ------------------------------------------------------------ 模型生成标题（D5-9，roles.title）
    def _maybe_ai_title(self) -> None:
        s = self.session
        if s is None or self.meta.get("title_ai_done") or self.meta.get("source") in ("exec", "automation"):
            return
        if not bool((s.config.data.get("title") or {}).get("ai", False)):
            return
        users = [it for it in self.items if it.get("type") == "user"]
        if len(users) != 1:
            return
        reply = next((it.get("text") or "" for it in reversed(self.items) if it.get("type") == "assistant"), "")
        self.meta["title_ai_done"] = True
        first = str(users[0].get("text") or "")
        old = self.meta.get("title")
        try:
            self._title_task = asyncio.get_running_loop().create_task(self._ai_title(first, reply, old))
        except RuntimeError:
            pass

    async def _ai_title(self, first: str, reply: str, old: str | None) -> None:
        s = self.session
        try:
            from ee.core.context import load_prompt
            prompt = f"用户的第一条消息：\n{first[:2000]}\n\n助手的回答开头：\n{reply[:800]}"
            title = await asyncio.wait_for(s.side_complete(load_prompt("title.md"), prompt, role="title"), 30)
        except Exception as e:  # noqa: BLE001
            log.info("生成标题失败（会话 %s）：%s", self.id, e)
            return
        title = clean_title(title)
        if not title or self.meta.get("title") != old:      # 生成期间被改过名就不覆盖
            return
        self.meta["title"] = title
        self.save_meta()
        u = s.stats.totals()
        if u.get("cost") is not None:
            self.usage = dict(u)
            self.notify("thread/usage", {"thread_id": self.id, "usage": self.usage, "context_left": self.context_left})
        self.notify("thread/updated", {"thread": self.summary()})

    def _mark_turn_start(self, tree: str | None = None) -> None:
        """给本回合的用户条目记下它在历史里的位置和文件快照层号（编辑重发 / 分叉用，D3-E）。"""
        s = self.session
        if s is None or not s.turn_starts:
            return
        for it in reversed(self.items):
            if it.get("type") == "user":
                if it.get("queued"):           # 还在排队的不是本回合的开头
                    continue
                if "hist" not in it:
                    it["hist"] = s.turn_starts[-1]
                    it["snap"] = len(s.state.snapshots) - 1
                    it["snap_epoch"] = s.snap_epoch
                    if tree:
                        it["tree"] = tree
                    self._update(it)
                break

    def _mark_tree_after(self, tree: str) -> None:
        for it in reversed(self.items):
            if it.get("type") == "user" and it.get("tree"):
                if "tree_after" not in it:
                    it["tree_after"] = tree
                    self._update(it)
                break

    def mark_undone(self, tree: str) -> None:
        """/undo 走磁盘快照时，把对应用户条目标成已撤销（持久化，重启后不会再撤一次）。"""
        for it in reversed(self.items):
            if it.get("type") == "user" and it.get("tree") == tree:
                it["undone"] = True
                self._update(it)
                break

    def _assistant(self) -> dict:
        if self.cur_assistant is None:
            self.cur_assistant = self._add({"type": "assistant", "text": "", "reasoning": "", "streaming": True},
                                           persist=False)
        return self.cur_assistant

    def _on_event(self, ev) -> None:
        if isinstance(ev, Status):
            if ev.text != self.status_text:
                self.status_text = ev.text
                self.notify("turn/status", {"thread_id": self.id, "text": ev.text})
        elif isinstance(ev, TurnStart):
            self._mark_turn_start(ev.tree)
            self.notify("turn/started", {"thread_id": self.id, "turn": ev.turn})
        elif isinstance(ev, StepStart):
            self.cur_assistant = None
        elif isinstance(ev, TextDelta):
            self._delta(self._assistant(), "text", ev.text)
        elif isinstance(ev, ReasoningDelta):
            self._delta(self._assistant(), "reasoning", ev.text)
        elif isinstance(ev, AssistantMessage):
            it = self.cur_assistant
            if it is None and (ev.text or ev.reasoning):
                it = self._assistant()
            if it is not None:
                it["text"], it["reasoning"], it["streaming"] = ev.text or "", ev.reasoning or "", False
                if it["text"] or it["reasoning"]:
                    self._update(it)
                else:
                    self.items.remove(it)
                    self.notify("item/removed", {"thread_id": self.id, "item_id": it["id"]})
            self.cur_assistant = None
        elif isinstance(ev, ToolStart):
            self.cur_assistant = None
            self._add({"id": f"tool_{ev.call_id}", "type": "tool", "call_id": ev.call_id, "name": ev.name,
                       "args": ev.args, "summary": ev.summary, "status": "running", "output": "",
                       "details": {}}, persist=False)
        elif isinstance(ev, ToolOutput):
            it = self._by_id.get(f"tool_{ev.call_id}")
            if it is not None and len(it.get("output") or "") < UI_OUTPUT_LIMIT:
                self._delta(it, "output", ev.text)
        elif isinstance(ev, ToolEnd):
            it = self._by_id.get(f"tool_{ev.call_id}")
            if it is None:
                it = self._add({"id": f"tool_{ev.call_id}", "type": "tool", "call_id": ev.call_id, "name": ev.name,
                                "args": {}}, persist=False)
            status = "ok" if ev.ok else ("denied" if ev.details.get("denied") else "error")
            details = dict(ev.details)
            if ev.diff and not details.get("diff"):
                details["diff"] = _clip(ev.diff)
            it.update({"status": status, "output": _clip(ev.output or ""), "summary": ev.summary or it.get("summary"),
                       "details": details})
            self._update(it)
        elif isinstance(ev, QuestionRequest):
            it = self._add({"type": "question", "call_id": ev.call_id, "question": ev.question,
                            "options": list(ev.options or []), "multiple": bool(ev.multiple), "status": "pending"})
            if ev.future is not None:
                self.approvals[ev.call_id] = (ev.future, it)
            self.notify("thread/updated", {"thread": self.summary()})
        elif isinstance(ev, ApprovalRequest):
            from ee.core.rules import rule_from_decision
            it = self._add({"type": "approval", "call_id": ev.call_id, "tool": ev.tool, "title": ev.title,
                            "detail": _clip(ev.detail or "", 20000), "prefix": ev.prefix, "status": "pending",
                            "rule": rule_from_decision(ev.tool, ev.prefix) or "",
                            **({"source": dict(ev.source)} if ev.source else {})})
            if ev.future is not None:
                self.approvals[ev.call_id] = (ev.future, it)
            self.notify("thread/updated", {"thread": self.summary()})
        elif isinstance(ev, PlanUpdate):
            if self.plan_item is None:
                self.plan_item = self._add({"type": "plan", "items": ev.items})
            else:
                self.plan_item["items"] = ev.items
                self._update(self.plan_item)
        elif isinstance(ev, Usage):
            self.usage = dict(ev.totals)
            s = self.session
            if s is not None:
                self.context_left = round(s.context_left(), 4)
            self.notify("thread/usage", {"thread_id": self.id, "usage": self.usage,
                                         "context_left": self.context_left})
        elif isinstance(ev, (SubagentStart, SubagentUpdate, SubagentEnd)):
            # 子代理进度挂在父 task 工具条目的 meta.subagent 上（界面渲染成子代理卡片）
            it = self._by_id.get(f"tool_{ev.call_id}")
            if it is not None:
                det = dict(it.get("details") or {})
                det["kind"] = "subagent"
                det["subagent"] = apply_subagent_event(det.get("subagent"), ev)
                it["details"] = det
                self._update(it, persist=False)
            if isinstance(ev, SubagentUpdate) and ev.kind == "usage" and ev.totals:
                # 子代理的用量实时并入主会话（D5-2：并行子代理跑的时候顶部花费也在涨）
                self.usage = dict(ev.totals)
                self.notify("thread/usage", {"thread_id": self.id, "usage": self.usage,
                                             "context_left": self.context_left})
        elif isinstance(ev, QueueDelivered):
            # 排队消息送入：挪到当前位置（重新编号）并落盘
            for qid in ev.ids:
                it = self._drop_item(qid)
                if it is not None:
                    it.pop("queued", None)
                    it["queue"] = ev.kind
                    self._add(it)
        elif isinstance(ev, QueueReturned):
            for q in ev.items:
                self._drop_item(q["id"])
            self.notify("turn/queue_returned", {"thread_id": self.id, "items": ev.items})
        elif isinstance(ev, Notice):
            self._add({"type": "notice", "text": ev.text, "level": ev.level})
        elif isinstance(ev, Error):
            self._add({"type": "error", "message": ev.message})
