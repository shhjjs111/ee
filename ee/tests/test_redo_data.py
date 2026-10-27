"""数据安全修复（redo-data）：撤销冲突检测、快照恢复预检、worktree 锁/忽略文件备份、坏 JSONL、原子写入。"""
import asyncio
import shutil

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _write(cid, content, path="a.txt"):
    return tool_chunks(0, cid, "write", {"path": path, "content": content}) + finish("tool_calls")


def _users(items):
    return [it for it in items if it["type"] == "user"]


# ---------------------------------------------------------------- 1) /undo、编辑重发不覆盖用户手改
@needs_git
def test_undo_detects_user_edit_and_saves_restore_point(ee_home, workspace):
    script = [_write("c1", "v1"), text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
        (workspace / "a.txt").write_text("用户手改", encoding="utf-8")       # 回合结束后用户自己改了

        r = await b.call("thread/undo", {"thread_id": tid})
        assert r["needs_confirm"] is True and r["conflicts"] == ["a.txt"] and r["restored"] == []
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "用户手改"   # 一个字节都没动

        r = await b.call("thread/undo", {"thread_id": tid, "force": True})
        assert any(p.endswith("a.txt") for p in r["restored"])
        assert not (workspace / "a.txt").exists()          # 回合前没有这个文件
        rp = r["restore_point"]
        assert rp and rp["kind"] == "restore"
        lst = await b.call("checkpoint/list", {"thread_id": tid})
        assert any(c["id"] == rp["id"] for c in lst["checkpoints"])
        # 反悔：恢复到恢复点，用户手改的内容回来了
        await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": rp["id"]})
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "用户手改"
    asyncio.run(go())


@needs_git
def test_undo_without_user_edit_needs_no_confirm(ee_home, workspace):
    script = [_write("c1", "v1"), text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        r = await b.call("thread/undo", {"thread_id": tid})
        assert not r.get("needs_confirm") and any(p.endswith("a.txt") for p in r["restored"])
        assert not (workspace / "a.txt").exists()
    asyncio.run(go())


@needs_git
def test_edit_resend_detects_user_edit(ee_home, workspace):
    script = [_write("c1", "v1"), text_chunks("ok1") + finish("stop"),
              text_chunks("重新来") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        (workspace / "a.txt").write_text("用户手改", encoding="utf-8")
        u1 = _users((await b.call("thread/get", {"thread_id": tid}))["thread"]["items"])[0]

        ev.clear()
        r = await b.call("thread/edit_resend", {"thread_id": tid, "item_id": u1["id"], "text": "别写了"})
        assert r["needs_confirm"] is True and r["conflicts"] == ["a.txt"]
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "用户手改"
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert [u["id"] for u in _users(d["items"])] == [u1["id"]]          # 历史没被截断、没开新回合
        assert not any(m == "turn/started" for m, _ in ev)

        r = await b.call("thread/edit_resend", {"thread_id": tid, "item_id": u1["id"], "text": "别写了", "force": True})
        assert r["files_reverted"] is True and r["restore_point"]
        assert not (workspace / "a.txt").exists()
        await wait_for(ev, "turn/completed")
        await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": r["restore_point"]["id"]})
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "用户手改"
    asyncio.run(go())


# ---------------------------------------------------------------- 2) 快照恢复：先预检，再如实报告
def _two_files(workspace, ee_home):
    from ee.core import snapshot
    (workspace / "a.txt").write_bytes(b"a1\r\n")
    (workspace / "b.txt").write_bytes(b"b1")
    ds = snapshot.DiskSnapshot(workspace, ee_home)
    t1 = ds.capture()
    (workspace / "a.txt").write_bytes(b"a2")
    (workspace / "b.txt").write_bytes(b"b2")
    (workspace / "new.txt").write_bytes(b"n")
    return snapshot, ds, t1


@needs_git
def test_restore_precheck_locked_file_touches_nothing(workspace, ee_home, monkeypatch):
    snapshot, ds, t1 = _two_files(workspace, ee_home)
    real = snapshot._probe_writable

    def probe(full):
        if full.name == "b.txt":
            raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
        return real(full)
    monkeypatch.setattr(snapshot, "_probe_writable", probe)
    paths = ds.changed(t1, ds.capture())
    with pytest.raises(snapshot.RestoreBlocked) as ei:
        ds.restore(t1, paths)
    assert [p for p, _ in ei.value.blocked] == ["b.txt"]
    assert (workspace / "a.txt").read_bytes() == b"a2" and (workspace / "new.txt").exists()   # 一个都没动


@needs_git
def test_restore_midway_failure_reports_done_and_failed(workspace, ee_home, monkeypatch):
    snapshot, ds, t1 = _two_files(workspace, ee_home)
    real = snapshot._put

    def put(full, data, mode):
        if full.name == "b.txt":
            raise PermissionError(32, "被占用")
        return real(full, data, mode)
    monkeypatch.setattr(snapshot, "_put", put)
    paths = ds.changed(t1, ds.capture())
    with pytest.raises(snapshot.RestorePartial) as ei:
        ds.restore(t1, paths)
    e = ei.value
    assert sorted(p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in e.restored) == ["a.txt", "new.txt"]
    assert [p for p, _ in e.failed] == ["b.txt"]
    assert (workspace / "a.txt").read_bytes() == b"a1\r\n" and not (workspace / "new.txt").exists()
    assert (workspace / "b.txt").read_bytes() == b"b2"


@needs_git
def test_undo_reports_locked_file_without_touching_anything(ee_home, workspace, monkeypatch):
    from ee.core import snapshot
    from ee.server.app import RpcError
    script = [tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "v1"}) + tool_chunks(1, "c2", "write", {"path": "b.txt", "content": "v1"}) + finish("tool_calls"),
              text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a b"})
        await wait_for(ev, "turn/completed")
        real = snapshot._probe_writable
        monkeypatch.setattr(snapshot, "_probe_writable",
                            lambda f: (_ for _ in ()).throw(PermissionError(32, "被占用")) if f.name == "b.txt" else real(f))
        with pytest.raises(RpcError) as ei:
            await b.call("thread/undo", {"thread_id": tid})
        assert "b.txt" in str(ei.value) and "没有恢复任何文件" in str(ei.value)
        assert (workspace / "a.txt").exists() and (workspace / "b.txt").exists()
        monkeypatch.setattr(snapshot, "_probe_writable", real)      # 关掉占用后再撤销一次就好了
        r = await b.call("thread/undo", {"thread_id": tid})
        assert len(r["restored"]) == 2 and not (workspace / "a.txt").exists()
    asyncio.run(go())


@needs_git
def test_checkpoint_restore_blocked_keeps_files_and_drops_restore_point(ee_home, workspace, monkeypatch):
    from ee.core import snapshot
    from ee.server.app import RpcError
    script = [_write("c1", "v1"), text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        u1 = _users((await b.call("thread/get", {"thread_id": tid}))["thread"]["items"])[0]
        monkeypatch.setattr(snapshot, "_probe_writable", lambda f: (_ for _ in ()).throw(PermissionError(32, "x")))
        with pytest.raises(RpcError) as ei:
            await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": u1["id"]})
        assert "a.txt" in str(ei.value)
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
        lst = await b.call("checkpoint/list", {"thread_id": tid})
        assert not [c for c in lst["checkpoints"] if c["kind"] == "restore"]
    asyncio.run(go())


# ---------------------------------------------------------------- 3) worktree：锁定的拒删；删前备份被忽略的文件
def _git(cwd, *a):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def wt_repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    (r / ".gitignore").write_text(".env\nlocal/\nnode_modules/\n", encoding="utf-8")
    (r / ".env").write_text("KEY=main\n", encoding="utf-8")
    (r / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@needs_git
def test_locked_worktree_is_not_removed(wt_repo, ee_home):
    from pathlib import Path

    from ee.core import worktree as W
    info = W.create(wt_repo, "locked", home=ee_home)
    p = Path(info["path"])
    (p / "work.txt").write_text("未提交", encoding="utf-8")
    _git(wt_repo, "worktree", "lock", "--reason", "放在移动硬盘上", str(p))
    with pytest.raises(W.WorktreeError, match="锁定") as ei:
        W.remove(p, home=ee_home)
    assert "放在移动硬盘上" in str(ei.value) and "unlock" in str(ei.value)
    assert (p / "work.txt").read_text(encoding="utf-8") == "未提交"
    assert [x["path"] for x in W.list_for(wt_repo, ee_home, with_status=False)] == [str(p)]
    with pytest.raises(W.WorktreeError, match="锁定"):        # 强制删也不行
        W.remove(p, delete_branch=True, force=True, home=ee_home)
    assert p.is_dir()


@needs_git
def test_remove_backs_up_ignored_files(wt_repo, ee_home):
    from pathlib import Path

    from ee.core import worktree as W
    info = W.create(wt_repo, "bak", home=ee_home)
    p = Path(info["path"])
    (p / ".env").write_bytes(b"KEY=changed-in-worktree\r\n")      # 被 git 忽略、和主目录不同 → 要备份
    (p / "local").mkdir()
    (p / "local" / "secret.json").write_text("{}", encoding="utf-8")
    (p / "node_modules").mkdir()
    (p / "node_modules" / "dep.js").write_text("x", encoding="utf-8")  # 依赖目录：不备份
    r = W.remove(p, home=ee_home)
    assert not p.exists()
    bak = Path(r["ignored_backup"])
    assert ee_home in bak.parents
    assert (bak / ".env").read_bytes() == b"KEY=changed-in-worktree\r\n"
    assert (bak / "local" / "secret.json").is_file() and not (bak / "node_modules").exists()
    assert sorted(r["backed_up"]) == [".env", "local/secret.json"]


@needs_git
def test_remove_skips_backup_when_ignored_files_unchanged(wt_repo, ee_home):
    from pathlib import Path

    from ee.core import worktree as W
    info = W.create(wt_repo, "same", home=ee_home)             # .env 从主目录原样复制过来，没改过
    r = W.remove(Path(info["path"]), home=ee_home)
    assert r["ignored_backup"] is None and r["backed_up"] == []
