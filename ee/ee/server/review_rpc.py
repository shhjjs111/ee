"""审查与提交 RPC（D4-2）。

  review/targets       {cwd}                                           → {is_repo, branch, dirty, branches, base, commits}
  review/start         {thread_id, kind, ref?, instructions?, model?}  → turn/start 的返回（这一回合只读）
  git/commit_message   {cwd, thread_id?, model?}                       → {message, stage_all, files}
  git/commit           {cwd, message, stage_all}                       → {ok, sha, output}
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ee.core import gitinfo
from ee.core import review as R


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _dir(cwd: str) -> Path:
        p = Path(cwd or "")
        if not cwd or not p.is_dir():
            raise RpcError(f"目录不存在：{cwd}")
        return p

    async def targets(cwd: str) -> dict:
        return await asyncio.to_thread(R.targets, _dir(cwd))

    async def start(thread_id: str, kind: str, ref: str = "", instructions: str = "", model: str | None = None) -> dict:
        t = await backend._athread(thread_id)
        if t.compacting:
            raise RpcError("正在压缩上下文，稍等片刻再审查")
        if t.running:
            raise RpcError("ee 正在工作，等这一回合结束再开始审查")
        if kind == "custom" and not (instructions or "").strip():
            raise RpcError("按要求审查需要写明审查什么")
        try:
            target = await asyncio.to_thread(R.collect, Path(t.cwd), kind, ref)
        except R.ReviewError as e:
            raise RpcError(str(e))
        await t.ensure_session_async()
        try:
            return t.start_turn(R.build_prompt(target, instructions or ""), model=model,
                                display=R.display_text(target, instructions or ""), readonly=True, kind="review")
        except ValueError as e:
            raise RpcError(str(e))

    async def _session_for(cwd: str, thread_id: str | None, model: str | None):
        if thread_id:
            t = await backend._athread(thread_id)
            if os.path.normcase(str(Path(t.cwd).resolve())) == os.path.normcase(str(Path(cwd).resolve())):
                return await t.ensure_session_async()
        from ee.config import load_config
        from ee.core.session import Session
        def build():   # Session 初始化会跑 git 子进程：放到工作线程
            cfg = load_config(Path(cwd))
            return Session(cfg, model=model, cwd=Path(cwd), client_factory=backend.client_factory, persist=False,
                           home=backend.home, user_home=backend.user_home)
        return await asyncio.to_thread(build)

    async def commit_message(cwd: str, thread_id: str | None = None, model: str | None = None) -> dict:
        p = _dir(cwd)
        st = await asyncio.to_thread(gitinfo.status, p)
        if not st.is_repo:
            raise RpcError("这个文件夹不是 git 仓库")
        s = await _session_for(cwd, thread_id, model)
        try:
            msg, stage_all = await s.commit_message()
        except Exception as e:  # noqa: BLE001  模型错误 / 没有改动
            raise RpcError(str(e))
        files = st.files if stage_all else await asyncio.to_thread(_staged_files, p)
        return {"message": _clean(msg), "stage_all": stage_all, "files": files}

    async def commit(cwd: str, message: str, stage_all: bool = True) -> dict:
        p = _dir(cwd)
        message = (message or "").strip()
        if not message:
            raise RpcError("提交信息不能为空")
        ok, out = await asyncio.to_thread(gitinfo.commit, p, message + "\n", bool(stage_all))
        if not ok:
            raise RpcError("git commit 失败：\n" + out.strip()[-2000:])
        _, sha = await asyncio.to_thread(gitinfo._run, ["rev-parse", "--short", "HEAD"], p)
        return {"ok": True, "sha": sha.strip(), "output": out.strip()[-2000:]}

    methods.update({"review/targets": targets, "review/start": start,
                    "git/commit_message": commit_message, "git/commit": commit})


def _staged_files(cwd: Path) -> list[str]:
    _, out = gitinfo._run(["diff", "--cached", "--name-only"], cwd)
    return [x for x in out.splitlines() if x.strip()]


def _clean(msg: str) -> str:
    """去掉模型偶尔加的代码块围栏和首尾引号。"""
    m = (msg or "").strip()
    if m.startswith("```"):
        m = m.split("\n", 1)[1] if "\n" in m else ""
        if m.rstrip().endswith("```"):
            m = m.rstrip()[:-3]
    return m.strip().strip('"').strip()
