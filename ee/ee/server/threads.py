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
import os
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
RUNS_KEEP = 5                     # meta 里只留最近几次派发（防止会话元数据无限长）


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


MAX_GOAL_ROUNDS = 20      # 界面上限：防一次传个巨大值把机器跑满


def _norm_goal(goal: dict) -> dict:
    """校验并规范化 Goal 判据。

    判据只从 start_turn 这条入口来，模型无法写入——那会让"测试通过"变成
    "不用测"。这里做三件事：命令不能为空、轮次必须是正整数且有上限、
    超时可选但给了必须是正数。
    """
    if not isinstance(goal, dict):
        raise ValueError("goal 必须是对象")
    cmd = str(goal.get("command") or "").strip()
    if not cmd:
        raise ValueError("goal.command 不能为空：没有判据的循环没有意义")
    raw = goal.get("max_rounds", 3)
    try:
        rounds = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"goal.max_rounds 必须是整数：{raw!r}") from None
    if rounds < 1:
        raise ValueError(f"goal.max_rounds 必须 >= 1：{rounds}")
    rounds = min(rounds, MAX_GOAL_ROUNDS)
    out = {"command": cmd[:2000], "max_rounds": rounds}
    if goal.get("timeout") is not None:
        try:
            t = float(goal["timeout"])
        except (TypeError, ValueError):
            raise ValueError("goal.timeout 必须是数字") from None
        if t > 0:
            out["timeout"] = min(t, 3600.0)
    return out


def spent_before_from_usage(u: dict | None) -> dict[str, float]:
    """从 turn_end 的 usage 快照恢复「重启前这个会话已花多少」，按币种。

    **为什么不能只看 u["cost"]**：v2 口径下混币会话的 `cost` 是 None（见
    core/usage.py），只读它会把整个已花金额丢掉，重启后会话预算从 0 重新算——
    用户以为还剩很多额度，实际早就超了。

    **为什么优先读 `spent_total`**：`turn_end.usage` 由 `Session.stats` 生成，而
    `stats` 每次重启都是新的 `UsageStats`，从 0 重新累加。只读最后一条快照，等于
    只恢复最后一个进程的花费——重启三次就丢掉前两段（实测 20261030-235538 丢 93.2%）。
    `spent_total` 在写入侧就把「重启前已花 + 本进程花费」相加，是跨重启的累计事实。

    兼容三种形态：`spent_total` 累计口径、v2 的 `costs` 分币种桶、v1 的单值
    `cost`+`currency`。后两者只在老会话（无 `spent_total`）时用到，恢复的是最后
    一段——比丢光好，但仍是少算，无法凭空补齐。
    未知金额（币种存在但 complete=false）**不恢复成 0 元**：有已知部分就恢复已知部分，
    完全不已知就不恢复，宁可少算也不假设没花钱。
    """
    out: dict[str, float] = {}
    if not isinstance(u, dict):
        return out
    total = u.get("spent_total")
    if isinstance(total, dict) and total:
        for cur, amount in total.items():
            try:
                value = float(amount)
            except (TypeError, ValueError):
                continue
            if value > 0:
                out[str(cur)] = value
        if out:
            return out
    costs = u.get("costs")
    if isinstance(costs, dict) and costs:
        for cur, bucket in costs.items():
            if not isinstance(bucket, dict):
                continue
            try:
                amount = float(bucket.get("known_amount") or 0)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                out[str(cur)] = amount
        if out:
            return out
    # v1 兼容：单值 cost + currency
    cost = u.get("cost")
    if cost is not None:
        try:
            amount = float(cost)
        except (TypeError, ValueError):
            return out
        if amount > 0:
            out[str(u.get("currency") or "¥")] = amount
    return out


def usage_snapshot(usage: dict | None, spent_before: dict | None) -> dict:
    """给 turn_end 的 usage 快照补一个跨重启累计字段 `spent_total`。

    **为什么写入侧做**：`usage` 来自 `Session.stats`，每个进程从 0 累加；重启次数
    越多，最后一条快照离真实总额越远。恢复侧只能看到最后一条快照，无从还原被覆盖
    的旧段——除非快照自身携带累计事实。这里在写入时就把「本进程之前的进程已花」
    （`spent_before`）加上去，于是任一条快照都是「会话至今花了多少」。
    """
    out = dict(usage or {})
    before = {str(k): float(v) for k, v in (spent_before or {}).items() if v}
    current = spent_before_from_usage(out)
    total: dict[str, float] = dict(before)
    for cur, amount in current.items():
        total[cur] = total.get(cur, 0.0) + amount
    if total:
        out["spent_total"] = total
    return out


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Thread:
    def __init__(self, tid: str, home: Path, notify: Callable[[str, dict], None], meta: dict,
                 messages: list[dict] | None = None, items: list[dict] | None = None,
                 client_factory: Callable | None = None, persist: bool = True,
                 configure: Callable | None = None, user_home: Path | None = None,
                 orchestrator=None):
        self.id = tid
        self.home = home
        self.user_home = Path(user_home) if user_home is not None else Path.home()
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
        self.deleted = False           # 已删除：迟到的条目/元数据不能再写回磁盘
        self.status_text = ""          # 临时状态（正在连接模型…），不落盘
        self.orchestrator = orchestrator   # 指挥会话（D6-1）：回合结束要回收名额

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
                    persist=self.persist, home=self.home, agent=agent, user_home=self.user_home,
                    orchestrator_role=self.meta.get("orchestrator_role"))
        # 指挥会话（D6-1）：工具只拿到窄接口与本会话 id，拿不到 Backend，也就碰不到别的会话
        s.orchestrator = self.orchestrator
        s.thread_id = self.id
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
        restored = spent_before_from_usage((last or {}).get("usage"))
        if restored:
            s.spent_before = restored
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
            self.session._drop_client()                    # 旧连接池要登记待释放，不能只丢引用
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
                # 指挥关系与普通分叉分开：parent_id 是分叉来源（branch.py 写入、左栏显示分叉图标），
                # orchestrator_parent_id 才是"由哪个指挥会话派发"，权限与树形都只认后者。
                "orchestrator_parent_id": self.meta.get("orchestrator_parent_id"),
                "orchestrator_role": self.meta.get("orchestrator_role"),
                "runs": list(self.meta.get("runs") or []),
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

    def _usage_snapshot(self) -> dict:
        """落盘用的 usage 快照：带上跨重启累计花费（见 threads.usage_snapshot）。

        对外通知（`thread/usage`、`thread/completed`）仍用 `self.usage` 原样，
        前端读取的字段形状不变——累计值只用于恢复，不参与展示。
        """
        s = self.session
        return usage_snapshot(self.usage, getattr(s, "spent_before", None) if s is not None else None)

    def save_meta(self) -> None:
        self.meta["updated"] = time.time()
        if self.persist and not self.deleted:
            SessionStore(self.home, session_id=self.id).meta(self.meta)

    # ------------------------------------------------------------ 条目
    def _store_item(self, it: dict) -> None:
        if self.deleted:
            return                     # 会话已删除：运行中的回合收尾也不重建文件
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
                   effort=None, style: str | None = None, queue: str | None = None,
                   goal: dict | None = None) -> dict:
        """images：已保存的附件路径；attachments：对应的界面元数据（D3-D）。
        display：界面上显示的用户消息（模型收到的仍是 text，例如 /review 拼好的长提示）；
        readonly：这一回合临时只读，回合结束恢复原模式（/review）；kind：用户条目的类型标记（review）。
        queue：运行中发送时排进哪条队列：steer 插话（默认）/ follow 完成后追加；空闲时忽略。
        goal：`{command, max_rounds}`。给了就走 Goal 循环（跑到判据通过或轮次用完）。
        判据只从这条入口来，模型无法自己改——那会让"测试通过"变成"不用测"。"""
        s = self.ensure_session()
        if goal is not None:
            goal = _norm_goal(goal)
        if images and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再发图片")
        if (readonly or display) and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再开始审查")
        if effort is not None or style is not None:
            self.set_options(effort, style)
        user_item = {"type": "user", "text": display or text}
        if kind:
            user_item["kind"] = kind
        if goal:
            # 记在用户条目上：界面据此显示"这一回合在追什么目标"，
            # 也让"判据从哪来"可追溯（只可能来自这里，不是模型写的）。
            user_item["goal"] = dict(goal)
        if attachments:
            user_item["attachments"] = attachments
        if self.running:
            if queue not in (None, "", *Q.KINDS):
                raise ValueError(f"未知的排队类型：{queue}")
            qkind = queue or Q.STEER
            user_item.update(queued=True, queue=qkind)
            user = self._add(user_item, persist=False)      # 送入时才落盘（撤回 / 退回的不留痕）
            s.queue_input(text, qkind, qid=user["id"])
            # model/mode 属于「这一回合怎么跑」。回合正跑着时改它，等于中途换模型：
            # 冻结前缀整段失效（缓存全 miss、双倍计费），已发出的 tool_call 还可能变
            # 「未知工具」。这里先记下来，等回合真正开始（非排队路径）再生效。
            if model or mode:
                self._pending_model = model or getattr(self, "_pending_model", None)
                self._pending_mode = mode or getattr(self, "_pending_mode", None)
            return {"queued": True, "item_id": user["id"], "queue": qkind}
        pending_model = getattr(self, "_pending_model", None)
        pending_mode = getattr(self, "_pending_mode", None)
        self._pending_model = self._pending_mode = None
        model = pending_model or model
        mode = pending_mode or mode
        if model and model != s.spec.alias:
            s.set_model(model)
            self.meta["model"] = s.spec.alias
        if mode and mode != s.policy.mode:
            s.set_mode(mode)
            self.meta["mode"] = mode
        user = self._add(user_item)
        self._new_run(text, initiator=kind or "user")
        if not any(it.get("type") == "user" for it in self.items[:-1]) or self.meta.get("title") in (None, "", "新对话"):
            self.meta["title"] = _title(display or text)
        self.save_meta()
        if readonly and s.policy.mode != "read-only":
            self._restore_mode = s.policy.mode
            s.set_mode("read-only")
        s._turn_role = "review" if kind == "review" else None      # 审查回合按 roles.review 取模型（D5-9）
        self.notify("thread/updated", {"thread": self.summary()})
        self.task = asyncio.create_task(self._consume(text, images, goal=goal))
        self.notify("thread/updated", {"thread": self.summary()})
        return {"queued": False, "item_id": user["id"]}

    def interrupt(self) -> bool:
        """用户点停止：停掉属于这个会话的一切工作，返回是否真的停到了东西。

        **为什么不能只转给 session.interrupt()**：Goal 循环跑在 self.task（_consume）
        里，时序是「判 → 跑 → 判」。用户可能正好停在**判**那一段（判据命令在子进程里
        跑），此时模型会话空闲、session.interrupt() 取消不了任何东西，返回 False。
        结果：界面提示“没停下来”，判据继续跑到超时，剩余轮次还会照常启动。

        先试 session（能停模型回合就按原来的路子停），不行再取消外层 task；
        两者都没有可停的东西才返回 False。
        """
        if self.session is not None and self.session.interrupt():
            return True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return True
        return False

    def withdraw(self, item_id: str) -> dict:
        """撤回还没送入的排队消息，返回原文（界面放回输入框）。"""
        s = self.session
        q = s.withdraw_input(item_id) if s is not None else None
        if q is None:
            raise ValueError("这条消息已经送给 ee 了（或已退回），不能撤回")
        self._drop_item(item_id)
        # 这条排队消息带来的 model/mode 随它一起作废，否则下一次「不带 model」的
        # 回合会静默套用被撤回那条的模型（独立审查发现）。
        self._pending_model = self._pending_mode = None
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

    async def _consume(self, text: str, images: list[str] | None = None,
                       goal: dict | None = None) -> None:
        s = self.session
        assert s is not None
        self.turn_started_at = time.monotonic()
        self.cur_assistant = None
        self.plan_item = None
        reason = "error"
        try:
            if goal:
                # Goal 模式：反复跑回合直到判据通过或轮次用完。
                # 回合本身仍走 s.run_turn（不新建 agent 循环），只是调用方变成循环。
                # 必须在这个 try 里：提前 return 会跳过下面的 finally，
                # _finish() 不执行 → 会话永远卡在 running。
                reason = await self._consume_goal(text, images, goal)
            else:
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

    async def _consume_goal(self, text: str, images: list[str] | None, goal: dict) -> str:
        """跑 Goal 循环，返回停止原因（met / exhausted / budget / interrupted / error）。

        回合执行仍是 s.run_turn：这里只负责"再跑一轮"和"判定"，不是第二个 agent 循环。
        每轮判定结果通过 goal_check 通知推给界面，用户能看到进度而不是干等。
        """
        from ee.core.goal import GoalSpec, run_goal

        s = self.session
        assert s is not None
        spec = GoalSpec(command=goal["command"], cwd=str(self.cwd),
                        min_tests=int(goal.get("min_tests") or 1),
                        timeout=float(goal.get("timeout") or 300.0))
        round_no = {"n": 0}

        async def _one_turn(verdict=None) -> None:
            """跑一个修复回合。

            第一回合发用户原话；之后发「判据命令 + 上一轮失败输出」的完整反馈。
            原来只发固定文案「继续，直到判据通过」——模型既不知道判据是什么、
            也看不到失败原因，只能猜，多跑几轮只是多花钱。
            """
            round_no["n"] += 1
            if round_no["n"] == 1 or verdict is None:
                from ee.core.goal_feedback import build_first_turn
                # 第一轮就把判据命令作为数据附在用户目标后：Goal 中途可能触发
                # 自动压缩，判据只在局部变量的话会在压缩时永久丢失（见 goal_feedback.build_first_turn）
                payload = build_first_turn(text, spec)
            else:
                from ee.core.goal_feedback import build_feedback
                payload = build_feedback(verdict, spec, attempt=round_no["n"] - 1,
                                         max_checks=int(goal.get("max_rounds") or 3))
            async for ev in s.run_turn(payload, images):
                self._on_event(ev)

        def _emit(verdict) -> None:
            self.notify("goal_check", {
                "thread_id": self.id,
                "round": round_no["n"] + 1,
                "met": bool(verdict.met),
                "reason": verdict.reason,
                "exit": verdict.exit,
                "tests": verdict.tests,
            })

        result = await run_goal(spec, turn_runner=_one_turn,
                                max_rounds=int(goal.get("max_rounds") or 3),
                                budget_exhausted=lambda: bool(getattr(s.budget, "stop_reason", None)),
                                emit=_emit)
        self.notify("goal_done", {
            "thread_id": self.id,
            "stop_reason": result.stop_reason,
            "rounds": len(result.rounds),
            "report": result.report(),
        })
        return result.stop_reason

    def _new_run(self, text: str, initiator: str = "user") -> dict:
        """记一次派发。终态由 _finish 写入，不能靠 status=idle 推断成功。

        `pid` 是判断「这条 running 还是不是活的」的依据：进程被杀后磁盘上留着 running，
        下次启动读到时只要 pid 不是自己，就知道那个回合永远不会有终态（见
        `store._close_stale_runs`）。
        """
        run = {"id": _new_id("run"), "started": time.time(), "finished": None,
               "status": "running", "terminal_reason": None, "initiator": initiator,
               "pid": os.getpid()}
        runs = self.meta.setdefault("runs", [])
        runs.append(run)
        del runs[:-RUNS_KEEP]
        return run

    def _close_run(self, reason: str) -> None:
        """回合结束：把 reason 写进当前 run，供界面/编排判断真实结果。"""
        runs = self.meta.get("runs") or []
        if runs and runs[-1].get("status") == "running":
            runs[-1].update(status="done", finished=time.time(), terminal_reason=reason)

    def _finish(self, reason: str) -> None:
        self._close_run(reason)
        # 运行名额在回合真正结束时回收。不走 notify：Backend.notify 对 listener 的异常是
        # `except Exception: pass`，靠通知释放会在某个 listener 抛错时静默泄漏名额。
        if self.orchestrator is not None:
            try:
                self.orchestrator.release(self.id)
            except Exception:  # noqa: BLE001  回收失败不能拖垮回合收尾
                log.warning("回收子会话名额失败（%s）", self.id, exc_info=True)
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
        self._add({"type": "turn_end", "reason": reason, "duration_ms": dur, "usage": self._usage_snapshot()})
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
        if s is None or self.meta.get("title_ai_done") or self.meta.get("source") in (
                "exec", "automation", "orchestrator"):
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

    def _bubble_to_parent(self, kind: str, call_id: str, tool: str = "", title: str = "",
                          detail: str = "", prefix: str = "") -> None:
        """子会话的阻塞请求同时告诉父会话——用户正在看的是父会话那个界面。

        审批与提问是同一个阻塞语义：只冒泡一种，另一种就会静默卡到回合被中断。
        通知里带 parent_id 而不是让客户端自己推，因为磁盘上未加载的子会话也要能冒泡到。
        """
        pid = self.meta.get("orchestrator_parent_id")
        if not pid:
            return
        self.notify("thread/child_approval", {
            "parent_id": pid, "thread_id": self.id, "kind": kind,
            "child_title": self.meta.get("title") or "子会话",
            "call_id": call_id, "tool": tool, "summary": title,
            "detail": _clip(detail or "", 2000), "prefix": prefix})

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
            self._bubble_to_parent("question", ev.call_id, "ask_user", ev.question)
        elif isinstance(ev, ApprovalRequest):
            from ee.core.rules import rule_from_decision
            it = self._add({"type": "approval", "call_id": ev.call_id, "tool": ev.tool, "title": ev.title,
                            "detail": _clip(ev.detail or "", 20000), "prefix": ev.prefix, "status": "pending",
                            "rule": rule_from_decision(ev.tool, ev.prefix) or "",
                            # 自动审批建议（T10-c）：事件上算了就一定要落到条目里，
                            # 否则前端永远拿不到——和 LSP 那次"字段存在但没人赋值"同类。
                            **({"suggestion": dict(ev.suggestion)} if getattr(ev, "suggestion", None) else {}),
                            **({"source": dict(ev.source)} if ev.source else {})})
            if ev.future is not None:
                self.approvals[ev.call_id] = (ev.future, it)
            self.notify("thread/updated", {"thread": self.summary()})
            self._bubble_to_parent("approval", ev.call_id, ev.tool, ev.title, ev.detail, ev.prefix)
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
            # 退回界面的排队消息也不会再执行，它带来的 model/mode 一并作废
            self._pending_model = self._pending_mode = None
            self.notify("turn/queue_returned", {"thread_id": self.id, "items": ev.items})
        elif isinstance(ev, Notice):
            self._add({"type": "notice", "text": ev.text, "level": ev.level})
        elif isinstance(ev, Error):
            self._add({"type": "error", "message": ev.message})
