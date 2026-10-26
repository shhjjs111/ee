"""thread/compact：回合结束后手动压缩；运行中拒绝；压缩后推送 thread/usage，并出现「已压缩」通知条目。"""
import asyncio

import pytest
from conftest import finish, text_chunks
from test_server import make_backend, wait_for

from ee.server.app import RpcError


def test_compact_after_turn(ee_home, workspace):
    script = [
        text_chunks("第一回合的回答，" * 20) + finish("stop", prompt=3000, completion=200),
        text_chunks("## 摘要\n用户问了问题，已回答。") + finish("stop", prompt=3200, completion=30),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "你好，帮我看看这个项目" * 10})
        await wait_for(ev, "turn/completed")
        n_usage = sum(1 for m, _ in ev if m == "thread/usage")
        r = await b.call("thread/compact", {"thread_id": tid})
        assert r["ok"] is True and r["tokens_before"] > 0 and "压缩" in r["message"]
        assert set(r) >= {"ok", "message", "tokens_before", "tokens_after"}
        usage = [p for m, p in ev if m == "thread/usage"]
        assert len(usage) > n_usage and usage[-1]["thread_id"] == tid and usage[-1]["context_left"] is not None
        notices = [p["item"] for m, p in ev if m == "item/started" and p["item"]["type"] == "notice"]
        assert any("已压缩" in n["text"] for n in notices)
        # 压缩请求复用了历史（第二次模型调用里有第一回合的内容）
        assert len(fake.calls) == 2
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert d["status"] == "idle" and "instructions" in d
    asyncio.run(go())


def test_compact_rejected_while_running(ee_home, workspace):
    script = [text_chunks("慢慢回答" * 30) + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script, delay=0.02)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "hi"})
        with pytest.raises(RpcError, match="运行中"):
            await b.call("thread/compact", {"thread_id": tid})
        await wait_for(ev, "turn/completed")
    asyncio.run(go())


def test_compact_empty_thread(ee_home, workspace):
    async def go():
        b, _, _ = make_backend(ee_home, [])
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        r = await b.call("thread/compact", {"thread_id": tid})
        assert r["ok"] is False and r["message"]
        with pytest.raises(RpcError):
            await b.call("thread/compact", {"thread_id": "nope"})
    asyncio.run(go())
