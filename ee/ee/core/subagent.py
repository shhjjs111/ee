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
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path

from ee.core.agents import AgentDef, load_agents
from ee.core.events import (ApprovalRequest, AssistantMessage, Error, Notice, StepStart, SubagentEnd, SubagentStart,
                            SubagentUpdate, ToolEnd, ToolStart, TurnDone, Usage)
from ee.core.permissions import PermissionPolicy
from ee.tools.base import ToolState

DEFAULT_SUBAGENT = {"enabled": True, "max_steps": 30, "max_steps_limit": 100}
MAX_TOOL_ROWS = 200          # 界面记录里最多保留多少条工具行

BUILTIN: dict[str, AgentDef] = {
    "general": AgentDef(name="general", description="通用子代理：能读能改（权限同主会话），适合独立的多步子任务",
                        prompt=""),
    "explore": AgentDef(name="explore", description="只读探索：搜索、阅读代码和网页，回答问题，不改任何文件",
                        tools=["read", "grep", "glob", "shell", "todo", "web_search", "web_fetch", "skill"],
                        prompt="你是只读的探索子代理：只搜索和阅读（代码、文档、网页），不修改任何文件。"
                               "尽量并行地读和搜，给出有依据的结论，并列出关键文件路径和行号。"),
}

PREAMBLE = ("[子代理任务] 你是主代理派生出来的子代理，在独立的上下文里完成下面这一个子任务。"
            "你看不到主代理和用户之间的对话，任务说明就是你的全部信息；不要向用户提问。\n"
            "完成后用文字给出最终报告：结论、关键发现（附文件路径或 URL）、做了哪些改动、还有什么没完成。"
            "这份报告会原样交给主代理，是它唯一能看到的内容，所以要完整但简洁。")


def subagent_config(data: dict | None) -> dict:
    return {**DEFAULT_SUBAGENT, **((data or {}).get("subagent") or {})}


def agent_defs(home: Path, cwd: Path) -> dict[str, AgentDef]:
    defs = dict(BUILTIN)
    try:
        defs.update(load_agents([Path(home) / "agents", Path(cwd) / ".ee" / "agents"]))
    except Exception:  # noqa: BLE001  坏的定义文件不影响内置子代理
        pass
    return defs


# ------------------------------------------------------------------ 界面记录（纯函数，server 端也用）

def new_record(ev: SubagentStart) -> dict:
    return {"agent": ev.agent, "description": ev.description, "model": ev.model, "mode": ev.mode,
            "max_steps": ev.max_steps, "status": "running", "step": 0, "tools": [], "text": "", "summary": "",
            "notices": [], "usage": {}}


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

    limit = max(1, int(sc.get("max_steps_limit") or 100))
    steps = args.get("max_steps") or (adef.max_steps if kind not in BUILTIN and adef.max_steps else None) \
        or sc.get("max_steps") or 30
    steps = max(1, min(limit, int(steps)))

    want = str(args.get("mode") or "").strip().lower()
    read_only = (want in ("read-only", "readonly", "read_only") or parent.policy.mode in ("read-only", "plan")
                 or kind == "explore")
    model = str(args.get("model") or adef.model or "").strip() or parent.spec.alias

    data = copy.deepcopy(parent.config.data)
    data["max_steps"] = steps
    data.setdefault("subagent", {})["enabled"] = False      # 不嵌套
    cfg = copy.copy(parent.config)
    cfg.data = data

    def build() -> Session:
        child = Session(cfg, model=model, cwd=parent.cwd, mode="read-only" if read_only else parent.policy.mode,
                        client=parent._client_override, client_factory=parent._client_factory, persist=False,
                        home=parent.home, shell=parent.shell_name)
        if not read_only:
            child.policy = parent.policy           # 继承：同一模式 + 共享“以后都行”
        else:
            p = parent.policy
            child.policy = PermissionPolicy(mode="read-only", workspace=parent.cwd, allow=list(p.allow),
                                            ask=list(p.ask), deny=list(p.deny))
        child.state = _SharedSnapshots(parent.state)
        child.bg = parent.bg                       # 后台命令归主会话管（子代理结束不杀，主会话里也能看到）
        child.disk_enabled = False                 # 磁盘快照由主会话的回合前后统一抓（含子代理的改动）
        child._mcp_done = True                     # 子代理不自己连 MCP：沿用父会话已连好的工具
        if adef.tools:
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

    start = SubagentStart(call_id, kind, desc, child.spec.alias, child.policy.mode, steps)
    rec = new_record(start)
    emit(start)

    def up(ev) -> None:
        nonlocal rec
        rec = apply_event(rec, ev)
        emit(ev)

    text = PREAMBLE + (f"\n\n[子代理角色：{adef.name}] {adef.prompt}" if adef.prompt else "") + \
        f"\n\n## 子任务\n{task}"
    reason, final, step, errors = "error", "", 0, []
    gen = child.run_turn(text)
    try:
        async for ev in gen:
            if isinstance(ev, ApprovalRequest):
                emit(replace(ev, call_id=f"{call_id}/{ev.call_id}", title=f"[子代理 {kind}] {ev.title}"))
            elif isinstance(ev, StepStart):
                step = ev.step
                up(SubagentUpdate(call_id, "step", step=ev.step))
            elif isinstance(ev, ToolStart):
                up(SubagentUpdate(call_id, "tool_start", tool=ev.name, tool_call_id=ev.call_id, summary=ev.summary))
            elif isinstance(ev, ToolEnd):
                up(SubagentUpdate(call_id, "tool_end", tool=ev.name, tool_call_id=ev.call_id, summary=ev.summary,
                                  ok=ev.ok))
            elif isinstance(ev, AssistantMessage):
                if ev.text:
                    up(SubagentUpdate(call_id, "text", text=_clip(ev.text, 600)))
            elif isinstance(ev, Notice):
                up(SubagentUpdate(call_id, "notice", text=ev.text))
            elif isinstance(ev, Error):
                errors.append(ev.message)
                up(SubagentUpdate(call_id, "notice", text=ev.message))
            elif isinstance(ev, Usage):
                pass
            elif isinstance(ev, TurnDone):
                reason, final = ev.reason, ev.text or ""
    except asyncio.CancelledError:
        await _close(gen)
        _merge_usage(parent, child)
        up(SubagentEnd(call_id, "interrupted", final, step, child.stats.totals()))
        raise
    await _close(gen)
    _merge_usage(parent, child)
    usage = child.stats.totals()
    if not final.strip():
        final = ("（子代理没有给出文字报告" + (f"：{errors[-1]}" if errors else "") + "）")
    up(SubagentEnd(call_id, reason, final, step, usage))
    ok = reason in ("completed", "max_steps")
    note = {"completed": "", "max_steps": f"（子代理用完了 {steps} 步上限，报告可能不完整）",
            "loop": "（子代理陷入重复调用被停止）", "error": "（子代理出错结束）"}.get(reason, "")
    out = (f'<task_result agent="{kind}" status="{reason}" steps="{step}">\n{final.strip()}\n</task_result>' +
           (f"\n{note}" if note else ""))
    return out, ok, rec


async def _close(gen) -> None:
    try:
        await gen.aclose()
    except BaseException:  # noqa: BLE001
        pass


def _merge_usage(parent, child) -> None:
    """子代理的 token 和花费计入主会话统计，但不动主会话的“上次上下文大小”（压缩判断用）。"""
    st = parent.stats
    lp, lc = st.last_prompt, st.last_completion
    for p, c, h in child.stats.history:
        st.add({"prompt": p, "completion": c, "hit": h}, child.profile.price)
    st.requests += child.stats.usage_missing
    st.usage_missing += child.stats.usage_missing
    st.last_prompt, st.last_completion = lp, lc
