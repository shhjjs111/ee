"""桌面端截图：后端向界面要「当前窗口的一张图」。

**方向**：后端 → 桌面端。现在的通道只有单向通知（`notify`），所以这里用「通知 + 回调 RPC」：

1. 工具 emit `CaptureRequest(id)`；
2. `Thread._on_event` 把它变成 `notify("ui/capture", {thread_id, id})`；
3. 界面 `webContents.capturePage()` 截图，把 data URL 通过 RPC `ui/capture_result` 送回；
4. `ScreenHub.resolve` 唤醒等着这次截图的那个工具。

等待上限 `TIMEOUT` 秒：窗口关掉 / CLI 里跑（没有界面）时按失败返回，不要卡住回合。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import struct
import time
import uuid
from pathlib import Path

TIMEOUT = 20.0
MAX_BYTES = 20 * 1024 * 1024          # 超过这个大小的截图直接拒绝：data URL 走 WebSocket 会被放大 4/3


class CaptureUnavailable(RuntimeError):
    """界面没有响应截图请求。"""


def decode_data_url(data_url: str) -> bytes:
    """`data:image/png;base64,...` → 字节。只收 PNG，避免把任意数据当图片写盘。"""
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        raise ValueError("只接受 PNG 的 data URL")
    raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"截图太大（{len(raw) // 1024 // 1024} MB）")
    return raw


def png_size(data: bytes) -> tuple[int, int]:
    """从 IHDR 读宽高；不是 PNG 就返回 (0, 0)。只用于给模型一句尺寸说明，不解析内容。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    try:
        w, h = struct.unpack(">II", data[16:24])
    except struct.error:
        return 0, 0
    return int(w), int(h)


class ScreenHub:
    """一次会话里等待界面回图的请求表。"""

    def __init__(self, root: Path | None = None):
        self.pending: dict[str, asyncio.Future] = {}
        self.root = Path(root) if root is not None else None

    async def request(self, emit_event, timeout: float = TIMEOUT) -> bytes:
        """发请求并等图。emit_event 是会话的事件通道（`ToolContext.extra["emit_event"]`）。

        注意别传成 `ToolContext.emit`——那个是「往工具输出里追加一行进度文字」的回调，
        只收字符串（探针实测：传错了会报 can only concatenate str）。
        """
        if emit_event is None:
            raise CaptureUnavailable("当前会话没有事件通道，截不了图")
        rid = "shot_" + uuid.uuid4().hex[:8]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise CaptureUnavailable("截图只能在会话里用")
        fut: asyncio.Future = loop.create_future()
        self.pending[rid] = fut
        from ee.core.events import CaptureRequest
        emit_event(CaptureRequest(rid))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise CaptureUnavailable(
                f"桌面端 {int(timeout)} 秒内没有送回截图：窗口可能关了，或者当前是在 CLI 里运行（没有界面）。")
        finally:
            self.pending.pop(rid, None)

    def resolve(self, rid: str, png: bytes | None, error: str = "") -> bool:
        """界面回调。返回 False 表示这次请求已经超时或不存在（静默丢弃，不抛错）。"""
        fut = self.pending.get(rid)
        if fut is None or fut.done():
            return False
        if error or not png:
            fut.set_exception(CaptureUnavailable(error or "界面没有返回图片"))
        else:
            fut.set_result(png)
        return True

    def save(self, png: bytes) -> Path:
        """把截图写到会话的 screenshots/ 下，返回路径。"""
        root = self.root or (Path.home() / ".ee" / "screenshots")
        root.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png"
        p = root / name
        p.write_bytes(png)
        return p
