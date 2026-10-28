"""MCP 连接管理：每个事件循环一个管理器，按「连接身份」缓存连接，并行连接，失败只记状态。

连接身份 = (服务器名, 配置指纹, 规范化工作区)。以前缓存键只有 (名字, 配置)，调用又按名字
挑「最近连接」——两个项目用同名服务器时，A 的工具可能落到 B 的连接上执行：schema / read_only /
trusted 来自原连接，实际执行却在别的工作区。现在工具持有不可变的 ConnectionHandle，
调用与重连都认这个身份，不再按名字猜。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ee.mcp.client import McpError, make_client
from ee.mcp.config import ServerConfig, load_servers, mcp_settings

log = logging.getLogger("ee.mcp")


def normalize_cwd(cwd: Path | str | None) -> str:
    """连接身份里的工作区：真实路径、统一大小写（Windows 不区分大小写）。"""
    if not cwd:
        return ""
    try:
        return os.path.normcase(os.path.realpath(str(cwd)))
    except OSError:
        return os.path.normcase(str(cwd))


@dataclass(frozen=True)
class ConnectionHandle:
    """一条 MCP 连接的身份。同名服务器在不同工作区/不同配置下是不同连接。"""
    server: str
    key: str               # 配置指纹（ServerConfig.key），授权记录也用它
    cwd: str = ""          # 规范化工作区（"" = 没有工作区，例如 mcp/test）

    @property
    def conn_id(self) -> str:
        return f"{self.server}:{self.key}:{self.cwd}"

    def __str__(self) -> str:      # 给日志/界面看的短名
        return self.server


@dataclass
class ServerState:
    cfg: ServerConfig
    status: str = "idle"          # idle | connecting | ready | error | disabled | needs_approval
    error: str = ""
    tools: list[dict] = field(default_factory=list)
    client: object = None
    server_info: dict = field(default_factory=dict)
    connected_at: float = 0.0
    handle: ConnectionHandle | None = None
    cwd: Path | None = None

    def summary(self) -> dict:
        return {**self.cfg.summary(), "status": self.status, "error": self.error,
                "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:200],
                           "read_only": bool((t.get("annotations") or {}).get("readOnlyHint"))} for t in self.tools],
                "server_info": self.server_info,
                "conn_id": self.handle.conn_id if self.handle else "",
                "cwd": str(self.cwd) if self.cwd else ""}


class McpManager:
    def __init__(self):
        self.states: dict[str, ServerState] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.listeners: list = []
        self.http = None                          # 测试可注入 httpx.AsyncClient

    def _emit(self, st: ServerState) -> None:
        for fn in list(self.listeners):
            try:
                fn(st.summary())
            except Exception:  # noqa: BLE001
                pass

    async def connect(self, cfg: ServerConfig, timeout: float, cwd: Path | None = None) -> ServerState:
        handle = ConnectionHandle(cfg.name, cfg.key, normalize_cwd(cwd))
        key = handle.conn_id
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            st = self.states.get(key)
            if st and st.status == "ready" and st.client is not None and not st.client.closed:
                return st
            st = ServerState(cfg=cfg, status="connecting", handle=handle, cwd=cwd)
            self.states[key] = st
            self._emit(st)
            client = make_client(cfg, roots=[str(cwd)] if cwd else [], base_cwd=str(cwd) if cwd else None,
                                 http=self.http)
            try:
                async def go():
                    await client.start()
                    await client.initialize(timeout)
                    return await client.list_tools(timeout)
                st.tools = await asyncio.wait_for(go(), timeout)
                st.client = client
                st.server_info = client.server_info
                st.status, st.error, st.connected_at = "ready", "", time.time()
            except (McpError, asyncio.TimeoutError, OSError) as e:
                msg = str(e) or "连接超时"
                tail = client.stderr_tail()
                if tail and tail not in msg:
                    msg += "\n" + tail[-800:]
                st.status, st.error = "error", msg
                await _close_quiet(client)
            except Exception as e:  # noqa: BLE001
                st.status, st.error = "error", f"{type(e).__name__}: {e}"
                await _close_quiet(client)
            self._emit(st)
            log.info("MCP %s（%s）：%s %s", cfg.name, key[:16], st.status, st.error[:200])
            return st

    async def connect_all(self, data: dict, cwd: Path | None) -> list[ServerState]:
        """连接所有启用的服务器（并行）。未允许的项目服务器、关闭的服务器只记状态不连接。"""
        m = mcp_settings(data)
        if not m.get("enabled", True):
            return []
        servers, errors = load_servers(data, cwd)
        for e in errors:
            log.warning("MCP 配置：%s", e)
        timeout = float(m.get("startup_timeout") or 20)
        out: list[ServerState] = []
        tasks = []
        for cfg in servers:
            if not cfg.enabled:
                st = ServerState(cfg=cfg, status="needs_approval" if cfg.source == "project" else "disabled",
                                 handle=ConnectionHandle(cfg.name, cfg.key, normalize_cwd(cwd)), cwd=cwd)
                out.append(st)
                continue
            tasks.append(self.connect(cfg, timeout, cwd))
        out += await asyncio.gather(*tasks)
        return out

    def state_for(self, cfg: ServerConfig, cwd: Path | None = None) -> ServerState | None:
        """按连接身份取状态（界面列服务器用；不再按名字挑最近的）。"""
        return self.states.get(ConnectionHandle(cfg.name, cfg.key, normalize_cwd(cwd)).conn_id)

    def _resolve(self, target) -> ServerState:
        """把连接句柄（或唯一的服务器名）解析成状态。名字不唯一时报错，不猜。"""
        if isinstance(target, ConnectionHandle):
            st = self.states.get(target.conn_id)
            if st is None:
                raise McpError(f"MCP 服务器 {target.server} 未连接")
            return st
        name = str(target)
        matches = [st for st in self.states.values() if st.handle is not None and st.handle.server == name]
        if not matches:
            raise McpError(f"MCP 服务器 {name} 未连接")
        if len(matches) > 1:
            cwds = "、".join(sorted({str(m.cwd or "（无工作区）") for m in matches})[:5])
            raise McpError(f"MCP 服务器 {name} 有多个连接（{cwds}），无法确定调用哪一个；"
                           "请用连接句柄指定，避免跨工作区串用")
        return matches[0]

    def _find(self, server: str) -> ServerState | None:
        """按名字找唯一连接（兼容旧调用方；有歧义时返回 None）。"""
        try:
            return self._resolve(server)
        except McpError:
            return None

    async def call(self, target, tool: str, args: dict, timeout: float = 120) -> dict:
        """调用工具。target 是 ConnectionHandle（推荐）或唯一的服务器名。

        timeout 是**整个调用**的截止时间（连接 + 发送 + 等响应）。超时后不重试有副作用的调用：
        我们无法知道服务端是否已经执行。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, float(timeout))
        st = self._resolve(target)
        for attempt in (0, 1):
            if st.status != "ready" or st.client is None or st.client.closed:
                left = deadline - loop.time()
                if left <= 0:
                    raise McpError(f"{tool} 超时（{timeout:g} 秒）")
                st = await self.connect(st.cfg, timeout=min(20.0, left), cwd=st.cwd)
                if st.status != "ready":
                    raise McpError(st.error or "连接失败")
            left = deadline - loop.time()
            if left <= 0:
                raise McpError(f"{tool} 超时（{timeout:g} 秒）")
            try:
                return await asyncio.wait_for(st.client.call_tool(tool, args, timeout=left), left)
            except asyncio.TimeoutError:
                raise McpError(f"{tool} 超时（{timeout:g} 秒）") from None
            except McpError:
                if attempt == 0 and (st.client is None or st.client.closed):
                    st.status = "error"
                    continue                    # 断线：按同一身份重连一次再试
                raise
        raise McpError("调用失败")

    async def test(self, cfg: ServerConfig, timeout: float, cwd: Path | None = None) -> dict:
        """独立连接一次、列工具后关闭（设置页“测试连接”）。"""
        tmp = McpManager()
        tmp.http = self.http
        st = await tmp.connect(cfg, timeout, cwd)
        await tmp.close_all()
        return st.summary()

    def snapshot(self) -> list[dict]:
        return [st.summary() for st in self.states.values()]

    async def close_all(self) -> None:
        for st in list(self.states.values()):
            if st.client is not None:
                await _close_quiet(st.client)
        self.states.clear()


async def _close_quiet(client) -> None:
    try:
        await asyncio.wait_for(client.close(), 5)
    except Exception:  # noqa: BLE001
        pass


_MANAGERS: dict[int, McpManager] = {}


def get_manager() -> McpManager:
    loop = asyncio.get_running_loop()
    m = _MANAGERS.get(id(loop))
    if m is None:
        m = _MANAGERS[id(loop)] = McpManager()
    return m


async def shutdown() -> None:
    loop = asyncio.get_running_loop()
    m = _MANAGERS.pop(id(loop), None)
    if m is not None:
        await m.close_all()
