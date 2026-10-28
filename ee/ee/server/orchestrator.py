"""指挥会话的服务实现（D6-1 U5/U6）。

铁律（与 docs/plans 的不变量一一对应）：

1. 只能操作 `orchestrator_parent_id == 当前会话` 的子会话。普通分叉的 `parent_id`
   不是控制关系，不能拿来授权。
2. 子会话不能再建子会话（由工具表层保证，这里再兜一层）。
3. 子会话能力不超过父会话（`orchestrator_cap`）。
4. 名额在**发起运行**时预留（`orchestrator_slots`），create 与 send 共用。
5. 查询不改 Git 索引：改动统计只读 `git status` 的计数，不调 `W.diff()`。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ee.core.ids import is_thread_id
from ee.core.store import SessionStore

log = logging.getLogger("ee.orchestrator")

WAIT_DEFAULT = 600.0
WAIT_LIMIT = 3600.0


class OrchestratorError(Exception):
    """编排操作失败。消息直接给模型/用户看。"""


def _last_assistant(items: list[dict], limit: int = 300) -> str:
    for it in reversed(items or []):
        if it.get("type") == "assistant" and (it.get("text") or "").strip():
            text = it["text"].strip()
            return text if len(text) <= limit else text[:limit] + "…"
    return ""


def _last_run(meta: dict) -> dict:
    runs = meta.get("runs") or []
    return runs[-1] if runs else {}


class Orchestrator:
    """Backend 侧实现。只通过 OrchestratorAPI 的形状暴露给工具。"""

    def __init__(self, backend) -> None:
        self.b = backend

    # ------------------------------------------------------------ 发现
    def list_children(self, parent_id: str) -> list[dict]:
        """内存里的子会话 + 磁盘上未加载的。重启后不先点开也要能列出。"""
        out: list[dict] = []
        seen: set[str] = set()
        for t in list(self.b.threads.values()):
            if t.meta.get("orchestrator_parent_id") != parent_id:
                continue
            run = _last_run(t.meta)
            out.append({"id": t.id, "title": t.meta.get("title") or "新对话", "status": t.status,
                        "terminal_reason": run.get("terminal_reason"), "run_id": run.get("id"),
                        "cwd": t.meta.get("cwd", ""), "mode": t.meta.get("mode", ""),
                        "worktree": t.meta.get("worktree"),
                        "last_message": _last_assistant(t.items),
                        "created": t.meta.get("created"), "updated": t.meta.get("updated")})
            seen.add(t.id)
        for p in SessionStore.list_sessions(self.b.home):
            sid = p.stem
            if not is_thread_id(sid) or sid in seen or sid in self.b.threads:
                continue
            try:
                s = SessionStore.summary(p)
            except Exception:  # noqa: BLE001  单个坏文件不影响列表
                continue
            if s.get("orchestrator_parent_id") != parent_id:
                continue
            run = _last_run({"runs": s.get("runs")})
            out.append({"id": sid, "title": s.get("title") or "新对话", "status": "idle",
                        "terminal_reason": run.get("terminal_reason"), "run_id": run.get("id"),
                        "cwd": s.get("cwd", ""), "mode": s.get("mode", ""),
                        "worktree": s.get("worktree"), "last_message": "",
                        "created": s.get("created"), "updated": s.get("updated")})
            seen.add(sid)
        out.sort(key=lambda x: x.get("created") or 0)
        return out

    def _own_child(self, parent_id: str, child_id: str):
        """只认 orchestrator_parent_id；不是自己的、或不存在，报同一个错。"""
        if not is_thread_id(child_id):
            raise OrchestratorError("不是本会话创建的子会话")
        t = self.b.threads.get(child_id)
        if t is None:
            from ee.server.app import RpcError
            try:
                t = self.b._thread(child_id)
            except RpcError as e:
                raise OrchestratorError("不是本会话创建的子会话") from e
        if t.meta.get("orchestrator_parent_id") != parent_id:
            raise OrchestratorError("不是本会话创建的子会话")
        return t

    # ------------------------------------------------------------ 查询
    def status(self, parent_id: str, child_id: str) -> dict:
        t = self._own_child(parent_id, child_id)
        run = _last_run(t.meta)
        changed = 0
        wt = t.meta.get("worktree") or {}
        if wt.get("path"):
            from ee.core import worktree as W
            try:
                # 只读：W.status 走 git status，不执行 intent-to-add，不碰索引内容
                st = W.status(W._find(wt["path"], self.b.home))
                changed = int(st.get("changed") or 0) + int(st.get("untracked") or 0)
            except Exception:  # noqa: BLE001  worktree 被删/仓库坏了不影响状态查询
                changed = 0
        else:
            cwd = Path(t.meta.get("cwd") or "")
            if cwd.is_dir():
                from ee.core import gitinfo
                try:
                    st = gitinfo.status(cwd)
                    changed = st.changed + st.untracked
                except Exception:  # noqa: BLE001
                    changed = 0
        return {"id": t.id, "title": t.meta.get("title") or "新对话", "status": t.status,
                "terminal_reason": run.get("terminal_reason"), "run_id": run.get("id"),
                "mode": t.meta.get("mode", ""), "cwd": t.meta.get("cwd", ""),
                "worktree": t.meta.get("worktree"), "changed_count": changed,
                "last_message": _last_assistant(t.items),
                "usage": dict(t.usage or {}), "created": t.meta.get("created"),
                "updated": t.meta.get("updated")}

    async def wait(self, parent_id: str, child_ids: list[str], timeout: float | None = None) -> dict:
        """等到子会话都不在运行，或超时。取消等待不等于停止子任务。"""
        if not child_ids:
            raise OrchestratorError("没有要等待的子会话")
        kids = [self._own_child(parent_id, cid) for cid in child_ids]
        limit = min(max(1.0, float(timeout if timeout is not None else WAIT_DEFAULT)), WAIT_LIMIT)
        deadline = time.monotonic() + limit
        timed_out = False
        while True:
            if not any(k.running for k in kids):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            await asyncio.sleep(0.1)
        return {"timed_out": timed_out,
                "threads": [self.status(parent_id, k.id) for k in kids]}

    # ------------------------------------------------------------ 供 U6 复用
    def forget(self, child_id: str) -> None:
        """子会话被删除：回收名额占用。"""
        ctl = getattr(self.b, "slots", None)
        if ctl is not None:
            ctl.forget(child_id)
