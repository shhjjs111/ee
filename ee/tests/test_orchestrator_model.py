"""D6-1 U1：指挥关系字段。

`parent_id` 已经被普通会话分叉占用（server/branch.py 写它、左栏用它显示分叉图标）。
如果编排关系复用它，普通 fork 会被当成"受控子会话"，权限判断和树形展示都会错。
所以控制关系单独一个字段，并且在 summary 里明确区分。

本文件只钉数据层：字段存在、两者不混、重启后还在。
"""
import asyncio

import pytest

from ee.server.app import Backend


def test_summary_exposes_orchestrator_fields(ee_home, workspace):
    """meta 里有编排字段时，summary 要原样给出。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        t.meta["orchestrator_parent_id"] = "20260101-000000-aaaaaa"
        t.meta["orchestrator_role"] = "child"
        t.save_meta()

        s = t.summary()
        assert s["orchestrator_parent_id"] == "20260101-000000-aaaaaa"
        assert s["orchestrator_role"] == "child"

    asyncio.run(go())


def test_summary_defaults_are_none(ee_home, workspace):
    """没有编排关系时给 None，不是空串——调用方靠它判断"是不是子会话"。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        s = t.summary()
        assert s["orchestrator_parent_id"] is None
        assert s["orchestrator_role"] is None

    asyncio.run(go())


def test_fork_parent_id_is_not_orchestrator_relation(ee_home, workspace):
    """普通分叉只写 parent_id，不能被当成受控子会话。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        t.meta["parent_id"] = "20260101-000000-bbbbbb"     # 模拟 server/branch.py 的 fork
        t.save_meta()

        s = t.summary()
        assert s["parent_id"] == "20260101-000000-bbbbbb"
        assert s["orchestrator_parent_id"] is None

    asyncio.run(go())


def test_relation_survives_restart(ee_home, workspace):
    """关系要随会话落盘，重启后 thread/get 还能读到。"""
    async def go():
        b1 = Backend(home=ee_home, persist=True)
        th = (await b1.call("thread/start", {"cwd": str(workspace)}))["thread"]
        tid = th["id"]
        t = b1.threads[tid]
        t.meta["orchestrator_parent_id"] = "20260101-000000-cccccc"
        t.meta["orchestrator_role"] = "child"
        t.save_meta()

        b2 = Backend(home=ee_home, persist=True)      # 冷缓存，从磁盘读
        d = (await b2.call("thread/get", {"thread_id": tid}))["thread"]
        assert d["orchestrator_parent_id"] == "20260101-000000-cccccc"
        assert d["orchestrator_role"] == "child"

    asyncio.run(go())


def test_orchestrator_role_is_not_a_permission(ee_home, workspace):
    """role=child 不能顺带改变权限模式——字段是关系标签，不是授权依据。"""
    async def go():
        b = Backend(home=ee_home, persist=True)
        th = (await b.call("thread/start", {"cwd": str(workspace), "mode": "read-only"}))["thread"]
        t = b.threads[th["id"]]
        t.meta["orchestrator_role"] = "child"
        t.save_meta()
        assert t.summary()["mode"] == "read-only"

    asyncio.run(go())


# ---------------------------------------------------------------- U2：Run 记录与终态


from conftest import FakeClient, finish, text_chunks          # noqa: E402
from ee.core.providers.openai_chat import ChatClient          # noqa: E402


def _backend(home, script, delay=0.0):
    fake = FakeClient(script, delay=delay)
    return Backend(home=home, persist=True,
                   client_factory=lambda spec: ChatClient(spec, client=fake)), fake


async def _idle(b, tid, timeout=20.0):
    t = b.threads[tid]
    for _ in range(int(timeout / 0.02)):
        if not t.running:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("回合没结束")


def test_run_records_terminal_reason(ee_home, workspace):
    """回合结束后，run 记录要说明它是怎么结束的——idle 本身没有信息量。"""
    async def go():
        b, fake = _backend(ee_home, [text_chunks("好了") + finish("stop")])
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": th["id"], "text": "干活"})
        await _idle(b, th["id"])

        runs = b.threads[th["id"]].meta["runs"]
        assert len(runs) == 1
        assert runs[0]["terminal_reason"] == "completed"
        assert runs[0]["status"] == "done"
        assert runs[0]["id"].startswith("run_")
        assert b.threads[th["id"]].status == "idle"

    asyncio.run(go())


def test_idle_is_not_completed(ee_home, workspace):
    """被中断的回合：状态回到 idle，但终态必须是 interrupted（不能显示成完成）。"""
    async def go():
        b, fake = _backend(ee_home, [text_chunks("慢慢来", n=1) + finish("stop")], delay=0.2)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": th["id"], "text": "长任务"})
        await asyncio.sleep(0.05)
        await b.call("turn/interrupt", {"thread_id": th["id"]})
        await _idle(b, th["id"])

        t = b.threads[th["id"]]
        assert t.status == "idle"
        assert t.meta["runs"][-1]["terminal_reason"] == "interrupted"

    asyncio.run(go())


def test_run_ids_are_unique_per_dispatch(ee_home, workspace):
    async def go():
        b, fake = _backend(ee_home, [text_chunks("一") + finish("stop"),
                                     text_chunks("二") + finish("stop")])
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        for text in ("第一次", "第二次"):
            await b.call("turn/start", {"thread_id": th["id"], "text": text})
            await _idle(b, th["id"])
        runs = b.threads[th["id"]].meta["runs"]
        assert len(runs) == 2 and runs[0]["id"] != runs[1]["id"]
        assert all(r["terminal_reason"] == "completed" for r in runs)

    asyncio.run(go())


def test_run_history_survives_restart(ee_home, workspace):
    async def go():
        b, fake = _backend(ee_home, [text_chunks("好了") + finish("stop")])
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": th["id"], "text": "干活"})
        await _idle(b, th["id"])

        b2 = Backend(home=ee_home, persist=True)
        d = (await b2.call("thread/get", {"thread_id": th["id"]}))["thread"]
        assert d["runs"][-1]["terminal_reason"] == "completed"

    asyncio.run(go())


def test_run_history_is_capped(ee_home, workspace):
    """meta 不能无限长：只保留最近若干条。"""
    async def go():
        b, fake = _backend(ee_home, [text_chunks("x") + finish("stop")] * 6)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        for i in range(6):
            t._new_run(f"第 {i} 次")
            t._close_run("completed")
        assert len(t.meta["runs"]) <= 5

    asyncio.run(go())
