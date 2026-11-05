"""RPC 分发层的回归（2026-11 审计修复）。

test_export_rpc.py 直接调 backend.methods[...]（绕过分发器），所以「BLOCKING_RPCS 把
async 方法塞进 to_thread」这个坑一直没被发现：方法体从未执行，返回值是协程对象的字符串。
这份用例一律走真实入口 Backend.call。
"""
import asyncio
import inspect

import pytest

from ee.server.app import BLOCKING_RPCS, Backend


@pytest.fixture
def backend(tmp_path):
    return Backend(home=tmp_path, persist=False)


def _seed(home, tid, text="你好"):
    from ee.core.store import SessionStore

    s = SessionStore(home, tid)
    s.message({"role": "user", "content": text})
    s.message({"role": "assistant", "content": "收到"})
    s.item({"type": "user", "text": text, "id": "us_1", "seq": 1})
    s.meta({"title": "导出用例", "created": 1793205821.0, "cwd": str(home)})
    return tid


def test_blocking_rpcs_must_be_sync(backend):
    """放进 BLOCKING_RPCS 的必须是同步函数。

    to_thread(async_fn) 只调用一次得到协程对象就返回，函数体永远不执行；
    json.dumps(default=str) 再把它序列化成 "<coroutine object ...>"，调用方看到的是成功。
    """
    for name in BLOCKING_RPCS:
        fn = backend.methods[name]
        assert not inspect.iscoroutinefunction(fn), (
            f"{name} 是 async 函数，放不进 BLOCKING_RPCS：它自己应该 await asyncio.to_thread(...)")


def test_thread_export_through_dispatch_returns_real_content(backend):
    """导出必须经真实 RPC 入口返回真实文本（修复前拿到的是协程字符串）。"""
    tid = _seed(backend.home, "20261029-020202-cccccc")

    async def go():
        return await backend.call("thread/export", {"thread_id": tid, "format": "markdown"})

    res = asyncio.run(go())
    assert isinstance(res, dict), f"导出返回了非 dict：{res!r}"
    assert "你好" in res["content"]
    assert "coroutine" not in res["content"]


def test_thread_export_writes_file_through_dispatch(backend, tmp_path):
    """给了 path 时经分发也要真的落盘。"""
    tid = _seed(backend.home, "20261029-020203-dddddd")
    target = tmp_path / "out" / "会话.md"

    async def go():
        return await backend.call("thread/export", {
            "thread_id": tid, "format": "markdown", "path": str(target)})

    res = asyncio.run(go())
    assert res["path"] == str(target)
    assert target.is_file() and "你好" in target.read_text(encoding="utf-8")


def test_thread_delete_through_dispatch_removes_files(backend):
    """删除经分发要真的删掉会话文件（async 化之后不能变成「返回协程、什么都没做」）。"""
    tid = _seed(backend.home, "20261029-020204-eeeeee")

    async def go():
        return await backend.call("thread/delete", {"thread_id": tid})

    assert asyncio.run(go())["ok"] is True
    assert not (backend.home / "sessions" / f"{tid}.jsonl").exists()


def test_thread_delete_schedules_session_cleanup_on_the_loop(backend, monkeypatch):
    """删除时必须在**事件循环**上调度会话清理（aclose 释放连接池、bg.close 停后台命令）。

    修复前 thread/delete 在 BLOCKING_RPCS 里，整段跑在工作线程：
    asyncio.get_running_loop() 必然 RuntimeError，aclose 被 pass 吞掉，每删一个会话漏一个连接池。
    """
    tid = _seed(backend.home, "20261029-020205-ffffff")
    closed: list[str] = []

    class FakeSession:
        def __init__(self):
            self.bg = type("BG", (), {"close": self._bg_close})()

        async def _bg_close(self):
            closed.append("bg")

        async def aclose(self):
            closed.append("aclose")

    class FakeThread:
        meta: dict = {}
        deleted = False
        running = False
        session = FakeSession()

        def interrupt(self):  # pragma: no cover - 本用例不触发
            pass

    async def go():
        backend.threads[tid] = FakeThread()
        await backend.call("thread/delete", {"thread_id": tid})
        # 清理任务是 create_task 出去的，让它跑完
        for _ in range(10):
            await asyncio.sleep(0)
        return True

    assert asyncio.run(go())
    assert "aclose" in closed, "会话的 aclose 没有被调度（连接池泄漏）"
    assert "bg" in closed, "后台命令没有被关闭"
