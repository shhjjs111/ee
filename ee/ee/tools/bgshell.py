"""后台命令的配套工具（D4-6）：shell_output 读增量输出、shell_write 写 stdin、shell_kill 结束。

后台命令由 shell 工具 background=true 启动，进程归会话的 BgManager（ee/core/bgproc.py）管理。
"""
from __future__ import annotations

import re

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_WAIT = 120


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
    return f"[{p.id} 已退出 · exit {p.exit_code}]"


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


class ShellOutputTool(Tool):
    name = "shell_output"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "wait": {"type": "integer", "description": f"没有新输出时最多等几秒（0～{MAX_WAIT}）"},
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
        wait = max(0, min(MAX_WAIT, int(args.get("wait") or 0)))
        if wait:
            await mgr.wait_output(p, wait, pattern)
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
        wait = max(0, min(MAX_WAIT, int(args.get("wait") if args.get("wait") is not None else 2)))
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
