"""Goal 续跑控制器（T10-b / U2）。

守的是「循环本身的失控」：
- 条件本来就成立时**一个 turn 都不跑**（避免无谓改动）；
- 一直不成立时必须在上限处停，不能无限跑；
- 预算耗尽与轮次耗尽要分开报（一个是钱用完，一个是次数用完）；
- 用户中断是独立终态，不能和「跑满了」混为一谈；
- 任何情况下零测试都不许判成 met。

`turn_runner` 由调用方注入——这是「不新建第二个 agent 循环」的落点。
"""
from __future__ import annotations

import pytest


def _spec(**kw):
    from ee.core.goal import GoalSpec

    d = {"command": "x", "cwd": None, "min_tests": 1, "timeout": 30.0}
    d.update(kw)
    return GoalSpec(**d)


def _verdict(met: bool, why: str = ""):
    from ee.core.goal import Verdict

    if met:
        return Verdict(exit=0, tests=3, timed_out=False, output="3 passed", min_tests=1)
    return Verdict(exit=1, tests=1, timed_out=False, output=why or "1 failed", min_tests=1)


class _FakeJudge:
    """按脚本返回判定结果；记录被调用次数。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, spec, **_kw):
        self.calls += 1
        if not self.results:
            return _verdict(False, "脚本用完了")
        return self.results.pop(0)


def test_met_first_check_runs_no_turn():
    """条件本来就成立 → turn 调用次数为 0。"""
    import asyncio
    from ee.core.goal import run_goal

    turns = []

    async def turn():
        turns.append(1)
        return {"ok": True}

    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=_FakeJudge([_verdict(True)])))
    assert res.met is True
    assert res.stop_reason == "met"
    assert len(turns) == 0, "首轮就达标时不该跑任何回合"
    assert len(res.rounds) == 1


def test_runs_turn_until_met():
    """第 2 次判定成立 → 只跑 1 个 turn。"""
    import asyncio
    from ee.core.goal import run_goal

    turns = []

    async def turn():
        turns.append(1)

    judge = _FakeJudge([_verdict(False), _verdict(True)])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge, max_rounds=5))
    assert res.met is True
    assert len(turns) == 1
    assert judge.calls == 2


def test_exhausted_after_max_rounds():
    """一直不成立 → 跑满上限后报 exhausted，不能无限跑。

    `max_rounds` 的单位是**判定次数**，不是 turn 次数。顺序是「判 → 跑 → 判 …」，
    最后一次判完就停，不再跑一个注定没用的回合。所以 max_rounds=3 时：
    3 次判定、2 个 turn。这两条都断言，否则「上限」到底是哪个上限说不清。
    """
    import asyncio
    from ee.core.goal import run_goal

    turns = []

    async def turn():
        turns.append(1)

    judge = _FakeJudge([_verdict(False) for _ in range(10)])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge, max_rounds=3))
    assert res.met is False
    assert res.stop_reason == "exhausted"
    assert judge.calls == 3, f"应该判 3 次，实际 {judge.calls}"
    assert len(turns) == 2, f"判 3 次之间只该跑 2 个 turn，实际 {len(turns)}"
    assert len(res.rounds) == 3


def test_budget_stop_reason():
    """预算耗尽 → stop_reason=budget，不是 exhausted。"""
    import asyncio
    from ee.core.goal import run_goal

    async def turn():
        return {}

    def out_of_budget():
        return True

    judge = _FakeJudge([_verdict(False) for _ in range(5)])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge,
                               max_rounds=5, budget_exhausted=out_of_budget))
    assert res.stop_reason == "budget"
    assert res.met is False


def test_interrupt_is_its_own_terminal_state():
    """中断 → stop_reason=interrupted，既不报 met 也不报 exhausted。"""
    import asyncio
    from ee.core.goal import run_goal

    async def turn():
        raise KeyboardInterrupt

    judge = _FakeJudge([_verdict(False), _verdict(False)])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge, max_rounds=3))
    assert res.stop_reason == "interrupted"
    assert res.met is False


def test_turn_error_is_reported_not_raised():
    """回合抛普通异常 → stop_reason=error，不把异常抛给调用方。"""
    import asyncio
    from ee.core.goal import run_goal

    async def turn():
        raise RuntimeError("模型挂了")

    judge = _FakeJudge([_verdict(False)])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge, max_rounds=3))
    assert res.stop_reason == "error"
    assert "模型挂了" in res.detail


def test_report_includes_last_output():
    """报告里要有最后一次判据的输出，人能自己复核为什么没达标。"""
    import asyncio
    from ee.core.goal import run_goal

    async def turn():
        return {}

    judge = _FakeJudge([_verdict(False, "断言失败：期望 3 实际 2")])
    res = asyncio.run(run_goal(_spec(), turn_runner=turn, judge_fn=judge, max_rounds=1))
    rep = res.report()
    assert "exhausted" in rep
    assert "1 failed" in rep or "断言失败" in rep
    assert "轮" in rep


def test_zero_tests_never_counts_as_met():
    """判据永远零测试 → 无论跑多少轮都不许报 met。"""
    import asyncio
    from ee.core.goal import GoalSpec, Verdict, run_goal

    spec = GoalSpec(command="x", min_tests=1)

    async def turn():
        return {}

    async def judge_zero(_spec, **_kw):
        return Verdict(exit=0, tests=0, timed_out=False, output="collected 0 items", min_tests=1)

    res = asyncio.run(run_goal(spec, turn_runner=turn, judge_fn=judge_zero, max_rounds=3))
    assert res.met is False
    assert res.stop_reason == "exhausted"


# ---------------------------------------------------------------- 真实收敛

def test_real_convergence_red_to_green(tmp_path):
    """真实场景：判据先红，第一轮 turn 把文件改绿，随后判据成立。

    这是整件事的用户可观察验收：先失败 → 修 → 再判 → 达标。
    """
    import asyncio
    import sys

    from ee.core.goal import GoalSpec, judge, run_goal
    from ee.tools.shellenv import resolve_shell

    target = tmp_path / "test_thing.py"
    target.write_text("def test_it():\n    assert False, 'red'\n", encoding="utf-8")

    ps = resolve_shell("auto").syntax == "powershell"
    body = f'"{sys.executable}" -m pytest -q "{target}"' if " " in sys.executable \
        else f'{sys.executable} -m pytest -q "{target}"'
    command = f"& {body}" if ps else body
    spec = GoalSpec(command=command, cwd=str(tmp_path), timeout=120)

    turns = []

    async def turn():
        turns.append(1)
        # 模拟 agent 修好：第二次才写绿
        if len(turns) == 1:
            target.write_text("def test_it():\n    assert True\n", encoding="utf-8")

    res = asyncio.run(run_goal(spec, turn_runner=turn, judge_fn=judge, max_rounds=3))
    assert res.met is True, res.report()
    assert res.stop_reason == "met"
    assert len(turns) == 1
    # 第一次判定必须是红的（否则这条用例没验到"先红后绿"）
    assert res.rounds[0].met is False
    assert res.rounds[-1].met is True
