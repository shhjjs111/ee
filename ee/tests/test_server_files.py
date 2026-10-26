"""文件面板后端：files/tree（懒加载、.gitignore）、files/read（文本/图片/二进制、截断、路径越界）、terminal/shell。"""
import asyncio
import os
import shutil
import subprocess
import sys

import pytest

from ee.server.app import Backend, RpcError

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def call(b, method, **params):
    return asyncio.run(b.call(method, params))


def make_tree(ws):
    (ws / "src").mkdir()
    (ws / "src" / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    (ws / "src" / "util").mkdir()
    (ws / "README.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    (ws / "logo.png").write_bytes(PNG)
    (ws / "blob.dat").write_bytes(b"\x00\x01\x02binary\x00" * 50)
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("1", encoding="utf-8")
    (ws / "build").mkdir()
    (ws / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (ws / "debug.log").write_text("noise", encoding="utf-8")


def test_tree_non_git(ee_home, workspace, monkeypatch):
    monkeypatch.setattr("ee.core.gitinfo.git_exe", lambda: None)
    make_tree(workspace)
    b = Backend(home=ee_home)
    r = call(b, "files/tree", cwd=str(workspace))
    names = [e["name"] for e in r["entries"]]
    # 目录在前、按名字排序；没有 git 时跳过常见的大目录
    assert names == ["src", ".gitignore", "blob.dat", "debug.log", "logo.png", "README.md"]
    assert r["gitignore"] is False
    kinds = {e["name"]: e["type"] for e in r["entries"]}
    assert kinds["src"] == "dir" and kinds["README.md"] == "file"
    sub = call(b, "files/tree", cwd=str(workspace), path="src")
    assert [(e["name"], e["path"], e["type"]) for e in sub["entries"]] == [
        ("util", "src/util", "dir"), ("calc.py", "src/calc.py", "file")]
    assert sub["entries"][1]["size"] == len("def add(a, b):\n    return a + b\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_tree_respects_gitignore(ee_home, workspace):
    make_tree(workspace)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (workspace / ".git" / "info" / "exclude").write_text("node_modules/\n", encoding="utf-8")
    b = Backend(home=ee_home)
    r = call(b, "files/tree", cwd=str(workspace))
    names = [e["name"] for e in r["entries"]]
    assert r["gitignore"] is True
    assert "build" not in names and "debug.log" not in names and "node_modules" not in names
    assert ".git" not in names
    assert {"src", "README.md", ".gitignore", "logo.png"} <= set(names)
    allr = call(b, "files/tree", cwd=str(workspace), show_ignored=True)
    ig = {e["name"] for e in allr["entries"] if e.get("ignored")}
    assert {"build", "debug.log", "node_modules"} <= ig


def test_read_kinds(ee_home, workspace):
    make_tree(workspace)
    b = Backend(home=ee_home)
    t = call(b, "files/read", cwd=str(workspace), path="src/calc.py")
    assert t["kind"] == "text" and t["text"].startswith("def add") and t["lines"] == 2 and not t["truncated"]
    md = call(b, "files/read", cwd=str(workspace), path="README.md")
    assert md["text"] == "# 标题\n\n正文\n"
    # 绝对路径也行，只要在工作区里
    ab = call(b, "files/read", cwd=str(workspace), path=str(workspace / "src" / "calc.py"))
    assert ab["path"] == "src/calc.py"
    img = call(b, "files/read", cwd=str(workspace), path="logo.png")
    assert img["kind"] == "image" and img["data_url"].startswith("data:image/png;base64,")
    bn = call(b, "files/read", cwd=str(workspace), path="blob.dat")
    assert bn["kind"] == "binary" and "text" not in bn


def test_read_truncates_and_decodes(ee_home, workspace):
    (workspace / "big.txt").write_bytes(b"ab\n" * 200_000)   # write_text 在 Windows 上会写成 \r\n
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (workspace / "bom.txt").write_bytes(b"\xef\xbb\xbfhello")
    (workspace / "gbk.txt").write_bytes("中文内容".encode("gbk"))
    b = Backend(home=ee_home)
    big = call(b, "files/read", cwd=str(workspace), path="big.txt")
    assert big["truncated"] and len(big["text"]) <= 512 * 1024 and big["size"] == 600_000
    small = call(b, "files/read", cwd=str(workspace), path="big.txt", max_bytes=2048)
    assert small["truncated"] and len(small["text"]) <= 2048
    assert call(b, "files/read", cwd=str(workspace), path="crlf.txt")["text"] == "a\nb\n"
    assert call(b, "files/read", cwd=str(workspace), path="bom.txt")["text"] == "hello"
    g = call(b, "files/read", cwd=str(workspace), path="gbk.txt")
    assert g["text"] == "中文内容" and g["encoding"] == "gbk"


def test_paths_must_stay_inside(ee_home, workspace, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    b = Backend(home=ee_home)
    for bad in ["../secret.txt", str(outside), "..", "a.txt/../../secret.txt"]:
        with pytest.raises(RpcError, match="不在工作区内"):
            call(b, "files/read", cwd=str(workspace), path=bad)
    with pytest.raises(RpcError, match="不在工作区内"):
        call(b, "files/tree", cwd=str(workspace), path="..")
    with pytest.raises(RpcError, match="不存在"):
        call(b, "files/read", cwd=str(workspace), path="nope.txt")
    with pytest.raises(RpcError, match="目录"):
        call(b, "files/read", cwd=str(workspace), path="")
    with pytest.raises(RpcError, match="目录不存在"):
        call(b, "files/tree", cwd=str(workspace / "missing"))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上建符号链接需要权限")
def test_symlink_escape_blocked(ee_home, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")
    os.symlink(outside, workspace / "link")
    b = Backend(home=ee_home)
    with pytest.raises(RpcError, match="不在工作区内"):
        call(b, "files/read", cwd=str(workspace), path="link/x.txt")
    with pytest.raises(RpcError, match="不在工作区内"):
        call(b, "files/tree", cwd=str(workspace), path="link")


def test_terminal_shell(ee_home, workspace):
    b = Backend(home=ee_home)
    r = call(b, "terminal/shell", cwd=str(workspace))
    assert r["setting"] == "auto"
    if sys.platform != "win32":
        assert r["kind"] in ("bash", None)
    if r["exe"]:
        assert os.path.exists(r["exe"]) or shutil.which(r["exe"])
