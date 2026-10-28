"""指挥会话的五个工具（D6-1 U5/U6）。

工具只拿到 `ctx.extra["orchestrator"]`（窄接口）和 `ctx.extra["thread_id"]`（调用者身份），
不拿 Backend——避免工具层能触达任意会话或任意 RPC。调用者身份由服务端注入，工具参数里
没有 parent 字段，模型伪造不了父关系（I1）。

所有阻塞操作（读会话文件、git status、建 worktree）都走 `asyncio.to_thread`，不卡事件循环。
创建与发送的名额在服务层预留，工具层不重复判断——两层各判一次会给出两种不一致的上限。
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


class ThreadCreateTool(Tool):
    name = "thread_create"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "子任务标题（几个字，显示在会话列表里）"},
            "prompt": {"type": "string",
                       "description": "给子会话的第一件事（完整、自足的任务说明）。省略则只建会话，之后用 thread_send"},
            "mode": {"type": "string", "enum": ["read-only", "default", "auto"],
                     "description": "子会话的权限上限，不能比当前会话宽；不填沿用当前会话"},
            "worktree": {"type": "boolean",
                         "description": "是否在独立 worktree 里跑（默认 true）。改文件的任务必须是 true；"
                                        "false 时只能是只读"},
        },
        "required": ["title"],
    }

    def summary(self, args: dict) -> str:
        who = args.get("title") or ""
        return f"派发子会话：{who}" if args.get("prompt") else f"新建子会话：{who}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        orch, pid = _orch(ctx)
        title = str(args.get("title") or "").strip()
        if not title:
            raise ToolError("缺少 title")
        try:
            res = await orch.create_child(pid, title=title, prompt=args.get("prompt"),
                                          mode=args.get("mode"), worktree=args.get("worktree", True))
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None
        t = res.get("thread") or {}
        tip = "" if res.get("started") else "\n（会话已建好、还没派活：用 thread_send 给它第一件事）"
        return ToolResult(_dump(t) + tip, summary=f"{t.get('title', title)} · {t.get('status', 'idle')}")


class ThreadSendTool(Tool):
    name = "thread_send"
    parameters = {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "子会话 id（threads_list 里拿）"},
            "text": {"type": "string", "description": "要追加给它的任务说明"},
        },
        "required": ["thread_id", "text"],
    }

    def summary(self, args: dict) -> str:
        return f"给子会话发消息：{args.get('thread_id', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        orch, pid = _orch(ctx)
        cid = str(args.get("thread_id") or "").strip()
        text = str(args.get("text") or "")
        if not cid:
            raise ToolError("缺少 thread_id")
        if not text.strip():
            raise ToolError("text 不能为空")
        try:
            r = await orch.send(pid, cid, text)
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None
        return ToolResult(_dump(r), summary="已排队（当前回合一结束就送入）" if r.get("queued") else "已派发")
