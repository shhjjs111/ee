"""skills/* RPC（D5-5）：设置页 / 斜杠菜单列出技能，查看全文，打开技能目录。文件操作都放在线程里。

  skills/list {cwd?}                    → {enabled, compat, skills: [{name, description, path, dir, source, origin,
                                            enabled, shadows}], errors: [{path, error}], dirs: {user, project}}
  skills/read {name, cwd?}              → {name, path, dir, text}（SKILL.md 全文 + 文件列表，和 skill 工具返回的一样）
  skills/dir  {scope=user|project, cwd?} → {path}（不存在就创建，界面用来「打开技能目录」）
启用 / 禁用写在 config.toml 的 [skills] disabled（通过 settings/set），从新会话起生效。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import skills as K


def _cwd(cwd: str | None) -> Path | None:
    return Path(cwd) if cwd else None


def _data() -> dict:
    from ee.server.settings import effective
    return effective()


def register(methods: dict, home: Path) -> None:
    from ee.server.app import RpcError

    async def list_(cwd: str | None = None) -> dict:
        def go():
            data = _data()
            cfg = K.settings(data)
            cat = K.discover(home, _cwd(cwd), data)
            return {"enabled": bool(cfg.get("enabled", True)), "compat": bool(cfg.get("compat", True)),
                    "skills": [s.summary() for s in cat.skills.values()], "errors": cat.errors,
                    "dirs": {"user": str(Path(home) / "skills"),
                             "project": str(K.project_dir(Path(cwd))) if cwd else None}}
        return await asyncio.to_thread(go)

    async def read(name: str, cwd: str | None = None) -> dict:
        def go():
            cat = K.discover(home, _cwd(cwd), _data())
            sk = cat.skills.get(str(name or "").lstrip("$/"))
            if sk is None:
                raise RpcError(f"没有这个技能：{name}")
            return {"name": sk.name, "path": sk.path, "dir": sk.dir, "text": K.render_full(sk)}
        return await asyncio.to_thread(go)

    async def dir_(scope: str = "user", cwd: str | None = None) -> dict:
        def go():
            if scope == "project":
                if not cwd:
                    raise RpcError("没有打开项目，不能使用项目技能目录")
                p = K.project_dir(Path(cwd))
            elif scope == "user":
                p = Path(home) / "skills"
            else:
                raise RpcError(f"未知范围：{scope}（只能是 user 或 project）")
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RpcError(f"创建目录失败：{e}") from None
            return {"path": str(p)}
        return await asyncio.to_thread(go)

    methods.update({"skills/list": list_, "skills/read": read, "skills/dir": dir_})
