"""混合换行文件改一处，不能把整个文件都改成 CRLF。

缺陷（探针可复现）：`read_text()` 用 `eol = "\\r\\n" if "\\r\\n" in s else "\\n"` 判断换行风格——
只要文件里**任何一处**是 CRLF，就把整个文件的 eol 记成 CRLF；`_render()` 写回时
把每个 \\n 都换成 \\r\\n。于是混排文件（常见：Windows 编辑器写的新行是 CRLF、
脚本生成的是 LF）改一行，全文都变 CRLF，diff 变成整文件重写。

修法：读入时记住**每一行**原来的换行符，写回时按行还原；只有新加的行才用文件的主风格。
"""
import pytest

from ee.core.atomic import write_bytes as atomic_write
from ee.tools import fileops


def write_raw(path, data: bytes):
    atomic_write(path, data)


# ---------------------------------------------------------------- 缺陷复现


def test_mixed_eol_only_touches_changed_line(workspace):
    """混合换行：改第一行，其余行的换行符必须原样保留。"""
    f = workspace / "mixed.txt"
    write_raw(f, b"aaa\r\nbbb\nccc\r\n")          # 第 1、3 行 CRLF，第 2 行 LF
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "AAA\nbbb\nccc\n")   # 只改第一行内容
    assert f.read_bytes() == b"AAA\r\nbbb\nccc\r\n", f.read_bytes()


def test_mixed_eol_insert_uses_dominant_style(workspace):
    """新增的行用文件的主风格（多数派的那个）。"""
    f = workspace / "mixed2.txt"
    write_raw(f, b"a\r\nb\r\nc\nd\n")             # 2 个 CRLF、2 个 LF → 平手，取首个出现的
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "a\nb\nc\nd\nNEW\n")
    raw = f.read_bytes()
    assert raw.startswith(b"a\r\nb\r\nc\nd\n"), raw
    assert raw.rstrip().split(b"\n")[-1].replace(b"\r", b"") == b"NEW"


def test_pure_lf_stays_lf(workspace):
    f = workspace / "lf.txt"
    write_raw(f, b"a\nb\nc\n")
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "A\nb\nc\n")
    assert f.read_bytes() == b"A\nb\nc\n"


def test_pure_crlf_stays_crlf(workspace):
    f = workspace / "crlf.txt"
    write_raw(f, b"a\r\nb\r\nc\r\n")
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "A\nb\nc\n")
    assert f.read_bytes() == b"A\r\nb\r\nc\r\n"


def test_no_trailing_newline_preserved(workspace):
    """最后一行没有换行符时不能被补上。"""
    f = workspace / "nonl.txt"
    write_raw(f, b"a\r\nb")
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "A\nb")
    assert f.read_bytes() == b"A\r\nb"


def test_bom_with_mixed_eol(workspace):
    """BOM + 混合换行一起处理。"""
    f = workspace / "bom.txt"
    write_raw(f, b"\xef\xbb\xbfa\r\nb\n")
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "A\nb\n")
    assert f.read_bytes() == b"\xef\xbb\xbfA\r\nb\n"


def test_fully_replaced_content_uses_dominant(workspace):
    """整篇换掉（内容行数完全不同）时，新行按主风格写。"""
    f = workspace / "replace.txt"
    write_raw(f, b"a\r\nb\r\nc\r\n")
    tf = fileops.read_text(f)
    fileops.write_text(f, tf, "x\ny\nz\n")
    assert f.read_bytes() == b"x\r\ny\r\nz\r\n"
