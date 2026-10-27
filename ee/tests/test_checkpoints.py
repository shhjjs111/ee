"""D5-7：检查点时间线、非 git 项目审阅、按块接受/拒绝、影子仓库清理。"""
import asyncio
import shutil
import subprocess
import time

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core import checkpoints, snapshot
from ee.server import gitops
from ee.server.app import RpcError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(workspace):
    git(workspace, "init", "-q")
    (workspace / "中文.txt").write_text("一\n二\n三\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "init")
    return workspace


def test_git_review_chinese_path_not_quoted(repo):
    (repo / "中文.txt").write_text("一\n二改\n三\n", encoding="utf-8")
    r = gitops.review(repo)
    assert [f["path"] for f in r["files"]] == ["中文.txt"]
    assert gitops.revert_file(repo, "中文.txt")["action"] == "restored"
    assert (repo / "中文.txt").read_text(encoding="utf-8") == "一\n二\n三\n"


def _script_writes(*writes):
    out = []
    for i, (path, content) in enumerate(writes):
        out += [tool_chunks(0, f"c{i}", "write", {"path": path, "content": content}) + finish("tool_calls"),
                text_chunks(f"ok{i}") + finish("stop")]
    return out


async def _run_turns(b, ev, tid, texts):
    for text in texts:
        ev.clear()
        await b.call("turn/start", {"thread_id": tid, "text": text})
        await wait_for(ev, "turn/completed")


def test_timeline_list_diff_restore_and_undo_restore(ee_home, workspace):
    (workspace / "keep.txt").write_text("原样", encoding="utf-8")

    async def go():
        b, _, ev = make_backend(ee_home, _script_writes(("a.txt", "v1\n"), ("a.txt", "v2\n"), ("b.txt", "新文件\n")))
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await _run_turns(b, ev, tid, ["写 a 第一版", "改 a", "写 b"])
        r = await b.call("checkpoint/list", {"thread_id": tid})
        assert r["available"]
        cps = r["checkpoints"]
        assert [c["turn"] for c in cps] == [1, 2, 3] and cps[0]["text"] == "写 a 第一版"
        assert cps[1]["stat"] == {"files": 1, "additions": 1, "deletions": 1}
        assert not any(c["expired"] or c["running"] or c["undone"] for c in cps)

        d = await b.call("checkpoint/diff", {"thread_id": tid, "checkpoint_id": cps[1]["id"]})
        assert [f["path"] for f in d["files"]] == ["a.txt"] and "+v2" in d["files"][0]["patch"]
        # 恢复会改什么：当前 → 第 2 回合之前
        d = await b.call("checkpoint/diff", {"thread_id": tid, "checkpoint_id": cps[1]["id"], "mode": "before"})
        assert sorted(f["path"] for f in d["files"]) == ["a.txt", "b.txt"]

        r = await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": cps[1]["id"]})
        assert sorted(r["restored"]) == ["a.txt", "b.txt"] and r["restore_point"]["kind"] == "restore"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1\n" and not (workspace / "b.txt").exists()
        assert (workspace / "keep.txt").read_text(encoding="utf-8") == "原样"
        cps2 = (await b.call("checkpoint/list", {"thread_id": tid}))["checkpoints"]
        assert [c["undone"] for c in cps2 if c["kind"] == "turn"] == [False, True, True]
        rp = [c for c in cps2 if c["kind"] == "restore"]
        assert len(rp) == 1 and "第 2 回合之前" in rp[0]["text"]
        # /undo 接着按新状态走：撤销的是第 1 回合
        u = await b.call("thread/undo", {"thread_id": tid})
        assert any(p.endswith("a.txt") for p in u["restored"]) and not (workspace / "a.txt").exists()

        # 反悔：恢复到恢复点 → 回到恢复之前的样子，撤销标记也回去
        await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": rp[0]["id"]})
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v2\n"
        assert (workspace / "b.txt").read_text(encoding="utf-8") == "新文件\n"
        cps3 = (await b.call("checkpoint/list", {"thread_id": tid}))["checkpoints"]
        assert [c["undone"] for c in cps3 if c["kind"] == "turn"] == [False, False, False]

        # 恢复到第 1 回合之后
        await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": cps[0]["id"], "side": "after"})
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1\n" and not (workspace / "b.txt").exists()
        cps4 = (await b.call("checkpoint/list", {"thread_id": tid}))["checkpoints"]
        assert [c["undone"] for c in cps4 if c["kind"] == "turn"] == [False, True, True]
        # 没有变化时不建恢复点
        r = await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": cps[0]["id"], "side": "after"})
        assert r["restored"] == [] and r["restore_point"] is None

        # 重启后照样能列、能恢复（tree 持久化在条目和 meta 里）
        b2, _, _ = make_backend(ee_home, [])
        cps5 = (await b2.call("checkpoint/list", {"thread_id": tid}))["checkpoints"]
        assert len([c for c in cps5 if c["kind"] == "restore"]) == 3      # 恢复到恢复点也会再存一个
        with pytest.raises(RpcError):
            await b2.call("checkpoint/diff", {"thread_id": tid, "from_tree": "0" * 40})
        with pytest.raises(RpcError):
            await b2.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": "nope"})
    asyncio.run(go())


def test_non_git_review_hunks_and_revert(ee_home, workspace):
    body = "".join(f"第 {i} 行\r\n" for i in range(1, 31))
    (workspace / "doc.txt").write_bytes(body.encode())
    new = body.replace("第 3 行\r\n", "第 3 行（改）\r\n").replace("第 25 行\r\n", "第 25 行\r\n加一行\r\n")

    async def go():
        b, _, ev = make_backend(ee_home, _script_writes(("other.txt", "x\n")))
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        r = await b.call("git/diff", {"cwd": str(workspace), "thread_id": tid})
        assert not r["is_repo"] and r["snapshot"]["empty"]
        await _run_turns(b, ev, tid, ["写 other"])
        (workspace / "doc.txt").write_bytes(new.encode())          # 会话开始之后改的（谁改的都算）
        (workspace / "mine.txt").write_text("用户新建\n", encoding="utf-8")      # 会话开始后用户自己加的也算改动
        r = await b.call("git/diff", {"cwd": str(workspace), "thread_id": tid})
        assert r["snapshot"]["available"] and r["snapshot"]["since"]
        files = {f["path"]: f for f in r["files"]}
        assert set(files) == {"doc.txt", "mine.txt", "other.txt"}
        hs = files["doc.txt"]["hunks"]
        assert len(hs) == 2 and not any(h["accepted"] for h in hs)

        await b.call("review/hunk", {"cwd": str(workspace), "thread_id": tid, "path": "doc.txt",
                                     "hunk_id": hs[1]["id"], "action": "accept"})
        await b.call("review/hunk", {"cwd": str(workspace), "thread_id": tid, "path": "doc.txt",
                                     "hunk_id": hs[0]["id"], "action": "reject"})
        data = (workspace / "doc.txt").read_bytes().decode()
        assert "第 3 行\r\n" in data and "（改）" not in data and "加一行\r\n" in data      # CRLF 保留
        r = await b.call("git/diff", {"cwd": str(workspace), "thread_id": tid})
        doc = next(f for f in r["files"] if f["path"] == "doc.txt")
        assert [h["accepted"] for h in doc["hunks"]] == [True]
        with pytest.raises(RpcError):                    # 已经拒绝掉的块再拒绝：对不上
            await b.call("review/hunk", {"cwd": str(workspace), "thread_id": tid, "path": "doc.txt",
                                         "hunk_id": hs[0]["id"], "action": "reject"})
        # 整个文件撤销：恢复成会话开始时的版本 / 新文件删除
        await b.call("git/revert_file", {"cwd": str(workspace), "path": "doc.txt", "thread_id": tid})
        assert (workspace / "doc.txt").read_bytes() == body.encode()
        res = await b.call("git/revert_file", {"cwd": str(workspace), "path": "mine.txt", "thread_id": tid})
        assert res["action"] == "deleted" and not (workspace / "mine.txt").exists()
        await b.call("git/revert_file", {"cwd": str(workspace), "path": "other.txt", "thread_id": tid})
        r = await b.call("git/diff", {"cwd": str(workspace), "thread_id": tid})
        assert r["files"] == []
    asyncio.run(go())


def test_git_hunk_reject_accept(ee_home, repo):
    body = "".join(f"line {i}\r\n" for i in range(1, 31)).encode()
    (repo / "w.txt").write_bytes(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "w")
    (repo / "w.txt").write_bytes(body.replace(b"line 2\r\n", b"line two\r\n").replace(b"line 28\r\n", b""))
    (repo / "新建.txt").write_text("全新\n", encoding="utf-8")

    async def go():
        b, _, _ = make_backend(ee_home, [])
        r = await b.call("git/diff", {"cwd": str(repo)})
        files = {f["path"]: f for f in r["files"]}
        assert set(files) == {"w.txt", "新建.txt"}
        w = files["w.txt"]["hunks"]
        assert len(w) == 2
        await b.call("review/hunk", {"cwd": str(repo), "path": "w.txt", "hunk_id": w[1]["id"], "action": "reject"})
        assert (repo / "w.txt").read_bytes() == body.replace(b"line 2\r\n", b"line two\r\n")
        await b.call("review/hunk", {"cwd": str(repo), "path": "w.txt", "hunk_id": w[0]["id"], "action": "accept"})
        r = await b.call("git/diff", {"cwd": str(repo)})
        assert [h["accepted"] for f in r["files"] if f["path"] == "w.txt" for h in f["hunks"]] == [True]
        await b.call("review/hunk", {"cwd": str(repo), "path": "w.txt", "hunk_id": w[0]["id"], "action": "unaccept"})
        r = await b.call("git/diff", {"cwd": str(repo)})
        assert [h["accepted"] for f in r["files"] if f["path"] == "w.txt" for h in f["hunks"]] == [False]
        # 新的未跟踪文件只有一块：拒绝 = 删掉文件
        n = next(f for f in r["files"] if f["path"] == "新建.txt")["hunks"]
        res = await b.call("review/hunk", {"cwd": str(repo), "path": "新建.txt", "hunk_id": n[0]["id"], "action": "reject"})
        assert res["action"] == "deleted" and not (repo / "新建.txt").exists()
        with pytest.raises(RpcError):
            await b.call("review/hunk", {"cwd": str(repo), "path": "../x", "hunk_id": "x", "action": "reject"})
    asyncio.run(go())


def test_gc_keeps_recent_and_drops_old(ee_home, workspace, tmp_path):
    async def go():
        b, _, ev = make_backend(ee_home, _script_writes(("a.txt", "v1\n"), ("a.txt", "v2\n"), ("a.txt", "v3\n")))
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await _run_turns(b, ev, tid, ["一", "二", "三"])
        return tid
    tid = asyncio.run(go())
    ds = snapshot.for_dir(workspace, ee_home)
    from ee.core.store import SessionStore
    d = SessionStore.load(ee_home / "sessions" / f"{tid}.jsonl")
    users = [it for it in d["items"] if it.get("type") == "user"]
    old_tree = users[1]["tree"]          # 第 1 回合的是空 tree（git 内置，永远在）
    # 把前两回合改成 30 天前；keep_turns=1 → 只留最后一回合
    st = SessionStore(ee_home, session_id=tid)
    for it in users[:2]:
        st.item({**it, "ts": time.time() - 30 * 86400})
    # 一个工作区已经不存在的孤儿影子仓库
    gone = tmp_path / "gone"
    gone.mkdir()
    orphan = snapshot.DiskSnapshot(gone, ee_home)
    (gone / "f").write_text("x")
    assert orphan.capture()
    shutil.rmtree(gone)
    # 一小时宽限期：把对象时间改旧，模拟很久以前写的
    import os
    old = time.time() - 7200
    for p in ds.gitdir.joinpath("objects").rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    r = checkpoints.gc_all(ee_home, keep_days=14, keep_turns=1, force=True)
    assert r["removed_repos"] == 1 and not orphan.gitdir.exists()
    alive = ds.existing([it["tree"] for it in users] + [users[-1]["tree_after"]])
    assert users[-1]["tree"] in alive and users[-1]["tree_after"] in alive
    assert old_tree not in alive
    # 一天内不重复跑
    assert checkpoints.gc_all(ee_home, 14, 1)["skipped"]

    async def check():
        b, _, _ = make_backend(ee_home, [])
        cps = (await b.call("checkpoint/list", {"thread_id": tid}))["checkpoints"]
        assert [c["expired"] for c in cps] == [False, True, False]      # 第 1 回合前是空 tree，永远在
        assert [c["after_expired"] for c in cps] == [True, False, False]   # 第 2 回合后 = 第 3 回合前，保留
        with pytest.raises(RpcError):
            await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": cps[1]["id"]})
        with pytest.raises(RpcError):
            await b.call("checkpoint/restore", {"thread_id": tid, "checkpoint_id": cps[0]["id"], "side": "after"})
    asyncio.run(check())


def test_keep_settings_defaults_and_bad_values():
    assert checkpoints.keep_settings({}) == (checkpoints.DEFAULT_KEEP_DAYS, checkpoints.DEFAULT_KEEP_TURNS)
    assert checkpoints.keep_settings({"snapshot": {"keep_days": "x", "keep_turns": 0}}) == (
        checkpoints.DEFAULT_KEEP_DAYS, 1)
