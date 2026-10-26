"""task 工具：派生子代理执行独立子任务（实现见 ee/core/subagent.py）。"""
from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult


class TaskTool(Tool):
    name = "task"
    read_only = False      # 子代理可能改文件：多个 task 调用按顺序执行
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3～10 个字的任务标题（界面显示用）"},
            "prompt": {"type": "string", "description": "完整的子任务说明：目标、已知背景、相关路径、要求的输出"},
            "agent": {"type": "string", "description": "子代理类型：general（默认，可读写）/ explore（只读探索）/ 自定义"},
            "mode": {"type": "string", "enum": ["inherit", "read-only"],
                     "description": "权限：inherit 与主会话相同（默认）；read-only 只读"},
            "max_steps": {"type": "integer", "description": "最多步数，默认用设置里的子代理步数"},
            "model": {"type": "string", "description": "可选：子代理用的模型名，默认与主会话相同"},
        },
        "required": ["description", "prompt"],
    }

    def summary(self, args: dict) -> str:
        return f"Task {args.get('description', '')}".strip()

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
