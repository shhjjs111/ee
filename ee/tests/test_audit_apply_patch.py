"""apply_patch 的两处「静默丢改动」（2026-11 审计修复）。

1) 同一文件出现两段 *** Update File 时，每段都拿磁盘原文算新内容、再逐份整份覆盖：
   前一段的改动被后一段抹掉，而回执里两条都写着 Edited，模型以为两处都改好了。
2) *** Move to 目标已存在时直接覆盖（Add File 有 exists() 检查，Move 没有），
   目标文件的内容永久丢失。
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


# ---------------------------------------------------------------- 同一文件多段 Update

def test_two_update_sections_on_same_file_both_apply(workspace):
    f = workspace / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    patch(workspace, "*** Update File: m.py\n-a = 1\n+a = 10\n"
                     "*** Update File: m.py\n-c = 3\n+c = 30")
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"


def test_second_section_sees_first_section_result(workspace):
    """第二段必须基于第一段的结果演算。

    第一段插入的 `Z = 9` 若被第二段当成「原文件没有这一行」而覆盖掉，
    说明两段各自拿磁盘原文计算——正是这次修的 bug。
    """
    f = workspace / "n.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    patch(workspace, "*** Update File: n.py\n-x = 1\n+x = 1\n+Z = 9\n"
                     "*** Update File: n.py\n-y = 2\n+y = 20")
    assert f.read_text(encoding="utf-8") == "x = 1\nZ = 9\ny = 20\n"


def test_same_path_is_reported_once(workspace):
    """回执不该对同一文件报两条 Edited（用户会以为改了两份文件）。"""
    f = workspace / "one.py"
    f.write_text("a = 1\nc = 3\n", encoding="utf-8")
    res = patch(workspace, "*** Update File: one.py\n-a = 1\n+a = 10\n"
                           "*** Update File: one.py\n-c = 3\n+c = 30")
    assert res.summary.count("Edited") == 1, res.summary


# ---------------------------------------------------------------- Move to 的存在性

def test_move_to_existing_target_is_refused(workspace):
    src = workspace / "a.py"
    dst = workspace / "b.py"
    src.write_text("source\n", encoding="utf-8")
    dst.write_text("目标原有内容\n", encoding="utf-8")

    with pytest.raises(ToolError) as ei:
        patch(workspace, "*** Update File: a.py\n*** Move to: b.py")

    assert "已存在" in str(ei.value)
    assert dst.read_text(encoding="utf-8") == "目标原有内容\n", "目标文件被静默覆盖了"
    assert src.is_file(), "报错后源文件不该被删"


def test_move_to_new_target_still_works(workspace):
    src = workspace / "c.py"
    src.write_text("source\n", encoding="utf-8")
    patch(workspace, "*** Update File: c.py\n*** Move to: d.py")
    assert not src.exists()
    assert (workspace / "d.py").read_text(encoding="utf-8") == "source\n"
