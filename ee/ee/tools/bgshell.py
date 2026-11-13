"""后台命令的配套工具（D4-6）：shell_output 读增量输出、shell_write 写 stdin、shell_kill 结束。

后台命令由 shell 工具 background=true 启动，进程归会话的 BgManager（ee/core/bgproc.py）管理。
"""
from __future__ import annotations

import re

from ee.core.events import Status
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_WAIT = 600                 # 与 shell 的 MAX_TIMEOUT 一致：等 CI / dev server 本来就该一次等到
                               # 出新输出。原先 120 时模型常要 180～600（实测 61/147 次），
                               # 被静默截到 120 后以为没等到、再发一次，多出 106 次徒劳请求。


def _mgr(ctx: ToolContext):
    mgr = getattr(ctx.extra.get("session"), "bg", None)
    if mgr is None:
        raise ToolError("当前会话不支持后台命令")
    return mgr


def _proc(mgr, args: dict):
    pid = str(args.get("id") or "").strip()
    if not pid:
        if not mgr.procs:
            raise ToolError("还没有后台命令（用 shell 工具 background=true 启动）")
        raise ToolError("缺少 id；现有后台命令：" + "、".join(f"{p.id}（{p.status}）{p.command[:40]}" for p in mgr.procs.values()))
    try:
        return mgr.get(pid)
    except KeyError as e:
        raise ToolError(str(e.args[0]))


def _status_line(p) -> str:
    if p.running:
        return f"[{p.id} 运行中 · 已运行 {p.info()['duration']}s]"
    if p.status == "killed":
        return f"[{p.id} 已被结束]"
    # 已退出的命令不要再轮询：实测模型会无视纯状态行，反复 shell_output 空转
    # （eval-full 任务 05：测试 7.5s 就过了，模型空等 782s 直到超时）。显式引导它继续干活。
    return (f"[{p.id} 已退出 · exit {p.exit_code}。命令已结束，不要再等待或轮询，"
            f"直接基于上面的输出继续下一步]")


def _format(p, text: str, notes: dict) -> str:
    parts = []
    if notes.get("lost"):
        parts.append(f"[更早的 {notes['lost']} 个字符已滚出缓冲，完整日志：{p.log_path}]")
    body = text.rstrip("\n")
    if not body:
        body = "（没有新的匹配行）" if notes.get("filtered") else "（没有新输出）"
    parts.append(body)
    parts.append(_status_line(p))
    return "\n".join(parts)


def _wait_tick(ctx: ToolContext, p, wait: int):
    """长等待期间给界面发进度（Status），不再是一段无事件的空白。

    实测一次 `wait=500` 期间界面零事件，用户分不清「还在正常等后台命令」和「卡死了」，
    于是强杀了 ee。每 5 秒报一次「等待中 · 已等 Ns / 上限 Ms」，成本可忽略（一次 status 通知）。
    """
    ev = (ctx.extra or {}).get("emit_event")
    if ev is None:
        return None

    def tick(elapsed: float) -> None:
        ev(Status(f"等待后台命令 {p.id} 输出：已等 {int(elapsed)}s / 上限 {wait}s"))

    return tick


def _max_wait(ctx: ToolContext) -> int:
    """本会话允许的最长等待：`shell_max_timeout`（无人值守硬上限）优先，否则 MAX_WAIT。

    ShellTool 已经按这个上限掐命令；后台等待如果不受同一上限约束，`shell_output(wait=600)`
    仍能合法占满 10 分钟——评测里实测到一次 600.0s 的空洞，整轮耗时被它一个盖住。
    """
    cap = int(getattr(ctx, "max_timeout", 0) or 0)
    return max(1, min(MAX_WAIT, cap)) if cap > 0 else MAX_WAIT


def _clear_status(ctx: ToolContext) -> None:
    """等待结束（等到输出 / 进程退出 / 到点）：把进度清掉，免得停在「等待中」误导用户。"""
    ev = (ctx.extra or {}).get("emit_event")
    if ev is not None:
        ev(Status(""))


class ShellOutputTool(Tool):
    name = "shell_output"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "wait": {"type": "integer", "description": f"没有新输出时最多等几秒（0～{MAX_WAIT}；超过按 {MAX_WAIT}）。等编译/测试/CI 这类长任务直接用 600，别反复小步轮询"},
            "filter": {"type": "string", "description": "只返回匹配这个正则的行"},
        },
        "required": ["id"],
    }

    def summary(self, args: dict) -> str:
        return f"Read output of {args.get('id', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        mgr = _mgr(ctx)
        p = _proc(mgr, args)
        pattern = None
        if args.get("filter"):
            try:
                pattern = re.compile(str(args["filter"]))
            except re.error as e:
                raise ToolError(f"filter 不是合法正则：{e}")
        wait = max(0, min(_max_wait(ctx), int(args.get("wait") or 0)))
        if wait:
            try:
                await mgr.wait_output(p, wait, pattern, tick=_wait_tick(ctx, p, wait))
            finally:
                _clear_status(ctx)
        text, notes = mgr.read_new(p, pattern=pattern)
        if text:
            ctx.emit(text)
        return ToolResult(_format(p, text, notes), summary=f"Read output of {p.id}",
                          meta={"kind": "shell_bg", "bg_id": p.id, "command": p.command, "status": p.status,
                                "exit": p.exit_code, "output": text[-4000:]})


class ShellWriteTool(Tool):
    name = "shell_write"
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "input": {"type": "string"},
            "close_stdin": {"type": "boolean", "description": "写完后发送 EOF"},
            "wait": {"type": "integer", "description": "写完后等几秒收集回应，默认 2"},
        },
        "required": ["id"],
    }

    def summary(self, args: dict) -> str:
        return f"Wrote to {args.get('id', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        mgr = _mgr(ctx)
        p = _proc(mgr, args)
        data = str(args.get("input") or "")
        close = bool(args.get("close_stdin"))
        if not data and not close:
            raise ToolError("input 为空（只想发送 EOF 就传 close_stdin=true）")
        try:
            await mgr.write(p, data, close)
        except RuntimeError as e:
            raise ToolError(str(e))
        raw_wait = args.get("wait")
        try:
            # isinstance 同时收窄类型并挡住非数字的字符串（模型可能传 "2" / "abc"）。
            wait_s = int(raw_wait) if isinstance(raw_wait, (int, float, str)) else 2
        except (TypeError, ValueError):
            wait_s = 2
        wait = max(0, min(_max_wait(ctx), wait_s))
        if wait:
            await mgr.wait_output(p, wait)
        text, notes = mgr.read_new(p)
        if text:
            ctx.emit(text)
        return ToolResult(_format(p, text, notes), summary=f"Wrote to {p.id}",
                          meta={"kind": "shell_bg", "bg_id": p.id, "command": p.command, "status": p.status,
                                "input": data[:2000], "output": text[-4000:]})


class ShellKillTool(Tool):
    name = "shell_kill"
    read_only = True          # 只能结束 ee 自己启动的后台命令，不改文件
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    def summary(self, args: dict) -> str:
        return f"Stopped {args.get('id', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        mgr = _mgr(ctx)
        p = _proc(mgr, args)
        was = p.running
        await mgr.kill(p)
        text, notes = mgr.read_new(p)
        msg = (f"已结束后台命令 {p.id}：{p.command}" if was else f"后台命令 {p.id} 本来就已结束（{p.status}）")
        if text.strip():
            msg += "\n最后的输出：\n" + text.rstrip("\n")[-4000:]
        return ToolResult(msg, summary=f"Stopped {p.id}",
                          meta={"kind": "shell_bg", "bg_id": p.id, "command": p.command, "status": p.status})
