import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.apply_patch import ApplyPatchTool, parse_patch
from ee.tools.edit import EditTool
from ee.tools.fileops import replace
from ee.tools.read import ReadTool
from ee.tools.write import WriteTool

SRC = "def f(x):\n    if x:\n        return 1\n    return 2\n"


def test_exact():
    out, strat, n = replace(SRC, "return 1", "return 10")
    assert strat == "exact" and n == 1 and "return 10" in out


def test_rstrip_trailing_whitespace():
    out, strat, _ = replace("a = 1   \nb = 2\n", "a = 1\nb = 2", "a = 3\nb = 4")
    assert strat == "rstrip" and out == "a = 3\nb = 4\n"


def test_relative_indent_mismatch_rejected():
    # 相对缩进和原文不同（不是整体平移）：不再逐行 trim 硬套，报错让模型照原文重试
    with pytest.raises(ToolError, match="最接近"):
        replace(SRC, "if x:\n return 1", "if x:\n        return 3")


def test_indent_flexible():
    content = "class A:\n    def g(self):\n        a = 1\n          b = 2\n"
    find = "a = 1\n  b = 2"  # 相对缩进相同、绝对缩进不同
    out, strat, _ = replace(content, find, "        a = 5\n          b = 6")
    assert strat == "indent" and "a = 5" in out


def test_similar_block_not_replaced():
    # 只是“相似”的块不再按首尾锚点替换：报错并给出最接近的原文
    content = "def h():\n    x = compute(1, 2)\n    y = x + 1\n    return y\n"
    find = "def h():\n    x = compute(1,2)\n    y = x+1\n    return y"
    with pytest.raises(ToolError, match="最接近"):
        replace(content, find, "def h():\n    return 0")


def test_not_unique_and_replace_all():
    with pytest.raises(ToolError, match="2 处"):
        replace("a\na\n", "a", "b")
    out, _, n = replace("a\na\n", "a", "b", replace_all=True)
    assert out == "b\nb\n" and n == 2


def test_not_found():
    with pytest.raises(ToolError, match="找不到"):
        replace(SRC, "nothing like this", "x")


def test_reject_much_larger_block():
    content = "start\n" + "\n".join(f"line{i}" for i in range(30)) + "\nend\n"
    find = "start\nlineX\nend"
    with pytest.raises(ToolError):
        replace(content, find, "z")


def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def test_edit_requires_read_and_keeps_crlf(workspace):
    p = workspace / "w.py"
    p.write_bytes("a = 1\r\nb = 2\r\n".encode())
    ctx = _ctx(workspace)
    with pytest.raises(ToolError, match="read"):
        run(EditTool().run({"path": "w.py", "old_string": "a = 1", "new_string": "a = 9"}, ctx))
    run(ReadTool().run({"path": "w.py"}, ctx))
    r = run(EditTool().run({"path": "w.py", "old_string": "a = 1\nb = 2", "new_string": "a = 9\nb = 2"}, ctx))
    assert r.ok and "(+1 -1)" in r.summary
    assert p.read_bytes() == b"a = 9\r\nb = 2\r\n"
    assert "-a = 1" in r.diff and "+a = 9" in r.diff


def test_write_overwrite_requires_read(workspace):
    (workspace / "x.txt").write_text("old", encoding="utf-8")
    ctx = _ctx(workspace)
    with pytest.raises(ToolError):
        run(WriteTool().run({"path": "x.txt", "content": "new"}, ctx))
    r = run(WriteTool().run({"path": "sub/new.txt", "content": "hi\n"}, ctx))
    assert r.summary.startswith("Added") and (workspace / "sub" / "new.txt").read_text() == "hi\n"
    assert str(workspace / "x.txt") not in ctx.state.snapshots[-1]


def test_read_ref_dedupe(workspace):
    p = workspace / "big.txt"
    p.write_text("\n".join(f"line {i} " + "x" * 40 for i in range(200)), encoding="utf-8")
    ctx = _ctx(workspace)
    r1 = run(ReadTool().run({"path": "big.txt"}, ctx))
    r2 = run(ReadTool().run({"path": "big.txt"}, ctx))
    r3 = run(ReadTool().run({"path": "big.txt"}, ctx))
    assert "line 0" in r1.output and "与上次读取相同" in r2.output and "line 0" in r3.output


def test_apply_patch(workspace):
    (workspace / "app.py").write_text('def greet():\n    print("Hi")\n\nx = 1\n', encoding="utf-8")
    (workspace / "old.txt").write_text("bye\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: app.py
@@ def greet():
-    print("Hi")
+    print("Hello, world!")
*** Delete File: old.txt
*** End Patch"""
    ctx = _ctx(workspace)
    r = run(ApplyPatchTool().run({"patch": patch}, ctx))
    assert r.ok
    assert (workspace / "hello.txt").read_text() == "Hello world\n"
    assert 'print("Hello, world!")' in (workspace / "app.py").read_text()
    assert not (workspace / "old.txt").exists()


def test_apply_patch_errors_atomic(workspace):
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    bad = "*** Begin Patch\n*** Add File: n.txt\n+n\n*** Update File: a.py\n-y = 2\n+y = 3\n*** End Patch"
    with pytest.raises(ToolError, match="找不到"):
        run(ApplyPatchTool().run({"patch": bad}, _ctx(workspace)))
    assert not (workspace / "n.txt").exists()   # 全部演算通过才落盘
    with pytest.raises(ToolError):
        parse_patch("no header")
