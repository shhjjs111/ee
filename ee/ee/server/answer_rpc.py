"""回答与花费相关 RPC（D5-9）。

  thread/options     {thread_id, effort?, style?}   → {effort, style, effort_support, messages}
  styles/list        {}                              → {styles: [{id, label, description, source, path}], dir}
  budget/status      {thread_id?}                    → {currency, action, warn_ratio, scopes: {session|daily|monthly: {spent, limit}}}
"""
from __future__ import annotations

import asyncio

from ee.config import load_config
from ee.core import styles as ST
from ee.core.budget import BudgetGuard


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    async def options(thread_id: str, effort=None, style: str | None = None) -> dict:
        t = await backend._athread(thread_id)
        await t.ensure_session_async()
        try:
            return await asyncio.to_thread(t.set_options, effort, style)
        except ValueError as e:
            raise RpcError(str(e))

    async def styles_list() -> dict:
        def run():
            return {"styles": [s.summary() for s in ST.available(backend.home)],
                    "dir": str(ST.user_dir(backend.home))}
        return await asyncio.to_thread(run)

    async def budget_status(thread_id: str | None = None) -> dict:
        t = backend.threads.get(thread_id) if thread_id else None

        def run():
            if t is not None and t.session is not None:
                s = t.session
                s.budget.reload(load_config(t.cwd).data)
                return s.budget.status(s.session_spent(s.budget.cfg["currency"]))
            from ee.server.settings import effective
            return BudgetGuard(effective(), backend.home).status(0.0)
        return await asyncio.to_thread(run)

    methods.update({"thread/options": options, "styles/list": styles_list, "budget/status": budget_status})
