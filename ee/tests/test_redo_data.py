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


# ---------------------------------------------------------------- 4) 会话 JSONL 末尾半行
def _good_session(ee_home, sid="20260101-000000-abcdef"):
    import json
    d = ee_home / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    recs = [{"t": 1.0, "kind": "meta", "meta": {"cwd": str(ee_home), "title": "旧会话"}},
            {"t": 2.0, "kind": "message", "message": {"role": "user", "content": "你好"}},
            {"t": 2.1, "kind": "item", "item": {"id": "u1", "type": "user", "text": "你好", "seq": 1}},
            {"t": 3.0, "kind": "message", "message": {"role": "assistant", "content": "在的"}}]
    data = b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in recs)
    return d / f"{sid}.jsonl", data, sid


def test_half_written_last_line_is_skipped_and_logged(ee_home, caplog):
    import json
    import logging

    from ee.core.store import SessionStore
    path, data, _ = _good_session(ee_home)
    half = json.dumps({"t": 4.0, "kind": "message", "message": {"role": "user", "content": "崩溃时写到一半的中文"}},
                      ensure_ascii=False).encode("utf-8")
    path.write_bytes(data + half[:-9])                 # 截在一个中文字符的中间：既不是完整 JSON 也不是完整 UTF-8
    with caplog.at_level(logging.WARNING):
        d = SessionStore.load(path)
    assert [m["content"] for m in d["messages"]] == ["你好", "在的"]
    assert d["meta"]["title"] == "旧会话" and [it["id"] for it in d["items"]] == ["u1"]
    assert any("第 5 行" in r.getMessage() for r in caplog.records)
    assert SessionStore.load_messages(path)[-1]["content"] == "在的"
    assert SessionStore.summary(path)["title"] == "旧会话"


def test_crlf_bom_and_malformed_records_are_tolerated(ee_home):
    from ee.core.store import SessionStore
    path, data, _ = _good_session(ee_home)
    bad = b'12\n["x"]\n{"t": 5, "kind": "message"}\n{"t": 6, "kind": "item", "item": "oops"}\n'
    path.write_bytes(b"\xef\xbb\xbf" + data.replace(b"\n", b"\r\n") + bad)
    d = SessionStore.load(path)
    assert [m["content"] for m in d["messages"]] == ["你好", "在的"]
    assert d["meta"]["title"] == "旧会话"


def test_append_after_half_line_starts_on_new_line(ee_home):
    from ee.core.store import SessionStore
    path, data, sid = _good_session(ee_home)
    path.write_bytes(data + b'{"t": 4.0, "kind": "mess')
    SessionStore(ee_home, sid).message({"role": "user", "content": "重启后的新消息"})
    assert [m["content"] for m in SessionStore.load_messages(path)] == ["你好", "在的", "重启后的新消息"]


def test_backend_opens_session_with_half_line(ee_home):
    path, data, sid = _good_session(ee_home)
    path.write_bytes(data + "{\"t\": 4.0, \"kind\": \"item\", \"item\": {\"id\": \"a\", \"text\": \"半".encode("utf-8")[:-1])

    async def go():
        b, _, _ = make_backend(ee_home, [])
        d = (await b.call("thread/get", {"thread_id": sid}))["thread"]
        assert [it["id"] for it in d["items"]] == ["u1"]
        lst = await b.call("thread/list", {})
        assert any(t["id"] == sid for t in lst["threads"])
    asyncio.run(go())


# ---------------------------------------------------------------- 5) 配置 / 记忆原子写入
def test_atomic_write_retries_replace_on_windows_lock(tmp_path, monkeypatch):
    import os

    from ee.core import atomic
    d = tmp_path / "d"
    d.mkdir()
    f = d / "x.json"
    f.write_bytes(b"old")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(5, "拒绝访问。")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    atomic.write_bytes(f, b"new\r\n")
    assert f.read_bytes() == b"new\r\n" and len(calls) == 3
    assert [p.name for p in d.iterdir()] == ["x.json"]           # 临时文件没留下


def test_atomic_write_final_failure_keeps_old_and_cleans_tmp(tmp_path, monkeypatch):
    import os

    from ee.core import atomic
    d = tmp_path / "d"
    d.mkdir()
    f = d / "x.json"
    f.write_bytes(b"\xef\xbb\xbfold")
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")))
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        atomic.write_text(f, "new")
    assert f.read_bytes() == b"\xef\xbb\xbfold" and [p.name for p in d.iterdir()] == ["x.json"]


@pytest.fixture
def crash_on_write(monkeypatch):
    """模拟写到一半崩溃：直接写目标文件的老办法会留下半截文件。"""
    from pathlib import Path

    def half(self, data, encoding=None, errors=None, newline=None):
        with open(self, "w", encoding=encoding or "utf-8") as fh:
            fh.write(data[: len(data) // 2])
        raise OSError(28, "磁盘已满（模拟崩溃）")

    def half_bytes(self, data):
        with open(self, "wb") as fh:
            fh.write(data[: len(data) // 2])
        raise OSError(28, "磁盘已满（模拟崩溃）")
    monkeypatch.setattr(Path, "write_text", half)
    monkeypatch.setattr(Path, "write_bytes", half_bytes)


def test_config_write_setting_is_atomic(tmp_path, crash_on_write):
    from ee import config
    f = tmp_path / "config.toml"
    original = '# 我的配置\r\ndefault_model = "deepseek"\r\n\r\n[ui]\r\ntheme = "dark"\r\n'
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(original)
    try:
        config.write_setting("ui.theme", "light", f)
    except OSError:
        pass
    text = f.read_bytes().decode("utf-8")
    assert text == original or ('theme = "light"' in text and "# 我的配置" in text and 'default_model = "deepseek"' in text)


def test_config_write_setting_survives_crash_exactly(tmp_path, crash_on_write):
    from ee import config
    f = tmp_path / "config.toml"
    original = 'default_model = "deepseek"\n[ui]\ntheme = "dark"\n'
    f.open("w", encoding="utf-8").write(original)
    config.write_setting("ui.theme", "light", f)          # 原子写不经过 Path.write_text，不受模拟崩溃影响
    assert 'theme = "light"' in f.read_text(encoding="utf-8")


def test_save_user_file_and_permissions_retry_replace(tmp_path, ee_home, monkeypatch):
    import os

    from ee import config
    from ee.core import rules
    real, n = os.replace, {"c": 0}

    def flaky(a, b):
        n["c"] += 1
        if n["c"] % 2:
            raise PermissionError(5, "拒绝访问。")      # 每次第一下都失败（杀毒/索引正打开着目标文件）
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    f = tmp_path / "config.toml"
    config.save_user_file({"default_model": "x"}, f)
    assert 'default_model = "x"' in f.read_text(encoding="utf-8")
    rules._save({"version": 1, "projects": {"p": ["shell(ls)"]}}, ee_home)
    assert "shell(ls)" in (ee_home / "permissions.json").read_text(encoding="utf-8")


def test_memory_writes_are_atomic(tmp_path, crash_on_write):
    from ee.core import memory
    f = tmp_path / "memory.md"
    original = "# 记忆\n\n- 第一条\n- 第二条\n"
    f.open("w", encoding="utf-8").write(original)
    memory.add_entry(f, "第三条")
    memory.remove_entry(f, index=1)
    assert memory.parse_entries(f.read_text(encoding="utf-8")) == ["第二条", "第三条"]
    memory.save_text(f, "# 记忆\n\n- 全新\n")
    assert memory.parse_entries(f.read_text(encoding="utf-8")) == ["全新"]
