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
