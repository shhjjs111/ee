"""read 的两个护栏缺口（2026-11 审计修复）。

1) `mark_read` 在 offset 越界检查**之前**执行：一次失败的读取也算「读过」，
   write 的「覆盖前必须先 read 过」护栏被一次读错（模型猜 offset、读空文件）满足。
2) UTF-16 文本被判成二进制：Windows PowerShell 5.1 的 `>` 重定向与 Out-File
   默认写 UTF-16LE+BOM，每隔一个字节就是 0x00，正好命中「前 8KB 有 0x00」的判据。
   本产品的主平台就是 Windows，脚本/命令产出的文本文件于是完全读不了、也改不了。
"""
import asyncio

import pytest

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.read import ReadTool
from ee.tools.write import WriteTool


def ctx_for(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / "out")


# ---------------------------------------------------------------- ① 读失败不算已读

def test_failed_read_does_not_satisfy_the_write_gate(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("only one line\n", encoding="utf-8")
    ctx = ctx_for(tmp_path)

    with pytest.raises(ToolError):
        asyncio.run(ReadTool().run({"path": str(f), "offset": 999}, ctx))

    # 模型没真正看到内容，覆盖就该被拦住
    with pytest.raises(ToolError):
        asyncio.run(WriteTool().run({"path": str(f), "content": "覆盖"}, ctx))
    assert f.read_text(encoding="utf-8") == "only one line\n"


def test_successful_read_still_satisfies_the_gate(tmp_path):
    """对照：真读过了，覆盖照常允许。"""
    f = tmp_path / "b.txt"
    f.write_text("old\n", encoding="utf-8")
    ctx = ctx_for(tmp_path)

    asyncio.run(ReadTool().run({"path": str(f)}, ctx))
    asyncio.run(WriteTool().run({"path": str(f), "content": "new\n"}, ctx))
    assert f.read_text(encoding="utf-8") == "new\n"


# ---------------------------------------------------------------- ② UTF-16 文本

def test_utf16_text_file_is_readable(tmp_path):
    f = tmp_path / "u16.txt"
    f.write_bytes("中文测试\n第二行\n".encode("utf-16"))      # 带 BOM，与 PowerShell 5.1 一致

    res = asyncio.run(ReadTool().run({"path": str(f)}, ctx_for(tmp_path)))

    assert "中文测试" in res.output and "第二行" in res.output


def test_utf16_file_roundtrip_keeps_encoding(tmp_path):
    f = tmp_path / "u16b.txt"
    f.write_bytes("第一行\n第二行\n".encode("utf-16"))
    ctx = ctx_for(tmp_path)

    asyncio.run(ReadTool().run({"path": str(f)}, ctx))
    asyncio.run(WriteTool().run({"path": str(f), "content": "第一行\n改过的第二行\n"}, ctx))

    raw = f.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff"), "写回后不再是 UTF-16"
    assert "改过的第二行" in raw.decode("utf-16")
