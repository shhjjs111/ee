"""read 工具读图片（方案 A：显式 as_image + 下一轮注入）的行为契约。

背景：模型做 PPT/项目验收时，需要 read 项目里已有的图片（截图/导出页）。
OpenAI 协议下 tool 结果只能是字符串，不能带图；所以 read 遇图片默认提示
「用 as_image=true 读视觉内容」，模型显式传参后，图片路径登记进会话的
待看队列，下一轮作为 image_url 块注入 user 消息。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ee.tools.base import ToolContext, ToolError, ToolState
from ee.tools.read import ReadTool


def _make_png(path: Path) -> None:
    # 1x1 透明 PNG（最小合法 PNG）
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6260000000000300010000050000"
        "000000000049454e44ae426082"
    ))


def _ctx(tmp_path: Path) -> ToolContext:
    state = ToolState()
    return ToolContext(cwd=tmp_path, state=state, output_dir=tmp_path / ".out")


def test_png_without_as_image_hints():
    """read 一个 png 但没传 as_image：返回提示，不登记待看图片，不报二进制错误。"""
    png = Path("a.png")
    _make_png(png)

    async def go():
        ctx = _ctx(Path.cwd())
        r = await ReadTool().run({"path": "a.png"}, ctx)
        return r, ctx

    r, ctx = asyncio.run(go())
    assert "as_image" in r.output, f"应提示用 as_image，实际：{r.output[:200]}"
    assert ctx.state.pending_images == [], "未传 as_image 不应登记待看图片"
    png.unlink()


def test_png_with_as_image_registers_pending():
    """as_image=true 时：登记进 pending_images，返回占位说明。"""
    png = Path("b.png")
    _make_png(png)

    async def go():
        ctx = _ctx(Path.cwd())
        r = await ReadTool().run({"path": "b.png", "as_image": True}, ctx)
        return r, ctx

    r, ctx = asyncio.run(go())
    assert len(ctx.state.pending_images) == 1, "as_image=true 应登记一张待看图片"
    assert ctx.state.pending_images[0].endswith("b.png")
    png.unlink()


def test_non_image_file_unaffected():
    """普通文本文件仍走原路径，as_image 参数对它们无副作用。"""
    txt = Path("c.txt")
    txt.write_text("hello\nworld\n", encoding="utf-8")

    async def go():
        ctx = _ctx(Path.cwd())
        r = await ReadTool().run({"path": "c.txt"}, ctx)
        return r, ctx

    r, ctx = asyncio.run(go())
    assert "hello" in r.output
    assert ctx.state.pending_images == []
    txt.unlink()


def test_misnamed_png_rejected_not_registered(tmp_path):
    """扩展名是 .png、文件头是 ZIP（Word 导出的 XPS）：不得登记待看图片，且要说清它是什么。

    如果只报「不是图片」，模型会重试；必须把真实格式告诉它，它才会换路（用 read 读文档）。
    """
    p = tmp_path / "_page1.png"
    p.write_bytes(b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 60)

    async def go():
        ctx = _ctx(tmp_path)
        with pytest.raises(ToolError) as ei:
            await ReadTool().run({"path": "_page1.png", "as_image": True}, ctx)
        return ei.value, ctx

    err, ctx = asyncio.run(go())
    assert "XPS" in str(err) or "ZIP" in str(err), f"要指明真实格式：{err}"
    assert ctx.state.pending_images == [], "格式不对就不能进待看队列（否则每轮重发、上游 400）"


def test_bmp_rejected_with_conversion_hint(tmp_path):
    """BMP 是图片但不是模型能收的格式：拒绝登记，并给出可行下一步。"""
    p = tmp_path / "a.bmp"
    p.write_bytes(b"BM" + b"\x00" * 60)

    async def go():
        ctx = _ctx(tmp_path)
        with pytest.raises(ToolError) as ei:
            await ReadTool().run({"path": "a.bmp", "as_image": True}, ctx)
        return ei.value, ctx

    err, ctx = asyncio.run(go())
    assert "BMP" in str(err) and "PNG" in str(err), f"要说明 BMP 不支持并给转换建议：{err}"
    assert ctx.state.pending_images == []
