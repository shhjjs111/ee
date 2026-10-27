"""分叉 / 编辑重发（D3-E）。

  thread/fork        {thread_id, item_id}               → {thread, draft}
      新建会话，复制该用户消息之前的历史与条目（meta.parent_id 记来源），draft = 那条消息原文，
      界面把它预填进新会话输入框。不动文件。
  thread/edit_resend {thread_id, item_id, text, revert_files=true, model?, mode?, force?}
                     → {thread, restored, files_reverted, restore_point}
      在原会话里把该用户消息及之后的历史和条目丢掉，（可选）把那之后 ee 改过的文件恢复，再用新文字重新发送。
      要恢复的文件在回合后被用户手改过：返回 {needs_confirm: true, conflicts: [相对路径]}，历史和文件都不动；
      界面确认后带 force=true 再调，先把当前工作区存成恢复点（时间线里可反悔）再恢复。

定位：用户条目在回合开始时记下 hist（在 session.history 里的下标）和 snap（文件快照层号）。
调用前用 history[hist] 的原文核对；对不上（被上下文压缩过、或是老会话没有 hist）就报错。
文件快照只在内存里：后端重启过的回合无法恢复文件（files_reverted=false，界面提示）。
"""
from __future__ import annotations

import asyncio
import copy
import time

from ee.core.store import SessionStore, _title, strip_context


class BranchError(ValueError):
    pass


def _locate(t, item_id: str) -> tuple[dict, int]:
    s = t.session
    it = t._by_id.get(item_id)
    if it is None or it.get("type") != "user":
        raise BranchError("找不到这条用户消息")
    if it.get("queued"):
        raise BranchError("这条消息是在回合进行中插入的，不能单独编辑或分叉")
    hist = it.get("hist")
    if not isinstance(hist, int):
        raise BranchError("这条消息来自旧版本的会话，不支持编辑或分叉")
    got = s.user_text_at(hist)
    want = strip_context(it.get("text") or "").strip()
    # history 里的用户消息可能在前面拼了备注（!命令输出、/undo 提示），所以只要求以原文结尾
    if got is None or not strip_context(got).strip().endswith(want):
        raise BranchError("这条消息之前的上下文已经被压缩过，不能再编辑或分叉")
    return it, hist


def register(methods: dict, backend) -> None:
    from ee.core.session import RestoreConflict
    from ee.core.snapshot import RestoreBlocked, RestorePartial
    from ee.server.app import RpcError
    from ee.server.checkpoint_rpc import add_restore_point, restore_point_view
    from ee.server.threads import Thread

    async def _ready(thread_id: str):
        t = await backend._athread(thread_id)
        try:
            await t.ensure_session_async()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        if t.running or t.compacting or t.session.running:
            raise RpcError("正在运行中，等这一回合结束再操作")
        return t

    async def fork(thread_id: str, item_id: str) -> dict:
        t = await _ready(thread_id)
        try:
            it, hist = _locate(t, item_id)
        except BranchError as e:
            raise RpcError(str(e))
        cut = int(it.get("seq", 0))

        def build():
            s = t.session
            messages = copy.deepcopy(s.history[:hist])
            items = [copy.deepcopy(x) for x in t.items if int(x.get("seq", 0)) < cut]
            store = SessionStore(backend.home, enabled=backend.persist)
            from ee.core.attachments import attachments_dir, rebase
            old_dir, new_dir = attachments_dir(backend.home, t.id), attachments_dir(backend.home, store.id)
            for m in messages:
                if m.get("_images"):
                    m["_images"] = rebase(m["_images"], old_dir, new_dir)
            meta = {"cwd": t.meta["cwd"], "model": t.meta.get("model", ""), "mode": t.meta.get("mode", ""),
                    "title": "分叉：" + (t.meta.get("title") or _title(it.get("text") or "")),
                    "created": time.time(), "parent_id": t.id, "parent_item": item_id}
            for m in messages:
                store.message(m)
            for x in items:
                store.item(x)
            nt = Thread(store.id, backend.home, backend.notify, meta, messages=messages, items=items,
                        client_factory=backend.client_factory, persist=backend.persist,
                        user_home=backend.user_home)
            nt.ensure_session()
            nt.usage = {}
            nt.save_meta()
            # 附件目录（D3-D）随分叉一起复制
            src = backend.home / "sessions" / t.id / "attachments"
            if src.is_dir():
                import shutil
                shutil.copytree(src, backend.home / "sessions" / nt.id / "attachments", dirs_exist_ok=True)
            return nt

        nt = await asyncio.to_thread(build)
        backend.threads[nt.id] = nt
        backend.notify("thread/updated", {"thread": nt.summary()})
        return {"thread": nt.detail(), "draft": it.get("text") or ""}

    async def edit_resend(thread_id: str, item_id: str, text: str, revert_files: bool = True,
                          model: str | None = None, mode: str | None = None, force: bool = False) -> dict:
        text = (text or "").strip()
        if not text:
            raise RpcError("消息不能为空")
        t = await _ready(thread_id)
        try:
            it, hist = _locate(t, item_id)
        except BranchError as e:
            raise RpcError(str(e))
        s = t.session
        cut = int(it.get("seq", 0))
        restored: list[str] = []
        reverted = False
        box: dict = {}
        failed: list[tuple[str, str]] = []
        if revert_files:
            res = None
            if it.get("tree"):          # D4-1 磁盘快照：重启后也能恢复，含 shell 改的文件
                entries = [(x["tree"], x.get("tree_after")) for x in t.items
                           if x.get("type") == "user" and x.get("tree") and int(x.get("seq", 0)) >= cut]

                def keep(tree: str) -> None:
                    box["rp"] = add_restore_point(t, tree, "编辑重发前的文件（含你手改的内容）")

                try:
                    res = await asyncio.to_thread(s.revert_trees, entries, bool(force), keep)
                except RestoreConflict as e:   # 用户在回合后手改过要恢复的文件：先让界面确认，什么都不动
                    return {"thread": None, "restored": [], "files_reverted": False,
                            "needs_confirm": True, "conflicts": e.conflicts}
                except RestoreBlocked as e:    # 预检没过：文件、历史都不动
                    raise RpcError(f"编辑重发失败：{e}\n关掉占用这些文件的程序后再试。")
                except RestorePartial as e:    # 恢复到一半：照常重发，但如实列出没恢复的文件
                    res, failed = e.restored, e.failed
            snap = it.get("snap")
            if res is None and isinstance(snap, int) and it.get("snap_epoch") == s.snap_epoch:
                res = await asyncio.to_thread(s.revert_files_from, snap)
            if res is not None:
                restored, reverted = res, True
        s.truncate_to(hist)
        removed = [x["id"] for x in t.items if int(x.get("seq", 0)) >= cut]
        t.items = [x for x in t.items if int(x.get("seq", 0)) < cut]
        t._by_id = {x["id"]: x for x in t.items}
        s.turns = sum(1 for x in t.items if x.get("type") == "user")
        if t.persist:
            s.store.append("truncate", {"from_seq": cut})
        for rid in removed:
            t.notify("item/removed", {"thread_id": t.id, "item_id": rid})
        if revert_files and not reverted:
            t._add({"type": "notice", "level": "warn",
                    "text": "这条消息之后的文件改动无法自动恢复（后端重启过，快照已丢失），如需恢复请用 git。"})
        elif failed:
            t._add({"type": "notice", "level": "warn",
                    "text": "编辑重发：这些文件没能恢复（可能被其他程序占用），仍是改动后的内容：\n"
                            + "\n".join(f"{p}（{why}）" for p, why in failed)
                            + ("\n已恢复：\n" + "\n".join(restored) if restored else "")})
        elif restored:
            t._add({"type": "notice", "level": "info", "text": "编辑重发：已恢复这些文件：\n" + "\n".join(restored)})
        t.start_turn(text, model=model, mode=mode)
        return {"thread": t.detail(), "restored": restored, "files_reverted": reverted,
                "failed": [{"path": p, "reason": why} for p, why in failed],
                "restore_point": restore_point_view(box.get("rp"))}

    methods.update({"thread/fork": fork, "thread/edit_resend": edit_resend})
