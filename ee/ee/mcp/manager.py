"""MCP 连接管理：每个事件循环一个管理器，按 (服务器名, 配置指纹) 缓存连接，并行连接，失败只记状态。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ee.mcp.client import McpError, make_client
from ee.mcp.config import ServerConfig, load_servers, mcp_settings

log = logging.getLogger("ee.mcp")


@dataclass
class ServerState:
    cfg: ServerConfig
    status: str = "idle"          # idle | connecting | ready | error | disabled | needs_approval
    error: str = ""
    tools: list[dict] = field(default_factory=list)
    client: object = None
    server_info: dict = field(default_factory=dict)
    connected_at: float = 0.0

    def summary(self) -> dict:
        return {**self.cfg.summary(), "status": self.status, "error": self.error,
                "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:200],
                           "read_only": bool((t.get("annotations") or {}).get("readOnlyHint"))} for t in self.tools],
                "server_info": self.server_info}


class McpManager:
    def __init__(self):
        self.states: dict[tuple[str, str], ServerState] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.listeners: list = []
        self.http = None                          # 测试可注入 httpx.AsyncClient

    def _emit(self, st: ServerState) -> None:
        for fn in list(self.listeners):
            try:
                fn(st.summary())
            except Exception:  # noqa: BLE001
                pass

    async def connect(self, cfg: ServerConfig, timeout: float, cwd: Path | None = None) -> ServerState:
        key = (cfg.name, cfg.key)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            st = self.states.get(key)
            if st and st.status == "ready" and st.client is not None and not st.client.closed:
                return st
            st = ServerState(cfg=cfg, status="connecting")
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
            log.info("MCP %s：%s %s", cfg.name, st.status, st.error[:200])
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
                st = ServerState(cfg=cfg, status="needs_approval" if cfg.source == "project" else "disabled")
                out.append(st)
                continue
            tasks.append(self.connect(cfg, timeout, cwd))
        out += await asyncio.gather(*tasks)
        return out

    def _find(self, server: str) -> ServerState | None:
        best = None
        for (name, _), st in self.states.items():
            if name == server and (best is None or st.connected_at >= best.connected_at):
                best = st
        return best

    async def call(self, server: str, tool: str, args: dict, timeout: float = 120) -> dict:
        st = self._find(server)
        if st is None:
            raise McpError(f"MCP 服务器 {server} 未连接")
        for attempt in (0, 1):
            if st.status != "ready" or st.client is None or st.client.closed:
                st = await self.connect(st.cfg, timeout=20)
                if st.status != "ready":
                    raise McpError(st.error or "连接失败")
            try:
                return await st.client.call_tool(tool, args, timeout=timeout)
            except McpError as e:
                if attempt == 0 and (st.client is None or st.client.closed):
                    st.status = "error"
                    continue                    # 断线：重连一次再试
                raise e
        raise McpError("调用失败")

    async def test(self, cfg: ServerConfig, timeout: float, cwd: Path | None = None) -> dict:
        """独立连接一次、列工具后关闭（设置页“测试连接”）。"""
        tmp = McpManager()
        tmp.http = self.http
        st = await tmp.connect(cfg, timeout, cwd)
        await tmp.close_all()
        return st.summary()

    def snapshot(self) -> list[dict]:
        latest: dict[str, ServerState] = {}
        for (name, _), st in self.states.items():
            if name not in latest or st.connected_at >= latest[name].connected_at:
                latest[name] = st
        return [st.summary() for st in latest.values()]

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
