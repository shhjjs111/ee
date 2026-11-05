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
