"""会话生命周期：加载只建一份、删除后不复活、设置重载释放旧客户端。

三条都在现有代码里能指出具体位置：

1. `Backend._athread` 先查内存、`await to_thread(self._thread)`、再 setdefault。
   两个协程同时进来会各自读一次会话文件、各建一个 Thread/Session（后写入的覆盖先写入的），
   双方的 `ensure_session` 与持久化句柄就此分叉。
2. `thread_delete` 只是把 Thread 从 dict 里 pop 掉并删文件；正在跑的回合仍持有 session.store，
   下一条 `_add` 会把 jsonl 重新写出来 —— 会话"复活"成一条没有元数据的残记录。
3. `Thread.reload_config` 把 `session._client = None` 直接丢掉引用；
   而 `Session.set_model` 走的是 `_drop_client()`（登记进 `_closing`，由 aclose 释放）。
   保存设置走的正是 reload_config，于是旧 HTTP 连接池泄漏（Windows 上表现为偶发 10053）。
"""
import asyncio

import pytest

from ee.server.app import Backend


class TrackedClient:
    """记录是否被释放的假 client。"""

    def __init__(self, spec):
        self.spec = spec
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def stream(self, messages, tools, **kw):     # pragma: no cover - 不跑回合
        if False:
            yield None


# ---------------------------------------------------------------- 1) 并发加载只建一份


def test_concurrent_thread_load_creates_one_instance(ee_home, workspace, monkeypatch):
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        b.threads.clear()                       # 模拟内核重启后的冷缓存

        calls = []
        real = b._thread

        def counting(tid):
            calls.append(tid)
            return real(tid)

        monkeypatch.setattr(b, "_thread", counting)
        t1, t2 = await asyncio.gather(b._athread(th["id"]), b._athread(th["id"]))
        assert len(calls) == 1, f"同一会话被读了 {len(calls)} 次"
        assert t1 is t2

    asyncio.run(go())


def test_concurrent_load_returns_same_session(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        b.threads.clear()
        t1, t2 = await asyncio.gather(b._athread(th["id"]), b._athread(th["id"]))
        s1 = await t1.ensure_session_async()
        s2 = await t2.ensure_session_async()
        assert s1 is s2, "两个 Thread 实例各自持有一份 Session"

    asyncio.run(go())


# ---------------------------------------------------------------- 2) 删除后不复活


def test_deleted_thread_does_not_reappear_when_late_item_arrives(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        tid = th["id"]
        t = b.threads[tid]
        await t.ensure_session_async()
        path = ee_home / "sessions" / f"{tid}.jsonl"
        assert path.is_file()

        await b.call("thread/delete", {"thread_id": tid})
        assert not path.exists()

        # 运行中的回合收尾时还会写条目：不能让文件回来
        t._add({"type": "notice", "text": "迟到的条目"})
        t.save_meta()
        assert not path.exists(), "删除后的会话被迟到条目写回来了"

    asyncio.run(go())


def test_deleted_thread_stays_gone_in_listing(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        tid = th["id"]
        t = b.threads[tid]
        await t.ensure_session_async()
        await b.call("thread/delete", {"thread_id": tid})
        t._add({"type": "notice", "text": "迟到的条目"})
        listing = (await b.call("thread/list", {}))["threads"]
        assert tid not in [x["id"] for x in listing]

    asyncio.run(go())


# ---------------------------------------------------------------- 3) 设置重载释放旧客户端


def test_reload_config_releases_old_client(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home, persist=False, client_factory=TrackedClient)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        s = await t.ensure_session_async()
        first = s.client()                   # TrackedClient（工厂创建）
        assert not first.closed

        t.reload_config()                    # 等价于保存设置后的刷新
        await s.aclose()
        assert first.closed, "reload_config 丢掉了旧 client 的引用，连接池没释放"

    asyncio.run(go())


def test_reload_config_uses_same_path_as_set_model(ee_home, workspace):
    """两条入口应当走同一套替换逻辑（都登记待释放，而不是直接置空）。"""
    async def go():
        b = Backend(home=ee_home, persist=False, client_factory=TrackedClient)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        s = await t.ensure_session_async()
        s.client()
        before = list(getattr(s, "_closing", []))
        t.reload_config()
        after = list(getattr(s, "_closing", []))
        assert len(after) == len(before) + 1, "旧 client 没有登记到待释放列表"

    asyncio.run(go())


# ------------------------------------------------------------------ 孤儿 tool_calls

def _write_jsonl(path, recs):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")


def test_load_messages_fills_orphan_tool_calls(tmp_path):
    """assistant(tool_calls) 落盘后、tool 结果落盘前进程被杀 → 恢复时要补占位 tool 消息。

    服务端硬约束：带 tool_calls 的 assistant 必须跟齐每个 id 的 tool 消息，
    否则这条会话之后**每次**请求都 400，用户只能删会话重来。
    """
    from ee.core.store import SessionStore
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"kind": "message", "message": {"role": "user", "content": "hi"}},
        {"kind": "message", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]}},
        # 只有 call_a 的结果落盘了；call_b 丢了
        {"kind": "message", "message": {"role": "tool", "tool_call_id": "call_a", "content": "ok"}},
    ])
    out = SessionStore.load_messages(p)
    ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
    assert "call_a" in ids and "call_b" in ids, f"孤儿 tool_call 没补：{ids}"
    # 补齐的占位必须紧跟在 assistant 之后，且不能重复补已有的那条
    assert len(ids) == 2, f"补多了或重复了：{ids}"
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool"]


def test_load_messages_leaves_healthy_history_untouched(tmp_path):
    """配对完整的会话不能被改动（顺序、条数、内容都保持）。"""
    from ee.core.store import SessionStore
    p = tmp_path / "s.jsonl"
    recs = [
        {"kind": "message", "message": {"role": "user", "content": "hi"}},
        {"kind": "message", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}},
        {"kind": "message", "message": {"role": "tool", "tool_call_id": "call_a", "content": "ok"}},
        {"kind": "message", "message": {"role": "assistant", "content": "done"}},
    ]
    _write_jsonl(p, recs)
    out = SessionStore.load_messages(p)
    assert out == [r["message"] for r in recs]
