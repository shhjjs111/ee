"""memory 工具：增 / 列 / 删长期记忆条目（~/.ee/memory.md 与工作区 .ee/memory.md）。"""
from __future__ import annotations

import asyncio

from ee.core import memory as M
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

SCOPE_LABEL = {"user": "用户级", "workspace": "工作区级"}


class MemoryTool(Tool):
    name = "memory"
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"],
                       "description": "add 新增 / list 列出 / remove 删除"},
            "scope": {"type": "string", "enum": ["user", "workspace"],
                      "description": "user（默认）/ workspace"},
            "text": {"type": "string", "description": "add：要记住的一句话"},
            "index": {"type": "integer", "description": "remove：list 里的序号"},
            "match": {"type": "string", "description": "remove：或给唯一匹配的内容片段"},
        },
        "required": ["action"],
    }

    def __init__(self, home=None, workspace_enabled: bool = True):
        super().__init__()
        self.home = home
        self.workspace_enabled = workspace_enabled

    def summary(self, args: dict) -> str:
        act = {"add": "Remembered", "list": "Listed memory", "remove": "Forgot"}.get(args.get("action", ""), "Memory")
        return act

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from ee.config import ee_home
        home = self.home or (ctx.extra or {}).get("home") or ee_home()
        action = str(args.get("action") or "").lower()
        scope = str(args.get("scope") or "user").lower()
        if scope == "workspace" and not self.workspace_enabled:
            raise ToolError("工作区记忆已在设置里关闭，请用 scope=user")
        try:
            path = M.path_for(scope, home, ctx.cwd)
            if action == "add":
                n, entries = await asyncio.to_thread(M.add_entry, path, args.get("text") or "")
                return ToolResult(f"已记住（{SCOPE_LABEL[scope]}第 {n} 条，文件 {path}）。从下一个新会话起会自动加载。",
                                  summary="Remembered",
                                  meta={"kind": "memory", "action": "add", "scope": scope, "entries": entries})
            if action == "list":
                if scope == "user" and "scope" not in args:
                    blocks = []
                    for sc in (("user", "workspace") if self.workspace_enabled else ("user",)):
                        p = M.path_for(sc, home, ctx.cwd)
                        es = M.parse_entries(await asyncio.to_thread(M.read_text, p))
                        blocks.append(_fmt(sc, p, es))
                    return ToolResult("\n\n".join(blocks), summary="Listed memory", meta={"kind": "memory"})
                es = M.parse_entries(await asyncio.to_thread(M.read_text, path))
                return ToolResult(_fmt(scope, path, es), summary="Listed memory", meta={"kind": "memory"})
            if action == "remove":
                idx = args.get("index")
                removed, entries = await asyncio.to_thread(M.remove_entry, path, int(idx) if idx not in (None, "") else None,
                                                           args.get("match"))
                return ToolResult(f"已删除{SCOPE_LABEL[scope]}记忆：{removed}（剩 {len(entries)} 条）",
                                  summary="Forgot",
                                  meta={"kind": "memory", "action": "remove", "scope": scope, "entries": entries})
        except M.MemoryError_ as e:
            raise ToolError(str(e)) from None
        except OSError as e:
            raise ToolError(f"读写记忆文件失败：{e}") from None
        raise ToolError(f"未知 action：{action}（只能是 add / list / remove）")


def _fmt(scope: str, path, entries: list[str]) -> str:
    if not entries:
        return f"{SCOPE_LABEL[scope]}记忆（{path}）：空"
    return f"{SCOPE_LABEL[scope]}记忆（{path}），共 {len(entries)} 条：\n" + \
        "\n".join(f"{i}. {e}" for i, e in enumerate(entries, 1))
