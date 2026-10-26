"""permissions/* RPC：设置页查看 / 删除「本项目以后都允许」的永久规则（D4-8）。

  permissions/list   {}                  → {rules: [{project, rule, added, title}], path}
  permissions/remove {project, rule}     → {ok}（同时从已打开会话的权限里去掉）
"""
from __future__ import annotations

import asyncio


def register(methods: dict, backend) -> None:
    from ee.core import rules as R

    async def list_() -> dict:
        def go():
            return {"rules": R.list_all(backend.home), "path": str(R._file(backend.home))}
        return await asyncio.to_thread(go)

    async def remove(project: str, rule: str) -> dict:
        ok = await asyncio.to_thread(R.remove, project, rule, backend.home)
        for t in backend.threads.values():
            s = t.session
            if s is None:
                continue
            key = R.project_key(s.cwd)
            if (key == project or key.startswith(project.rstrip("/") + "/")) and rule in s.policy.allow:
                s.policy.allow.remove(rule)
        return {"ok": ok}

    methods["permissions/list"] = list_
    methods["permissions/remove"] = remove
