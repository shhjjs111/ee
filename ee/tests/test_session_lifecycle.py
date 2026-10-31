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


def test_approval_without_answer_times_out(ee_home, workspace, monkeypatch):
    """审批一直没人回答时，回合必须超时结束，不能永远占着 _history_lock。

    无人值守（关掉窗口 / 自动运行 / 前端断线）时，ApprovalRequest 的 future
    永远等不到结果：_turn 一直挂着、_history_lock 一直被持有，/compact 和
    下一回合全部堵在锁后，会话变成"永远 running"（报告 P1-5）。
    """
    import asyncio as _a
    from conftest import FakeClient, finish, text_chunks, tool_chunks

    from ee.config import load_config

    from ee.core import session as S

    # 把超时压到很短，测试才跑得动
    monkeypatch.setattr(S, "APPROVAL_TIMEOUT", 0.3)
    # 工作区外的写入会落到 ask 分支，才会产生 ApprovalRequest
    outside = str(workspace.parent / "outside.txt")
    fake = FakeClient([
        tool_chunks(0, "c1", "write", {"path": outside, "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    s = S.Session(load_config(workspace), model="deepseek-flash", cwd=workspace,
                  client=fake, persist=False)

    async def go():
        evs = []
        # 不回应任何 ApprovalRequest
        async for ev in s.run_turn("写个文件"):
            evs.append(ev)
        return evs

    evs = _a.run(go())
    assert any(type(e).__name__ == "ApprovalRequest" for e in evs), "没走到审批分支，测试无效"
    reasons = [e.reason for e in evs if type(e).__name__ == "TurnDone"]
    assert reasons, "没有 TurnDone"
    # 关键：回合必须结束（不挂死），且锁已释放
    assert not s._history_lock.locked(), "回合结束后 _history_lock 仍被持有"


def test_aclose_releases_lsp_manager(ee_home, workspace):
    """会话关闭时必须释放 LSP 管理器，否则每个会话泄漏一个语言服务器进程。

    session 里 new LspManager() + acquire()，但 release/shutdown 全仓无调用
    （报告 P1-11）。会话删除 / 服务退出后子进程一直挂着。
    """
    from conftest import FakeClient, finish, make_session, text_chunks

    calls = []

    class FakeMgr:
        async def release(self, handle):
            calls.append(("release", handle))

        async def shutdown(self):
            calls.append(("shutdown", None))

    s, _ = make_session(workspace, [text_chunks("好") + finish("stop")])
    s.lsp_manager = FakeMgr()
    s._lsp_handle = object()
    asyncio.run(s.aclose())
    kinds = [c[0] for c in calls]
    assert "release" in kinds and "shutdown" in kinds, f"LSP 没被释放：{kinds}"
    # 重复调用安全
    asyncio.run(s.aclose())
    assert len(calls) == 2, f"重复 aclose 又调了一次：{calls}"


def test_mcp_failure_is_retried_on_next_turn(ee_home, workspace, monkeypatch):
    """MCP 首次连接失败后，下一次回合要能重试，而不是整会话永久没有 MCP 工具。

    _attach_mcp 在真正连接之前就置 _mcp_done=True，任何失败（服务器还没起来、
    配置临时读失败、连接超时）都会让这个会话再也连不上 MCP（报告 P1-12）。
    """
    import ee.mcp.manager as MM
    from conftest import finish, make_session, text_chunks

    attempts = []

    class FakeMgr:
        async def connect_all(self, data, cwd):
            attempts.append(1)
            raise RuntimeError("服务器还没起来")

    monkeypatch.setattr(MM, "get_manager", lambda: FakeMgr())
    s, _ = make_session(workspace, [text_chunks("一") + finish("stop")])
    s.mcp_enabled = True
    s.config.data["mcp"] = {"enabled": True, "project_json": False,
                            "servers": {"demo": {"command": "node", "args": ["x.js"]}}}

    async def go():
        for _ in range(2):
            try:
                await s._attach_mcp(lambda *a, **k: None)
            except Exception:  # noqa: BLE001
                pass

    asyncio.run(go())
    assert len(attempts) >= 2, f"失败后没有重试，只尝试了 {len(attempts)} 次"
