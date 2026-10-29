"""Goal 停止要停到「外层判据」这一层（Wave 2 / T09，对应 ACC-15）。

现象：Goal 循环的时序是「判 → 跑 → 判」。用户点停止时，可能正好停在
**判**这一段——即判据命令（pytest 之类）正在子进程里跑，而模型会话本身空闲。
`session.interrupt()` 只取消模型回合的任务，此时无事可取消，返回 False：
用户看到"没停下来"，判据进程继续跑到超时，剩余轮次照旧启动。

要求：
1. 用户停止时，如果正在跑判据，必须能真的把这次判据取消掉；
2. 取消后不再启动后续轮次；
3. 停止的终态要如实报「中断」，不能报成 met/exhausted。
"""
from __future__ import annotations

import asyncio

import pytest


def _run_goal_cancelled_mid_judge():
    """构造一个「判据永不返回」的场景，然后在判据进行中取消整个循环。"""
    from ee.core.goal import GoalSpec, run_goal

    state = {"turns": 0, "judge_started": asyncio.Event(), "judge_cancelled": False}

    async def never_ending_judge(spec, **_kw):
        state["judge_started"].set()
        try:
            await asyncio.sleep(3600)          # 模拟一条跑了很久的判据
        except asyncio.CancelledError:
            state["judge_cancelled"] = True
            raise

    async def turn(_v=None):
        state["turns"] += 1

    async def main():
        task = asyncio.create_task(
            run_goal(GoalSpec(command="pytest -q"), turn_runner=turn,
                     judge_fn=never_ending_judge, max_rounds=3))
        await state["judge_started"].wait()    # 确保已经进到判据
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    return state


class TestCancelDuringJudge:
    def test_cancelling_loop_cancels_running_judge(self):
        """取消 Goal 循环时，正在跑的判据必须收到取消（不能变成孤儿进程继续跑）。"""
        state = _run_goal_cancelled_mid_judge()
        assert state["judge_cancelled"] is True, "判据没有被取消：它会一直跑到超时"
        assert state["turns"] == 0, "还没跑过修复回合"
