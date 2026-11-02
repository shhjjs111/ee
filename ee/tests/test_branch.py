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
        # 重启后（D4-1 磁盘快照）：编辑第二条照样恢复文件——b.txt 是那一回合新建的，被删掉
        b3, _, ev3 = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        d3 = (await b3.call("thread/get", {"thread_id": tid}))["thread"]
        assert _users(d3["items"])[1].get("tree") and _users(d3["items"])[1].get("tree_after")
        r = await b3.call("thread/edit_resend", {"thread_id": tid, "item_id": _users(d3["items"])[1]["id"], "text": "算了"})
        assert r["files_reverted"] is True and not (workspace / "b.txt").exists()
        assert (workspace / "a.txt").read_text() == "v1"
        await wait_for(ev3, "turn/completed")
        assert not any(it["type"] == "notice" and "无法自动恢复" in it["text"] for it in b3.threads[tid].items)
    asyncio.run(go())


def test_edit_resend_after_restart_without_disk_snapshot(ee_home, workspace):
    """关掉磁盘快照（[snapshot] disk=false）时回退内存快照：重启后只截历史，文件不动并提示。"""
    (ee_home / "config.toml").write_text("[snapshot]\ndisk = false\n", encoding="utf-8")
    script = [_write("c1", "v1"), text_chunks("ok") + finish("stop"), text_chunks("好") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        b3, _, ev3 = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        d3 = (await b3.call("thread/get", {"thread_id": tid}))["thread"]
        assert "tree" not in _users(d3["items"])[0]
        r = await b3.call("thread/edit_resend", {"thread_id": tid, "item_id": _users(d3["items"])[0]["id"], "text": "算了"})
        assert r["files_reverted"] is False and (workspace / "a.txt").read_text() == "v1"
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


def test_fork_and_edit_resend_only_return_one_page(ee_home, workspace):
    """fork / edit_resend 也只回尾部一页，不能绕过分页把几 MB 一次塞给渲染进程。

    桌面端首屏（thread/get）已经分页了，但这两条路径一直 `detail()` 全量返回：
    长会话里编辑重发一条早期消息，回包仍是全部条目，刚做好的分页收益当场抵掉一半。
    """
    from ee.server.threads import PAGE_SIZE

    N = 35                       # 每回合 4 条左右，35 回合 > 一页（120）
    script = [text_chunks(f"答{i}") + finish("stop") for i in range(1, N + 2)]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        for i in range(1, N + 1):
            ev.clear()
            await b.call("turn/start", {"thread_id": tid, "text": f"问{i}"})
            await wait_for(ev, "turn/completed")
        full = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert full["items_total"] > PAGE_SIZE, "样本不够长，一页装得下就测不出分页"

        # fork：切到最后一条用户消息之前，那里的条目本来就超过一页（>=120），
        # 所以应当只回尾部一页 + has_more=True（而不是 136 条全部）
        u = _users(full["items"])[-1]
        nt = (await b.call("thread/fork", {"thread_id": tid, "item_id": u["id"]}))["thread"]
        assert len(nt["items"]) <= PAGE_SIZE, f"fork 回了 {len(nt['items'])} 条，没有分页"
        assert nt["items_total"] > PAGE_SIZE and nt["items_has_more"] is True
        # 回的是**尾部**一页：最后一条应当就是切点前最近的那条
        assert nt["items"][-1]["id"] == full["items"][next(
            i for i, x in enumerate(full["items"]) if x["id"] == u["id"]) - 1]["id"]

        # edit_resend：从第一条消息重发，只剩 1 回合的历史，但仍必须是「一页 + has_more 正确」
        first = _users(full["items"])[0]
        ev.clear()
        r = await b.call("thread/edit_resend", {"thread_id": tid, "item_id": first["id"], "text": "重来"})
        got = r["thread"]
        assert len(got["items"]) <= PAGE_SIZE and got["items_total"] == len(got["items"])
        assert got["items_has_more"] is False
        await wait_for(ev, "turn/completed")
    asyncio.run(go())
