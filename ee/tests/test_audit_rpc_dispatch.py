"""RPC 分发层的回归（2026-11 审计修复）。

test_export_rpc.py 直接调 backend.methods[...]（绕过分发器），所以「BLOCKING_RPCS 把
async 方法塞进 to_thread」这个坑一直没被发现：方法体从未执行，返回值是协程对象的字符串。
这份用例一律走真实入口 Backend.call。

那张表已经在 2026-11-12 拆掉（它最后是空集，to_thread 分支是死代码）：现在的不变量反过来——
**所有处理函数都必须是 async**，阻塞的活由处理函数自己 `await asyncio.to_thread(...)`。
"""
import asyncio
import inspect

import pytest

from ee.server.app import Backend


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


def test_all_handlers_are_coroutine_functions(backend):
    """所有 RPC 处理函数必须是 async。

    同步处理函数会被 `Backend.call` 直接在事件循环上执行：一次 git / 磁盘 IO 就卡住
    所有连接（通知、流式输出、其他会话的 RPC 全排它后面）。原先靠一张
    `BLOCKING_RPCS` 把整个方法丢进 to_thread 绕开，但那样粒度太大且不能碰事件循环
    （thread/delete 就是这么漏连接池的）。现在分工固定：处理函数 async，阻塞调用自己
    `await asyncio.to_thread(...)`。加处理函数时忘了写 async，这条会直接报出来。
    """
    sync = sorted(name for name, fn in backend.methods.items() if not inspect.iscoroutinefunction(fn))
    assert not sync, (
        f"这些 RPC 处理函数不是 async，会阻塞事件循环：{sync}。"
        "请写成 async def，并把阻塞的调用包成 await asyncio.to_thread(...)")
    assert len(backend.methods) > 50, "方法表好像没装配完，这条断言就失去意义了"


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
    （那张表已拆掉，用例保留——它是“别把整方法丢进 to_thread”的反例。）
    """
    tid = _seed(backend.home, "20261029-020205-ffffff")
    closed: list[str] = []

    class FakeSession:
        def __init__(self):
            self.bg = type("BG", (), {"close": self._bg_close})()

        def mark_deleted(self):
            closed.append("mark_deleted")

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


def test_internal_typeerror_is_not_masked_as_bad_params(backend):
    """handler 内部抛的 TypeError 不能被伪装成「参数错误」。

    Backend.call 原先统一 `except TypeError` → RpcError(-32602)：真实的代码 bug
    会被报成用户输入问题，日志里也只剩一条没有栈的 warning。现在用
    inspect.signature().bind() 区分：签名绑不上才是参数错误，其余原样抛出。
    """
    from ee.server.app import RpcError

    async def boom(x: int = 0) -> dict:
        from typing import Any
        value: Any = None                    # 运行时的 TypeError；标 Any 以免静态检查先把它拦下
        return {"n": len(value)}             # TypeError: object of type 'NoneType' has no len()

    async def fine(x: int = 0) -> dict:
        return {"x": x}

    backend.methods["test/boom"] = boom
    backend.methods["test/fine"] = fine

    async def go():
        with pytest.raises(TypeError):
            await backend.call("test/boom", {})
        with pytest.raises(RpcError) as e:
            await backend.call("test/fine", {"y": 1})
        assert e.value.code == -32602 and "参数错误" in str(e.value)
        assert (await backend.call("test/fine", {"x": 2})) == {"x": 2}

    asyncio.run(go())
