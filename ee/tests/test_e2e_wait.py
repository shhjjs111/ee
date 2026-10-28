"""e2e_ws 的等待判定（D6-1 U9）。

脚本原先死等 `type == "approval"` 的 item/started 条目。但假模型 step 0 发的是
todo/read/read/grep —— 这四个都在 permissions.READ_TOOLS 里，**免审批**，所以那个
事件永远不会来，脚本空等 60 秒后失败。这里把判定抽成纯函数钉住：
看到 turn/completed 就结束；看到 approval 就响应；两者都不是就继续等。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.e2e_ws import pending_approvals, turn_finished  # noqa: E402


def note(method: str, **params):
    return {"method": method, "params": params}


def approval(call_id="c1", tid="t1"):
    return note("item/started", thread_id=tid, item={"type": "approval", "call_id": call_id})


def test_turn_completed_ends_wait():
    assert turn_finished([note("turn/completed", thread_id="t1", reason="completed")]) is True


def test_no_completion_keeps_waiting():
    assert turn_finished([note("turn/status", thread_id="t1", text="正在连接模型…")]) is False


def test_tool_items_alone_do_not_end_the_wait():
    """工具条目出现不代表回合结束：只读工具跑完还有后续步骤。"""
    notes = [note("item/started", thread_id="t1", item={"type": "tool", "call_id": "c1", "name": "read"})]
    assert turn_finished(notes) is False


def test_pending_approvals_extracts_only_approval_items():
    notes = [
        note("item/started", thread_id="t1", item={"type": "tool", "call_id": "c1"}),
        approval("c2"),
        note("item/started", thread_id="t1", item={"type": "assistant"}),
    ]
    got = pending_approvals(notes)
    assert [a["call_id"] for a in got] == ["c2"]


def test_pending_approvals_empty_when_only_read_tools():
    """只读工具不触发审批——这正是原先空等 60s 的场景，必须判成"无需响应"。"""
    notes = [
        note("item/started", thread_id="t1", item={"type": "tool", "call_id": "c1", "name": "todo"}),
        note("item/started", thread_id="t1", item={"type": "tool", "call_id": "c2", "name": "read"}),
        note("item/started", thread_id="t1", item={"type": "tool", "call_id": "c3", "name": "grep"}),
    ]
    assert pending_approvals(notes) == []


def test_question_items_are_not_approvals():
    """ask_user 是另一种阻塞，响应方式不同，不能混进审批回复里。"""
    notes = [note("item/started", thread_id="t1", item={"type": "question", "call_id": "q1"})]
    assert pending_approvals(notes) == []
