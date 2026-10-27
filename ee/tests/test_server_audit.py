"""server 层审查：RPC 参数校验（会话 id 不能跳出 sessions 目录）、慢操作不卡事件循环、广播。"""
import asyncio
import json
import time

import pytest

from ee.server.app import Backend, RpcError


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("bad", ["..", "", ".", "../x", "a/b", "a\\b", "C:\\x", "/etc/passwd", "x" * 300, 123, None, ["a"]])
def test_thread_id_cannot_escape_sessions_dir(ee_home, bad):
    b = Backend(home=ee_home)
    (ee_home / "sessions").mkdir(exist_ok=True)
    keep = ee_home / "keep.txt"
    keep.write_text("x")
    (ee_home / "sessions" / "s1.jsonl").write_text("{}\n")

    async def go():
        for method, extra in [("thread/delete", {}), ("thread/get", {}), ("thread/rename", {"title": "t"}),
                              ("turn/start", {"text": "hi"}), ("shell/list", {})]:
            with pytest.raises(RpcError) as ei:
                await b.call(method, {"thread_id": bad, **extra})
            assert ei.value.code == -32602, (method, ei.value)
    run(go())
    assert keep.exists(), "非法 thread_id 删掉了 ee 主目录"
    assert (ee_home / "sessions" / "s1.jsonl").exists()


def test_valid_thread_id_still_works(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        assert (await b.call("thread/get", {"thread_id": th["id"]}))["thread"]["id"] == th["id"]
        assert (await b.call("thread/delete", {"thread_id": th["id"]}))["ok"] is True
    run(go())


def test_wrong_param_types_are_rpc_errors(ee_home, workspace):
    async def go():
        b = Backend(home=ee_home)
        for method, params in [("thread/start", {"cwd": 5}), ("workspace/open", {"path": None}),
                               ("files/search", {"cwd": ["x"]}), ("thread/list", {"limit": "many"}),
                               ("turn/start", {"thread_id": "20260101-000000-abcdef", "text": 5})]:
            with pytest.raises(RpcError) as ei:
                await b.call(method, params)
            assert ei.value.code in (-32602, -32000, 404), (method, ei.value.code, ei.value)
    run(go())


def test_thread_delete_does_not_block_event_loop(ee_home, workspace, monkeypatch):
    """删会话时删附件目录 / 搜索索引是磁盘操作：放到工作线程，事件循环照常响应。"""
    import shutil
    real = shutil.rmtree

    def slow_rmtree(*a, **kw):
        time.sleep(0.6)
        return real(*a, **kw)

    async def go():
        b = Backend(home=ee_home)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        (ee_home / "sessions" / th["id"]).mkdir()
        monkeypatch.setattr(shutil, "rmtree", slow_rmtree)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        tk = asyncio.create_task(ticker())
        await b.call("thread/delete", {"thread_id": th["id"]})
        tk.cancel()
        assert ticks >= 5, f"thread/delete 期间事件循环被卡住（只跑了 {ticks} 次）"
    run(go())


def test_files_search_runs_off_loop(ee_home, workspace, monkeypatch):
    from ee.core import fileindex
    real = fileindex.FileIndex.search

    def slow(self, *a, **kw):
        time.sleep(0.6)
        return real(self, *a, **kw)

    async def go():
        b = Backend(home=ee_home)
        (workspace / "a.txt").write_text("x")
        await b.call("files/search", {"cwd": str(workspace), "query": ""})   # 建好索引
        monkeypatch.setattr(fileindex.FileIndex, "search", slow)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        tk = asyncio.create_task(ticker())
        r = await b.call("files/search", {"cwd": str(workspace), "query": "a"})
        tk.cancel()
        assert any("a.txt" in str(f) for f in r["files"])
        assert ticks >= 5, f"files/search 期间事件循环被卡住（只跑了 {ticks} 次）"
    run(go())


def test_broadcast_from_worker_thread_reaches_clients_in_order(ee_home):
    """工作线程里发出的通知和事件循环里的通知都送达，且同一线程内顺序不乱。"""
    from ee.server.app import serve
    import websockets

    async def go():
        b = Backend(home=ee_home)
        port_f = asyncio.get_running_loop().create_future()
        srv = asyncio.create_task(serve(port=0, token="t0k", backend=b, ready=port_f.set_result))
        port = await port_f
        async with websockets.connect(f"ws://127.0.0.1:{port}/?token=t0k") as ws:
            await asyncio.sleep(0.05)

            def worker():
                for i in range(20):
                    b.notify("test/n", {"i": i})
            await asyncio.to_thread(worker)
            got = []
            while len(got) < 20:
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m.get("method") == "test/n":
                    got.append(m["params"]["i"])
            assert got == list(range(20))
        srv.cancel()
        try:
            await srv
        except (asyncio.CancelledError, Exception):
            pass
    run(go())
