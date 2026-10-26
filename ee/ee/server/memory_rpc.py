"""memory/* RPC：设置页查看 / 编辑长期记忆。文件读写放到线程里，不卡事件循环。

  memory/get    {cwd?}                    → {user: {path, exists, text, entries}, workspace: {...}|null}
  memory/add    {text, scope=user, cwd?}  → 同 memory/get
  memory/remove {index, scope=user, cwd?} → 同 memory/get（index 从 1 开始）
  memory/save   {text, scope=user, cwd?}  → 同 memory/get（整份覆盖）
改动从下一个新会话起注入上下文（已开始的会话保持不变，保住前缀缓存）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import memory as M


def _cwd(cwd: str | None) -> Path | None:
    return Path(cwd) if cwd else None


def _ws_enabled() -> bool:
    from ee.server.settings import effective
    return bool((effective().get("memory") or {}).get("workspace", True))


def _get(home: Path, cwd: str | None) -> dict:
    return M.describe(home, _cwd(cwd), _ws_enabled())


def _path(home: Path, scope: str, cwd: str | None) -> Path:
    try:
        return M.path_for(scope or "user", home, _cwd(cwd))
    except M.MemoryError_ as e:
        raise ValueError(str(e)) from None


def _rpc(fn):
    """ValueError → RpcError（界面显示干净的中文错误，而不是 “ValueError: …”）。"""
    async def wrapped(**kw):
        from ee.server.app import RpcError
        try:
            return await fn(**kw)
        except ValueError as e:
            raise RpcError(str(e)) from None
    return wrapped


def register(methods: dict, home: Path) -> None:
    async def get(cwd: str | None = None) -> dict:
        return await asyncio.to_thread(_get, home, cwd)

    async def add(text: str, scope: str = "user", cwd: str | None = None) -> dict:
        def go():
            try:
                M.add_entry(_path(home, scope, cwd), text)
            except M.MemoryError_ as e:
                raise ValueError(str(e)) from None
            return _get(home, cwd)
        return await asyncio.to_thread(go)

    async def remove(index: int, scope: str = "user", cwd: str | None = None) -> dict:
        def go():
            try:
                M.remove_entry(_path(home, scope, cwd), index=int(index))
            except M.MemoryError_ as e:
                raise ValueError(str(e)) from None
            return _get(home, cwd)
        return await asyncio.to_thread(go)

    async def save(text: str, scope: str = "user", cwd: str | None = None) -> dict:
        def go():
            M.save_text(_path(home, scope, cwd), text or "")
            return _get(home, cwd)
        return await asyncio.to_thread(go)

    methods.update({"memory/get": _rpc(get), "memory/add": _rpc(add), "memory/remove": _rpc(remove),
                    "memory/save": _rpc(save)})
