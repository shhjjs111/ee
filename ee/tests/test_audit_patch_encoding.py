"""apply_patch 遇到认不出编码的文件会损坏未改动的行（2026-11 审计 P1#27 的剩余部分）。

`fileops.read_text` 在既不是 UTF-8 也不是 GBK 时用 `errors="replace"` 解码，并在
TextFile 上标 `lossy=True`。`edit` 工具早就有这道护栏（`edit.py:44-45`）：

    认不出编码，局部编辑会损坏原内容。要改请用 write 整体重写。

`apply_patch` 没有——而它恰恰是「读原文 → 只替换匹配到的那几行 → 整份写回」：
lossy 文本写回时，所有**没被改动**的行也一起变成 U+FFFD，原始字节永久丢失，
而回执仍然说「改好了」。

`write` 不需要这道护栏：它是整体覆盖，用户给的就是完整新内容。
"""
import asyncio

import pytest

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.apply_patch import ApplyPatchTool


def ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws.parent / "tool-output")


def patch(ws, body):
    return asyncio.run(ApplyPatchTool().run(
        {"patch": f"*** Begin Patch\n{body}\n*** End Patch"}, ctx(ws)))


def test_update_file_refuses_lossy_encoding(workspace):
    f = workspace / "old.txt"
    raw = bytes([0x81, 0x30, 0x41, 0x0A, 0x42, 0x0A])      # 非 UTF-8、非 GBK、无 BOM、无 0x00
    f.write_bytes(raw)

    with pytest.raises(ToolError) as ei:
        patch(workspace, "*** Update File: old.txt\n-B\n+C\n")

    msg = str(ei.value)
    assert "编码" in msg, msg
    assert f.read_bytes() == raw, "拒绝之后一个字节都不能变"


def test_add_file_still_works_on_broken_directory(workspace):
    """护栏只能拦「按原文改」，不能连新文件都写不了。"""
    (workspace / "old.txt").write_bytes(bytes([0x81, 0x30, 0x41, 0x0A]))

    patch(workspace, "*** Add File: new.txt\n+hello")

    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hello\n"
