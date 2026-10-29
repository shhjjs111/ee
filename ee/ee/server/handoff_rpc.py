"""文件交接的 RPC（T10-e 界面侧）：handoff/manifest、handoff/verify。

界面要能让用户在两处之间搬东西前**先看见清单**。两条纪律：

- **只读**：manifest 不写盘、不动源。它有测试守（比较生成前后的 git status）。
- **不假装**：不是 git 仓库、路径不存在时明确报错，不返回空清单冒充
  「没东西要搬」——那会让人以为搬完了。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import handoff as H


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _check_dir(raw: str, what: str) -> Path:
        p = Path(raw).expanduser()
        if not p.is_dir():
            raise RpcError(f"{what}不存在或不是目录：{p}", -32602)
        return p

    async def manifest(path: str, ignored_policy: str = "skip",
                       include_ignored: bool = False) -> dict:
        root = _check_dir(path, "目录")
        try:
            m = await asyncio.to_thread(H.build_manifest, root,
                                        ignored_policy=ignored_policy,
                                        include_ignored=include_ignored)
        except RuntimeError as e:
            # build_manifest 里的 git 失败（典型是非 git 仓库）走这里
            raise RpcError(f"生成清单失败（不是 git 仓库？）：{e}") from None
        return {"root": m.root,
                "ignored_policy": m.ignored_policy,
                "entries": [e.as_dict() for e in m.entries],
                "summary": m.summary()}

    async def verify(path: str, dest: str) -> dict:
        root = _check_dir(path, "源目录")
        target = _check_dir(dest, "目标目录")
        try:
            m = await asyncio.to_thread(H.build_manifest, root)
        except RuntimeError as e:
            raise RpcError(f"生成清单失败（不是 git 仓库？）：{e}") from None
        problems = await asyncio.to_thread(H.verify_target, m, target)
        return {"ok": not problems, "problems": problems,
                "checked": sum(1 for e in m.entries if e.selected and e.kind in ("file", "symlink"))}

    methods["handoff/manifest"] = manifest
    methods["handoff/verify"] = verify
