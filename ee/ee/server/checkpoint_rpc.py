"""检查点时间线与按块审阅 RPC（D5-7）。

  checkpoint/list    {thread_id}                                  → {available, reason?, checkpoints: [...]}
  checkpoint/diff    {thread_id, checkpoint_id, mode?}             → {files, additions, deletions, from, to}
                     mode = turn（这一回合 前→后，默认）| before / after（当前 → 恢复到回合前/后，即恢复会改什么）
                     | current（恢复点 → 当前）；也可直接给 from_tree / to_tree（必须是这个会话的检查点，空=当前）
  checkpoint/restore {thread_id, checkpoint_id, side?}             → {restored, restore_point}
                     side = before（默认）| after；恢复前先把当前工作区存成“恢复点”，可再恢复回来反悔
  checkpoint/gc      {force?}                                      → 清理统计
  review/hunk        {cwd, path, hunk_id, action, thread_id?}       → {path, action}
                     action = accept | unaccept | reject；git 项目按 HEAD，非 git 项目按会话开始的快照
git/diff、git/revert_file 加了可选 thread_id：非 git 项目时改用会话的磁盘快照（会话开始 → 当前）。
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path

from ee.core import checkpoints as C
from ee.core import hunks
from ee.core.snapshot import RestoreBlocked, RestorePartial

MAX_RESTORE_POINTS = 50


def turn_entries(t) -> list[tuple[int, dict]]:
    """(第几回合, 用户条目)，只要有回合前 tree 的。回合号按对话里用户消息的顺序。"""
    out = []
    n = 0
    for it in t.items:
        if it.get("type") != "user" or it.get("queued"):
            continue
        n += 1
        if it.get("tree"):
            out.append((n, it))
    return out


def session_base(t) -> str | None:
    """会话开始时的 tree（第一个有快照的回合的回合前）。"""
    ents = turn_entries(t)
    return ents[0][1]["tree"] if ents else None


def add_restore_point(t, tree: str, label: str) -> dict:
    """把当前工作区 tree 存成会话的“恢复点”（时间线里可以恢复回来反悔），返回恢复点。"""
    users = [it for _, it in turn_entries(t)]
    rp = {"id": "rp-" + secrets.token_hex(6), "ts": time.time(), "tree": tree, "label": label,
          "undone": {it["id"]: bool(it.get("undone")) for it in users}}
    t.meta["restore_points"] = ([*(t.meta.get("restore_points") or []), rp])[-MAX_RESTORE_POINTS:]
    t.save_meta()
    return rp


def restore_point_view(rp: dict | None) -> dict | None:
    return None if not rp else {"id": rp["id"], "kind": "restore", "ts": rp["ts"], "text": rp["label"]}


def register(methods: dict, backend) -> None:
    from ee.server import gitops
    from ee.server.app import RpcError

    async def _session(thread_id: str):
        t = await backend._athread(thread_id)
        try:
            s = await t.ensure_session_async()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        return t, s

    def _disk_or_raise(s):
        d = s.disk()
        if d is None:
            ds = s._disk_obj
            raise RpcError("这个工作区没有磁盘快照" + (f"（{ds.disabled}）" if ds is not None and ds.disabled else "（已在设置里关闭）"))
        return d

    def _find(t, checkpoint_id: str) -> tuple[str, int, dict]:
        for n, it in turn_entries(t):
            if it.get("id") == checkpoint_id:
                return "turn", n, it
        for rp in t.meta.get("restore_points") or []:
            if rp.get("id") == checkpoint_id:
                return "restore", 0, rp
        raise RpcError("找不到这个检查点（可能已被清理或会话被编辑重发过）")

    def _known_trees(t) -> set[str]:
        out = set()
        for _, it in turn_entries(t):
            out |= {x for x in (it.get("tree"), it.get("tree_after")) if x}
        out |= {rp["tree"] for rp in t.meta.get("restore_points") or [] if rp.get("tree")}
        return out

    # ------------------------------------------------------------ list
    def _list(t, s) -> dict:
        d = s.disk()
        if d is None:
            ds = s._disk_obj
            return {"available": False, "reason": (ds.disabled if ds is not None and ds.disabled else "磁盘快照已关闭"),
                    "checkpoints": []}
        ents = turn_entries(t)
        rps = [rp for rp in t.meta.get("restore_points") or [] if rp.get("tree")]
        alive = d.existing([x for _, it in ents for x in (it.get("tree"), it.get("tree_after")) if x]
                           + [rp["tree"] for rp in rps])
        out = []
        last_id = ents[-1][1].get("id") if ents else None
        for n, it in ents:
            before, after = it.get("tree"), it.get("tree_after")
            running = bool(t.running and it.get("id") == last_id and not after)
            stat = None
            if before in alive and after in alive:
                try:
                    stat = d.numstat(before, after)
                except Exception:  # noqa: BLE001
                    stat = None
            out.append({"id": it["id"], "kind": "turn", "turn": n, "ts": it.get("ts"),
                        "text": C.summarize(it.get("text") or ""), "before": before, "after": after,
                        "undone": bool(it.get("undone")), "running": running, "stat": stat,
                        "expired": before not in alive, "after_expired": bool(after) and after not in alive})
        for rp in rps:
            out.append({"id": rp["id"], "kind": "restore", "ts": rp.get("ts"), "text": rp.get("label") or "恢复前的文件",
                        "before": rp["tree"], "after": None, "undone": False, "running": False, "stat": None,
                        "expired": rp["tree"] not in alive, "after_expired": False})
        out.sort(key=lambda e: e.get("ts") or 0)
        return {"available": True, "checkpoints": out}

    async def list_(thread_id: str) -> dict:
        t, s = await _session(thread_id)
        return await asyncio.to_thread(_list, t, s)

    # ------------------------------------------------------------ diff
    def _diff(t, s, checkpoint_id, mode, from_tree, to_tree) -> dict:
        d = _disk_or_raise(s)
        if checkpoint_id:
            kind, _, ent = _find(t, checkpoint_id)
            before = ent.get("tree")
            after = ent.get("tree_after") if kind == "turn" else None
            if mode == "turn" and kind == "turn":
                a, b = before, after          # after 为空（回合进行中/中断）= 当前
            elif mode in ("before", "current") or kind == "restore":
                a, b = None, before           # 当前 → 回合前（恢复会改什么）；恢复点同理
                if mode == "current":
                    a, b = before, None
            elif mode == "after":
                if not after:
                    raise RpcError("这一回合还没有结束后的快照")
                a, b = None, after
            else:
                raise RpcError(f"未知 mode：{mode}")
        else:
            known = _known_trees(t)
            for x in (from_tree, to_tree):
                if x and x not in known:
                    raise RpcError("只能对比这个会话自己的检查点")
            if not from_tree and not to_tree:
                raise RpcError("缺少 checkpoint_id 或 from_tree")
            a, b = from_tree, to_tree
        cur = None
        if not a or not b:
            cur = d.capture()
            if not cur:
                raise RpcError("抓取当前工作区快照失败")
        a, b = a or cur, b or cur
        if d.existing([a, b]) != {a, b}:
            raise RpcError("这个检查点已被清理（超过保留期限）")
        files = gitops.split_diff(hunks.display(d.diff_text(a, b)))
        for f in files:
            f["hunks"] = []
        return {"files": files, "from": a, "to": b,
                "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}

    async def diff(thread_id: str, checkpoint_id: str | None = None, mode: str = "turn",
                   from_tree: str | None = None, to_tree: str | None = None) -> dict:
        t, s = await _session(thread_id)
        return await asyncio.to_thread(_diff, t, s, checkpoint_id, mode, from_tree, to_tree)

    # ------------------------------------------------------------ restore
    def _restore(t, s, checkpoint_id: str, side: str) -> dict:
        d = _disk_or_raise(s)
        kind, n, ent = _find(t, checkpoint_id)
        if kind == "turn":
            target = ent.get("tree") if side == "before" else ent.get("tree_after")
            if side not in ("before", "after"):
                raise RpcError(f"未知 side：{side}")
            if not target:
                raise RpcError("这一回合还没有结束后的快照")
            label = f"第 {n} 回合{'之前' if side == 'before' else '之后'}"
        else:
            target = ent["tree"]
            label = "恢复点"
        if d.existing([target]) != {target}:
            raise RpcError("这个检查点已被清理（超过保留期限），无法恢复")
        cur = d.capture()
        if not cur:
            raise RpcError("抓取当前工作区快照失败，没有恢复")
        paths = d.changed(target, cur)
        if not paths:
            return {"restored": [], "restore_point": None, "label": label}
        users = [it for _, it in turn_entries(t)]
        rp = add_restore_point(t, cur, f"执行「恢复到{label}」前的文件")
        try:
            restored = d.restore(target, paths)
        except RestoreBlocked as e:          # 预检没过：一个文件都没动，刚存的恢复点也撤掉
            t.meta["restore_points"] = [x for x in t.meta.get("restore_points") or [] if x.get("id") != rp["id"]]
            t.save_meta()
            raise RpcError(f"恢复失败：{e}\n关掉占用这些文件的程序后再试。")
        except RestorePartial as e:          # 恢复到一半：如实报告；恢复点保留，可以恢复回去
            s._after_restore(e.restored, f"[系统提示] 用户在检查点时间线里把工作区文件恢复到了{label}，但只恢复了一部分，以下文件已变化（如需再改请重新 read）：")
            rel = [os.path.relpath(p, d.worktree).replace("\\", "/") for p in e.restored]
            t._add({"type": "notice", "level": "warn",
                    "text": f"恢复到{label}只完成了一部分：{e}\n恢复前的状态已存为检查点，可在时间线里恢复回去。"})
            return {"restored": rel, "label": label, "partial": True,
                    "failed": [{"path": p, "reason": why} for p, why in e.failed],
                    "restore_point": restore_point_view(rp)}
        # 同步“已撤销”标记，/undo 之后按新状态继续
        if kind == "turn":
            for m, it in turn_entries(t):
                if m < n:
                    continue
                want = True if side == "before" else m > n     # 恢复到回合前：这回合及之后都算撤销；回合后：之后的算
                if bool(it.get("undone")) != want:
                    it["undone"] = want
                    t._update(it)
        else:
            saved = ent.get("undone") or {}
            for it in users:
                if it["id"] in saved and bool(it.get("undone")) != saved[it["id"]]:
                    it["undone"] = saved[it["id"]]
                    t._update(it)
        s.turn_trees = [{"before": it["tree"], "after": it.get("tree_after"), "undone": bool(it.get("undone"))}
                        for it in t.items if it.get("type") == "user" and it.get("tree")]
        s._after_restore(restored, f"[系统提示] 用户在检查点时间线里把工作区文件恢复到了{label}，以下文件已变化（如需再改请重新 read）：")
        rel = [os.path.relpath(p, d.worktree).replace("\\", "/") for p in restored]
        t._add({"type": "notice", "level": "info",
                "text": f"已把文件恢复到{label}（{len(rel)} 个文件）。恢复前的状态已存为检查点，可在时间线里恢复回来。"})
        return {"restored": rel, "label": label,
                "restore_point": restore_point_view(rp)}

    async def restore(thread_id: str, checkpoint_id: str, side: str = "before") -> dict:
        t, s = await _session(thread_id)
        if t.running or s.running:
            raise RpcError("运行中不能恢复，先停止这一回合")
        res = await asyncio.to_thread(_restore, t, s, checkpoint_id, side)
        if res["restored"]:
            backend.notify("git/changed", {"cwd": t.meta.get("cwd")})
            backend.forget_reads(t.meta.get("cwd") or "")
        return res

    async def gc(force: bool = True) -> dict:
        from ee.config import load_config
        cfg = await asyncio.to_thread(load_config, Path.home())
        days, turns = C.keep_settings(cfg.data)
        return await asyncio.to_thread(C.gc_all, backend.home, days, turns, bool(force))

    # ------------------------------------------------------------ 非 git 审阅 + 按块
    async def _snapshot_ctx(thread_id: str):
        t, s = await _session(thread_id)
        d = s.disk()
        base = session_base(t)
        return t, s, d, base

    async def hunk(cwd: str, path: str, hunk_id: str, action: str, thread_id: str | None = None) -> dict:
        if not Path(cwd).is_dir():
            raise RpcError(f"目录不存在：{cwd}")
        from ee.core import gitinfo
        is_repo = (await asyncio.to_thread(gitinfo.status, Path(cwd))).is_repo
        try:
            if is_repo:
                res = await asyncio.to_thread(gitops.hunk_git, Path(cwd), path, hunk_id, action, backend.home)
            else:
                if not thread_id:
                    raise RpcError("非 git 项目需要会话的快照")
                t, s, d, base = await _snapshot_ctx(thread_id)
                if d is None or not base:
                    raise RpcError("这个会话还没有快照，无法按块操作")
                if t.running:
                    raise RpcError("运行中不能改文件，先停止这一回合")
                res = await asyncio.to_thread(gitops.hunk_snapshot, d, base, path, hunk_id, action, backend.home)
        except ValueError as e:
            raise RpcError(str(e))
        if action == "reject":
            backend.forget_reads(cwd)
        return res

    methods.update({"checkpoint/list": list_, "checkpoint/diff": diff, "checkpoint/restore": restore,
                    "checkpoint/gc": gc, "review/hunk": hunk})
    backend.snapshot_review_ctx = _snapshot_ctx
