"""prompts/* RPC：斜杠命令里的自定义提示（D3-B）。

  prompts/list   {cwd?}                 → {prompts: [{name, description, argument_hint, source, path}], dirs: {user, project}}
  prompts/expand {name, args?, cwd?}    → {name, text}
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import prompts as P


def register(methods: dict, home: Path) -> None:
    from ee.server.app import RpcError

    def _cwd(cwd):
        return Path(cwd) if cwd else None

    async def list_(cwd: str | None = None) -> dict:
        def go():
            ps = P.load_prompts(home, _cwd(cwd))
            return {"prompts": [p.summary() for p in ps.values()],
                    "dirs": {src: str(d) for src, d in P.dirs(home, _cwd(cwd))}}
        return await asyncio.to_thread(go)

    async def expand(name: str, args: str = "", cwd: str | None = None) -> dict:
        def go():
            ps = P.load_prompts(home, _cwd(cwd))
            p = ps.get(str(name).lstrip("/"))
            if p is None:
                raise RpcError(f"没有这个命令：/{name}")
            return {"name": p.name, "text": P.expand(p, args)}
        return await asyncio.to_thread(go)

    methods.update({"prompts/list": list_, "prompts/expand": expand})
