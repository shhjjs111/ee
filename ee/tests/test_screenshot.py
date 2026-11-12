"""桌面端截图：后端 → 界面 → 后端 的回路。

覆盖三块：`core/screen.py` 的等待表与入参校验、`screenshot` 工具落盘与登记、
`ui/capture_result` RPC 真能把图交回等着的那次请求（含超时后迟到的回包）。
"""
from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from pathlib import Path

import pytest

from test_server import make_backend

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6260000000000300010000050000"
    "000000000049454e44ae426082"
)


def _png_data_url(data: bytes = PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _png(w: int, h: int) -> bytes:
    """造一张 w×h 的合法 PNG（只要 IHDR 与尺寸正确；内容全白）。"""
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body +
                struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_decode_data_url_rejects_non_png_and_bad_base64():
    from ee.core.screen import decode_data_url

    assert decode_data_url(_png_data_url()) == PNG
    for bad in ("data:image/jpeg;base64,AAAA", "https://x/y.png", "data:image/png;base64,@@@@"):
        with pytest.raises(ValueError):
            decode_data_url(bad)


def test_png_size_reads_ihdr():
    from ee.core.screen import png_size

    assert png_size(_png(7, 5)) == (7, 5)
    assert png_size(b"not a png") == (0, 0)


def test_hub_request_times_out():
    from ee.core.screen import CaptureUnavailable, ScreenHub

    async def main():
        hub = ScreenHub()
        events = []
        with pytest.raises(CaptureUnavailable) as e:
            await hub.request(events.append, timeout=0.05)
        assert "没有送回截图" in str(e.value)
        assert hub.pending == {}, "超时后要清掉等待项"
        assert events and events[0].request_id.startswith("shot_")

    asyncio.run(main())


def test_hub_late_response_is_dropped():
    from ee.core.screen import ScreenHub

    async def main():
        hub = ScreenHub()
        fut = asyncio.get_running_loop().create_future()
        hub.pending["shot_x"] = fut
        assert hub.resolve("shot_x", PNG) is True and fut.result() == PNG
        assert hub.resolve("shot_x", PNG) is False, "已经完成的不该再接受第二次"
        assert hub.resolve("shot_missing", PNG) is False, "不存在的请求静默丢弃"

    asyncio.run(main())


def _session_stub(workspace):
    """真实 Session（不跑回合），只用来给工具和 hub 提供落脚点。"""
    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(workspace)
    return Session(cfg, cwd=workspace, persist=False, agent="lean")


def test_tool_saves_png_and_registers_for_next_step(workspace, ee_home):
    from ee.core.screen import ScreenHub
    from ee.tools.base import ToolContext, ToolState
    from ee.tools.screenshot import ScreenshotTool

    s = _session_stub(workspace)
    s.screen = ScreenHub(ee_home / "screenshots")
    events = []
    ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / "out",
                      extra={"session": s, "emit_event": events.append}, emit=lambda ev: None)

    async def respond():
        await asyncio.sleep(0.01)
        rid, fut = next(iter(s.screen.pending.items()))
        s.screen.resolve(rid, _png(640, 360))

    async def main():
        task = asyncio.create_task(respond())
        res = await ScreenshotTool().run({}, ctx)
        await task
        return res

    res = asyncio.run(main())
    path = next(iter(s.screen.root.iterdir()))
    assert path.read_bytes() == _png(640, 360)
    assert str(path) in res.output and "640×360" in res.output
    snap = ctx.state.pending_images[0]
    assert Path(snap).read_bytes() == _png(640, 360), "截图要先快照到会话目录再进待看队列"
    assert snap != str(path) and str(ee_home) in snap
    assert res.details["kind"] == "screenshot" and res.details["width"] == 640
    assert events and events[0].request_id.startswith("shot_"), \
        "没把 CaptureRequest 发出去（界面就永远不知道要截图）"


def test_tool_reports_unavailable_without_desktop(workspace):
    from ee.core.screen import ScreenHub
    from ee.tools.base import ToolContext, ToolError, ToolState
    from ee.tools.screenshot import ScreenshotTool

    s = _session_stub(workspace)
    s.screen = ScreenHub(workspace)
    ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / "out",
                      extra={"session": s, "emit_event": lambda ev: None}, emit=lambda ev: None)

    async def main():
        with pytest.raises(ToolError) as e:
            await ScreenshotTool().run({}, ctx)
        return str(e.value)

    msg = asyncio.run(main())
    assert "没有送回截图" in msg


def test_rpc_delivers_image_to_waiting_tool(ee_home, workspace, monkeypatch):
    """走完整 RPC：界面回送 data URL → 唤醒等待 → 拿到 PNG 字节。"""

    monkeypatch.setenv("EE_DESKTOP", "1")

    async def main():
        b, _fake, ev = make_backend(ee_home, [])
        t = await b.call("thread/start", {"cwd": str(workspace)})
        tid = t["thread"]["id"]
        th = b.threads[tid]
        sess = th.ensure_session()
        fut = asyncio.get_running_loop().create_future()
        sess.screen.pending["shot_abc"] = fut

        r = await b.call("ui/capture_result", {"thread_id": tid, "id": "shot_abc", "data_url": _png_data_url()})
        assert r["ok"] is True and fut.result() == PNG

        # 迟到的第二个回包：不抛错，只是 ok=False
        r2 = await b.call("ui/capture_result", {"thread_id": tid, "id": "shot_abc", "data_url": _png_data_url()})
        assert r2["ok"] is False

        # 界面报了错：等待方拿到异常，而不是一直挂着
        fut2 = asyncio.get_running_loop().create_future()
        sess.screen.pending["shot_err"] = fut2
        r3 = await b.call("ui/capture_result", {"thread_id": tid, "id": "shot_err", "error": "窗口被最小化"})
        assert r3["ok"] is True
        with pytest.raises(Exception) as e:
            await asyncio.wait_for(fut2, 1)
        assert "窗口被最小化" in str(e.value)

        # 坏数据：同样是失败结果，不写盘
        fut3 = asyncio.get_running_loop().create_future()
        sess.screen.pending["shot_bad"] = fut3
        r4 = await b.call("ui/capture_result", {"thread_id": tid, "id": "shot_bad", "data_url": "data:image/jpeg;base64,AA"})
        assert r4["ok"] is True
        with pytest.raises(Exception):
            await asyncio.wait_for(fut3, 1)

        assert not ev or all(m != "ui/capture" for m, _ in ev)

    asyncio.run(main())


def test_capture_request_reaches_the_frontend(ee_home, workspace, monkeypatch):
    """工具 emit 的 CaptureRequest 要变成给前端的 ui/capture 通知。"""
    from ee.core.events import CaptureRequest

    monkeypatch.setenv("EE_DESKTOP", "1")

    async def main():
        b, _fake, ev = make_backend(ee_home, [])
        t = await b.call("thread/start", {"cwd": str(workspace)})
        tid = t["thread"]["id"]
        b.threads[tid].ensure_session()
        b.threads[tid]._on_event(CaptureRequest("shot_zzz"))
        assert ("ui/capture", {"thread_id": tid, "id": "shot_zzz"}) in [(m, p) for m, p in ev]

    asyncio.run(main())
