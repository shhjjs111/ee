"""D4-1：磁盘快照（影子 git 仓库）。"""
import asyncio
import shutil
import subprocess

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core import snapshot

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _ds(workspace, ee_home):
    return snapshot.DiskSnapshot(workspace, ee_home)


def test_capture_changed_restore(workspace, ee_home):
    (workspace / "a.txt").write_text("a1", encoding="utf-8")
    (workspace / "c.txt").write_text("c1", encoding="utf-8")
    (workspace / "子目录").mkdir()
    (workspace / "子目录" / "中文.txt").write_text("旧", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    t1 = ds.capture()
    assert t1 and ds.gitdir.is_dir() and not (workspace / ".git").exists()   # 不碰工作区
    (workspace / "a.txt").write_text("a2", encoding="utf-8")
    (workspace / "b.txt").write_text("new", encoding="utf-8")
    (workspace / "c.txt").unlink()
    (workspace / "子目录" / "中文.txt").write_text("新", encoding="utf-8")
    (workspace / "新目录").mkdir()
    (workspace / "新目录" / "x.txt").write_text("x", encoding="utf-8")
    t2 = ds.capture()
    changed = set(ds.changed(t1, t2))
    assert changed == {"a.txt", "b.txt", "c.txt", "子目录/中文.txt", "新目录/x.txt"}
    restored = ds.restore(t1, sorted(changed))
    assert len(restored) == 5
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "a1"
    assert (workspace / "c.txt").read_text(encoding="utf-8") == "c1"
    assert (workspace / "子目录" / "中文.txt").read_text(encoding="utf-8") == "旧"
    assert not (workspace / "b.txt").exists() and not (workspace / "新目录").exists()
    assert ds.changed(t1, ds.capture()) == []


def test_crlf_bytes_kept(workspace, ee_home):
    (workspace / "w.txt").write_bytes(b"line1\r\nline2\r\n")
    ds = _ds(workspace, ee_home)
    t1 = ds.capture()
    (workspace / "w.txt").write_bytes(b"changed\n")
    ds.restore(t1, ds.changed(t1, ds.capture()))
    assert (workspace / "w.txt").read_bytes() == b"line1\r\nline2\r\n"


def test_respects_gitignore_and_skips_big_files(workspace, ee_home):
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / ".gitignore").write_text("out/\n", encoding="utf-8")
    (workspace / "out").mkdir()
    (workspace / "out" / "build.bin").write_text("x", encoding="utf-8")
    (workspace / "big.dat").write_bytes(b"0" * (snapshot.MAX_UNTRACKED_BYTES + 10))
    (workspace / "src.py").write_text("print(1)\n", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    t = ds.capture()
    files = set(ds._run("ls-tree", "-r", "--name-only", t).decode().split())
    assert "src.py" in files and ".gitignore" in files
    assert "out/build.bin" not in files and "big.dat" not in files


def test_non_git_project_default_excludes(workspace, ee_home):
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "i.js").write_text("x", encoding="utf-8")
    (workspace / "app.js").write_text("y", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    files = set(ds._run("ls-tree", "-r", "--name-only", ds.capture()).decode().split())
    assert files == {"app.js"}


def test_unavailable_for_home_dir(ee_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ds = snapshot.DiskSnapshot(tmp_path, ee_home)
    assert not ds.available and ds.capture() is None


SHELL_EDIT = "echo changed-by-shell > s.txt"


def test_undo_restores_shell_changes_and_survives_restart(ee_home, workspace):
    """内存快照只管 write/edit；磁盘快照连 shell 改的文件也能撤销，重启后照样可用、不会重复撤销。"""
    (workspace / "s.txt").write_text("old", encoding="utf-8")
    script = [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "v1"}) + finish("tool_calls"),
        text_chunks("ok1") + finish("stop"),
        tool_chunks(0, "c2", "shell", {"command": SHELL_EDIT}) + finish("tool_calls"),
        text_chunks("ok2") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        for text in ("写 a", "用 shell 改 s"):
            ev.clear()
            await b.call("turn/start", {"thread_id": tid, "text": text})
            await wait_for(ev, "turn/completed")
        assert (workspace / "s.txt").read_bytes() != b"old"          # PowerShell 可能写成 UTF-16，只看变了没有
        users = [it for it in (await b.call("thread/get", {"thread_id": tid}))["thread"]["items"] if it["type"] == "user"]
        assert all(u.get("tree") and u.get("tree_after") for u in users)

        # 重启后撤销最近一回合（shell 改的 s.txt）
        b2, _, _ = make_backend(ee_home, [])
        r = await b2.call("thread/undo", {"thread_id": tid})
        assert any(p.endswith("s.txt") for p in r["restored"])
        assert (workspace / "s.txt").read_text(encoding="utf-8") == "old"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
        # 再重启：已撤销的不重复，下一次撤销第一回合（删掉新建的 a.txt）
        b3, _, _ = make_backend(ee_home, [])
        r = await b3.call("thread/undo", {"thread_id": tid})
        assert any(p.endswith("a.txt") for p in r["restored"]) and not (workspace / "a.txt").exists()
        assert (workspace / "s.txt").read_text(encoding="utf-8") == "old"
        r = await b3.call("thread/undo", {"thread_id": tid})
        assert r["restored"] == []
    asyncio.run(go())


def test_undo_keeps_user_edits_to_other_files(ee_home, workspace):
    """撤销只动那一回合改过的文件；用户之后手改的其他文件不受影响。"""
    script = [tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "v1"}) + finish("tool_calls"),
              text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        (workspace / "mine.txt").write_text("用户自己写的", encoding="utf-8")
        r = await b.call("thread/undo", {"thread_id": tid})
        assert [p for p in r["restored"] if p.endswith("a.txt")] and not (workspace / "a.txt").exists()
        assert (workspace / "mine.txt").read_text(encoding="utf-8") == "用户自己写的"
    asyncio.run(go())
