"""hooks/* RPC（D5-4）。

  hooks/list {cwd?}                    → {enabled, user: [...], projects: [{file, allow_id, allowed, hooks}], errors,
                                          events: [{name, label}]}
  hooks/test {event, hook, cwd?}       → 用示例输入跑一次（设置页「测试运行」）：退出码、stdout、stderr、ee 的理解。
      hook = {command, matcher?, timeout?, shell?}；项目 hook 测试前必须先允许（传 file + allow_id 校验）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import hooks as H


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _cwd(cwd) -> Path | None:
        return Path(cwd) if cwd and Path(cwd).is_dir() else None

    async def list_(cwd: str | None = None) -> dict:
        cfg = await asyncio.to_thread(H.load, _cwd(cwd))
        return {**cfg.summary(), "events": [{"name": e, "label": H.EVENT_LABEL[e]} for e in H.EVENTS]}

    async def test(event: str, hook: dict, cwd: str | None = None, allow_id: str | None = None) -> dict:
        source = "user"
        if allow_id:
            cfg = await asyncio.to_thread(H.load, _cwd(cwd))
            p = next((x for x in cfg.projects if x.allow_id == allow_id), None)
            if p is None:
                raise RpcError("项目 hooks 文件已经改动或不存在，请刷新后重新允许")
            if not p.allowed:
                raise RpcError("项目里的 Hook 要先允许并保存，才能测试运行")
            source = "project"
        try:
            h = H.parse_hook(str(event), dict(hook or {}), source)
        except ValueError as e:
            raise RpcError(str(e))
        from ee.config import load_config
        where = _cwd(cwd) or Path.home()
        shell = (await asyncio.to_thread(load_config, where)).data.get("shell", "auto")
        return await H.test_hook(h, where, shell)

    methods.update({"hooks/list": list_, "hooks/test": test})
