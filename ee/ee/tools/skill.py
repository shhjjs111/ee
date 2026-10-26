"""skill 工具：按名字加载一个技能的 SKILL.md 全文 + 目录文件列表（D5-5，渐进式披露的第二步）。"""
from __future__ import annotations

import asyncio

from ee.core import skills as K
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult


class SkillTool(Tool):
    name = "skill"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（会话开头技能清单里的名字）"},
        },
        "required": ["name"],
    }

    def summary(self, args: dict) -> str:
        return str(args.get("name") or "")

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip().lstrip("$/").strip()
        if not name:
            raise ToolError("缺少 name（技能名）")
        session = (ctx.extra or {}).get("session")
        if session is not None and hasattr(session, "skill_catalog"):
            cat = await asyncio.to_thread(session.skill_catalog)
        else:
            from ee.config import ee_home
            cfg = (ctx.extra or {}).get("config")
            cat = await asyncio.to_thread(K.discover, ee_home(), ctx.cwd, getattr(cfg, "data", None))
        sk = cat.skills.get(name)
        if sk is None:
            low = {k.lower(): k for k in cat.skills}
            sk = cat.skills.get(low.get(name.lower(), ""))
        if sk is None:
            avail = "、".join(s.name for s in cat.enabled()) or "（没有）"
            raise ToolError(f"没有名为 {name} 的技能。可用的技能：{avail}")
        if not sk.enabled:
            raise ToolError(f"技能 {sk.name} 已在设置里禁用，不能使用。")
        try:
            text = await asyncio.to_thread(K.render_full, sk)
        except OSError as e:
            raise ToolError(f"读取技能 {sk.name} 失败：{e}") from None
        return ToolResult(text, summary=sk.name,
                          meta={"kind": "skill", "name": sk.name, "path": sk.path, "dir": sk.dir,
                                "source": sk.source, "origin": sk.origin})
