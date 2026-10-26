"""mcp/* RPC（D3-A）。

  mcp/list {cwd?}                 → {enabled, servers: [配置 + 当前连接状态 + 工具], errors, project_json}
  mcp/test {name?, server?, cwd?} → 单个服务器的测试结果（独立连接，列工具后关闭）
      server 给的是未保存的配置（设置页编辑中），name 给的是已保存的服务器名。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.config import load_config
from ee.mcp.config import find_project_json, load_servers, mcp_settings, parse_server


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _data(cwd):
        return load_config(Path(cwd) if cwd and Path(cwd).is_dir() else Path.home()).data

    async def list_(cwd: str | None = None) -> dict:
        def go():
            data = _data(cwd)
            servers, errors = load_servers(data, Path(cwd) if cwd else None)
            pj = find_project_json(Path(cwd)) if cwd else None
            return data, servers, errors, pj
        data, servers, errors, pj = await asyncio.to_thread(go)
        from ee.mcp.manager import get_manager
        live = {s["name"]: s for s in get_manager().snapshot()}
        out = []
        for sc in servers:
            row = sc.summary()
            st = live.get(sc.name)
            if st and st.get("allow_id") == sc.allow_id:
                row.update({"status": st["status"], "error": st["error"], "tools": st["tools"]})
            else:
                row.update({"status": "needs_approval" if (sc.source == "project" and not sc.enabled)
                            else ("disabled" if not sc.enabled else "idle"), "error": "", "tools": []})
            out.append(row)
        return {"enabled": bool(mcp_settings(data).get("enabled", True)), "servers": out, "errors": errors,
                "project_json": str(pj) if pj else None}

    async def test(name: str | None = None, server: dict | None = None, cwd: str | None = None) -> dict:
        data = await asyncio.to_thread(_data, cwd)
        try:
            if server is not None:
                sc = parse_server(str(name or server.get("name") or "test"), server)
            else:
                servers, _ = await asyncio.to_thread(load_servers, data, Path(cwd) if cwd else None)
                sc = next((x for x in servers if x.name == name), None)
                if sc is None:
                    raise RpcError(f"没有这个 MCP 服务器：{name}")
        except ValueError as e:
            raise RpcError(str(e))
        from ee.mcp.manager import get_manager
        timeout = float(mcp_settings(data).get("startup_timeout") or 20)
        return await get_manager().test(sc, timeout, Path(cwd) if cwd else None)

    methods.update({"mcp/list": list_, "mcp/test": test})
