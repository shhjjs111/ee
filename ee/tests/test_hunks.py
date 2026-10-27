"""按块解析 / 反向应用 unified diff（纯函数）。用真实 git diff 生成 patch。"""
import shutil
import subprocess

import pytest

from ee.core import hunks as H

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git_patch(tmp_path, old: bytes, new: bytes, context: int = 3) -> str:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(old)
    b.write_bytes(new)
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "diff", "--no-index", "--no-color", f"-U{context}",
                        str(a), str(b)], capture_output=True)
    assert r.returncode in (0, 1), r.stderr
    return H.decode(r.stdout)


def check_all(tmp_path, old: bytes, new: bytes, context: int = 3):
    patch = git_patch(tmp_path, old, new, context)
    hs = H.parse_hunks(patch)
    assert hs, patch
    o, n = H.decode(old), H.decode(new)
    # 全部拒绝（任意顺序）= 旧内容；全部应用 = 新内容
    if context > 0:                    # 从前往后拒绝：后面的块行号会挪，要靠上下文偏移查找（0 上下文的纯增删没法定位）
        cur = n
        for h in hs:
            cur = H.reverse_apply(cur, h)
        assert H.encode(cur) == old
    cur = n
    for h in reversed(hs):
        cur = H.reverse_apply(cur, h)
    assert H.encode(cur) == old
    cur = o
    for h in reversed(hs):
        cur = H.apply(cur, h)
    assert H.encode(cur) == new
    return hs


def lines(n, fmt="line {i}", eol="\n"):
    return "".join(fmt.format(i=i) + eol for i in range(1, n + 1))


@needs_git
def test_multi_hunk_reject_each_independently(tmp_path):
    old = lines(40)
    new = old.replace("line 3\n", "line 3 改\n").replace("line 20\n", "").replace("line 35\n", "line 35\n新增\n")
    hs = check_all(tmp_path, old.encode(), new.encode())
    assert len(hs) == 3
    # 只拒绝中间一块：其他两块的改动保留
    r = H.reverse_apply(new, hs[1])
    assert "line 20\n" in r and "line 3 改\n" in r and "新增\n" in r


@needs_git
def test_crlf_bytes_preserved(tmp_path):
    old = lines(30, eol="\r\n").encode()
    new = old.replace(b"line 5\r\n", b"line five\r\n").replace(b"line 25\r\n", b"line 25\r\nextra\r\n")
    hs = check_all(tmp_path, old, new)
    assert any(t.endswith("\r") for _, t in hs[0].lines)            # 行内容保留了 \r


@needs_git
def test_chinese_and_gbk_bytes(tmp_path):
    old = "第一行\n第二行\n第三行\n".encode() + "GBK 内容\n".encode("gbk") + b"\xff\xfe raw\n"
    new = old.replace("第二行".encode(), "第二行（改）".encode()).replace(b"\xff\xfe raw", b"\xff\xfe raw2")
    check_all(tmp_path, old, new)


@needs_git
@pytest.mark.parametrize("old,new", [
    (b"a\nb\nc", b"a\nb\nc\n"),            # 只是补上结尾换行
    (b"a\nb\nc\n", b"a\nb\nc"),            # 去掉结尾换行
    (b"a\nb\nc", b"a\nB\nc"),              # 两边都没有结尾换行
    (b"a\nb", b"a\nb\nx\ny"),              # 末尾追加，两边都没有结尾换行
    (b"x\r\ny", b"x\r\nY\r\n"),            # CRLF + 无结尾换行
    (b"", b"new file\n"),                  # 从空到有
    (b"only\n", b""),                      # 清空
])
def test_no_newline_at_eof(tmp_path, old, new):
    check_all(tmp_path, old, new)


@needs_git
def test_zero_context(tmp_path):
    old = lines(10)
    new = old.replace("line 2\n", "").replace("line 8\n", "line 8\nins\n")
    check_all(tmp_path, old.encode(), new.encode(), context=0)


def test_normalized_crlf_match_keeps_file_eol():
    # 归一过换行的 patch（git 对未跟踪文件用 difflib 生成）对 CRLF 文件：按忽略 \r 匹配，换回去的行用 CRLF
    patch = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    h = H.parse_hunks(patch)[0]
    assert H.reverse_apply("a\r\nB\r\nc\r\n", h) == "a\r\nb\r\nc\r\n"


def test_mismatch_raises():
    h = H.parse_hunks("@@ -1,2 +1,2 @@\n a\n-b\n+B\n")[0]
    with pytest.raises(H.HunkError):
        H.reverse_apply("完全不同\n的内容\n", h)


def test_fingerprint_ignores_line_numbers_and_cr():
    a = H.parse_hunks("@@ -1,2 +1,2 @@\n x\n-b\n+B\n")[0]
    b = H.parse_hunks("@@ -10,2 +12,2 @@\n x\r\n-b\r\n+B\r\n")[0]
    c = H.parse_hunks("@@ -1,2 +1,2 @@\n x\n-b\n+C\n")[0]
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert (a.additions, a.deletions) == (1, 1)


def test_split_lines_only_on_lf():
    assert H.split_lines("a\u2028b\x0bc\rd\ne") == ["a\u2028b\x0bc\rd\n", "e"]
    assert H.split_lines("") == []


def test_parse_multiple_files_and_garbage():
    patch = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
             "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1,2 @@\n y\n+z\n")
    hs = H.parse_hunks(patch)
    assert [h.header for h in hs] == ["@@ -1 +1 @@", "@@ -1 +1,2 @@"]
    assert hs[1].lines == [(" ", "y"), ("+", "z")]


@needs_git
def test_random_edits_roundtrip(tmp_path):
    import random
    rnd = random.Random(7)
    pool = ["x", "y", "中文", "", "  缩进", "z\r", "tab\t"]
    for _ in range(40):
        old = [rnd.choice(pool) + str(rnd.randint(0, 5)) for _ in range(rnd.randint(0, 30))]
        new = list(old)
        for _ in range(rnd.randint(1, 6)):
            op = rnd.random()
            i = rnd.randint(0, len(new))
            if op < 0.4:
                new.insert(i, rnd.choice(pool) + "新")
            elif op < 0.7 and new:
                del new[min(i, len(new) - 1)]
            elif new:
                new[min(i, len(new) - 1)] += "改"
        eol_old, eol_new = rnd.choice(["\n", ""]), rnd.choice(["\n", ""])
        o = ("\n".join(old) + eol_old) if old else ""
        n = ("\n".join(new) + eol_new) if new else ""
        if o == n:
            continue
        check_all(tmp_path, o.encode(), n.encode(), context=rnd.choice([0, 1, 3]))
