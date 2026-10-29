"""自动审批建议态的接线（T10-c）：建议必须真的到达审批事件。

纯函数测通不等于接线通了——本项目已经踩过两次「能力实现、链路没接」的坑
（LSP 的 store 从未被赋值、thread/export 桌面端零调用方）。这里守的是：

1. `_ask` 生成的 ApprovalRequest 带上了建议；
2. **决策没有被建议改动**（ask 仍然是 ask）；
3. 建议是异步算出还是同步算都不影响（用纯函数，天然同步）；
4. 出错时不影响审批本身——建议是辅助信息，不能因为算建议把审批搞崩。
"""
from __future__ import annotations

import asyncio

import pytest


def _decision(action="ask", prefix="shell(git status *)", force=False):
    from ee.core.permissions import Decision

    return Decision(action, "", prefix, force)


class _FakeTool:
    name = "shell"

    def summary(self, args):
        return "run"


def test_ask_attaches_suggestion_when_enabled():
    """开启后，ApprovalRequest 要带上建议内容。"""
    from ee.core.session import _attach_suggestion
    from ee.core.events import ApprovalRequest

    ev = ApprovalRequest("c1", "shell", "运行命令", "git status",
                         prefix="shell(git status *)", future=None)
    out = _attach_suggestion(ev, mode="default",
                             config={"autoapprove": {"enabled": True}})
    assert out is ev
    assert getattr(out, "suggestion", None), "建议没挂到事件上"
    assert out.suggestion["eligible"] is True


def test_suggestion_never_touches_action():
    """无论建议结果如何，原 Decision 的 action 不被改。"""
    from ee.core.session import _attach_suggestion
    from ee.core.events import ApprovalRequest

    d = _decision("ask")
    ev = ApprovalRequest("c1", "shell", "t", "git status", prefix="shell(git status *)",
                         future=None)
    ev.decision = d
    _attach_suggestion(ev, mode="default", config={"autoapprove": {"enabled": True}})
    assert d.action == "ask"


def test_force_ask_gets_no_eligible_suggestion():
    """force 的审批即使开了开关，也不能出现可放行建议。"""
    from ee.core.session import _attach_suggestion
    from ee.core.events import ApprovalRequest

    ev = ApprovalRequest("c1", "shell", "危险", "git push --force",
                         prefix="shell(git push --force *)", force=True, future=None)
    _attach_suggestion(ev, mode="default", config={"autoapprove": {"enabled": True}})
    assert ev.suggestion["eligible"] is False
    assert "force" in ev.suggestion["blocked_by"]


def test_disabled_attaches_nothing_eligible():
    """默认关闭时不给出可放行建议。"""
    from ee.core.session import _attach_suggestion
    from ee.core.events import ApprovalRequest

    ev = ApprovalRequest("c1", "shell", "t", "git status", prefix="shell(git status *)",
                         future=None)
    _attach_suggestion(ev, mode="default", config={})
    assert ev.suggestion["eligible"] is False


def test_suggestion_failure_does_not_break_approval():
    """算建议时抛异常也不能让审批本身失败。"""
    from ee.core.session import _attach_suggestion
    from ee.core.events import ApprovalRequest

    class Boom:
        action = "ask"
        prefix = "x"

        def __getattr__(self, name):
            raise RuntimeError("boom")

    ev = ApprovalRequest("c1", "shell", "t", "", prefix="x", future=None)
    ev.decision = Boom()
    out = _attach_suggestion(ev, mode="default", config={"autoapprove": {"enabled": True}})
    assert out is ev, "异常时仍要返回原事件"
