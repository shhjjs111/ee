"""子代理（task 工具）：主代理派生一个独立上下文的子会话执行子任务，只把最终报告交回主代理。

- 独立上下文：子会话有自己的 history（同样加载 AGENTS.md 和记忆），看不到主代理的对话。
- 权限：mode=read-only 时子代理只读；mode=inherit（默认）时与主会话共用同一个 PermissionPolicy（同一模式、
  会话内“以后都行”的批准两边共享）。主会话本身是只读时子代理一定只读。
- 步数：max_steps（默认 [subagent].max_steps，上限 [subagent].max_steps_limit）。到上限时子代理被要求只做文字总结。
- 模型：model 可选，默认与主会话相同（同模型时直接复用主会话的模型客户端）。
- 不允许嵌套：子代理的工具表里没有 task。
- 审批冒泡：子代理的 ApprovalRequest 原样转发给主会话的事件流（call_id 加上父调用前缀，标题加上子代理名），
  界面照常显示审批卡片，用户的选择通过同一个 future 回到子代理。
- 中断：主回合被中断时 task 工具被取消 → 子会话的回合被取消（工具调用补“用户中断”）→ 发 SubagentEnd(interrupted)。
- /undo：子代理改文件的快照记进主会话当前回合的快照层，主会话 /undo 能一起撤销。
- 事件：SubagentStart / SubagentUpdate / SubagentEnd（core/events.py）；apply_event() 把它们归并成界面用的记录，
  server 端把记录挂在父 task 工具条目的 meta.subagent 上。
- 并行（D5-2）：同一步里的多个 task 由 session 的读写闸门调度（core/parallel.py）：
  只读子代理（explore、mode=read-only、主会话只读、只有只读工具的自定义子代理）一起跑；
  写型子代理：同一步有两个以上（或显式 isolation=worktree）且是 git 仓库、[subagent].worktree 开着时，
  各自在独立 git worktree（分支 ee/agent-…，带上主目录未提交的改动作为基线提交）里并行，结束后把改动
  提交在自己的分支上，向主代理汇报分支和改动统计，由用户或主代理决定是否合并；否则原地按顺序执行。
  同时运行的子代理数受 [subagent].max_parallel 限制（默认 3），超出的排队（界面显示“排队中”）。
- 只读 / 计划模式下：显式要 worktree 写的子代理直接拒绝；其它子代理一律只读运行。
- fork 模式（D5-10，task 的 fork=true）：子代理继承主会话到这一步为止的完整上下文，复用主会话冻结的
  system + tools 和历史前缀（字节相同 → 前缀缓存命中），历史末尾补上这一步各调用的占位结果再追加分支任务；
  工具表与主会话完全相同（保持前缀不变），只是 task / ask_user 在子代理里调用会直接报错（不嵌套、不提问）。
  只把最终报告回传主会话，子代理的过程不进主会话历史。并行、只读/写型、worktree、权限、Hooks 规则与普通
  子代理相同；只支持 general / explore，模型固定为主会话当前请求用的模型。
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path

from ee.core.agents import AgentDef, load_agents
from ee.core.events import (ApprovalRequest, AssistantMessage, Error, Notice, StepStart, SubagentEnd, SubagentStart,
                            SubagentUpdate, ToolEnd, ToolStart, TurnDone, Usage)
from ee.core import parallel
from ee.core.permissions import READONLY_MODES, PermissionPolicy
from ee.tools.base import Tool, ToolError, ToolResult, ToolState

DEFAULT_SUBAGENT = {"enabled": True, "max_steps": 30, "max_steps_limit": 100, "max_parallel": 3, "worktree": True}
MAX_PARALLEL_LIMIT = 8
MAX_TOOL_ROWS = 200          # 界面记录里最多保留多少条工具行

BUILTIN: dict[str, AgentDef] = {
    # 写型通用子代理给 60 步：改代码要「读 → 改 → 跑测试 → 看失败 → 再改」，
    # 30 步常常正好卡在收尾前（用完步数只能交一份不完整的报告，主代理还得重派
    # 一次，成本比给足步数更高）。上限仍受 [subagent].max_steps_limit 约束。
    "general": AgentDef(name="general", description="通用子代理：能读能改（权限同主会话），适合独立的多步子任务",
                        max_steps=60, prompt=""),
    "explore": AgentDef(name="explore", description="只读探索：搜索、阅读代码和网页，回答问题，不改任何文件",
                        # 只读探索不需要写型的步数预算：读几处就能给结论，30 步够用。
                        max_steps=30,
                        # shell 放回来：查「这个测试为什么失败」「这个命令输出什么」光靠
                        # grep 不够。只读沙箱下它会自动换成 powershell（见
                        # tools/sandbox_shell.py）——Git Bash 在受限令牌里起不来。
                        tools=["read", "grep", "glob", "shell", "todo", "web_search", "web_fetch", "skill",
                               "repo_map"],
                        prompt="你是只读的探索子代理：只搜索和阅读（代码、文档、网页），不修改任何文件。"
                               "尽量并行地读和搜，给出有依据的结论，并列出关键文件路径和行号。"),
}

PREAMBLE = ("[子代理任务] 你是主代理派生出来的子代理，在独立的上下文里完成下面这一个子任务。"
            "你看不到主代理和用户之间的对话，任务说明就是你的全部信息；不要向用户提问。\n"
            "完成后用文字给出最终报告：结论、关键发现（附文件路径或 URL）、做了哪些改动、还有什么没完成。"
            "这份报告会原样交给主代理，是它唯一能看到的内容，所以要完整但简洁。")


FORK_PREAMBLE = ("[分支子代理任务] 你是从主会话分叉出来的子代理：上面是主会话到目前为止的完整对话，可以直接利用其中的"
                 "信息，不必重新读一遍已经看过的内容（除非文件可能已变化）。现在只做下面这一个分支任务，不要接着做主会话"
                 "里的其他事情；不能再派子代理，也不要向用户提问。\n"
                 "完成后用文字给出最终报告：结论、关键发现（附文件路径或 URL）、做了哪些改动、还有什么没完成。"
                 "这份报告会交给主会话，你的中间过程它看不到，所以要完整但简洁，不要复述主会话已经知道的内容。")
FORK_AGENTS = ("general", "explore")
FORK_BLOCKED = ("task", "ask_user")


def _wants_fork(args: dict) -> bool:
    v = args.get("fork")
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


class _ForkBlocked(Tool):
    """分支子代理里的 task / ask_user：schema 与主会话一字不差（保住前缀缓存），调用时直接报错。"""
    read_only = True

    def __init__(self, orig) -> None:     # noqa: D401  不读描述文件，照抄原工具
        self.name = orig.name
        self.description = getattr(orig, "description", "")
        self.rules = list(getattr(orig, "rules", None) or [])   # 规则也照抄：系统提示词的「工具使用规则」段要一致
        self.parameters = orig.parameters
        self._schema = orig.schema()

    def schema(self) -> dict:
        return self._schema

    async def run(self, args: dict, ctx) -> ToolResult:
        raise ToolError(f"分支子代理里不能使用 {self.name}（不能再派子代理，也不能向用户提问）。请自己完成任务并给出报告。")


def fork_tools(tools: list) -> list:
    return [_ForkBlocked(t) if t.name in FORK_BLOCKED else t for t in tools]


def fork_history(history: list[dict], call_id: str) -> list[dict]:
    """主会话历史截到含这个 task 调用的 assistant 消息（含），再给这一步的每个调用补一条占位结果。
    前面的部分与主会话下一次请求逐字节相同，所以分支子代理的第一个请求能命中主会话的前缀缓存。"""
    k = None
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") == "assistant" and any(tc.get("id") == call_id for tc in m.get("tool_calls") or []):
            k = i
            break
    if k is None:
        raise ValueError("找不到这次 task 调用所在的消息，无法分叉主会话上下文")
    out = copy.deepcopy(history[:k + 1])
    for tc in history[k].get("tool_calls") or []:
        own = tc.get("id") == call_id
        out.append({"role": "tool", "tool_call_id": tc.get("id"),
                    "content": "已派出分支子代理（fork），它的任务见下一条消息。" if own
                    else "（同一步里的其他调用，它的结果不在这个分支里。）"})
    return out


def subagent_config(data: dict | None) -> dict:
    sc = {**DEFAULT_SUBAGENT, **((data or {}).get("subagent") or {})}
    try:
        sc["max_parallel"] = max(1, min(MAX_PARALLEL_LIMIT, int(sc.get("max_parallel") or 1)))
    except (TypeError, ValueError):
        sc["max_parallel"] = DEFAULT_SUBAGENT["max_parallel"]
    sc["worktree"] = bool(sc.get("worktree", True))
    return sc


# ------------------------------------------------------------------ 并行调度（D5-2）

_RO_ALIASES = ("read-only", "readonly", "read_only")


def _iso(args: dict) -> str:
    v = str(args.get("isolation") or "auto").strip().lower()
    return v if v in ("auto", "worktree", "none") else "auto"


def is_read_only(parent, args: dict, adef: AgentDef | None = None) -> bool:
    """这个 task 调用派出的子代理会不会是只读的。"""
    kind = str(args.get("agent") or "general").strip() or "general"
    if adef is None:
        adef = agent_defs(parent.home, parent.cwd).get(kind)
    want = str(args.get("mode") or "").strip().lower()
    if want in _RO_ALIASES or parent.policy.mode in READONLY_MODES or kind == "explore":
        return True
    if adef is not None and adef.tools and kind not in BUILTIN:
        tm = parent.tool_map
        return all(t in tm and getattr(tm[t], "read_only", False) for t in adef.tools if t in tm)
    return False


def access(parent, args: dict) -> str:
    """task 调用的访问类型：只读子代理 read（可并发），写型子代理 write（独占、按顺序）。"""
    kind = str(args.get("agent") or "general").strip() or "general"
    adef = agent_defs(parent.home, parent.cwd).get(kind)
    if adef is None or not str(args.get("prompt") or "").strip():
        return parallel.READ               # 马上报错，不碰任何东西
    if parent.policy.mode in READONLY_MODES and _iso(args) == "worktree":
        return parallel.READ               # 会被拒绝
    return parallel.READ if is_read_only(parent, args, adef) else parallel.WRITE


async def plan_step(parent, prepared: list, kinds: list[str], emit) -> dict[str, str]:
    """决定本步每个写型 task 是进独立 worktree 并行，还是原地按顺序。返回 {call_id: "worktree" | "inplace"}。"""
    writes = [p for p, k in zip(prepared, kinds) if p[1] is not None and p[1].name == "task" and p[3] is None
              and k == parallel.WRITE]
    if not writes:
        return {}
    sc = subagent_config(parent.config.data)
    want = [p for p in writes if _iso(p[2]) == "worktree" or (_iso(p[2]) == "auto" and len(writes) >= 2)]
    plan = {p[0]["id"]: "inplace" for p in writes}
    if not want:
        return plan
    why = ""
    if not sc["worktree"]:
        why = "设置里关闭了子代理 worktree（[subagent] worktree = false）"
    else:
        try:
            from ee.core import worktree as wt
            ok, why = await asyncio.to_thread(wt.agent_eligible, parent.cwd)
            if ok:
                why = ""
        except Exception as e:  # noqa: BLE001
            why = str(e)
    if why:
        if len(writes) >= 2:
            emit(Notice(f"{len(writes)} 个会改文件的子代理将按顺序执行（无法用独立 worktree 并行：{why}）。", "info"))
        return plan
    for p in want:
        plan[p[0]["id"]] = "worktree"
    return plan


def agent_defs(home: Path, cwd: Path) -> dict[str, AgentDef]:
    defs = dict(BUILTIN)
    try:
        defs.update(load_agents([Path(home) / "agents", Path(cwd) / ".ee" / "agents"]))
    except Exception as e:  # noqa: BLE001  坏的定义文件不影响内置子代理
        import logging
        logging.getLogger("ee.agents").warning("加载自定义子代理失败：%s", e)
    return defs


# ------------------------------------------------------------------ 界面记录（纯函数，server 端也用）

def new_record(ev: SubagentStart) -> dict:
    rec = {"id": ev.call_id, "agent": ev.agent, "description": ev.description, "model": ev.model, "mode": ev.mode,
           "max_steps": ev.max_steps, "status": ev.status or "running", "step": 0, "tools": [], "text": "",
           "summary": "", "notices": [], "usage": {}}
    if ev.worktree:
        rec["worktree"] = dict(ev.worktree)
    if getattr(ev, "fork", False):
        rec["fork"] = True
    return rec


def apply_event(rec: dict | None, ev) -> dict:
    """把一个子代理事件归并进记录（返回新 dict，不原地改）。"""
    if isinstance(ev, SubagentStart):
        return new_record(ev)
    rec = copy.deepcopy(rec) if rec else {"status": "running", "tools": [], "step": 0, "text": "", "notices": []}
    if isinstance(ev, SubagentUpdate):
        if ev.kind == "step":
            rec["step"] = ev.step
        elif ev.kind == "tool_start":
            rec["tools"].append({"id": ev.tool_call_id, "name": ev.tool, "summary": ev.summary, "status": "running"})
            if len(rec["tools"]) > MAX_TOOL_ROWS:
                rec["tools"] = rec["tools"][-MAX_TOOL_ROWS:]
                rec["tools_dropped"] = rec.get("tools_dropped", 0) + 1
        elif ev.kind == "tool_end":
            for t in reversed(rec["tools"]):
                if t["id"] == ev.tool_call_id:
                    t["status"] = "ok" if ev.ok else "error"
                    if ev.summary:
                        t["summary"] = ev.summary
                    break
        elif ev.kind == "text":
            rec["text"] = ev.text
        elif ev.kind == "notice":
            rec.setdefault("notices", []).append(ev.text)
        elif ev.kind == "usage":
            rec["usage"] = dict(ev.usage)
        elif ev.kind == "worktree":
            rec["worktree"] = {**(rec.get("worktree") or {}), **ev.data}
    elif isinstance(ev, SubagentEnd):
        rec["status"] = ev.status
        rec["summary"] = ev.summary
        rec["step"] = ev.steps or rec.get("step", 0)
        rec["usage"] = ev.usage
        for t in rec.get("tools", []):
            if t["status"] == "running":
                t["status"] = "interrupted"
    return rec


# ------------------------------------------------------------------ 运行

class _SharedSnapshots(ToolState):
    """子会话自己的“读过哪些文件”，但改前快照写进主会话的当前层（主会话 /undo 一起撤销）。"""

    def __init__(self, parent: ToolState):
        super().__init__()
        self.snapshots = parent.snapshots
        self.changed_files = parent.changed_files

    def new_turn(self) -> None:   # 不另开快照层
        pass


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def subagent_semaphore(parent, sc: dict) -> asyncio.Semaphore:
    """会话级的子代理并发闸门——**跨步共享**。

    原来在 _run_tools 里每步 new 一个，于是「最多 max_parallel 个并发」只在
    单步内成立：第 1 步派了 3 个还没结束，第 2 步又派 3 个，实际 6 个同时跑。
    闸门必须挂在 Session 上，只建一次；max_parallel 变了才重建。
    """
    want = max(1, int(sc.get("max_parallel") or 1))
    cur = getattr(parent, "_subagent_sem", None)
    if cur is not None and getattr(parent, "_subagent_sem_limit", None) == want:
        return cur
    sem = asyncio.Semaphore(want)
    parent._subagent_sem = sem
    parent._subagent_sem_limit = want
    return sem


async def run_subagent(parent, call_id: str, args: dict, emit) -> tuple[str, bool, dict]:
    """在 parent（Session）之下运行一个子代理。返回 (给主代理的输出, 是否成功, 界面记录)。"""
    from ee.core.session import Session

    sc = subagent_config(parent.config.data)
    defs = agent_defs(parent.home, parent.cwd)
    kind = str(args.get("agent") or "general").strip() or "general"
    adef = defs.get(kind)
    if adef is None:
        raise ValueError(f"没有名为 {kind} 的子代理类型。可用：{', '.join(defs)}")
    task = str(args.get("prompt") or "").strip()
    if not task:
        raise ValueError("prompt（子任务说明）不能为空")
    desc = _clip(str(args.get("description") or task.splitlines()[0]), 80)
    if parent.policy.mode in READONLY_MODES and _iso(args) == "worktree":
        raise ValueError(f"当前是{'计划' if parent.policy.mode == 'plan' else '只读'}模式，不能派会改文件的子代理"
                         "（isolation=worktree）。需要改文件请先让用户批准切换到默认或全自动模式；只读调研用 agent=explore。")

    fork = _wants_fork(args)
    if fork:
        if kind not in FORK_AGENTS:
            raise ValueError(f"fork 模式只支持 {' / '.join(FORK_AGENTS)} 子代理（自定义子代理有自己的提示词和工具表，无法复用主会话上下文）")
        if parent.prefix is None:
            raise ValueError("主会话还没有可继承的上下文，不能用 fork 模式")
        fork_model = parent.role_spec(parent.active_role()).alias
        if args.get("model") and str(args["model"]).strip() not in (fork_model, parent.spec.alias):
            raise ValueError(f"fork 模式沿用主会话当前的模型 {fork_model}（换模型就用不上主会话的缓存），不要指定 model；"
                             "需要别的模型请去掉 fork")
        fork_hist = fork_history(parent.history, call_id)

    limit = max(1, int(sc.get("max_steps_limit") or 100))
    # 优先级：调用时显式指定 > 这个 agent 自己定义 > 全局 [subagent].max_steps > 30。
    # 内置 agent 也要读 adef.max_steps——否则「general 给 60 步」这类按类型区分不生效
    # （原来的 kind not in BUILTIN 条件让内置 agent 永远拿全局默认值 30）。
    steps = args.get("max_steps") or adef.max_steps or sc.get("max_steps") or 30
    steps = max(1, min(limit, int(steps)))

    read_only = is_read_only(parent, args, adef)
    # 模型：调用时指定 > 子代理定义 > 只读子代理用 roles.explore（D5-9）> 主会话的模型
    model = str(args.get("model") or adef.model or "").strip() or \
        ((parent.config.role_model("explore") or "") if read_only else "") or parent.spec.alias
    if fork:
        model = fork_model
    use_wt = (not read_only) and (getattr(parent, "_subagent_plan", None) or {}).get(call_id) == "worktree"
    mode_label = "read-only" if read_only else parent.policy.mode

    data = copy.deepcopy(parent.config.data)
    data["max_steps"] = steps
    data.setdefault("subagent", {})["enabled"] = False      # 不嵌套
    data["ask_user"] = False                                 # 子代理不向用户提问
    cfg = copy.copy(parent.config)
    cfg.data = data
    source = {"call_id": call_id, "agent": kind, "description": desc}

    sem = subagent_semaphore(parent, sc)
    queued = sem.locked()
    if queued:                # 名额满了：先在界面上显示“排队中”
        emit(SubagentStart(call_id, kind, desc, model, mode_label, steps, status="queued"))
    try:
        await sem.acquire()
    except asyncio.CancelledError:
        if queued:
            emit(SubagentEnd(call_id, "interrupted", "", 0, {}))
        raise
    try:
        wt_info = None
        if use_wt:
            from ee.core import worktree as wt
            try:
                wt_info = await asyncio.to_thread(wt.create_for_agent, parent.cwd, desc, parent.home)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"无法为子代理建立独立 worktree：{e}。可以让它不并行（isolation=none）再试。") from None
        child_cwd = Path(wt_info["cwd"]) if wt_info else parent.cwd

        def build() -> Session:
            child = Session(cfg, model=model, cwd=child_cwd, mode="read-only" if read_only else parent.policy.mode,
                            client=parent._client_override, client_factory=parent._client_factory, persist=False,
                            home=parent.home, shell=parent.shell_name, user_home=parent.user_home)
            p = parent.policy
            if read_only:
                child.policy = PermissionPolicy(mode="read-only", workspace=parent.cwd, allow=list(p.allow),
                                                ask=list(p.ask), deny=list(p.deny), shell=p.shell)
                # 只读子代理用轻量上下文：不加载 AGENTS.md / 记忆 / 技能清单。
                # 依据 research/claude-code.md:24（子代理只收自身 system prompt +
                # 委派消息 + CLAUDE.md「Explore/Plan 跳过」+ git status）。
                # 实测这几块占 explore 首请求的 39%，而它拿到的是一件定好的调研任务。
                # 写型子代理不走这里——它要按项目约定改代码，AGENTS.md 必须留着。
                child._lean_context = True
            elif wt_info:
                # 工作区换成 worktree；会话内“以后都行”与主会话共享（同一个集合对象）
                child.policy = PermissionPolicy(mode=p.mode, workspace=child_cwd, allow=list(p.allow),
                                                ask=list(p.ask), deny=list(p.deny),
                                                session_prefixes=p.session_prefixes, session_tools=p.session_tools,
                                                shell=p.shell)
            else:
                child.policy = p                   # 继承：同一模式 + 共享“以后都行”
            if not wt_info:
                child.state = _SharedSnapshots(parent.state)   # 改动记进主会话当前回合，主会话 /undo 一起撤
            child.bg = parent.bg                       # 后台命令归主会话管（子代理结束不杀，主会话里也能看到）
            child.disk_enabled = False                 # 磁盘快照由主会话的回合前后统一抓（含子代理的改动）
            child.hooks_role = "subagent"              # Hooks：子代理只跑 PreToolUse / PostToolUse / Notification
            child.budget = parent.budget               # 花费上限：与主会话共用（并行子代理同时到线只问一次）
            child.budget_owner = False
            child.budget_root = parent.budget_root or parent
            child.style = "default"                    # 子代理的输出给主代理看，不套回答风格
            if child.spec.alias == parent.spec.alias:
                child.effort = parent.effort           # 同模型沿用主会话当前的推理强度
            child._mcp_done = True                     # 子代理不自己连 MCP：沿用父会话已连好的工具
            if fork:
                # 继承主会话：同一个冻结前缀 + 同一份工具表（task / ask_user 调用时报错）+ 历史前缀 + 环境附录状态
                child.prefix = parent.prefix
                child._prefix_style = parent._prefix_style
                child.style = parent._prefix_style or "default"
                child.tools = fork_tools(parent.tools)
                child.tool_map = {t.name: t for t in child.tools}
                child.mcp_tools = list(parent.mcp_tools)
                child.history = fork_hist
                child.updates = copy.deepcopy(parent.updates)
                child.effort = parent.effort_for(child.spec)
                # 会话级请求头（如 x-opencode-session）也用父会话的 id：前缀字节已经一致，
                # 再让 session id 对不上，服务端就不会把它们算作同一对话的缓存。
                child._fork_session_id = getattr(parent.store, "id", "") or None
            elif adef.tools:
                keep = set(adef.tools)
                child.tools = [t for t in child.tools if t.name in keep]
                child.tool_map = {t.name: t for t in child.tools}
            else:
                child.mcp_tools = list(parent.mcp_tools)
                child._install_mcp()
            if child.spec.alias == parent.spec.alias and parent._client is not None:
                child._client = parent._client          # 同模型直接复用客户端（含已学到的怪癖降级）
            else:
                child.client()                          # 可能解析代理/建 TLS：放在线程里
            return child

        try:
            child = await asyncio.to_thread(build)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"无法启动子代理（模型 {model}）：{e}") from None

        wt_pub = ({"path": wt_info["path"], "branch": wt_info["branch"], "base": wt_info["agent_base"],
                   "synced_dirty": wt_info.get("synced_dirty", False)} if wt_info else {})
        start = SubagentStart(call_id, kind, desc, child.spec.alias, child.policy.mode, steps, worktree=wt_pub,
                              fork=fork)
        rec = new_record(start)
        emit(start)

        def up(ev) -> None:
            nonlocal rec
            rec = apply_event(rec, ev)
            emit(ev)

        merged = [0, 0]      # 已并入主会话统计的 (history 条数, 缺用量的请求数)

        def merge_usage() -> None:
            _merge_usage(parent, child, merged)
            up(SubagentUpdate(call_id, "usage", usage=child.stats.totals(), totals=parent.stats.totals()))

        text = (FORK_PREAMBLE if fork else PREAMBLE) + (f"\n\n[子代理角色：{adef.name}] {adef.prompt}" if adef.prompt else "")
        if wt_info:
            text += ("\n\n[工作目录] 你在一个独立的 git worktree 里工作（分支 " + wt_info["branch"] + "），"
                     "与主目录隔离：其他子代理可能同时在改主目录或别的 worktree。只在当前目录里改文件，不要切换分支、"
                     "不要 push、不要合并；结束时不用自己提交，ee 会把改动提交在这个分支上交给主代理。")
        text += f"\n\n## 子任务\n{task}"
        reason, final, step, errors = "error", "", 0, []
        gen = child.run_turn(text)
        try:
            async for ev in gen:
                if isinstance(ev, ApprovalRequest):
                    emit(replace(ev, call_id=f"{call_id}/{ev.call_id}", title=f"[子代理 {kind}] {ev.title}",
                                 source=source))
                elif isinstance(ev, StepStart):
                    step = ev.step
                    up(SubagentUpdate(call_id, "step", step=ev.step))
                elif isinstance(ev, ToolStart):
                    up(SubagentUpdate(call_id, "tool_start", tool=ev.name, tool_call_id=ev.call_id,
                                      summary=ev.summary))
                elif isinstance(ev, ToolEnd):
                    up(SubagentUpdate(call_id, "tool_end", tool=ev.name, tool_call_id=ev.call_id,
                                      summary=ev.summary, ok=ev.ok))
                elif isinstance(ev, AssistantMessage):
                    if ev.text:
                        up(SubagentUpdate(call_id, "text", text=_clip(ev.text, 600)))
                elif isinstance(ev, Notice):
                    up(SubagentUpdate(call_id, "notice", text=ev.text))
                elif isinstance(ev, Error):
                    errors.append(ev.message)
                    up(SubagentUpdate(call_id, "notice", text=ev.message))
                elif isinstance(ev, Usage):
                    merge_usage()
                elif isinstance(ev, TurnDone):
                    reason, final = ev.reason, ev.text or ""
        except asyncio.CancelledError:
            await _close(gen)
            _merge_usage(parent, child, merged)
            up(SubagentEnd(call_id, "interrupted", final, step, child.stats.totals()))
            raise
        await _close(gen)
        _merge_usage(parent, child, merged)
        usage = child.stats.totals()
        if not final.strip():
            final = ("（子代理没有给出文字报告" + (f"：{errors[-1]}" if errors else "") + "）")
        wt_note = ""
        if wt_info:
            from ee.core import worktree as wt
            try:
                summ = await asyncio.to_thread(wt.finish_agent, wt_info, f"ee：子代理「{desc}」的改动", parent.home)
                up(SubagentUpdate(call_id, "worktree", data=summ))
                wt_note = worktree_note(summ)
            except Exception as e:  # noqa: BLE001
                wt_note = f"[worktree] 收尾失败：{e}。改动留在 {wt_info['path']}（分支 {wt_info['branch']}）。"
                up(SubagentUpdate(call_id, "notice", text=wt_note))
        up(SubagentEnd(call_id, reason, final, step, usage))
    finally:
        sem.release()
    ok = reason in ("completed", "max_steps")
    note = {"completed": "", "max_steps": f"（子代理用完了 {steps} 步上限，报告可能不完整）",
            "loop": "（子代理陷入重复调用被停止）", "error": "（子代理出错结束）",
            "budget": "（子代理因达到花费上限停止）"}.get(reason, "")
    fork_attr = ' fork="true"' if fork else ""
    digest = build_digest(rec, step)
    out = (f'<task_result agent="{kind}"{fork_attr} status="{reason}" steps="{step}">\n{final.strip()}\n</task_result>' +
           (f"\n{digest}" if digest else "") +
           (f"\n{note}" if note else "") + (f"\n{wt_note}" if wt_note else ""))
    return out, ok, rec


def build_digest(rec: dict, steps: int) -> str:
    """从**实际发生过**的工具调用推出结构化摘要。

    主代理拿到一段自由文本时，「改完了没 / 改了哪些文件 / 跑没跑过测试」全靠读文本
    猜；而模型的自述说的是它「打算」做什么（Claude Code 主代理侧提示词原话：
    trust but verify）。这里只用 rec["tools"] 里已经记录的事实，不让模型编。
    """
    tools = rec.get("tools") or []
    if not tools:
        return ""
    write_names = {"write", "edit", "apply_patch"}
    changed: list[str] = []
    ran: list[str] = []
    failed: list[str] = []
    for t in tools:
        name = str(t.get("name") or "")
        summ = str(t.get("summary") or "")
        first = summ.splitlines()[0].strip() if summ else ""
        if name in write_names and first:
            if first not in changed:
                changed.append(first)
        elif name == "shell" and first:
            if first not in ran:
                ran.append(first)
        if t.get("status") == "error":
            failed.append(f"{name}: {first or '(无输出)'}")
    lines: list[str] = []
    if changed:
        lines.append("改动文件：" + "、".join(changed[:12]) + ("（还有更多）" if len(changed) > 12 else ""))
    if ran:
        lines.append("执行命令：" + "；".join(ran[:6]) + ("（还有更多）" if len(ran) > 6 else ""))
    if failed:
        lines.append("出错工具：" + "；".join(failed[:6]))
    lines.append(f"工具调用 {len(tools)} 次，步数 {steps}")
    return "<task_digest>\n" + "\n".join(lines) + "\n</task_digest>"


def worktree_note(summ: dict) -> str:
    """写型子代理在 worktree 里的结果，给主代理看的说明。

    只说事实：`removed=True` 才是删掉了。保留但没统计到文件（diff 失败/不确定）时，
    不能写成“没有改动、已删除”——用户会据此以为没有东西要救。
    """
    if summ.get("removed"):
        return "[worktree] 子代理没有改任何文件，临时 worktree 和分支已删除。"
    if not summ.get("files"):
        why = summ.get("kept_reason") or "没有统计到文件"
        return (f"[worktree] 没有统计到文件改动，但为免丢失已保留 worktree（{why}）：目录 {summ.get('path')}，"
                f"分支 {summ.get('branch')}。里面的内容可能是子代理的成果，请先检查再决定是否删除"
                f"（项目菜单「Worktree…」）。")
    lines = [f"[worktree] 子代理的改动在独立分支 {summ['branch']} 上（目录 {summ['path']}），没有进入主目录："
             f"{summ['file_count']} 个文件 +{summ['additions']} -{summ['deletions']}"]
    for f in summ["files"][:30]:
        lines.append(f"  {f['path']}" + (" (二进制)" if f.get("binary") else f" (+{f['additions']} -{f['deletions']})"))
    if summ["file_count"] > 30:
        lines.append(f"  …共 {summ['file_count']} 个文件")
    lines.append(f"查看：git diff {summ['base'][:12]} {summ['branch']}。是否合并由用户决定（项目菜单「Worktree…」可合并或删除）；"
                 f"用户同意后也可以在主目录运行 git merge --no-ff {summ['branch']}，"
                 f"或只取这些改动：git cherry-pick --no-commit {summ['base'][:12]}..{summ['branch']}。")
    return "\n".join(lines)


async def _close(gen) -> None:
    try:
        await gen.aclose()
    except BaseException:  # noqa: BLE001
        pass


def _merge_usage(parent, child, merged: list | None = None) -> None:
    """子代理的 token 和花费增量计入主会话统计（可多次调用，merged 记已并入的位置），
    但不动主会话的“上次上下文大小”（压缩判断用）。"""
    merged = merged if merged is not None else [0, 0]
    st, cs = parent.stats, child.stats
    lp, lc = st.last_prompt, st.last_completion
    for r in cs.records[merged[0]:]:
        st.add_record(r)            # 按条复制：花费是子代理当时按它实际用的模型单价算好的
    merged[0] = len(cs.records)
    miss = cs.usage_missing - merged[1]
    if miss > 0:
        st.requests += miss
        st.usage_missing += miss
        merged[1] = cs.usage_missing
    st.last_prompt, st.last_completion = lp, lc
