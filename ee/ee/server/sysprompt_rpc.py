"""系统提示词 RPC（SYSTEM.md / APPEND_SYSTEM.md）。

  sysprompt/info    {cwd?, thread_id?}  → {cwd, user_dir, current: SystemSources 摘要（新对话会用的）,
                                           frozen: 该对话开始时冻结的摘要或 null, max_bytes}
  sysprompt/preview {cwd?, thread_id?}  → {text, frozen, model, chars}
      有 thread_id 且该对话已经发过请求：返回实际发给模型的那份（冻结版）；否则按默认模型、默认风格现算新对话会用的。
  sysprompt/trust   {cwd, allow_id}     → {allowed_project}   信任项目里的 .ee/SYSTEM.md / APPEND_SYSTEM.md（内容指纹要对得上）
  sysprompt/untrust {allow_id}          → {allowed_project}
  sysprompt/set     {model_patch}       → {model_patch}       是否追加按模型家族的补丁（写用户级配置）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.core import sysprompt as SP


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _cwd(cwd) -> Path | None:
        return Path(cwd) if cwd and Path(cwd).is_dir() else None

    def _session(thread_id):
        t = backend.threads.get(thread_id) if thread_id else None
        return t.session if t is not None else None

    async def info(cwd: str | None = None, thread_id: str | None = None) -> dict:
        s = _session(thread_id)
        where = _cwd(cwd) or (s.cwd if s is not None else None)

        def run():
            from ee.config import load_config
            data = load_config(where or Path.home()).data
            cur = SP.resolve(where, backend.home, data)
            frozen = s._sys_src.summary() if s is not None and s.prefix is not None and s._sys_src is not None else None
            return {"cwd": str(where) if where else "", "user_dir": str(backend.home), "current": cur.summary(),
                    "frozen": frozen, "max_bytes": SP.MAX_BYTES}
        return await asyncio.to_thread(run)

    async def preview(cwd: str | None = None, thread_id: str | None = None) -> dict:
        s = _session(thread_id)
        if s is not None and s.prefix is not None:
            text = s.prefix.system["content"]
            return {"text": text, "frozen": True, "model": s.spec.alias, "chars": len(text)}
        where = _cwd(cwd) or (s.cwd if s is not None else None)

        def run():
            from ee.config import load_config
            from ee.core import styles as ST
            from ee.core.context import system_prompt
            cfg = load_config(where or Path.home())
            spec = s.spec if s is not None else cfg.resolve_model(None)
            sdef = str((cfg.data.get("style") or {}).get("default") or "default")
            style = s.style if s is not None else (sdef if ST.exists(backend.home, sdef) else "default")
            seg = ST.system_segment(ST.get(backend.home, style))
            custom = SP.resolve(where, backend.home, cfg.data)
            if s is not None:
                tools, agent = s.tools, s.agent_profile
            else:     # 新对话会用的工具表与模式
                from ee.tools import agent_profile, build_tools
                agent = agent_profile(cfg)
                tools = build_tools(spec.profile, cfg, agent=agent)
            text = system_prompt(spec.profile, seg, custom, tools, agent)
            return {"text": text, "frozen": False, "model": spec.alias, "chars": len(text)}
        try:
            return await asyncio.to_thread(run)
        except ValueError as e:
            raise RpcError(str(e))

    async def trust(allow_id: str, cwd: str | None = None) -> dict:
        try:
            ids = await asyncio.to_thread(SP.trust, _cwd(cwd), backend.home, str(allow_id or ""))
        except ValueError as e:
            raise RpcError(str(e))
        return {"allowed_project": ids}

    async def untrust(allow_id: str) -> dict:
        return {"allowed_project": await asyncio.to_thread(SP.untrust, str(allow_id or ""))}

    async def set_(model_patch: bool) -> dict:
        return {"model_patch": await asyncio.to_thread(SP.set_model_patch, bool(model_patch))}

    methods.update({"sysprompt/info": info, "sysprompt/preview": preview, "sysprompt/trust": trust,
                    "sysprompt/untrust": untrust, "sysprompt/set": set_})
