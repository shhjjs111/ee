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
from typing import Any, Callable

from ee.config import load_config
from ee.core.events import (ApprovalDecision, ApprovalRequest, AssistantMessage, Error, Notice, PlanUpdate,
                            ReasoningDelta, Status, StepStart, TextDelta, ToolEnd, ToolOutput, ToolStart, TurnDone,
                            TurnStart, Usage)
from ee.core.events import QuestionRequest, QueueDelivered, QueueReturned, SubagentEnd, SubagentStart, SubagentUpdate
from ee.core.events import CaptureRequest
from ee.core import queue as Q
from ee.core.coerce import as_float, as_int
from ee.core.permissions import MODES
from ee.core.subagent import apply_event as apply_subagent_event
from ee.core.session import Session
from ee.core.store import SessionStore, _title

log = logging.getLogger("ee.thread")

UI_OUTPUT_LIMIT = 30_000          # 发给界面的单个工具输出上限（模型侧另有截断规则）
RUNS_KEEP = 5                     # meta 里只留最近几次派发（防止会话元数据无限长）
SAVE_FAIL_NOTICE_EVERY = 20       # 写盘一直失败时，每这么多次再往界面报一次（一次失败报一次太吵）
# 首屏/局部回包只给尾部这么多条。桌面端 store.ts 的 FIRST_PAGE 与它对齐：
# 一次全量返回 5215 条是 7MB，传输 + JSON.parse 就要一秒以上（实测 15MB 会话 3 秒）。
PAGE_SIZE = 120


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
    "不用测"。这里做四件事：命令不能为空、轮次必须是正整数且有上限、
    超时可选但给了必须是正数、回归命令（PASS_TO_PASS）可选。
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
    # 回归门（PASS_TO_PASS）：候选除了通过目标判据，还要通过这条命令，否则视为
    # 弄坏了原有测试。用户不填就不启用（v2 §4「回归门」）。
    p2p = str(goal.get("pass_to_pass") or "").strip()
    if p2p:
        out["pass_to_pass"] = p2p[:2000]
    return out


MAX_ALLOUT_CANDIDATES = 3      # 与 core/allout.py 的 CANDIDATES_CAP 一致；界面上限


def _norm_allout(allout: dict) -> dict:
    """校验并规范化全力以赴的开关参数。

    与 `_norm_goal` 同一条纪律：**只从 start_turn 这条入口来**，模型无法写入。
    这里只做三件事：`enabled` 必须是布尔、`candidates` 必须是 1~3 的整数、
    不接受任何模型字段——模型固定为用户当前所选（见 allout v2 §0）。
    """
    if not isinstance(allout, dict):
        raise ValueError("allout 必须是对象")
    if "model" in allout:
        raise ValueError("allout 不能指定模型：它固定使用会话当前的模型")
    enabled = allout.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"allout.enabled 必须是布尔值：{enabled!r}")
    raw = allout.get("candidates", 3)
    try:
        candidates = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"allout.candidates 必须是整数：{raw!r}") from None
    if candidates < 1:
        raise ValueError(f"allout.candidates 必须 >= 1：{candidates}")
    return {"enabled": enabled, "candidates": min(candidates, MAX_ALLOUT_CANDIDATES)}


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
    before: dict[str, float] = {}
    for cur, amount in (spent_before or {}).items():   # 来自会话文件：坏值按 0 跳过
        value = as_float(amount)
        if value:
            before[str(cur)] = value
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
                 orchestrator=None, usage: list[dict] | None = None):
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
        self._seq = max([as_int(it.get("seq", 0)) for it in self.items] + [len(self.items)]) + 1
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
        self.last_access = time.monotonic()   # 最后一次被后端访问（内存淘汰用它判“空闲”）
        self.status_text = ""          # 临时状态（正在连接模型…），不落盘
        # 会话记录写盘失败（磁盘满 / 文件被占用）：磁盘写不了，元数据也跟着写不进去，
        # 所以只能靠内存里的这个字段告诉界面“这段对话现在保存不了”。
        self.save_error: dict | None = None
        self._save_fail_count = 0      # 本会话累计写失败次数（跨多个 store 实例累加：save_meta 每次都新建一个）
        self._save_notice_at = 0       # 上一次上报时的累计次数（用于节流，不重复刷提示）
        self.orchestrator = orchestrator   # 指挥会话（D6-1）：回合结束要回收名额
        # 落盘的逐请求账本（SessionStore.load() 顺手带出），建 Session 时回放进 stats。
        # 不回放的话，同一条会话在面板和 `ee usage` 里会给出两套命中率，恢复后前 5 次
        # 请求的压缩水位自适应也会因为样本不足停摆（见 UsageStats.restore）。
        self._usage_records: list[dict] = list(usage or [])

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
        s.store = self._store(enabled=self.persist)
        s.on_store_fail = self._on_store_fail      # new() / 切 persist 重建 store 时也带上钩子
        s.on_store_write = self._on_store_write
        # 回放逐请求账本：成本不恢复（钱由 spent_before 负责），只补命中率/断点/上下文大小。
        # 恢复会话（重开、重启、被内存淘汰后重载）都走这里，所以这一步也决定了面板上
        # 「主对话缓存命中」是否与每回合 footer 同一口径。
        s.stats.restore(self._usage_records)
        if self._pending_messages:
            s.history = list(self._pending_messages)
            s.turns = sum(1 for it in self.items if it.get("type") == "user")
            self._pending_messages = []
            # 重启后恢复回合起点（编辑重发 / 回退用）：只认能对上原文的
            s.turn_starts = sorted({as_int(it.get("hist")) for it in self.items
                                    if it.get("type") == "user" and isinstance(it.get("hist"), int)
                                    and s.user_text_at(as_int(it.get("hist"))) is not None})
        # 旧推理清理游标（重启后别退回全带 reasoning，那会让首包再翻一倍）
        to = self.meta.get("reasoning_strip_to")
        if isinstance(to, int) and to >= 0:
            s._reasoning_strip_to = min(to, len(s.history) - 1)
        # 磁盘快照（D4-1）：从用户条目重建每回合前后 tree，重启后 /undo、编辑重发照样能恢复文件
        s.turn_trees = [{"before": it["tree"], "after": it.get("tree_after"), "undone": bool(it.get("undone"))}
                        for it in self.items if it.get("type") == "user" and it.get("tree")]
        self.meta["model"] = s.spec.alias
        self.meta["mode"] = s.policy.mode
        self.meta["profile"] = s.agent_profile
        # D5-9：会话级推理强度 / 回答风格（存在 meta 里，重启后照旧）；重启前已花的钱计入会话上限
        if "effort" in self.meta:
            try:
                s.set_effort(self.meta.get("effort") or "")
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
        s = self.session
        if s is None:                            # ensure_session 要么建好、要么抛；这里只是窄化类型
            raise RuntimeError("会话初始化失败")
        return s

    def reload_config(self) -> None:
        if self.session is None or self.session.running:
            return
        cfg = load_config(self.cwd)
        self.session.config = cfg
        alias = self.session.spec.alias
        try:
            spec = cfg.resolve_model(alias)
        except ValueError:
            self.session.set_model(cfg.default_model)
            self.meta["model"] = self.session.spec.alias
            return
        if self.session.spec_changed(spec):
            # 同别名换了底层模型 / provider / 前缀形状：必须走和用户切模型同一条路径。
            # 直接给 self.session.spec 赋值会漏掉 model_switch、旧前缀作废和工具表重建，
            # 真实会话里就出现过 prompt=29375 的模型切换在账上只剩 "unknown"。
            self.session.set_model(alias)
            self.meta["model"] = self.session.spec.alias
        else:
            # 只是价格 / key / 超时等配置：原地换 spec 和 client，不动前缀、工具与上下文附录。
            self.session.spec = spec
            self.session._drop_client()                    # 旧连接池要登记待释放，不能只丢引用

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
                # 本回合是否被强制只读（/review）。和 mode 分开报：界面要能说清
                # 「你的模式是 auto，但这一回合只读」，不能靠改 mode 来暗示。
                "readonly": bool(self.session.policy.readonly) if self.session else False,
                # 会话记录写盘失败时的状态（内存态，磁盘写不进去自然也无法从磁盘恢复）：
                # {count, error, at}。界面据此显示“保存不了”的警告。
                "save_error": self.save_error,
                "workspace": (self.meta.get("worktree") or {}).get("repo") or self.meta.get("cwd", "")}

    def detail(self, tail: int | None = None, before_seq: int | None = None) -> dict:
        """会话详情。

        `tail` / `before_seq` 用于**历史分页**（长会话一次 7MB 太重）：
        - `tail=N`：只返回最后 N 条；
        - `before_seq=S`：只返回 seq < S 的那一段（再取前 N 条），用于往上翻。
        两个都不传时返回全部，保持老调用方（脚本 / `ee exec` / 导出）不变。
        """
        s = self.session
        git = None
        if s is not None:
            git = {"is_repo": s.git.is_repo, "branch": s.git.branch, "changed": s.git.changed,
                   "untracked": s.git.untracked}
        items = self.items
        total = len(items)
        has_more = False
        if tail is not None or before_seq is not None:
            end = total
            if before_seq is not None:
                # seq 不连续（条目可能被撤回），按第一个 >= before_seq 的位置切
                end = next((i for i, it in enumerate(items)
                            if as_int(it.get("seq")) >= as_int(before_seq)), total)
            start = max(0, end - as_int(tail)) if tail is not None else 0
            # tail <= 0 表示“这一页不要任何条目”，不能退化成“返回全部”
            has_more = start > 0
            items = items[start:end]
        return {**self.summary(), "items": items, "usage": self.usage, "context_left": self.context_left,
                "shell": (s.shell.display if s and s.shell else None), "git": git,
                "instructions": self.instructions(), "status_text": self.status_text,
                "items_total": total, "items_has_more": has_more}

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
            self._store().meta(self.meta)

    # ------------------------------------------------------------ 落盘与写失败
    def _store(self, enabled: bool = True) -> SessionStore:
        """本会话专用的落盘对象（带上写失败的上报钩子）。

        不直接用 `SessionStore(...)`：写失败要能走到界面上，钩子只能在建对象时挂，
        散落的构造点（save_meta / _store_item / ensure_session）很容易漏一个。
        """
        st = SessionStore(self.home, session_id=self.id, enabled=enabled)
        st.on_fail = self._on_store_fail
        st.on_write = self._on_store_write
        return st

    def _on_store_fail(self, store: SessionStore, err: OSError) -> None:
        """会话记录没落盘：让用户在界面上看得见。

        原来只记一条 error 日志：磁盘满时对话看起来一切正常，重启才发现整段丢了。
        现在记在内存里（`summary()` 带出去 → 会话详情/左栏能显示），并往对话里
        插一条警告提示。**提示条目不落盘**——盘写不了，只能活着看；也因此要节流：
        一直失败时每条记录都会触发，逐条弹会把界面淹掉。
        """
        self._save_fail_count += 1
        n = self._save_fail_count
        self.save_error = {"count": n, "error": (store.last_error or str(err))[:300],
                           "at": time.time()}
        if not self.deleted:
            self.notify("thread/updated", {"thread": self.summary()})
        if self._save_notice_at and n - self._save_notice_at < SAVE_FAIL_NOTICE_EVERY:
            return
        self._save_notice_at = n
        self._add({"type": "notice", "level": "warn",
                   "text": f"这段对话没能保存到磁盘（{self.save_error['error']}）。"
                           "磁盘可能已满，或会话文件被别的程序占用——现在重启会丢掉最近的对话。"},
                  persist=False)

    def _on_store_write(self, store: SessionStore) -> None:
        """又能写盘了：撤掉界面上的失败状态，并告诉用户现在关掉也不会丢。

        只清内存状态——失败期间没落盘的记录已经丢了，不能假装补上。
        """
        if self.save_error is None:
            return
        self.save_error = None
        self._save_notice_at = 0
        self._add({"type": "notice", "level": "info",
                   "text": "会话已经能正常保存了。之前没能写盘的记录已经无法找回，之后的都正常落盘。"},
                  persist=False)
        if not self.deleted:
            self.notify("thread/updated", {"thread": self.summary()})

    # ------------------------------------------------------------ 条目
    def _store_item(self, it: dict) -> None:
        if self.deleted:
            return                     # 会话已删除：运行中的回合收尾也不重建文件
        if self.persist:
            (self.session.store if self.session else self._store()).item(it)

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
        """会话级选项（D5-9）：推理强度（只改本会话，不落盘）、回答风格。None = 不变。"""
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

    def set_mode(self, mode: str) -> dict:
        """切换本会话的权限模式。

        子会话不允许单独改：它的模式由派发它的父会话在 create_child 时按 orchestrator
        的能力上限算出来（orchestrator.child_capability），这里再开一个口子就等于把
        那个上限绕过去。
        """
        if self.meta.get("orchestrator_parent_id"):
            raise ValueError("子会话的权限模式由派发它的父会话决定，不能单独修改")
        s = self.ensure_session()
        s.policy.set_mode(mode)                  # 未知模式抛 ValueError
        self.meta["mode"] = s.policy.mode
        self.save_meta()
        self.notify("thread/updated", {"thread": self.summary()})
        return {"mode": s.policy.mode}

    def require_same_mode(self, mode: str | None) -> None:
        """回合入口不允许顺带改权限：模式只能由 `thread/set_mode` 或新建会话决定。

        `turn/start` / `thread/edit_resend` 都带 mode 参数，而界面显示的模式来自会话
        元数据。一旦某条路径把全局草稿模式塞进来，用户看到的是只读、实际是 auto：
        2026-11 独立审查实测到，编辑重发会把只读会话语权提到全自动且界面无任何线索。
        放宽权限必须是用户对**这个会话**做的动作，所以不一致就报错（fail closed）。
        内部调用方（headless / 定时任务）不受这个闸门约束：它们不走 RPC，模式是
        调用方对本次派发的显式意图。
        """
        if not mode:
            return
        s = self.ensure_session()
        if mode != s.policy.mode:
            raise ValueError(
                f"权限模式不能随消息一起改（当前 {s.policy.mode}，收到 {mode}）：请用模式控件切换")

    def start_turn(self, text: str, model: str | None = None, mode: str | None = None,
                   images: list[str] | None = None, attachments: list[dict] | None = None,
                   display: str | None = None, readonly: bool = False, kind: str | None = None,
                   effort=None, style: str | None = None, queue: str | None = None,
                   goal: dict | None = None, allout: dict | None = None) -> dict:
        """images：已保存的附件路径；attachments：对应的界面元数据（D3-D）。
        display：界面上显示的用户消息（模型收到的仍是 text，例如 /review 拼好的长提示）；
        readonly：这一回合临时只读，回合结束恢复原模式（/review）；kind：用户条目的类型标记（review）。
        queue：运行中发送时排进哪条队列：steer 插话（默认）/ follow 完成后追加；空闲时忽略。
        goal：`{command, max_rounds}`。给了就走 Goal 循环（跑到判据通过或轮次用完）。
        判据只从这条入口来，模型无法自己改——那会让"测试通过"变成"不用测"。"""
        s = self.ensure_session()
        if goal is not None:
            goal = _norm_goal(goal)
        if allout is not None:
            allout = _norm_allout(allout)
            if allout.get("enabled") and self.running:
                # 运行中改跑法等于中途换策略：与 goal 的排队语义一致，明确拒绝。
                raise ValueError("ee 正在工作，等这一回合结束再打开全力以赴")
            if allout.get("enabled"):
                # 只读 / 计划模式是硬边界（v2 §9）：allout 要改文件、并行跑多个候选，
                # 最后还会把补丁写回主目录——这些模式下一律拒绝，不靠模型自觉。
                eff = mode or getattr(self, "_pending_mode", None) or s.policy.mode
                if readonly or s.policy.readonly or eff in ("read-only", "plan"):
                    raise ValueError("只读 / 计划模式下不能开全力以赴：它会改文件并并行跑候选")
            if not allout.get("enabled"):
                allout = None            # 关着与不传等价：界面不显示、不落条目
        if images and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再发图片")
        if (readonly or display) and self.running:
            raise ValueError("ee 正在工作，等这一回合结束再开始审查")
        if effort is not None or style is not None:
            self.set_options(effort, style)
        user_item: dict[str, Any] = {"type": "user", "text": display or text}
        if kind:
            user_item["kind"] = kind
        if goal:
            # 记在用户条目上：界面据此显示"这一回合在追什么目标"，
            # 也让"判据从哪来"可追溯（只可能来自这里，不是模型写的）。
            user_item["goal"] = dict(goal)
        if allout:
            # 同样是可追溯的事实：这一回合开了全力以赴、上限几路。
            user_item["allout"] = dict(allout)
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
        if readonly:
            # 审查的只读是**回合级约束**，不是改会话模式：不动 meta、不动用户偏好，
            # 所以回合中切模式既不会给审查提权，收尾也不需要“恢复”（以前恢复时
            # 只改策略不回写 meta，收尾后 meta 与策略会分叉）。
            s.policy.readonly = True
        s._turn_role = "review" if kind == "review" else None      # 审查回合按 roles.review 取模型（D5-9）
        self.notify("thread/updated", {"thread": self.summary()})
        self.task = asyncio.create_task(self._consume(text, images, goal=goal, allout=allout))
        self.notify("thread/updated", {"thread": self.summary()})
        return {"queued": False, "item_id": user["id"]}

    def interrupt(self, why: str = "") -> bool:
        """用户点停止：停掉属于这个会话的一切工作，返回是否真的停到了东西。

        **为什么不能只转给 session.interrupt()**：Goal 循环跑在 self.task（_consume）
        里，时序是「判 → 跑 → 判」。用户可能正好停在**判**那一段（判据命令在子进程里
        跑），此时模型会话空闲、session.interrupt() 取消不了任何东西，返回 False。
        结果：界面提示“没停下来”，判据继续跑到超时，剩余轮次还会照常启动。

        先试 session（能停模型回合就按原来的路子停），不行再取消外层 task；
        两者都没有可停的东西才返回 False。
        """
        if self.session is not None and (self.session.interrupt(why) if why else self.session.interrupt()):
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
                       goal: dict | None = None, allout: dict | None = None) -> None:
        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        self.turn_started_at = time.monotonic()
        self.cur_assistant = None
        self.plan_item = None
        reason = "error"
        try:
            probe_denied = False
            if allout:
                proj = await self._plan_allout(allout, goal)
                if not goal and proj is not None and proj.command:
                    if self._allout_goal_confirmed(proj.command):
                        goal = {"command": proj.command, "max_rounds": 3}
                    elif await self._confirm_probe_goal(proj):
                        self._remember_allout_goal(proj.command)
                        self._add({"type": "notice", "level": "info",
                                   "text": f"全力以赴：已确认判据 {proj.command}"
                                           f"（来自 {proj.source or '项目文件'}），以后这个项目不用再确认。"})
                        goal = {"command": proj.command, "max_rounds": 3}
                        # 重新推导并推一条 allout_plan：确认前的通知是降级版（单路、未验证），
                        # 不重发的话界面会一直显示 1 路，与实际执行的路数不符。
                        await self._plan_allout(allout, goal)
                    else:
                        probe_denied = True
            if allout and goal:
                reason = await self._run_allout(text, images, goal, allout)
            elif allout:
                # 降级路径（allout v2 §4）：没有客观判据就不存在「极限」，
                # 按普通回合跑，并在对话里说清楚为什么没开火力。
                self._add({"type": "notice", "level": "warn",
                           "text": ("全力以赴：你这次没有确认探测到的判据，按单路跑，结果不保证正确性。"
                                    if probe_denied else
                                    "全力以赴已开，但这一回合没有可执行的判据：按单路尝试，结果不保证正确性。"
                                    "可以用「目标」填一条测试命令启用完整模式。")})
                async for ev in s.run_turn(text, images):
                    if isinstance(ev, TurnDone):
                        reason = ev.reason
                        if ev.tree_after:
                            self._mark_tree_after(ev.tree_after)
                    self._on_event(ev)
            elif goal:
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
        except asyncio.CancelledError:
            # 用户点停止时 `interrupt()` 会 cancel 外层 task（session 已经空闲停不到东西时只能这样）：
            # CancelledError 是 BaseException，上面的 except Exception 接不住，reason 会停在初值
            # "error"，于是这次主动停止被记成 terminal_reason=error，子会话卡片显示「出错」、
            # 编排也据此当成失败重试。写 interrupted 再抛，取消语义不变。
            reason = "interrupted"
            raise
        except Exception as e:  # noqa: BLE001
            log.error("回合异常（会话 %s）", self.id, exc_info=True)
            self._add({"type": "error", "message": f"内部错误：{type(e).__name__}: {e}"})
        finally:
            # 只读标记必须无条件撤掉：留着的会话会永久只能读，用户看不出原因。
            # 用 getattr：测试里的会话替身不一定带 policy，收尾路径绝不能因此抛错
            # （抛错会跳过后面的 _finish，会话就卡在 running）。
            if s is not None:
                policy = getattr(s, "policy", None)
                if policy is not None:
                    policy.readonly = False
            s._turn_role = None
            try:
                self._finish(reason)
            except Exception:  # noqa: BLE001
                log.error("回合收尾异常（会话 %s）", self.id, exc_info=True)
                self.notify("turn/completed", {"thread_id": self.id, "reason": "error", "duration_ms": 0,
                                               "usage": self.usage, "context_left": self.context_left})

    async def _plan_allout(self, allout: dict, goal: dict | None):
        """全力以赴的推导与判据探测：只发通知，不执行任何命令。

        返回探测结果（`ProbeResult` 或 None），供 `_consume` 决定是否需要用户确认。

        **为什么不在这里直接跑探测到的判据**：陌生仓库不能让 ee 自动执行它的
        测试脚本（allout v2 §9）。探测结果带 `confirmed=False` 推给界面，由用户
        确认一次；本回合在没有用户判据时按普通回合跑（单路、标注未验证），
        这正是 v2 §4 的降级路径，不违背任何边界。

        `goal` 是用户显式填的判据：有了它就不需要探测，`verified=True` 直接成立。
        """
        from ee.core.allout import derive
        from ee.core.probe import probe as probe_project

        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        has_goal = bool(goal and goal.get("command"))
        proj = None if has_goal else probe_project(self.cwd)
        if proj is not None and proj.command:
            # v2 §5：探测来的判据首次使用前要用户确认一次；确认过就不再问。
            proj.confirmed = self._allout_goal_confirmed(proj.command)
        # 用户设的 candidates 作为上限参与推导（而不是事后截断）：否则 reasons 会写
        # 「并行 3 路」而实际只跑 2 路，界面自相矛盾（2026-11-09 真模型演练实测到）。
        plan = derive(config=s.config.data, profile=s.profile, has_goal=has_goal,
                      max_parallel=as_int((s.config.data.get("subagent") or {}).get("max_parallel") or 3),
                      max_candidates=as_int(allout.get("candidates") or 3))
        # 推理强度是设计里最大的杠杆（v2 §1.1），必须真的落到请求参数上：
        # 推导结果原先只写进了 reasons，没写进会话——界面说「max」，请求仍是默认档。
        # 只在新会话应用：已有会话中途换档会断前缀缓存（session.py 的 effort_switch）。
        # 放在 _plan_allout 而不是 _run_allout：降级路径（没判据）不走 _run_allout，
        # 但用户开了全力以赴就该拿到最高强度（reasons 也一直是这么写的）。
        if plan.effort and s.turns == 0 and s.effort != plan.effort:
            s.set_effort(plan.effort)
            self.meta["effort"] = s.effort
            self.save_meta()
            self.notify("thread/updated", {"thread": self.summary()})
        self.notify("allout_plan", {
            "thread_id": self.id,
            "plan": plan.as_dict(),
            "goal": (goal or {}).get("command"),
            "probe": proj.as_dict() if proj is not None else None,
            "candidates_cap": plan.candidates,
        })
        return proj

    def _allout_meta(self) -> dict:
        m = self.meta.get("allout")
        return dict(m) if isinstance(m, dict) else {}

    def _allout_goal_confirmed(self, command: str) -> bool:
        return self._allout_meta().get("goal_confirmed") == command

    def _remember_allout_goal(self, command: str) -> None:
        m = self._allout_meta()
        m["goal_confirmed"] = command
        self.meta["allout"] = m
        self.save_meta()

    async def _confirm_probe_goal(self, proj) -> bool:
        """探测到的判据首次使用前问一次（v2 §5）。

        走审批卡片而不是 ask_user：无人值守下 `auto_approve` 能按主会话档位
        给出决定（ask_user 没有自动回答通路，会一直卡到超时）。
        """
        from ee.core.events import ApprovalRequest

        fut = asyncio.get_running_loop().create_future()
        ev = ApprovalRequest(
            call_id=_new_id("ap"), tool="allout_probe",
            title="全力以赴：用探测到的判据做闸门？",
            detail=f"{proj.command}（来自 {proj.source or '项目文件'}）"
                   + (f"\n\n{proj.note}" if getattr(proj, "note", "") else ""),
            future=fut)
        self._on_event(ev)
        decision = await fut
        return not str(getattr(decision, "choice", "deny")).startswith("deny")

    # ------------------------------------------------------------ 全力以赴（阶段 5）

    async def _run_allout(self, text: str, images: list[str] | None, goal: dict, allout: dict) -> str:
        """全力以赴的完整编排：单路 → 批评修复 → 分叉 → 排序 → 写回。

        返回停止原因（met / exhausted / budget / interrupted / error）。
        模型固定为用户当前所选：候选、修复、A/B 评定全部用同一个模型
        （allout v2 §0）。判据串行执行（§4）：并发抢 CPU 会造出假失败。
        """
        from ee.core import allout as A
        from ee.core.goal import GoalSpec, Verdict, judge
        from ee.core.goal_feedback import build_feedback, build_first_turn

        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        spec = GoalSpec(command=goal["command"], cwd=str(self.cwd),
                        min_tests=as_int(goal.get("min_tests") or 1),
                        timeout=as_float(goal.get("timeout") or 300.0))
        pass_to_pass = str(goal.get("pass_to_pass") or "").strip() or None
        plan = A.derive(config=s.config.data, profile=s.profile, has_goal=True,
                        max_parallel=as_int((s.config.data.get("subagent") or {}).get("max_parallel") or 3),
                        max_candidates=as_int(allout.get("candidates") or 3))
        cap = plan.candidates
        lock = getattr(self, "_allout_judge_lock", None)
        if lock is None:
            lock = self._allout_judge_lock = asyncio.Lock()

        async def judge_serial(cwd: str | None = None) -> Verdict:
            sp = spec if cwd is None else GoalSpec(command=spec.command, cwd=cwd,
                                                   min_tests=spec.min_tests, timeout=spec.timeout)
            async with lock:                      # 串行：判据命令本身吃 CPU
                v = await judge(sp)
            self.notify("allout_progress", {"thread_id": self.id, "phase": "judge",
                                            "met": bool(v.met), "reason": v.reason,
                                            "tests": v.tests, "cwd": cwd})
            return v

        async def p2p_judge(cwd: str | None = None) -> Verdict:
            """跑回归命令（PASS_TO_PASS）。只有在用户填了 pass_to_pass 时才会调用。

            与目标判据同样串行；min_tests=1（回归命令通常只覆盖一部分测试，
            不能套用目标判据的 min_tests，否则会把正常的回归子集误判为失败）。
            """
            if not pass_to_pass:
                raise RuntimeError("pass_to_pass 未设置")
            sp = GoalSpec(command=pass_to_pass, cwd=cwd or spec.cwd, min_tests=1, timeout=spec.timeout)
            async with lock:
                v = await judge(sp)
            self.notify("allout_progress", {"thread_id": self.id, "phase": "regression",
                                            "met": bool(v.met), "reason": v.reason,
                                            "tests": v.tests, "cwd": cwd})
            return v

        payload = build_first_turn(text, spec)
        rounds = {"n": 0}

        async def one_turn(msg: str) -> str:
            rounds["n"] += 1
            reason = "completed"
            async for ev in s.run_turn(msg, images if rounds["n"] == 1 else None):
                if isinstance(ev, TurnDone):
                    reason = ev.reason
                    if ev.tree_after:
                        self._mark_tree_after(ev.tree_after)
                self._on_event(ev)
            return reason

        self.notify("allout_progress", {"thread_id": self.id, "phase": "start",
                                        "candidates": cap, "reasons": list(plan.reasons)})
        # ---- 轮 1：单路。一次就过则零额外成本（预算不是配额）。
        if await one_turn(payload) == "interrupted":
            return "interrupted"
        best = await judge_serial()      # 目标判据的判定；同时作为 keep-policy 的基线（同一命令才可比）
        regressed = False                # 目标过了但回归失败：best 保持绿的判据结果，这个标记拦住「通过」
        last_fail = None if best.met else best   # 下一轮修复该看谁的失败输出

        def reg_fail_verdict(rv):
            """把回归失败包装成「判据失败」，只用于构造修复轮的反馈输入。

            不能直接改写 best：keep-policy 比较的是**同一命令**前后跑了多少测试，
            把回归命令的测试数塞进 best 会把正常修复误判成退步
            （回归套件常比目标判据大，例如目标 3 个、回归 100 个）。
            """
            from ee.core.goal import Verdict as _Verdict
            return _Verdict(exit=rv.exit, tests=rv.tests, timed_out=rv.timed_out,
                            output=(f"目标判据已通过，但回归命令失败（改动破坏了原有测试）。\n\n"
                                    f"回归命令：\n{pass_to_pass}\n\n输出：\n{(rv.output or '').rstrip()}"),
                            min_tests=1)

        if best.met and pass_to_pass:
            rv = await p2p_judge()
            if not rv.met:
                regressed = True
                last_fail = reg_fail_verdict(rv)
        if best.met and not regressed:
            self.notify("allout_progress", {"thread_id": self.id, "phase": "done", "how": "first_try"})
            return "met"
        if getattr(s.budget, "stop_reason", None):
            return "budget"

        # ---- 轮 2：批评修复（判据 + 失败输出喂回去），keep-policy 只在更好时保留。
        for attempt in range(1, max(1, plan.repair_rounds) + 1):
            msg = build_feedback(last_fail or best, spec, attempt=attempt, max_checks=plan.repair_rounds + 2)
            # 修复轮直接跑在主工作区：先记下修复前的位置，拒绝时能真的撤回去。
            before = await asyncio.to_thread(s.disk_capture)
            if await one_turn(msg) == "interrupted":
                return "interrupted"
            after = await judge_serial()
            keep, why = A.keep_policy(best, after)
            if keep and pass_to_pass:
                # 修复轮也要过回归门：判据过了但弄坏旧测试的修复不能保留。
                rv = await p2p_judge()
                if not rv.met:
                    keep, why = False, f"回归门未通过（{rv.reason}），丢弃修复"
                    regressed, last_fail = True, reg_fail_verdict(rv)
                else:
                    regressed = False
            # 「丢弃」必须包括磁盘：只改 kept/判定结果的话，界面说未保留、工作区里却是
            # 那份没过的改动（2026-11 独立审查实测：kept=False、回合 exhausted，文件仍是失败的修复）。
            reverted: list[str] | None = None
            if not keep:
                reverted = await asyncio.to_thread(self._revert_repair, s, before)
                if reverted is not None:
                    tree = await asyncio.to_thread(s.disk_capture)
                    if tree:
                        self._mark_tree_after(tree)   # 覆盖写：回合结束状态是「已回滚」
            self.notify("allout_progress", {"thread_id": self.id, "phase": "repair",
                                            "attempt": attempt, "kept": keep, "why": why,
                                            "reverted": None if reverted is None else len(reverted)})
            if keep:
                best = after
                last_fail = None
            elif not after.met:
                # 这次连目标判据都没过（可能是修复把功能改坏了）：下一轮看它的失败输出。
                last_fail = after
            if best.met and not regressed:
                self.notify("allout_progress", {"thread_id": self.id, "phase": "done", "how": "repair"})
                return "met"
            if getattr(s.budget, "stop_reason", None):
                return "budget"

        # ---- 轮 3：分叉（候选各自 worktree、温度阶梯、判据串行、先通过即取消其余）。
        if cap > 1:
            cands = await self._allout_fanout(spec, plan, text, cap, pass_to_pass)
            # 回归门拦下的候选不能进排序：它们让原有测试变红了。
            passed = [c for c in cands if A.candidate_ok(c)]
            if passed:
                winner = await self._allout_rank(passed, text, getattr(best, "output", "") or "")
                if winner is not None:
                    res = await asyncio.to_thread(A.apply_winner, winner["cwd"], winner["base"], self.cwd)
                    self.notify("allout_progress", {"thread_id": self.id, "phase": "apply",
                                                    "winner": winner["id"], "result": res})
                    if res.get("applied"):
                        await asyncio.to_thread(s.refresh_git)
                        tree = await asyncio.to_thread(s.disk_capture)
                        if tree:
                            self._mark_tree_after(tree)
                        return "met"
        return "exhausted" if (not best.met or regressed) else "met"

    def _revert_repair(self, s, tree: str | None) -> list[str] | None:
        """把一轮被丢弃的修复从工作区撤掉（回到 `tree`）。拿不到快照返回 None。

        修复轮直接跑在主工作区（候选才在 worktree 里），所以「不保留」必须落到磁盘上。
        回滚前把当前状态存成恢复点：万一用户其实想要那份尝试，时间线里还能找回来。
        返回 None 表示**没能回滚**（磁盘快照不可用 / 抓取失败）——调用方要把这件事
        写进通知，不能默默当成已回滚。
        """
        if not tree:
            return None
        d = s.disk()
        if d is None:
            return None
        cur = d.capture()
        if not cur:
            return None
        paths = d.changed(tree, cur)
        if not paths:
            return []                      # 这一轮确实没改文件：不用动，也算回滚成功
        try:
            from ee.server.checkpoint_rpc import add_restore_point
            add_restore_point(self, cur, "全力以赴：被丢弃的修复尝试")
        except Exception:  # noqa: BLE001  恢复点只是反悔用的，存不下不能拦住回滚
            log.warning("全力以赴：存恢复点失败（修复回滚）", exc_info=True)
        try:
            restored = d.restore(tree, paths)
        except Exception:  # noqa: BLE001
            log.warning("全力以赴：回滚被丢弃的修复失败", exc_info=True)
            return None
        s._after_restore(restored, "[系统提示] 全力以赴丢弃了一次没通过判据的修复，"
                                  "以下文件已恢复到修复前的内容（如需再改请重新 read）：")
        return restored

    def _allout_candidate_event(self, ev, prefix: str) -> bool:
        """候选会话的阻塞事件接回主会话；处理了返回 True。

        审批按**主会话的权限规则**走：候选与主会话共用同一个 PermissionPolicy
        判定，需要问的时候在主会话上出审批卡片（call_id 加 `allout-N/` 前缀，
        与子代理的父子前缀同一写法），用户的选择通过 `respond()` 回到候选等待的
        future。无人值守时由 headless 的 `auto_approve` 决定（危险命令仍拒绝）。

        这里不能"一律拒绝"：那会让候选连 `pytest` 都跑不了——权限判定在
        auto 模式下只对危险命令与动态替换（$(…)、反引号）返回 ask，其余直接放行；
        默认模式下普通命令也会 ask，一律拒绝等于分叉路永远失败。
        """
        from dataclasses import replace as _replace

        from ee.core.events import ApprovalRequest, QuestionRequest

        if isinstance(ev, ApprovalRequest):
            self._on_event(_replace(ev, call_id=f"{prefix}/{ev.call_id}",
                                    title=f"[全力以赴候选] {ev.title}"))
            return True
        if isinstance(ev, QuestionRequest):
            # 候选的 ask_user 在配置里已关；万一出现，不能让它卡到超时。
            if ev.future is not None and not ev.future.done():
                ev.future.set_result("（候选不能提问，请自行判断）")
            return True
        return False

    async def _allout_fanout(self, spec, plan, task_text: str, cap: int,
                             pass_to_pass: str | None = None) -> list[dict]:
        """并行候选：各自独立 worktree、温度阶梯、同一模型、判据串行。

        `task_text` 是**用户原话**（不是拼好判据段的第一轮输入）：每个候选自己
        按它的 worktree 重新构造，保证第一句话里的「判据执行目录」指向候选自己
        的目录，而不是主仓库。
        """
        import copy
        from pathlib import Path

        from ee.core import allout as A
        from ee.core import worktree as W
        from ee.core.permissions import PermissionPolicy
        from ee.core.session import Session
        from ee.core.goal import judge

        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        if getattr(s.budget, "stop_reason", None):
            return []
        ok, why = await asyncio.to_thread(W.agent_eligible, self.cwd)
        if not ok:
            self.notify("allout_progress", {"thread_id": self.id, "phase": "fanout_skipped", "why": why})
            return []
        lock = self._allout_judge_lock

        async def _create_worktree(i: int, label: str) -> dict:
            """建候选 worktree；被取消时把“已交给工作线程”的那次创建收尾掉。

            `asyncio.to_thread` 停不下来：await 被取消时线程里的 `git worktree add`
            照样会跑完。不管它，就会留下一个没有主人的 worktree（占磁盘、混进列表）。
            所以取消时挂一个“等它落地再回收”的任务，而不是直接丢掉 future。
            """
            fut = asyncio.ensure_future(asyncio.to_thread(W.create_for_agent, self.cwd, label, self.home))
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:

                async def _reap() -> None:
                    try:
                        info = await fut
                    except Exception:  # noqa: BLE001  创建失败就没什么可回收的
                        return
                    try:
                        await asyncio.shield(asyncio.ensure_future(asyncio.to_thread(
                            W.finish_agent, info, f"ee：全力以赴候选 {label}（已取消）", self.home)))
                    except BaseException:  # noqa: BLE001  收尾失败不能拖垮事件循环
                        pass

                asyncio.ensure_future(_reap())
                raise

        async def run_one(i: int) -> dict:
            label = f"allout-{i + 1}"
            wt = None
            child = None
            rec = {"id": f"cand-{i + 1}", "cwd": None, "base": None, "worktree": None,
                   "cost": 0.0, "verdict": None, "regressed": False,
                   "touched_tests": False, "files": 0, "additions": 0, "deletions": 0, "votes": 0}
            try:
                wt = await _create_worktree(i, label)
                rec.update(cwd=wt["cwd"], base=wt["agent_base"], worktree=wt["path"])
                data = copy.deepcopy(s.config.data)
                alias = s.spec.alias
                models = data.setdefault("models", {})
                models[alias] = {**(models.get(alias) or {}), "temperature": as_float(plan.temperature_ladder[i])}
                data.setdefault("subagent", {})["enabled"] = False      # 候选不嵌套
                data["ask_user"] = False
                cfg = copy.copy(s.config)
                cfg.data = data
                child = Session(cfg, model=alias, cwd=Path(wt["cwd"]), mode=s.policy.eff_mode,
                                client_factory=self.client_factory, persist=False, home=self.home,
                                shell=s.shell_name, user_home=self.user_home)
                p = s.policy
                # 隔离靠 deny：auto 模式下工作区外写是直接放行的，不加这条规则
                # 模型会用绝对路径改主仓库（2026-11-09 实测到），worktree 等于没用。
                child.policy = PermissionPolicy(mode=p.mode, workspace=Path(wt["cwd"]),
                                                allow=list(p.allow), ask=list(p.ask),
                                                deny=list(p.deny) + A.candidate_deny_rules(self.cwd),
                                                session_prefixes=p.session_prefixes,
                                                session_tools=p.session_tools, shell=p.shell)
                child.budget = s.budget
                child.budget_owner = False
                child.budget_root = s.budget_root or s
                child.disk_enabled = False
                child._mcp_done = True
                child.effort = s.effort_for(child.spec)
                # 给候选的任务说明必须指向它自己的 worktree：用主仓库 spec 构造的
                # payload 第一句就写着「判据执行目录：主仓库」——等于明确指挥模型
                # 去主仓库干活，这是隔离被破坏的直接诱因（2026-11-09 实测到）。
                from dataclasses import replace as _replace

                from ee.core.goal_feedback import build_first_turn

                cand_prompt = build_first_turn(task_text, _replace(spec, cwd=wt["cwd"]))
                async for ev in child.run_turn(cand_prompt):
                    # 候选的审批接回主会话（与子代理同一条纪律）：一律拒绝会把
                    # pytest 这类命令挡死，分叉路等于永远跑不完。桌面端出审批卡片，
                    # 无人值守下由 headless 的 auto_approve 决定。
                    self._allout_candidate_event(ev, f"allout-{i + 1}")
                # 判据必须跑在候选自己的 worktree 里：判的是候选的代码，不是主目录的。
                from ee.core.goal import GoalSpec as _GS
                cand_spec = _GS(command=spec.command, cwd=wt["cwd"],
                                min_tests=spec.min_tests, timeout=spec.timeout)
                async with lock:
                    v = await judge(cand_spec)
                rec["verdict"] = v
                if bool(getattr(v, "met", False)) and pass_to_pass:
                    # 回归门：目标判据过了不算数，还要证明没弄坏原有测试。
                    p2p_spec = _GS(command=pass_to_pass, cwd=wt["cwd"], min_tests=1,
                                   timeout=spec.timeout)
                    async with lock:
                        rv = await judge(p2p_spec)
                    rec["regressed"] = not bool(getattr(rv, "met", False))
                    self.notify("allout_progress", {"thread_id": self.id, "phase": "regression",
                                                    "id": rec["id"], "met": bool(getattr(rv, "met", False)),
                                                    "reason": rv.reason})
                try:
                    rec["cost"] = as_float(child.stats.totals().get("cost") or 0.0)
                except Exception:  # noqa: BLE001  费用读不到不影响判定
                    pass
                await self._allout_merge_usage(s, child)
                v_out = await asyncio.to_thread(W.finish_agent, wt, f"ee：全力以赴候选 {i + 1}", self.home)
                rec["files"] = as_int(v_out.get("file_count") or 0)
                rec["additions"] = as_int(v_out.get("additions") or 0)
                rec["deletions"] = as_int(v_out.get("deletions") or 0)
                rec["touched_tests"] = any(
                    _sub_touched(f.get("path", "")) for f in (v_out.get("files") or []))
                self.notify("allout_progress", {"thread_id": self.id, "phase": "candidate",
                                                "id": rec["id"], "met": bool(getattr(v, "met", False))})
                return rec
            except asyncio.CancelledError:
                # 被取消（别的候选先通过 / 用户点停止）：收尾保留改动，别留半个 worktree。
                # wt 为 None 说明取消在创建阶段，由 _create_worktree 的 reap 路径收尾。
                if wt is not None:
                    try:
                        await asyncio.to_thread(W.finish_agent, wt, f"ee：全力以赴候选 {i + 1}（已取消）", self.home)
                    except Exception:  # noqa: BLE001
                        pass
                rec["cancelled"] = True
                raise
            except Exception as e:  # noqa: BLE001  单个候选失败不影响其它
                rec["error"] = f"{type(e).__name__}: {e}"
                return rec

        def _sub_touched(rel: str) -> bool:
            from ee.core.probe import is_test_file
            return is_test_file(rel)

        tasks = [asyncio.ensure_future(run_one(i)) for i in range(cap)]
        results: list[dict] = []

        async def watch():
            nonlocal results
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    try:
                        r = t.result()
                        results.append(r)
                        if A.candidate_ok(r):
                            # 先通过（且没被回归门拦下）→ 立即取消其余（预算不是配额）
                            for q in pending:
                                q.cancel()
                            pending = set()
                            break
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass

        # 候选是独立 task：watch() 只负责“先通过就取消”，不管回收。
        # 但 watch() 自己也可能被取消（用户点停止 → Thread.interrupt cancel 外层 task），
        # 那一刻 CancelledError 直接从 `await watch()` 穿出去，原来跟在后面的 gather 永远不执行——
        # 候选继续烧 token、跑判据、占 worktree（2026-11 独立审查实测：父回合结束后仍有候选在跑）。
        # 所以回收放进 finally：不管正常结束、提前通过还是被取消，都要取消并等全部候选落地。
        try:
            await watch()
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return results

    @staticmethod
    async def _allout_merge_usage(parent, child) -> None:
        """把候选用量并入主会话账本，role 记 "allout"（不污染主对话命中率）。"""
        st, cs = parent.stats, child.stats
        store = getattr(parent, "store", None)
        for r in cs.records:
            if r.get("role") == "main":
                r = {**r, "role": "allout"}
            st.add_record(r)
            if store is not None:
                store.usage(r)

    async def _allout_rank(self, passed: list[dict], task: str, judge_output: str) -> dict | None:
        """同模型 A/B 评定（位置对调两轮），净胜票写回候选；再交 bestof.pick。"""
        from ee.core import allout as A
        from ee.core import bestof as B
        from ee.core import gitinfo
        from ee.proc import run_capture
        from ee.tools.textdecode import smart_decode

        exe = gitinfo.git_exe() or "git"
        diffs: dict[str, str] = {}

        def _diff(c: dict) -> str:
            code, out, _ = run_capture([exe, "diff", "--no-color", f"{c['base']}..HEAD"], cwd=c["cwd"], timeout=60)
            return smart_decode(out or b"") if code == 0 else ""

        for c in passed:
            diffs[c["id"]] = await asyncio.to_thread(_diff, c)
        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        for i in range(len(passed)):
            for j in range(i + 1, len(passed)):
                x, y = passed[i], passed[j]
                wins: list[str] = []
                for swap in (False, True):
                    a, b = (y, x) if swap else (x, y)
                    sys_msg, user = A.ab_prompt(task, judge_output, diffs[a["id"]], diffs[b["id"]],
                                                a_touched_tests=bool(a.get("touched_tests")),
                                                b_touched_tests=bool(b.get("touched_tests")))
                    try:
                        text = await s.side_complete(sys_msg, user, role="allout")
                    except Exception:  # noqa: BLE001  评定失败按平票处理，不阻塞写回
                        text = ""
                    pick = A.parse_ab(text)
                    if pick == "A":
                        wins.append(a["id"])
                    elif pick == "B":
                        wins.append(b["id"])
                x["votes"] = as_int(x.get("votes") or 0) + A.net_votes(wins, x["id"], y["id"])
                y["votes"] = as_int(y.get("votes") or 0) + A.net_votes(wins, y["id"], x["id"])
        cand_objs = [B.Candidate(id=c["id"], title=c["id"], worktree=c["worktree"], verdict=c["verdict"],
                                 cost=as_float(c.get("cost") or 0.0), votes=as_int(c.get("votes") or 0),
                                 regressed=bool(c.get("regressed")),
                                 touched_tests=bool(c.get("touched_tests")), files=as_int(c.get("files") or 0),
                                 additions=as_int(c.get("additions") or 0), deletions=as_int(c.get("deletions") or 0))
                     for c in passed]
        win = B.pick(cand_objs)
        if win is None:
            return None
        return next(c for c in passed if c["id"] == win.id)

    async def _consume_goal(self, text: str, images: list[str] | None, goal: dict) -> str:
        """跑 Goal 循环，返回停止原因（met / exhausted / budget / interrupted / error）。

        回合执行仍是 s.run_turn：这里只负责"再跑一轮"和"判定"，不是第二个 agent 循环。
        每轮判定结果通过 goal_check 通知推给界面，用户能看到进度而不是干等。
        """
        from ee.core.goal import GoalSpec, run_goal

        s = self.session
        if s is None:
            raise RuntimeError("会话未初始化")
        spec = GoalSpec(command=goal["command"], cwd=str(self.cwd),
                        min_tests=as_int(goal.get("min_tests") or 1),
                        timeout=as_float(goal.get("timeout") or 300.0))
        round_no = {"n": 0}

        # 回归命令（PASS_TO_PASS）：界面把它标成在 Goal 里生效，前端也确实提交了，
        # 但原来只有全力以赴用它，普通 Goal 静默丢掉整个字段——目标过了、旧测试红了
        # 照样返回 met（2026-11 独立审查实测）。这里有两条路：要么执行它，要么别在
        # 界面上摆出来。选执行：它正是"没弄坏其它东西"的唯一客观证据。
        p2p = str(goal.get("pass_to_pass") or "").strip()
        p2p_spec = (GoalSpec(command=p2p, cwd=str(self.cwd), min_tests=1,
                             timeout=as_float(goal.get("timeout") or 300.0)) if p2p else None)

        async def _judge(spec):
            """目标判据 + 回归门：回归命令失败时这一轮不算达标，并把回归输出交给修复回合。"""
            from ee.core.goal import Verdict as _Verdict
            from ee.core.goal import judge as _judge_one

            v = await _judge_one(spec)
            if not v.met or p2p_spec is None:
                return v
            rv = await _judge_one(p2p_spec)
            if rv.met:
                return v
            self._add({"type": "notice", "level": "warn",
                       "text": f"目标判据已通过，但回归命令未通过（{rv.reason}）：改动弄坏了原有测试。"})
            return _Verdict(exit=rv.exit, tests=rv.tests, timed_out=rv.timed_out,
                            output=("目标判据已通过，但回归命令失败（改动破坏了原有测试）。\n\n"
                                    f"回归命令：\n{p2p}\n\n输出：\n{(rv.output or '').rstrip()}"),
                            min_tests=1)

        async def _one_turn(verdict=None) -> str:
            """跑一个修复回合，返回内核给的回合终态（completed / interrupted / ...）。

            第一回合发用户原话；之后发「判据命令 + 上一轮失败输出」的完整反馈。
            原来只发固定文案「继续，直到判据通过」——模型既不知道判据是什么、
            也看不到失败原因，只能猜，多跑几轮只是多花钱。

            返回值必须往上传：用户点停止时内核会发 TurnDone("interrupted") 并让
            run_turn 正常结束（不是抛异常）。这里吞掉它，run_goal 就会接着判据、
            再起一轮——界面已经显示停止，后台还在跑（独立审查实测到）。
            """
            round_no["n"] += 1
            reason = "completed"
            if round_no["n"] == 1:
                from ee.core.goal_feedback import build_first_turn
                # 第一轮就把判据命令作为数据附在用户目标后：Goal 中途可能触发
                # 自动压缩，判据只在局部变量的话会在压缩时永久丢失（见 goal_feedback.build_first_turn）
                payload = build_first_turn(text, spec)
                if verdict is not None:
                    # 先判后跑：第一个回合之前就已经有一次判定（含回归门）。只给原话 + 判据，
                    # 模型要重新猜「哪里坏了」；把失败输出一并交出来，省一轮猜测。
                    from ee.core.goal_feedback import build_feedback as _fb
                    payload += "\n\n---\n\n" + _fb(verdict, spec, attempt=1,
                                                     max_checks=as_int(goal.get("max_rounds") or 3))
            else:
                from ee.core.goal_feedback import build_feedback
                payload = build_feedback(verdict, spec, attempt=round_no["n"] - 1,
                                         max_checks=as_int(goal.get("max_rounds") or 3))
            async for ev in s.run_turn(payload, images):
                if isinstance(ev, TurnDone):
                    if ev.reason:
                        reason = ev.reason
                    if ev.tree_after:
                        # 回合后快照：Goal 的多轮内层回合也要记，否则这一回合的
                        # tree_after 永远为空，/undo 只能退到「拿当前工作区推断改动」。
                        self._mark_tree_after(ev.tree_after)
                self._on_event(ev)
            return reason

        def _emit(verdict) -> None:
            self.notify("goal_check", {
                "thread_id": self.id,
                "round": round_no["n"] + 1,
                "met": bool(verdict.met),
                "reason": verdict.reason,
                "exit": verdict.exit,
                "tests": verdict.tests,
            })

        result = await run_goal(spec, turn_runner=_one_turn, judge_fn=_judge,
                                max_rounds=as_int(goal.get("max_rounds") or 3),
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
        # 先把等着的审批/提问解开：即使会话已删除，也不能让 future 悬着
        for cid, (fut, it) in list(self.approvals.items()):
            it["status"] = "cancelled"
            if not self.deleted:
                self._update(it)
            if not fut.done():
                fut.cancel()
        self.approvals.clear()
        if self.deleted:
            # 已删除：磁盘写入已经由 `deleted` 门控住，但**通知没有**。前端没有墓碑，
            # 收到 thread/updated 会把 id 不在列表里的会话重新插回左栏——删除等于没删。
            # 删除正在跑的会话时 `thread_delete` 先 mark_deleted 再 interrupt，这里一定会走到。
            log.info("回合结束（会话 %s 已删除）：%s", self.id, reason)
            return
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
        dur = as_int((time.monotonic() - self.turn_started_at) * 1000)
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
        if s is None:
            return
        try:
            from ee.core.context import load_prompt
            prompt = f"用户的第一条消息：\n{first[:2000]}\n\n助手的回答开头：\n{reply[:800]}"
            title = await asyncio.wait_for(s.side_complete(load_prompt("title.md"), prompt, role="title"), 30)
        except Exception as e:  # noqa: BLE001
            log.info("生成标题失败（会话 %s）：%s", self.id, e)
            return
        title = clean_title(title)
        if self.deleted:
            # 标题是异步生成的，生成期间会话可能已被删除：删了就别再广播（前端会复活它）。
            return
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
        """记下这一回合结束时的磁盘快照。**后来的写法覆盖前面的**。

        一个外层回合内部可能跑多轮：全力以赴的修复轮、写回胜者，Goal 的多轮内层回合。
        只在第一次写入，会把「第一轮之后」的状态当成回合结束状态：检查点里看不到
        最终补丁（全力以赴写回胜者后就漏在那里），/undo 只能退到「拿当前工作区推断
        本回合的改动」，把用户之后的修改也圈进撤销范围。
        """
        for it in reversed(self.items):
            if it.get("type") == "user" and it.get("tree"):
                if it.get("tree_after") != tree:
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
        if self.deleted:
            # 删除后的迟到事件：条目和通知都不再改。删除时后端已经把 Thread 从
            # backend.threads 摘掉，前端也没有墓碑——再广播一条 thread/updated
            # 就会把会话重新插回左栏。
            return
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
        elif isinstance(ev, CaptureRequest):
            # 截图：工具正等着界面回图（RPC ui/capture_result 唤醒），这里只把请求递给前端。
            self.notify("ui/capture", {"thread_id": self.id, "id": ev.request_id})
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
