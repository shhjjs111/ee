"""shell/* RPC：界面查看 / 结束后台命令（D4-6）。

  shell/list {thread_id}               → {procs: [info…]}（会话还没加载过 = 空）
  shell/read {thread_id, id, tail?}    → {proc: info, output}（最后 tail 个字符，不动模型的已读游标）
  shell/kill {thread_id, id}           → {proc: info}
通知：shell/updated {thread_id, proc}（启动、结束时）
"""
from __future__ import annotations


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _mgr(thread_id: str):
        t = backend.threads.get(thread_id)
        if t is None or t.session is None:
            return None
        return t.session.bg

    async def list_(thread_id: str) -> dict:
        m = _mgr(thread_id)
        return {"procs": m.list() if m else []}

    def _get(thread_id: str, id: str):
        m = _mgr(thread_id)
        if m is None:
            raise RpcError("这个会话没有后台命令", 404)
        try:
            return m, m.get(id)
        except KeyError as e:
            raise RpcError(str(e.args[0]), 404)

    async def read(thread_id: str, id: str, tail: int = 20000) -> dict:
        _, p = _get(thread_id, id)
        return {"proc": p.info(), "output": p.tail(max(1000, min(200_000, int(tail))))}

    async def kill(thread_id: str, id: str) -> dict:
        m, p = _get(thread_id, id)
        await m.kill(p)
        return {"proc": p.info()}

    methods["shell/list"] = list_
    methods["shell/read"] = read
    methods["shell/kill"] = kill
