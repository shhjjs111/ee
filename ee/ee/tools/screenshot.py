"""screenshot 工具：截一张当前桌面端窗口，登记为下一步可见的图片（只读）。

界面 → 后端的图片回路见 `core/screen.py`；这里只负责「要图 → 落盘 → 登记」。
登记进 `pending_images` 后，`Session._flush_pending_images` 会在下一步采样前把图补进上下文。
"""
from __future__ import annotations

import asyncio

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult


class ScreenshotTool(Tool):
    name = "screenshot"
    read_only = True
    parameters = {"type": "object", "properties": {}}

    def summary(self, args: dict) -> str:
        return "Screenshot"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from ee.core.screen import CaptureUnavailable, png_size
        sess = (ctx.extra or {}).get("session")
        hub = getattr(sess, "screen", None)
        if hub is None:
            raise ToolError("当前会话没有截图通道（只有桌面端里的会话才能截图）")
        try:
            png = await hub.request((ctx.extra or {}).get("emit_event"))
        except CaptureUnavailable as e:
            raise ToolError(str(e))
        path = await asyncio.to_thread(hub.save, png)
        ctx.state.pending_images.append(str(path))
        w, h = png_size(png)
        size = f"{w}×{h}" if w and h else "尺寸未知"
        return ToolResult(
            output=(f"已截取当前窗口（{size}），保存到 {path}。图片会在下一步作为视觉上下文出现；"
                    f"需要再看一次用 read(path=\"{path}\", as_image=true)。"),
            summary=f"Screenshot {size}",
            details={"kind": "screenshot", "path": str(path), "width": w, "height": h},
        )
