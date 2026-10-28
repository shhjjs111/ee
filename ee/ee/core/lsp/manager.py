"""语言服务器的生命周期：按 key 复用、引用计数、空闲回收。

**为什么要引用计数而不是「会话结束就关掉」**：
同一个工作区的两个会话（比如父会话和子会话）会用同一个 pyright。按「会话结束关全部」
做，子会话一结束就把父会话正在用的服务器杀了，父会话下一次取诊断就会莫名其妙地
等一轮重启。所以管理键 = 工作区根目录 + 语言 + 命令身份，关闭只减计数，
计数归零并且空闲超过 TTL 才真正回收。

**为什么 key 里要有根目录和命令身份**：pyright 的配置按项目走（pyproject.toml、
tsconfig.json），不同项目共用一个进程会拿到对方的诊断；命令身份进去是为了避免
用户改了启动命令之后复用旧进程。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("ee.lsp")

DEFAULT_IDLE_TTL = 120.0      # 计数归零后空闲多久回收（秒）


@dataclass
class Handle:
    """一次 acquire 的凭据。release 时用它找回池里的条目。"""
    key: tuple
    entry: "Entry"


class Entry:
    """池里的一条：一个真实的客户端进程 + 它的引用计数。"""

    def __init__(self, key: tuple, client, spec):
        self.key = key
        self.client = client
        self.spec = spec
        self.refs = 0
        self.last_used = time.monotonic()
        self.initialized = False
        self.start_lock = asyncio.Lock()

    @property
    def dead(self) -> bool:
        return bool(getattr(self.client, "closed", False))


class LspManager:
    """按 key 管一组语言服务器。线程内使用（同一个事件循环）。"""

    def __init__(self, *, client_factory=None, idle_ttl: float = DEFAULT_IDLE_TTL,
                 init_timeout: float = 20.0):
        self._pool: dict[tuple, Entry] = {}
        self._client_factory = client_factory or _default_factory
        self.idle_ttl = idle_ttl
        self.init_timeout = init_timeout
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------ 对外
    async def acquire(self, root: str, language: str, identity: str, spec=None) -> Handle:
        """取一个可用的服务器（必要时启动或重启），引用计数 +1。"""
        key = (str(root), language, identity)

        entry = self._pool.get(key)
        if entry is not None and entry.dead:
            # 进程已退出：摘掉，下面会重建。不能永久缓存失败状态。
            self._pool.pop(key, None)
            entry = None

        if entry is None:
            entry = Entry(key, None, spec)
            self._pool[key] = entry
            # 用 per-entry 的锁保证并发 acquire 只启动一次
            async with entry.start_lock:
                if entry.client is None:
                    entry = await self._spawn(key, spec, entry)
                    self._pool[key] = entry
        entry.refs += 1
        entry.last_used = time.monotonic()
        self._ensure_reaper()
        return Handle(key=key, entry=entry)

    async def release(self, handle: Handle) -> None:
        """释放一次引用。计数归零不立刻关，交给空闲回收（除非 ttl=0）。"""
        entry = self._pool.get(handle.key)
        if entry is None:
            return
        entry.refs = max(0, entry.refs - 1)
        entry.last_used = time.monotonic()
        if entry.refs == 0 and self.idle_ttl <= 0:
            await self._close_entry(handle.key)

    async def shutdown(self) -> None:
        """关掉所有服务器（进程退出 / 测试收尾用）。"""
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reaper = None
        for key in list(self._pool):
            await self._close_entry(key)

    def stats(self) -> list[dict]:
        """给设置页 / 诊断用：当前有哪些服务器在跑。"""
        return [{"root": k[0], "language": k[1], "identity": k[2], "refs": e.refs,
                 "alive": not e.dead} for k, e in self._pool.items()]

    # ------------------------------------------------------------ 内部
    async def _spawn(self, key: tuple, spec, entry: Entry) -> Entry:
        if spec is None or not getattr(spec, "available", True):
            reason = getattr(spec, "reason", "") or "没有可用的语言服务器"
            raise LspUnavailable(reason)
        client = self._client_factory(spec.command, name=f"lsp-{key[1]}")
        try:
            await client.start()
            await asyncio.wait_for(client.request("initialize", {
                "processId": None,
                "rootUri": _path_to_uri(key[0]),
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "workspaceFolders": [{"uri": _path_to_uri(key[0]), "name": key[1]}],
            }, timeout=self.init_timeout), self.init_timeout + 5)
            try:
                await client.notify("initialized", {})
            except Exception:  # noqa: BLE001  有的服务器不要求
                pass
        except Exception as e:  # noqa: BLE001
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool.pop(key, None)
            if isinstance(e, LspUnavailable):
                raise
            raise LspUnavailable(f"启动 {key[1]} 语言服务器失败：{e}") from None
        new = Entry(key, client, spec)
        new.refs = entry.refs
        new.initialized = True
        return new

    async def _close_entry(self, key: tuple) -> None:
        entry = self._pool.pop(key, None)
        if entry is None:
            return
        try:
            await entry.client.close()
        except Exception as e:  # noqa: BLE001  关不掉不该影响调用方
            log.warning("关闭语言服务器 %s 出错：%s", key, e)

    def _ensure_reaper(self) -> None:
        if self.idle_ttl <= 0 or self._reaper is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper = loop.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        interval = max(1.0, min(30.0, self.idle_ttl / 2))
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for key, entry in list(self._pool.items()):
                if entry.refs == 0 and (now - entry.last_used) >= self.idle_ttl:
                    await self._close_entry(key)


class LspUnavailable(RuntimeError):
    """没有可用服务器 / 启动失败。调用方据此报 unavailable 而不是 error。"""


def _default_factory(argv, **kw):
    from ee.core.lsp.client import LspClient
    return LspClient(argv, **kw)


def _path_to_uri(path: str) -> str:
    from urllib.parse import quote
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p)
