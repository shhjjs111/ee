"""后端自己的日志文件 + 事件循环卡顿看门狗。

位置（第一个可用的）：环境变量 EE_LOG_DIR（桌面端传 Electron 的 logs 目录，Windows 上即 %APPDATA%\\ee\\logs）
→ Windows：%APPDATA%\\ee\\logs → 其他：~/.ee/logs。文件名 ee-backend.log，2MB 轮转保留 3 份。
不往 stderr 写日志（stderr 只留给崩溃信息），避免任何管道写满的可能。
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

LOG_NAME = "ee-backend.log"
_configured: Path | None = None


def log_dir() -> Path:
    env = os.environ.get("EE_LOG_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "ee" / "logs"
    return Path.home() / ".ee" / "logs"


def setup(level: int = logging.INFO) -> Path | None:
    """配置 ee.* 日志写文件。重复调用无副作用。失败（目录不可写）时静默放弃，返回 None。"""
    global _configured
    if _configured is not None:
        return _configured
    root = logging.getLogger("ee")
    root.setLevel(level)
    root.propagate = False
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(d / LOG_NAME, maxBytes=2 * 2**20, backupCount=3, encoding="utf-8")
    except OSError:
        root.addHandler(logging.NullHandler())
        return None
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    from ee.core.secrets import RedactFilter
    h.addFilter(RedactFilter())                 # 日志里不留 key
    root.addHandler(h)
    # websockets 的握手失败等也记到同一个文件（不再走 lastResort → stderr）
    wl = logging.getLogger("websockets")
    wl.setLevel(logging.WARNING)
    wl.propagate = False
    wl.addHandler(h)
    _configured = d / LOG_NAME
    return _configured


class LoopWatchdog:
    """事件循环每秒打一次心跳；另一个线程发现心跳停了 ≥ threshold 秒，就把主线程的栈写进日志。

    这样以后再出现“界面全部转圈”，日志里能直接看到卡在哪一行。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, threshold: float = 8.0):
        self.loop = loop
        self.threshold = threshold
        self.beat = time.monotonic()
        self.loop_thread = threading.get_ident()
        self._stop = threading.Event()
        self.stalls = 0

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            self.beat = time.monotonic()
            await asyncio.sleep(1.0)

    def start(self) -> None:
        self.loop.create_task(self._heartbeat())
        threading.Thread(target=self._watch, name="ee-loop-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        log = logging.getLogger("ee.watchdog")
        reported = False
        while not self._stop.wait(1.0):
            lag = time.monotonic() - self.beat
            if lag >= self.threshold and not reported:
                reported = True
                self.stalls += 1
                frame = sys._current_frames().get(self.loop_thread)
                stack = "".join(traceback.format_stack(frame)) if frame else "（拿不到栈）"
                log.error("事件循环已卡住 %.0f 秒（所有请求都会转圈）。卡在：\n%s", lag, stack)
            elif lag < self.threshold and reported:
                reported = False
                log.warning("事件循环恢复")
