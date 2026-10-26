"""present_plan：计划模式下提交方案给用户批准（D4-3）。提交后本回合结束，界面显示方案卡片和「按此执行」按钮。"""
from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_PLAN = 40_000


class PresentPlanTool(Tool):
    name = "present_plan"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "plan": {"type": "string", "description": "完整方案（markdown）"},
        },
        "required": ["plan"],
    }

    def summary(self, args: dict) -> str:
        return str(args.get("title") or "实施方案")[:80]

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        plan = str(args.get("plan") or "").strip()
        if not plan:
            raise ToolError("plan 不能为空")
        session = ctx.extra.get("session")
        mode = getattr(getattr(session, "policy", None), "mode", "plan")
        if mode != "plan":
            raise ToolError("现在不是计划模式，不需要提交方案，直接按用户要求做即可。")
        title = str(args.get("title") or "").strip()[:120]
        return ToolResult("方案已提交给用户，等待批准。本回合到此结束，不要再调用工具或继续输出。",
                          summary=title or "实施方案",
                          meta={"kind": "plan_proposal", "title": title, "plan": plan[:MAX_PLAN], "end_turn": True})
