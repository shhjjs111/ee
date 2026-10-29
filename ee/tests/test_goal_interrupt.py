"""Thread.interrupt() 在 Goal 期间必须真的能停（Wave 2 / T09，对应 ACC-15）。

根因：Goal 循环跑在 `Thread._consume` 这个 task 里，而判据阶段模型会话
（`session._task`）是空闲的。`Thread.interrupt()` 只转发给
`session.interrupt()`，于是判据正在跑时点停止 → 返回 False、什么都没停，
用户只能等判据跑到超时（默认 300 秒），剩余轮次还会照常启动。

这里用真实 Thread 对象 + 卡住的假判据，验证「点停止 → 循环被取消」。
"""
from __future__ import annotations

import asyncio

import pytest


def _make_thread(workspace):
    from ee.server.threads import Thread

    return Thread("t-goal-cancel", workspace, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


def test_interrupt_cancels_running_goal_loop(ee_home, workspace, monkeypatch):
    """判据正在跑时点停止：必须取消 Goal 循环里的 task，而不是返回 False。"""
    from ee.core import goal as goal_mod
    from ee.core.goal import Verdict

    started = asyncio.Event()

    async def stuck_judge(spec, **_kw):
        started.set()
        await asyncio.sleep(3600)              # 模拟一条跑很久的判据
        return Verdict(exit=0, tests=1, output="1 passed", min_tests=1)   # pragma: no cover

    monkeypatch.setattr(goal_mod, "judge", stuck_judge)

    th = _make_thread(workspace)

    async def main():
        # 绕过 start_turn 的全链路，直接跑 _consume（这不是集成测试，只测取消路径）
        th.session = _FakeSession(workspace)
        th.task = asyncio.create_task(th._consume("修好它", None, {"command": "pytest -q", "max_rounds": 3}))
        await asyncio.wait_for(started.wait(), timeout=10)      # 确认已进到判据阶段

        accepted = th.interrupt()
        assert accepted is True, "判据正在跑，interrupt() 却报没停——用户以为停了其实没停"

        try:
            await asyncio.wait_for(th.task, timeout=5)
        except asyncio.CancelledError:
            pass                                                # 被取消是预期结果
        assert th.task.cancelled() or th.task.done(), "Goal 循环没有被真正取消"

    asyncio.run(main())


class _FakeSession:
    """_consume 需要的最小表面：run_turn 异步生成器、budget、若干内部字段。"""

    def __init__(self, ws):
        self.budget = type("B", (), {"stop_reason": None})()
        self._task = None
        self.turn_starts = []
        self.history = []
        self._turn_role = None
        self._restore_mode = None
        self.turns = 0
        # _finish() 会读这几个字段拼 summary
        self.effort = ""
        self.style = "default"
        self.agent_profile = "full"
        self.spec = type("S", (), {"alias": "fake"})()
        self.meta = {}

    def effort_support(self):
        return None

    async def run_turn(self, text, images=None):
        if False:
            yield None

    def interrupt(self):
        # 模拟真实情况：模型会话空闲时取消不了任何东西
        if self._task and not self._task.done():
            self._task.cancel()
            return True
        return False
