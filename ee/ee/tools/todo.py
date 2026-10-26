from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

STATUSES = ("pending", "in_progress", "completed")
MARK = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


class TodoTool(Tool):
    name = "todo"
    read_only = True   # 不碰文件系统，可与只读工具并行
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整清单",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "这一步做什么（祈使句）"},
                        "status": {"type": "string", "enum": list(STATUSES)},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    def summary(self, args: dict) -> str:
        return "Updated Plan"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        items = args.get("todos")
        if not isinstance(items, list):
            raise ToolError("todos 必须是数组")
        clean = []
        for i, it in enumerate(items, 1):
            if isinstance(it, str):
                it = {"content": it, "status": "pending"}
            if not isinstance(it, dict) or not str(it.get("content", "")).strip():
                raise ToolError(f"第 {i} 项缺少 content")
            st = str(it.get("status") or "pending").lower().replace("-", "_").replace(" ", "_")
            st = {"done": "completed", "complete": "completed", "doing": "in_progress", "todo": "pending",
                  "cancelled": "completed"}.get(st, st)
            if st not in STATUSES:
                raise ToolError(f"第 {i} 项 status 无效：{st}（只能是 pending / in_progress / completed）")
            clean.append({"content": str(it["content"]).strip(), "status": st})
        doing = [x for x in clean if x["status"] == "in_progress"]
        if len(doing) > 1:
            raise ToolError(f"同时只能有一项 in_progress，现在有 {len(doing)} 项。")
        ctx.state.todos = clean
        done = sum(1 for x in clean if x["status"] == "completed")
        text = "\n".join(f"{MARK[x['status']]} {x['content']}" for x in clean)
        return ToolResult(f"计划已更新（{done}/{len(clean)} 完成）：\n{text}", summary="Updated Plan",
                          meta={"kind": "plan", "items": clean})
