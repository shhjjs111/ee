"""D3-E：分叉 / 编辑重发。"""
import asyncio

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.server.app import RpcError


def _write(cid, content, path="a.txt"):
    return tool_chunks(0, cid, "write", {"path": path, "content": content}) + finish("tool_calls")


def _users(items):
    return [it for it in items if it["type"] == "user"]


def test_edit_resend_reverts_files_and_history(ee_home, workspace):
    script = [
        _write("c1", "v1"), text_chunks("写好 v1") + finish("stop"),
        _write("c2", "v2"), text_chunks("改成 v2") + finish("stop"),
        _write("c3", "v3", "b.txt"), text_chunks("改成 v3") + finish("stop"),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        for text in ("写 a.txt 内容 v1", "把 a.txt 改成 v2"):
            ev.clear()
            await b.call("turn/start", {"thread_id": tid, "text": text})
            await wait_for(ev, "turn/completed")
        assert (workspace / "a.txt").read_text() == "v2"
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u1, u2 = _users(d["items"])
        assert u1["hist"] == 0 and isinstance(u2["hist"], int) and u2["hist"] > 0

        ev.clear()
        r = await b.call("thread/edit_resend", {"thread_id": tid, "item_id": u2["id"], "text": "把 a.txt 改成 v3"})
        assert r["files_reverted"] is True and any(p.endswith("a.txt") for p in r["restored"])
        await wait_for(ev, "turn/completed")
        # a.txt 恢复到 v2 回合之前；重发的回合写了 b.txt
        assert (workspace / "a.txt").read_text() == "v1" and (workspace / "b.txt").read_text() == "v3"
        assert any(m == "item/removed" and p["item_id"] == u2["id"] for m, p in ev)
        # 第三次请求（重发后的第一步）里没有被丢掉的 v2 回合
        msgs = fake.calls[4]["messages"]
        joined = "\n".join(str(m.get("content")) for m in msgs)
        assert "改成 v2" not in joined and "把 a.txt 改成 v3" in joined and "写 a.txt 内容 v1" in joined
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert [u["text"] for u in _users(d["items"])] == ["写 a.txt 内容 v1", "把 a.txt 改成 v3"]

        # 重启后从磁盘恢复：被截掉的条目不再出现，历史也对
        b2, _, _ = make_backend(ee_home, [])
        d2 = (await b2.call("thread/get", {"thread_id": tid}))["thread"]
        assert [u["text"] for u in _users(d2["items"])] == ["写 a.txt 内容 v1", "把 a.txt 改成 v3"]
        assert [it["id"] for it in d2["items"]] == [it["id"] for it in d["items"]]
        s2 = b2.threads[tid].session
        assert "改成 v2" not in str(s2.history) and s2.turn_starts == [u["hist"] for u in _users(d2["items"])]
        # 重启后快照没了：编辑第二条只截历史，文件不动并提示
        b3, _, ev3 = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        d3 = (await b3.call("thread/get", {"thread_id": tid}))["thread"]
        r = await b3.call("thread/edit_resend", {"thread_id": tid, "item_id": _users(d3["items"])[1]["id"], "text": "算了"})
        assert r["files_reverted"] is False and (workspace / "b.txt").read_text() == "v3"
        await wait_for(ev3, "turn/completed")
        assert any(it["type"] == "notice" and "无法自动恢复" in it["text"] for it in b3.threads[tid].items)
    asyncio.run(go())


def test_fork(ee_home, workspace):
    script = [text_chunks("你好") + finish("stop"), text_chunks("第二条回复") + finish("stop"),
              text_chunks("分叉里的回复") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        for text in ("第一条", "第二条"):
            ev.clear()
            await b.call("turn/start", {"thread_id": tid, "text": text})
            await wait_for(ev, "turn/completed")
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u1, u2 = _users(d["items"])
        r = await b.call("thread/fork", {"thread_id": tid, "item_id": u2["id"]})
        nt = r["thread"]
        assert r["draft"] == "第二条" and nt["id"] != tid and nt["title"].startswith("分叉：")
        assert [u["text"] for u in _users(nt["items"])] == ["第一条"]
        assert all(it["type"] != "turn_end" or it["seq"] < u2["seq"] for it in nt["items"])
        # 原会话不变
        d_again = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert len(d_again["items"]) == len(d["items"])
        # 分叉会话可以继续聊，历史只有第一回合
        ev.clear()
        await b.call("turn/start", {"thread_id": nt["id"], "text": "换个问法"})
        await wait_for(ev, "turn/completed", lambda p: p["thread_id"] == nt["id"])
        joined = "\n".join(str(m.get("content")) for m in fake.calls[-1]["messages"])
        assert "第一条" in joined and "第二条" not in joined and "换个问法" in joined
        # 重启后分叉会话可以读出来，meta 带 parent_id
        b2, _, _ = make_backend(ee_home, [])
        lst = {t["id"]: t for t in (await b2.call("thread/list", {}))["threads"]}
        assert nt["id"] in lst
        assert b2._thread(nt["id"]).meta.get("parent_id") == tid
        # 删除会话
        await b2.call("thread/delete", {"thread_id": nt["id"]})
    asyncio.run(go())


def test_branch_errors(ee_home, workspace):
    async def go():
        b, fake, ev = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "嗨"})
        await wait_for(ev, "turn/completed")
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        asst = next(it for it in d["items"] if it["type"] == "assistant")
        with pytest.raises(RpcError, match="找不到"):
            await b.call("thread/fork", {"thread_id": tid, "item_id": asst["id"]})
        u = _users(d["items"])[0]
        with pytest.raises(RpcError, match="不能为空"):
            await b.call("thread/edit_resend", {"thread_id": tid, "item_id": u["id"], "text": "  "})
        # 历史被压缩/改写后核对不上原文
        b.threads[tid].session.history[0]["content"] = "别的内容"
        with pytest.raises(RpcError, match="压缩"):
            await b.call("thread/fork", {"thread_id": tid, "item_id": u["id"]})
    asyncio.run(go())
