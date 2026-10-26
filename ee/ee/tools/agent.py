"""task 工具：派生子代理执行独立子任务（实现见 ee/core/subagent.py）。"""
from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult


class TaskTool(Tool):
    name = "task"
    read_only = False      # 是否可并发按每次调用判断：见 access()
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3～10 个字的任务标题"},
            "prompt": {"type": "string", "description": "完整子任务说明：目标、背景、相关路径、要求的输出"},
            "agent": {"type": "string", "description": "general（默认）/ explore / 自定义子代理名"},
            "mode": {"type": "string", "enum": ["inherit", "read-only"],
                     "description": "inherit 同主会话（默认）/ read-only 只读"},
            "max_steps": {"type": "integer", "description": "最多步数"},
            "model": {"type": "string", "description": "子代理用的模型，默认同你"},
            "isolation": {"type": "string", "enum": ["auto", "worktree", "none"],
                          "description": "auto（默认）/ worktree / none（主目录顺序执行）"},
            "fork": {"type": "boolean",
                     "description": "true = 分支子代理：继承你的完整上下文（prompt 不用重复背景），只支持 general / explore"},
        },
        "required": ["description", "prompt"],
    }

    def access(self, args: dict, session) -> str:
        """只读子代理可与其它只读调用并发；写型子代理独占（进 worktree 的由 session 改为不排队）。"""
        from ee.core.subagent import access
        return access(session, args)

    def summary(self, args: dict) -> str:
        return f"Task{' (fork)' if args.get('fork') is True else ''} {args.get('description', '')}".strip()

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from ee.core.subagent import run_subagent
        parent = ctx.extra.get("session")
        emit = ctx.extra.get("emit_event")
        cid = ctx.extra.get("call_id", "")
        if parent is None or emit is None:
            raise ToolError("当前环境不支持子代理")
        try:
            out, ok, rec = await run_subagent(parent, cid, args, emit)
        except ValueError as e:
            raise ToolError(str(e)) from None
        return ToolResult(out, ok=ok, summary=self.summary(args), meta={"kind": "subagent", "subagent": rec})
