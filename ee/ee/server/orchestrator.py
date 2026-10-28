"""指挥会话的服务实现（D6-1 U5/U6）。

铁律（与 docs/plans 的不变量一一对应）：

1. 只能操作 `orchestrator_parent_id == 当前会话` 的子会话。普通分叉的 `parent_id`
   不是控制关系，不能拿来授权。
2. 子会话不能再建子会话（由工具表层保证，这里再兜一层）。
3. 子会话能力不超过父会话（`orchestrator_cap`）。
4. 名额在**发起运行**时预留（`orchestrator_slots`），create 与 send 共用。
5. 查询不改 Git 索引：改动统计只读 `git status` 的计数，不调 `W.diff()`。

U6 补上的两处接线（U5 只做了服务层，工具拿不到句柄）：

* `Backend.slots` 由本模块按父会话的生效配置**惰性构造**；在此以前它一直是 None，
  `forget()` 静默什么都不做，名额既不拦也不放。
* 名额的释放点在 `Thread._finish`（回合真正结束），不靠 notify——`Backend.notify`
  对 listener 异常是 `except Exception: pass`，用通知释放会静默泄漏名额。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from ee.config import load_config
from ee.core.ids import is_thread_id
from ee.core.orchestrator_cap import CapabilityError, ChildCapability, child_capability
from ee.core.orchestrator_slots import SlotController, SlotLimit, controller_from_config
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


def _dedup(seq) -> list[str]:
    return list(dict.fromkeys(str(x) for x in (seq or []) if str(x)))


def child_configure(cap: ChildCapability) -> Callable:
    """建子会话前把父会话的有效规则写进它的配置（与 headless.unattended_configure 同一手法）。

    子会话在 worktree 里会重新 load_config，读到的项目/用户配置与父相同；这一步补的是
    **父会话运行期才有的**规则（用户点了"以后都行"、父会话临时收紧），以及 cap 算出的上限。
    """
    allow, ask, deny = _dedup(cap.allow), _dedup(cap.ask), _dedup(cap.deny)

    def configure(cfg) -> None:
        d = cfg.data
        perms = dict(d.get("permissions") or {})
        perms["allow"] = _dedup([*(perms.get("allow") or []), *allow])
        perms["ask"] = _dedup([*(perms.get("ask") or []), *ask])
        perms["deny"] = _dedup([*(perms.get("deny") or []), *deny])
        d["permissions"] = perms
        # 只读子会话不替父开沙箱：cap.sandbox 是父配置的副本，没开就是没开
        if cap.sandbox:
            d["sandbox"] = {**(d.get("sandbox") or {}), **cap.sandbox}
        if cap.network:
            d["network"] = {**(d.get("network") or {}), **cap.network}

    return configure


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
                        # 界面按它判断是不是子任务、缩进挂在哪个父会话下
                        "orchestrator_parent_id": t.meta.get("orchestrator_parent_id"),
                        "orchestrator_role": t.meta.get("orchestrator_role"),
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
                        "orchestrator_parent_id": s.get("orchestrator_parent_id"),
                        "orchestrator_role": s.get("orchestrator_role"),
                        "cwd": s.get("cwd", ""), "mode": s.get("mode", ""),
                        "worktree": s.get("worktree"), "last_message": "",
                        "created": s.get("created"), "updated": s.get("updated")})
            seen.add(sid)
        out.sort(key=lambda x: x.get("created") or 0)
        return out

    def _own_parent(self, parent_id: str):
        """调用者必须是内存里的真实会话，且自己不是别人的子会话（不许递归建孙）。"""
        if not is_thread_id(parent_id):
            raise OrchestratorError("调用者身份不合法")
        t = self.b.threads.get(parent_id)
        if t is None:
            raise OrchestratorError("当前会话不在内存里，无法派发")
        if t.meta.get("orchestrator_parent_id"):
            raise OrchestratorError("子会话不能再建子会话")
        return t

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

    # ------------------------------------------------------------ 名额
    def _controller(self, parent) -> SlotController:
        """按父会话的生效配置构造（只构造一次）。未启用时报错，不静默放行。"""
        ctl = getattr(self.b, "slots", None)
        if ctl is not None:
            return ctl
        cfg = parent.session.config if parent.session is not None else load_config(parent.cwd)
        ctl = controller_from_config(cfg.data)
        if ctl is None:
            raise OrchestratorError("指挥会话没有启用（在用户配置里打开 [orchestrator] enabled = true 后新开对话）")
        self.b.slots = ctl
        return ctl

    def release(self, child_id: str) -> None:
        """回合结束回收运行名额（子会话仍存在，总数占用保留）。"""
        ctl = getattr(self.b, "slots", None)
        if ctl is not None:
            ctl.release(child_id)

    def forget(self, child_id: str) -> None:
        """子会话被删除：回收名额占用。"""
        ctl = getattr(self.b, "slots", None)
        if ctl is not None:
            ctl.forget(child_id)

    # ------------------------------------------------------------ 写：创建
    async def create_child(self, parent_id: str, title: str = "", prompt: str | None = None,
                           mode: str | None = None, worktree: bool = True) -> dict:
        """预留名额 → 建 worktree（写型）→ 建 Thread → 写父关系。

        `orchestrator_parent_id` 只在这里由服务端写入，工具参数里没有 parent 字段（I1）。
        """
        parent = self._own_parent(parent_id)
        cfg = parent.session.config if parent.session is not None else load_config(parent.cwd)
        try:
            cap = child_capability(parent_mode=parent.meta.get("mode"),
                                   parent_policy=getattr(parent.session, "policy", None),
                                   data=cfg.data, requested_mode=mode, worktree=worktree)
        except CapabilityError as e:
            raise OrchestratorError(str(e)) from None

        ctl = self._controller(parent)
        child_id = SessionStore(self.b.home).id
        try:
            await ctl.reserve(child_id)      # 超限在这里拒绝，下面的东西一个都还没建
        except SlotLimit as e:
            raise OrchestratorError(str(e)) from None
        created = False
        try:
            wt = None
            cwd = parent.cwd
            if worktree:
                from ee.core import worktree as W
                try:
                    wt = await asyncio.to_thread(W.create_for_agent, parent.cwd, title or "子任务", self.b.home)
                except W.WorktreeError as e:
                    raise OrchestratorError(f"没能新建 worktree：{e}") from None
                cwd = Path(wt["cwd"])
            meta = {"cwd": str(cwd), "model": parent.meta.get("model") or "", "mode": cap.mode,
                    "title": title or "子任务", "created": time.time(),
                    # source 让模型生成标题那条路径放过它：派发者给的名字比首句摘要有用
                    "source": "orchestrator",
                    "orchestrator_parent_id": parent_id, "orchestrator_role": "child"}
            if wt:
                meta["worktree"] = {"path": wt["path"], "branch": wt["branch"], "repo": wt["repo"],
                                    "name": wt["name"]}
            from ee.server.threads import Thread
            t = Thread(child_id, self.b.home, self.b.notify, meta, client_factory=self.b.client_factory,
                       persist=self.b.persist, configure=child_configure(cap), user_home=self.b.user_home,
                       orchestrator=self)
            try:
                await t.ensure_session_async()   # 配置错误在这里暴露，不留半个会话
            except ValueError as e:
                raise OrchestratorError(f"子会话配置错误：{e}") from None
            t.save_meta()
            self.b.threads[child_id] = t
            created = True
            self.b.notify("thread/updated", {"thread": t.summary()})
        except BaseException:
            if not created:
                ctl.forget(child_id)          # 会话没建成：名额一起退回
            else:
                ctl.release(child_id)
            raise

        if not (prompt or "").strip():
            ctl.release(child_id)             # 只建不跑：不占运行名额
            return {"thread": t.summary(), "started": False}

        try:
            started = t.start_turn(prompt)
        except Exception:
            ctl.release(child_id)
            raise
        # 派发者给的标题优先。start_turn 会用首条消息重设标题——对普通会话是对的
        # （用户第一句话就是它的名字），但子会话在父列表里显示「改文档」比显示首句更有用。
        if title and t.meta.get("title") != title:
            t.meta["title"] = title
            t.save_meta()
            self.b.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.summary(), "started": not started.get("queued", False)}

    # ------------------------------------------------------------ 写：追加回合
    async def send(self, parent_id: str, child_id: str, text: str) -> dict:
        """给已有子会话发一回合。名额与 create 共用同一个控制器（I5）。"""
        t = self._own_child(parent_id, child_id)
        text = (text or "").strip()
        if not text:
            raise OrchestratorError("要发送的内容不能为空")
        if t.running:
            # 正在跑：走排队（不额外占名额，当前回合一结束就轮到它）
            return t.start_turn(text)
        ctl = self._controller(t)
        try:
            await ctl.reserve(child_id)
        except SlotLimit as e:
            raise OrchestratorError(str(e)) from None
        try:
            return t.start_turn(text)
        except Exception:
            ctl.release(child_id)
            raise

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
