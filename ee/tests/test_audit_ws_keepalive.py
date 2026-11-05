"""半开 WebSocket 连接永不回收（2026-11 审计 P1#6）。

`ws_serve` 原来显式 `ping_interval=None`，两端都没有替代探测：服务端从不发 ping，
handler 的 `async for raw in ws` 也没有超时，只有收到 FIN/RST 才会退出。RDP 断开、
休眠唤醒、渲染进程被冻结留下的半开 TCP 会让 handler 永久阻塞——`conns[ws]` 与它的
writer 任务只增不减，此后每条广播还继续往死连接的队列里塞。

第二条用例用原始 socket 完成握手后「装死」：既不读也不写，因此不会回 pong，等价于
半开。判据直接看线上发生了什么——服务端必须先发出 ping 帧（opcode 0x9），并在等不到
pong 之后关掉连接（读到 EOF）。把 ping_interval 设回 None 时，第一步就拿不到任何帧。
"""
import asyncio
import base64
import os

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from ee.server import app


def _opts() -> dict:
    """用被测代码自己的保活参数起测试服务器。"""
    return {"ping_interval": app.KEEPALIVE_PING, "ping_timeout": app.KEEPALIVE_TIMEOUT}


async def _handshake(port: int):
    """手写 WebSocket 握手，返回 (reader, writer)。之后调用方可以「装死」。"""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    w.write((
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    assert b"101" in head.split(b"\r\n")[0], head
    return r, w


def test_normal_client_survives_keepalive():
    """客户端库会在协议层自动回 pong——恢复 ping 不能把正常连接误杀。"""
    async def go():
        async def echo(ws):
            async for m in ws:
                await ws.send(m)

        async with serve(echo, "127.0.0.1", 0, **_opts()) as srv:
            port = next(iter(srv.sockets)).getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", ping_interval=None) as c:
                await c.send("hi")
                assert await asyncio.wait_for(c.recv(), 5) == "hi"

    asyncio.run(go())


def test_half_open_connection_is_pinged_then_dropped(monkeypatch):
    """服务端必须主动 ping；等不到 pong 时必须关掉这条连接。"""
    monkeypatch.setattr(app, "KEEPALIVE_PING", 0.2)
    monkeypatch.setattr(app, "KEEPALIVE_TIMEOUT", 0.4)

    async def go():
        async def handler(ws):
            try:
                async for _ in ws:
                    pass
            except Exception:          # 被判死时库会抛 ConnectionClosedError，这是预期路径
                pass

        async with serve(handler, "127.0.0.1", 0, **_opts()) as srv:
            port = next(iter(srv.sockets)).getsockname()[1]
            r, w = await _handshake(port)
            try:
                # 装死：不读不写 = 不回 pong。第一件事必须看到 ping 帧（opcode 0x9）。
                first = await asyncio.wait_for(r.read(4096), 5)
                assert first, "连接被直接关掉，没发过 ping"
                assert (first[0] & 0x0F) == 0x9, f"第一帧应当是 ping，实际 {first[:8]!r}"

                # 之后服务端应当判死并关闭：读到 EOF。
                eof = False
                for _ in range(12):
                    chunk = await asyncio.wait_for(r.read(4096), 5)
                    if not chunk:
                        eof = True
                        break
                assert eof, "服务端没有关闭这条半开连接——它会被永久留在 conns 里"
            finally:
                w.close()

    asyncio.run(go())

def test_production_keepalive_is_enabled():
    """生产参数本身不能是 None —— 那正是原来的缺陷（ping_interval=None）。

    上面两条用 monkeypatch 改小间隔来跑得快，所以它们守不住「源码里的默认值」。
    这条专门守它：有人把 KEEPALIVE_PING 改回 None，这里立刻红。
    """
    assert app.KEEPALIVE_PING is not None and app.KEEPALIVE_PING > 0
    assert app.KEEPALIVE_TIMEOUT is not None and app.KEEPALIVE_TIMEOUT > 0
