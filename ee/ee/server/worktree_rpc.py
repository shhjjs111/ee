"""worktree/* RPC（D5-1）。

  worktree/list   {cwd}                              → {repo, worktrees: [...]}（cwd 不是 git 仓库时 repo=null）
  worktree/create {cwd, name?, base?}                → {worktree}
  worktree/diff   {path}                             → {worktree, files, additions, deletions}
  worktree/merge  {path, squash?}                    → {merged, autosaved, branch, repo}
  worktree/remove {path, delete_branch?, force?}     → {removed, autosaved, branch, branch_deleted}
thread/start 带 worktree: true | {name?, base?} 时先建 worktree，再在里面开会话（见 app.thread_start）。
"""
from __future__ import annotations

import asyncio


def register(methods: dict, backend) -> None:
    from ee.core import worktree as W
    from ee.server.app import RpcError

    async def _call(fn, *a, **kw):
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except W.WorktreeError as e:
            raise RpcError(str(e))

    async def list_(cwd: str) -> dict:
        def go():
            try:
                repo = str(W.repo_root(cwd))
            except W.WorktreeError:
                return {"repo": None, "worktrees": []}
            return {"repo": repo, "worktrees": W.list_for(cwd, backend.home)}
        return await _call(go)

    async def create(cwd: str, name: str | None = None, base: str | None = None) -> dict:
        return {"worktree": await _call(W.create, cwd, name, base, backend.home)}

    async def diff(path: str) -> dict:
        return await _call(W.diff, path, backend.home)

    async def merge(path: str, squash: bool = False) -> dict:
        r = await _call(W.merge, path, bool(squash), backend.home)
        backend.notify("git/changed", {"cwd": r["repo"]})
        return r

    async def remove(path: str, delete_branch: bool = False, force: bool = False) -> dict:
        for t in list(backend.threads.values()):     # 在这个 worktree 里还在跑的会话先停
            if W._norm(t.meta.get("cwd", "")) == W._norm(path) and t.running:
                raise RpcError("这个 worktree 里还有对话在运行，先停止它")
        r = await _call(W.remove, path, bool(delete_branch), bool(force), backend.home)
        for t in list(backend.threads.values()):
            if W._norm(t.meta.get("cwd", "")) == W._norm(path) and t.session is not None:
                await t.session.bg.close()
        return r

    methods["worktree/list"] = list_
    methods["worktree/create"] = create
    methods["worktree/diff"] = diff
    methods["worktree/merge"] = merge
    methods["worktree/remove"] = remove
