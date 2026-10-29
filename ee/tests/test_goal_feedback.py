"""Goal 失败反馈的反证（Wave 0 / T02，对应 ACC-10）。

问题：桌面端 Goal 跑的是「先判后跑」的循环，但模型在第二轮收到的是
固定文案「继续，直到判据通过」，**看不到判据命令本身，也看不到失败输出**。
模型只能靠猜该改哪里，于是多跑几轮 = 多花钱而不收敛。

本文件用真实的 `Thread._consume_goal` + 注入的判据函数来捕获
**真正传给 `Session.run_turn` 的文本**，不是测一个格式化 helper。
"""
from __future__ import annotations

import pytest

from ee.core import goal as goal_mod
from ee.core.goal import Verdict


class _FakeSession:
    """只实现 _consume_goal 需要的表面：run_turn 是异步生成器，budget.stop_reason 可读。"""

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.budget = type("B", (), {"stop_reason": None})()

    async def run_turn(self, text, images=None):
        self.inputs.append(text)
        if False:                       # 让它是异步生成器，但一句都不产出
            yield None


class _FakeThread:
    """_consume_goal 只用到 self.session / self.cwd / self.id / self.notify / self._on_event。"""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.cwd = "."
        self.id = "t-goal-probe"
        self.events: list[tuple[str, dict]] = []

    def notify(self, method, payload):
        self.events.append((method, payload))

    def _on_event(self, ev):
        pass


def _run_goal_with(monkeypatch, failure_output: str, command: str, max_rounds: int = 3):
    """注入固定失败的判据，跑真实的 _consume_goal，返回假会话收到的全部输入。"""

    async def fake_judge(spec, **_kw):
        return Verdict(exit=1, tests=1, timed_out=False, output=failure_output, min_tests=1)

    monkeypatch.setattr(goal_mod, "judge", fake_judge)

    from ee.server.threads import Thread

    th = _FakeThread()
    import asyncio
    asyncio.run(Thread._consume_goal(th, "把 parse_date 的时区 bug 修好", None,
                                     {"command": command, "max_rounds": max_rounds}))
    return th.session.inputs


def test_goal_command_reaches_model_input(monkeypatch):
    """判据命令必须出现在模型的输入里，否则它不知道"通过"由什么决定。"""
    inputs = _run_goal_with(monkeypatch, "1 failed", "pytest -q tests/test_dates.py")
    joined = "\n".join(inputs[1:])          # 第一轮是用户原话，之后才是续跑输入
    assert "tests/test_dates.py" in joined, f"判据命令没进模型输入：{inputs}"


def test_goal_failure_output_reaches_model_input(monkeypatch):
    """失败输出必须进模型输入，否则模型无法定向修复。"""
    marker = "AssertionError: expected 2024-03-01 but got 2024-02-29"
    inputs = _run_goal_with(monkeypatch, marker, "pytest -q tests/test_dates.py")
    joined = "\n".join(inputs[1:])
    assert ("2024-03-01" in joined) or ("AssertionError" in joined), (
        f"失败输出没进模型输入；模型只收到：{inputs[1:]}"
    )


def test_goal_zero_turns_when_already_met(monkeypatch):
    """首轮判据就成立时，一次模型回合都不跑（现有正确行为，防回归）。"""

    async def ok_judge(spec, **_kw):
        return Verdict(exit=0, tests=3, timed_out=False, output="3 passed", min_tests=1)

    monkeypatch.setattr(goal_mod, "judge", ok_judge)

    from ee.server.threads import Thread
    import asyncio

    th = _FakeThread()
    reason = asyncio.run(Thread._consume_goal(th, "随便", None, {"command": "pytest -q", "max_rounds": 3}))
    assert reason == "met"
    assert th.session.inputs == [], f"判据已成立却跑了模型回合：{th.session.inputs}"


def test_goal_rounds_semantics_unchanged(monkeypatch):
    """max_rounds=3 表示 3 次判定、最多 2 个修复回合（现有语义，防回归）。"""
    inputs = _run_goal_with(monkeypatch, "still failing", "pytest -q", max_rounds=3)
    assert len(inputs) == 2, f"3 次判定应最多 2 个修复回合，实际 {len(inputs)} 个：{inputs}"
