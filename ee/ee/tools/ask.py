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
            "question": {"type": "string", "description": "问题（一句话，必要时附背景）"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": f"候选答案（2～{MAX_OPTIONS} 个）；不给就是开放式问题"},
            "multiple": {"type": "boolean"},
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
        # 与工具审批共用一个超时：窗口关掉 / 前端断线时这个 future 永远等不到结果，
        # 整个回合挂在这里，_history_lock 不释放，之后连下一条消息都发不出去。
        # 延迟导入：ee.core.session 会导入 tools 包，顶层导入会成环。
        from ee.core.session import APPROVAL_TIMEOUT
        if APPROVAL_TIMEOUT > 0:
            try:
                answer = await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                if not fut.done():
                    fut.cancel()
                raise ToolError(
                    f"等了 {APPROVAL_TIMEOUT:.0f} 秒没有收到用户的回答（窗口可能关了或断线了）。"
                    "不要假设答案：把这个问题和当前进展写进回复里，等用户回来再继续。") from None
        else:
            answer = await fut
        return ToolResult(f"用户回答：{answer}", summary=q[:80],
                          meta={"kind": "question", "question": q, "options": opts, "answer": answer})
