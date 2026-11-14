"""等待用户输入的三个入口都要有超时（2026-11 审计修复）。

工具审批一直有 APPROVAL_TIMEOUT（窗口关掉 / 前端断线 → 300 秒后按拒绝继续），
但预算确认和 ask_user 是裸 `await fut`：窗口一关，整个回合挂在那里，_history_lock
不释放，下一条消息和 /compact 全堵在锁后，只能重启进程。
"""
import asyncio
from pathlib import Path

import pytest

import ee.core.session as session_mod
from ee.core.session import Session
from ee.tools.ask import AskUserTool
from ee.tools.base import ToolContext, ToolError, ToolState


# ---------------------------------------------------------------- ask_user

def test_ask_user_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 0.05)

    events: list = []
    ctx = ToolContext(cwd=Path.cwd(), state=ToolState(), output_dir=Path.cwd())
    ctx.extra["emit_event"] = events.append
    ctx.extra["call_id"] = "call_1"

    with pytest.raises(ToolError) as ei:
        asyncio.run(AskUserTool().run({"question": "选哪个？", "options": ["A", "B"]}, ctx))

    assert "没有收到" in str(ei.value), str(ei.value)
    assert events, "应该先把提问事件发出去"


def test_ask_user_still_returns_the_answer_when_it_arrives(monkeypatch):
    """守住正常路径：用户回答了就是那个答案，不该被超时逻辑影响。"""
    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 1.0)

    ctx = ToolContext(cwd=Path.cwd(), state=ToolState(), output_dir=Path.cwd())

    def emit(ev):
        # 界面立刻回答
        ev.future.set_result("A")

    ctx.extra["emit_event"] = emit
    ctx.extra["call_id"] = "call_2"

    res = asyncio.run(AskUserTool().run({"question": "选哪个？", "options": ["A", "B"]}, ctx))
    assert "A" in res.output


# ---------------------------------------------------------------- 预算确认

class _FakeGuard:
    active = True

    async def check(self, spent, warn, ask):
        return await ask("session", "本会话已花 ¥1.00，超过上限 ¥0.50")


class _BudgetSession(Session):
    def __init__(self):
        pass                      # 只跑 _budget_check 需要的两个入口

    @property
    def budget(self):
        return _FakeGuard()


def test_budget_confirm_times_out_to_stop(monkeypatch):
    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 0.05)

    events: list = []
    out = asyncio.run(_BudgetSession()._budget_check(events.append))

    assert out == "deny", "拿不到用户答复时不能继续花钱"
    texts = " ".join(str(getattr(e, "text", "") or getattr(e, "message", "")) for e in events)
    assert "没有回应" in texts, f"应当告诉用户是按超时停止的：{texts!r}"


def test_budget_confirm_still_honors_a_choice(monkeypatch):
    """守住正常路径：用户点了「继续这一回合」就返回 once。"""
    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 1.0)

    def emit(ev):
        if getattr(ev, "future", None) is not None:
            ev.future.set_result(type("D", (), {"choice": "once"})())

    assert asyncio.run(_BudgetSession()._budget_check(emit)) == "once"


# ---------------------------------------------------------------- 超时后卡片必须收尾

def test_approval_timeout_emits_dialog_ended(workspace, monkeypatch):
    """审批超时 → 必须发 DialogEnded，界面卡片不能停在「等待批准」。

    超时后 future 已作废：用户过一会儿再点「批准」，`approval/respond` 会把条目写成
    已批准，而工具早就按拒绝走完了——界面说的和实际发生的相反。
    """
    from conftest import make_session
    from ee.core.events import DialogEnded
    from ee.core.permissions import Decision

    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 0.05)
    s, _fake = make_session(workspace, [])

    class _Tool:
        name = "shell"
        read_only = False

    events: list = []
    dec = asyncio.run(s._ask("c1", _Tool(), {"command": "make deploy"},
                             Decision("ask", "需要确认"), events.append))
    assert dec.choice == "deny"
    ended = [e for e in events if isinstance(e, DialogEnded)]
    assert ended and ended[0].call_id == "c1" and ended[0].status == "cancelled", \
        f"超时没有发出 DialogEnded：{events}"


def test_dialog_ended_settles_pending_item(ee_home, workspace):
    """DialogEnded：条目改成 cancelled、从 approvals 表里摘掉，迟到的点击不能再改状态。"""
    from ee.core.events import DialogEnded
    from ee.server.threads import Thread

    th = Thread("t-dlg", ee_home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)
    th.notify = lambda m, p: None
    it = th._add({"type": "approval", "call_id": "c1", "tool": "shell", "title": "运行命令",
                  "detail": "", "prefix": "", "status": "pending"})

    async def go():
        fut = asyncio.get_running_loop().create_future()
        th.approvals["c1"] = (fut, it)
        th.task = asyncio.create_task(asyncio.sleep(3600))      # 模拟回合还在跑
        try:
            assert th.summary()["status"] == "waiting_approval"
            th._on_event(DialogEnded("c1", "cancelled", "等待超过时限"))
            assert it["status"] == "cancelled"
            assert it["reason"] == "等待超过时限"
            assert "c1" not in th.approvals, "作废的 future 不能留在审批表里"
            # 状态回到 running：父界面靠它撤掉「子会话在等你批准」的横幅
            assert th.summary()["status"] == "running"
            with pytest.raises(ValueError, match="已经处理过"):
                th.respond("c1", "once")
        finally:
            th.task.cancel()

    asyncio.run(go())
