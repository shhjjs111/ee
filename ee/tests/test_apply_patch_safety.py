"""apply_patch 的安全性（D6 遗留问题：与 edit 同类的危险模糊匹配）。

背景：`apply_patch.seek()` 原来逐级放宽 精确 → rstrip → strip，且取第一处命中不查歧义。
实测三种误改：
  1. 重复代码（同一段出现两处）→ 静默改第一处，用户不知道改的是哪个；
  2. 缩进不同也能匹配 → new 原样写入，**缩进被吞掉**，产出的 Python 直接语法错误；
  3. strip 级把首尾空白都不看，尾随空白的差异被当成命中。

原则（与 edit 一致，见 HANDOFF fix/redo-edit）：**宁可报错，不能误改**。
- 去掉 strip 级；
- 多处命中报歧义错（带行号，方便模型重读）；
- 缩进差异只接受**整体平移**，并按原文缩进对齐 new（复用 fileops 的规则）。
"""
import asyncio
from pathlib import Path

import pytest

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.apply_patch import ApplyPatchTool


def ctx(ws: Path) -> ToolContext:
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws.parent / "tool-output")


def patch(ws: Path, body: str):
    return asyncio.run(ApplyPatchTool().run({"patch": f"*** Begin Patch\n{body}\n*** End Patch"}, ctx(ws)))


# ---------------------------------------------------------------- ① 重复代码必须报歧义


def test_duplicate_context_reports_ambiguity(workspace):
    """同一段代码出现两处 → 报歧义并给行号，文件一个字节都不动。"""
    f = workspace / "dup.py"
    original = "def a():\n    x = 1\n\ndef b():\n    x = 1\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(ToolError) as ei:
        patch(workspace, "*** Update File: dup.py\n-x = 1\n+x = 2")
    msg = str(ei.value)
    assert "多处" in msg or "歧义" in msg
    assert "2" in msg and "5" in msg          # 两处的行号都要给出来
    assert f.read_text(encoding="utf-8") == original


def test_duplicate_exact_context_also_ambiguous(workspace):
    """连完全一致（exact）的重复也要报错——静默改第一处就是误改。"""
    f = workspace / "dup2.py"
    original = "x = 1\nprint('a')\nx = 1\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(ToolError, match="多处|歧义"):
        patch(workspace, "*** Update File: dup2.py\n-x = 1\n+x = 2")
    assert f.read_text(encoding="utf-8") == original


def test_unique_context_still_works(workspace):
    """唯一命中照常改，不能因为加了歧义检查就改不动了。"""
    f = workspace / "one.py"
    f.write_text("def a():\n    x = 1\n\ndef b():\n    y = 2\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: one.py\n-    x = 1\n+    x = 42")
    assert r.ok
    assert "x = 42" in f.read_text(encoding="utf-8")


def test_header_ambiguity_reports_ambiguity(workspace):
    """`@@` 定位行在文件里出现两处 → 同样报歧义，不能随便挑一个。"""
    f = workspace / "hdr.py"
    original = "def greet():\n    a = 1\n\ndef greet():\n    b = 2\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(ToolError, match="多处|歧义"):
        patch(workspace, "*** Update File: hdr.py\n@@ def greet():\n-    a = 1\n+    a = 9")
    assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------- ② 缩进必须按原文对齐


def test_unindented_pattern_keeps_original_indent(workspace):
    """old 写的是无缩进的单行 → 命中后有缩进的行，new 必须继承原文缩进（原来会吞掉缩进）。"""
    f = workspace / "ind.py"
    f.write_text("class C:\n    def m(self):\n        return 1\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: ind.py\n-return 1\n+return 2")
    assert r.ok
    text = f.read_text(encoding="utf-8")
    assert text == "class C:\n    def m(self):\n        return 2\n"      # 8 空格缩进保留
    assert "\nreturn 2" not in text


def test_unindented_pattern_in_nested_block_keeps_indent(workspace):
    """嵌套块的缩进（函数体内一层）同样必须保留。"""
    f = workspace / "nested.py"
    f.write_text("def a():\n    x = 1\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: nested.py\n-x = 1\n+x = 2")
    assert r.ok
    assert f.read_text(encoding="utf-8") == "def a():\n    x = 2\n"


def test_indent_shift_applied_to_every_new_line(workspace):
    """多行改动：new 的每一行都按 old→原文 的偏移整体平移。"""
    f = workspace / "multi.py"
    f.write_text("def a():\n    if x:\n        do(1)\n        do(2)\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: multi.py\n-if x:\n-    do(1)\n-    do(2)\n+if x:\n+    do(3)\n+    do(4)")
    assert r.ok
    assert f.read_text(encoding="utf-8") == "def a():\n    if x:\n        do(3)\n        do(4)\n"


def test_shifted_block_with_same_relative_indent_matches(workspace):
    """old 自带的相对缩进与原文一致（只是基准不同）→ 整体平移，允许匹配。"""
    f = workspace / "shift.py"
    f.write_text("class C:\n    def m(self):\n        if y:\n            go()\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: shift.py\n-if y:\n-    go()\n+if y:\n+    stop()")
    assert r.ok
    assert "            stop()" in f.read_text(encoding="utf-8")


def test_relative_indent_mismatch_rejected(workspace):
    """相对缩进和原文不一致（不是整体平移）→ 报错，不做硬套。"""
    f = workspace / "rel.py"
    original = "def f(x):\n    if x:\n        return 1\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(ToolError):
        patch(workspace, "*** Update File: rel.py\n-if x:\n- return 1\n+if x:\n+ return 9")
    assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------- ③ rstrip 级仍保留，strip 级去掉


def test_trailing_whitespace_only_still_matches(workspace):
    """只有行尾空白不同 → 仍按 rstrip 命中（这一级是安全的，保留）。"""
    f = workspace / "ws.py"
    f.write_text("x = 1   \ny = 2\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: ws.py\n-x = 1\n+x = 9")
    assert r.ok
    assert f.read_text(encoding="utf-8").splitlines() == ["x = 9", "y = 2"]


def test_strip_level_is_gone(workspace):
    """strip 级（首尾空白都不看）已去掉：old 那行首尾都有空白、原文没有 → 必须拒绝，不能命中。"""
    f = workspace / "strip.py"
    original = "a\nfoo\nb\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(ToolError):
        patch(workspace, "*** Update File: strip.py\n-   foo   \n+bar")
    assert f.read_text(encoding="utf-8") == original


def test_blank_line_and_indent_mix(workspace):
    """含空行的多行块：空行不参与缩进比较，非空行按平移对齐。"""
    f = workspace / "blank.py"
    f.write_text("def a():\n    x = 1\n\n    y = 2\n", encoding="utf-8")
    r = patch(workspace, "*** Update File: blank.py\n-x = 1\n-\n-y = 2\n+w = 8\n+\n+z = 9")
    assert r.ok
    assert f.read_text(encoding="utf-8") == "def a():\n    w = 8\n\n    z = 9\n"


# ---------------------------------------------------------------- 原子性 / 回归


def test_ambiguity_leaves_other_files_untouched(workspace):
    """一处歧义 → 整个补丁不落盘（含同补丁里的新增文件）。"""
    (workspace / "a.py").write_text("x = 1\n\ny = 2\nx = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="多处|歧义"):
        patch(workspace, "*** Add File: new.txt\n+hello\n*** Update File: a.py\n-x = 1\n+x = 9")
    assert not (workspace / "new.txt").exists()
    assert (workspace / "a.py").read_text(encoding="utf-8") == "x = 1\n\ny = 2\nx = 1\n"


def test_existing_patch_still_passes(workspace):
    """原有正常补丁（Add / Update / Delete + @@ 定位）不受影响。"""
    (workspace / "app.py").write_text('def greet():\n    print("Hi")\n\nx = 1\n', encoding="utf-8")
    (workspace / "old.txt").write_text("bye\n", encoding="utf-8")
    r = patch(workspace, """*** Add File: hello.txt
+Hello world
*** Update File: app.py
@@ def greet():
-    print("Hi")
+    print("Hello, world!")
*** Delete File: old.txt""")
    assert r.ok
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "Hello world\n"
    assert 'print("Hello, world!")' in (workspace / "app.py").read_text(encoding="utf-8")
    assert not (workspace / "old.txt").exists()
