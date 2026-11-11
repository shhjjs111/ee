"""会话记录写盘失败必须让用户看得见（backlog：原先只写一条日志，界面无感知）。

磁盘满 / 杀软占用 / 权限问题时，对话看起来一切正常，重启才发现整段丢了：
- `SessionStore` 记账（failed / last_error），并通过 `on_fail` 通知挂上来的上层；
- `Thread` 把它变成对话里的一条警告条目 + `summary()["save_error"]`
  （桌面端据此显示横幅与左栏图标，见 Conversation.tsx / Sidebar.tsx）；
- 一直失败时节流，否则每条记录都弹一次，界面会被淹掉。

`test_store_append_failure_is_reported_not_swallowed`（test_session_lifecycle.py）
只守到「有日志」这一层，这里守到用户那一层。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ee.server.app import Backend


def _break_sessions(home: Path) -> None:
    """把 `sessions` 目录换成同名普通文件：mkdir / open 都会抛 OSError。

    比 monkeypatch `builtins.open` 更接近真实故障（磁盘满、目录被占），
    也不会让无关的读取路径一起炸掉。
    """
    d = home / "sessions"
    if d.is_dir():
        shutil.rmtree(d)
    elif d.exists():
        d.unlink()
    d.write_text("这不是目录", encoding="utf-8")


def test_store_records_failure_and_calls_hook(tmp_path):
    """失败的次数、原因、回调都要有：调用方不能只依赖日志。"""
    from ee.core.store import SessionStore

    seen: list[tuple[int, str]] = []
    st = SessionStore(tmp_path)
    st.on_fail = lambda store, err: seen.append((store.failed, str(err)))
    _break_sessions(tmp_path)

    st.append("message", {"message": {"role": "user", "content": "hi"}})
    st.append("item", {"item": {"id": "us_1", "type": "user", "text": "hi"}})

    assert st.failed == 2, "写失败没有被记账"
    assert st.last_error and "Error" in st.last_error, f"没记下失败原因：{st.last_error!r}"
    assert [n for n, _ in seen] == [1, 2], f"on_fail 没被调用/次数不对：{seen}"


def test_thread_surfaces_save_failure_to_the_ui(ee_home, workspace):
    """写盘失败要出现在对话条目与会话摘要里（界面横幅/左栏图标读它们）。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        _break_sessions(ee_home)
        seen: list[dict] = []
        b.listeners.append(lambda m, p: seen.append(p) if m == "thread/updated" else None)

        d = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]        # 建会话时就要写 meta
        tid = d["id"]
        assert d.get("save_error"), f"会话详情里没有 save_error：{d.get('save_error')}"
        assert "磁盘" in (d["save_error"]["error"] or "") or d["save_error"]["count"] >= 1

        # 对话里能看到一条警告（否则用户只在左栏一个角标，不知道发生了什么）
        notices = [it for it in d["items"] if it.get("type") == "notice"]
        assert notices, f"对话里没有写盘失败的提示条目：{[it.get('type') for it in d['items']]}"
        assert notices[0]["level"] == "warn"
        assert "保存" in notices[0]["text"] and "磁盘" in notices[0]["text"], notices[0]["text"]

        # 左栏走 thread/list：已加载的会话用 summary()，也要带上这个状态
        row = next(t for t in (await b.call("thread/list", {}))["threads"] if t["id"] == tid)
        assert row.get("save_error"), "thread/list 里丢了 save_error：左栏就看不到「保存不了」"

        # 会话记录（message 这条腿）也要覆盖：它走 session.store，不是 Thread 的元数据 store
        t = b.threads[tid]
        s = await t.ensure_session_async()
        assert s.store.on_fail is not None, "会话的 store 没挂上失败上报钩子"
        before = (t.save_error or {}).get("count", 0)
        s.store.append("message", {"message": {"role": "user", "content": "再说一句"}})
        assert t.save_error and t.save_error["count"] > before, (
            "对话消息写失败没被转到线程上：重启后丢的就是这部分"
        )
        assert any(p.get("thread", {}).get("id") == tid for p in seen), "没有广播 thread/updated"

    asyncio.run(go())


def test_recovery_clears_the_flag(ee_home, workspace):
    """磁盘腾出空间后，失败状态要能自己消失（否则警告永远挂着）。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        _break_sessions(ee_home)
        d = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[d["id"]]
        assert t.save_error, "前置：先得有一次写失败"

        broken = ee_home / "sessions"      # 用户清磁盘：目录恢复正常
        broken.unlink()
        broken.mkdir(parents=True)

        s = await t.ensure_session_async()
        s.store.append("message", {"message": {"role": "user", "content": "恢复后"}})
        assert t.save_error is None, f"恢复写盘后失败状态没清掉：{t.save_error}"
        infos = [it for it in t.items if it.get("type") == "notice" and it.get("level") == "info"]
        assert infos, "恢复后应该告诉用户「现在关掉也不会丢」"
        assert s.store.failed == 0 and s.store.last_error is None

    asyncio.run(go())


def test_repeated_failures_are_throttled(ee_home, workspace):
    """一直失败时不能每条记录都插一条提示（把界面淹掉），但状态要持续更新。"""
    from ee.core.store import SessionStore

    async def go():
        b = Backend(home=ee_home, persist=True)
        _break_sessions(ee_home)
        d = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[d["id"]]

        def notices() -> list[dict]:
            return [it for it in t.items if it.get("type") == "notice"]

        assert len(notices()) == 1, f"首次失败应该有一条提示：{len(notices())}"

        st = SessionStore(ee_home)
        for _ in range(5):
            st.failed += 1
            t._on_store_fail(st, OSError(28, "磁盘已满"))
        assert len(notices()) == 1, "反复失败不该每次都插一条提示"
        assert t.save_error and t.save_error["count"] >= 5, "计数必须继续涨（界面要显示「已失败 N 次」）"

        for _ in range(30):                      # 超过节流间隔后应再报一次
            st.failed += 1
            t._on_store_fail(st, OSError(28, "磁盘已满"))
        assert len(notices()) == 2, f"节流间隔过了却没有再提示：{len(notices())}"

    asyncio.run(go())
