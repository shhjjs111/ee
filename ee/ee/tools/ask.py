"""ask_user：向用户提一个问题并等回答（D4-8）。可给 2～6 个选项（单选或多选），用户也可以自己写。

回答前本回合暂停（界面显示提问卡片、桌面通知“需要你回答”）；用户点停止则本次调用中断。
子代理里不提供（子代理应自己做决定、把疑问写进结果）。
"""
from __future__ import annotations

import asyncio

from ee.core.events import QuestionRequest
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_OPTIONS = 6


class AskUserTool(Tool):
    name = "ask_user"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题（一句话说清楚，必要时附背景）"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": f"可选的答案（2～{MAX_OPTIONS} 个，简短）；推荐的放第一个并在后面写“（推荐）”。不给就是开放式问题"},
            "multiple": {"type": "boolean", "description": "是否可以多选，默认单选"},
        },
        "required": ["question"],
    }

    def summary(self, args: dict) -> str:
        return str(args.get("question") or "提问")[:80]

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        q = str(args.get("question") or "").strip()
        if not q:
            raise ToolError("question 不能为空")
        opts = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
        if len(opts) > MAX_OPTIONS:
            raise ToolError(f"选项最多 {MAX_OPTIONS} 个")
        if len(opts) == 1:
            raise ToolError("只有一个选项就不用问了；要么给 2 个以上选项，要么不给选项问开放式问题")
        emit = ctx.extra.get("emit_event")
        if emit is None:
            raise ToolError("当前环境不能向用户提问（无人值守运行），请自己做出合理决定并在结果里说明")
        fut = asyncio.get_running_loop().create_future()
        emit(QuestionRequest(ctx.extra.get("call_id", ""), q, opts, bool(args.get("multiple")), fut))
        answer = await fut
        return ToolResult(f"用户回答：{answer}", summary=q[:80],
                          meta={"kind": "question", "question": q, "options": opts, "answer": answer})
