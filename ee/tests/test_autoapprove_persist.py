"""自动审批建议的后端接线（T10-c）：事件上的 suggestion 必须落到 ApprovalItem。

这一层最容易漏：`_attach_suggestion` 把建议挂到 ApprovalRequest 上、前端也写好
渲染，但 `threads._on_event` 若不把它写进条目，前端永远拿不到——表现是"功能做
完了但界面上看不到"，与 LSP 那次「字段存在但没人赋值」是同一类。本用例守这条。
"""
from __future__ import annotations

import pytest


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t1", home, lambda m, p: None, {"cwd": str(workspace)},
                  persist=False)


def test_suggestion_is_persisted_into_approval_item(ee_home, workspace):
    """ApprovalRequest 带 suggestion 时，条目里也要有。"""
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    ev = ApprovalRequest("c1", "shell", "运行命令", "git status",
                         prefix="shell(git status *)", future=None)
    ev.suggestion = {"eligible": True, "rule": "shell(git status *)",
                     "blocked_by": "", "summary": "以后遇到相同模式不再询问",
                     "hits": 2, "reason": "", "action": "ask"}
    th._on_event(ev)

    it = next(i for i in th.items if i.get("type") == "approval")
    assert it.get("suggestion"), "建议没写进条目——前端拿不到"
    assert it["suggestion"]["eligible"] is True
    assert it["suggestion"]["rule"] == "shell(git status *)"


def test_absent_suggestion_keeps_item_clean(ee_home, workspace):
    """没有建议时不写这个字段（老数据兼容，不塞空 dict）。"""
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    ev = ApprovalRequest("c2", "shell", "运行命令", "ls", prefix="shell(ls *)", future=None)
    th._on_event(ev)

    it = next(i for i in th.items if i.get("type") == "approval")
    assert "suggestion" not in it


def test_suggestion_does_not_change_approval_status(ee_home, workspace):
    """建议不影响审批状态：条目仍是 pending，等用户决定。"""
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    ev = ApprovalRequest("c3", "shell", "运行命令", "make", prefix="shell(make *)", future=None)
    ev.suggestion = {"eligible": True, "rule": "shell(make *)", "blocked_by": "",
                     "summary": "x", "hits": 1, "reason": "", "action": "ask"}
    th._on_event(ev)

    it = next(i for i in th.items if i.get("type") == "approval")
    assert it["status"] == "pending", "建议让审批自动变成已决定，这是越权"
