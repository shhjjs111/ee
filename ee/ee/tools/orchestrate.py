"""指挥会话的三个读工具（D6-1 U5）。

工具只拿到 `ctx.extra["orchestrator"]`（窄接口）和 `ctx.extra["thread_id"]`（调用者身份），
不拿 Backend——避免工具层能触达任意会话或任意 RPC。

所有阻塞操作（读会话文件、git status）都走 `asyncio.to_thread`，不卡事件循环。
"""
from __future__ import annotations

import asyncio
import json

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult


def _orch(ctx: ToolContext):
    orch = ctx.extra.get("orchestrator")
    tid = ctx.extra.get("thread_id")
    if orch is None or not tid:
        raise ToolError("当前会话没有启用指挥能力（在用户配置里打开 [orchestrator] enabled = true 后新开对话）")
    return orch, tid


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


class ThreadsListTool(Tool):
    name = "threads_list"
    read_only = True
    parameters = {"type": "object", "properties": {}, "required": []}

    def summary(self, args: dict) -> str:
        return "列出子会话"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        orch, tid = _orch(ctx)
        rows = await asyncio.to_thread(orch.list_children, tid)
        if not rows:
            return ToolResult("还没有子会话。用 thread_create 派发一个。", summary="0 个子会话")
        return ToolResult(_dump({"threads": rows}), summary=f"{len(rows)} 个子会话")


class ThreadStatusTool(Tool):
    name = "thread_status"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {"thread_id": {"type": "string", "description": "子会话 id（threads_list 里拿）"}},
        "required": ["thread_id"],
    }

    def summary(self, args: dict) -> str:
        return f"查看子会话 {args.get('thread_id', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        orch, pid = _orch(ctx)
        cid = str(args.get("thread_id") or "").strip()
        if not cid:
            raise ToolError("缺少 thread_id")
        try:
            st = await asyncio.to_thread(orch.status, pid, cid)
        except Exception as e:  # noqa: BLE001  OrchestratorError 等
            raise ToolError(str(e)) from None
        return ToolResult(_dump(st), summary=f"{st['title']} · {st['status']}")


class ThreadWaitTool(Tool):
    name = "thread_wait"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "thread_ids": {"type": "array", "items": {"type": "string"},
                           "description": "要等的一批子会话 id"},
            "timeout": {"type": "number", "description": "最多等多少秒（默认 600，上限 3600）"},
        },
        "required": ["thread_ids"],
    }

    def summary(self, args: dict) -> str:
        ids = args.get("thread_ids") or []
        return f"等待 {len(ids)} 个子会话"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        orch, pid = _orch(ctx)
        ids = [str(x) for x in (args.get("thread_ids") or []) if str(x).strip()]
        if not ids:
            raise ToolError("thread_ids 不能为空")
        try:
            res = await orch.wait(pid, ids, args.get("timeout"))
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None
        note = "（超时，下面是当时的快照）" if res.get("timed_out") else ""
        return ToolResult(_dump(res) + ("\n" + note if note else ""),
                          summary=f"{len(res.get('threads') or [])} 个子会话{'（超时）' if res.get('timed_out') else ''}")
