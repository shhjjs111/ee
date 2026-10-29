"""`ee exec --goal` 的 CLI 接入（T10-b / U3）。

守的是「退出码和停止原因对得上」——脚本和 CI 靠退出码判断成败，
报错了却退 0 等于骗人。

退出码复用 headless 既有的 EXIT_CODES：
met→0、exhausted→3（与步数上限同义：预算耗尽）、budget→5、interrupted→130、error→1。
"""
from __future__ import annotations

import pytest

from ee.headless import add_arguments, EXIT_CODES


def _parser():
    import argparse

    p = argparse.ArgumentParser()
    add_arguments(p)
    return p


def test_goal_flags_registered():
    """--goal 及配套参数要出现在 exec 的帮助里。"""
    ns = _parser().parse_args(["--goal", "pytest -q", "prompt"])
    assert ns.goal == "pytest -q"
    assert ns.goal_max_rounds == 3          # 默认有限轮次
    assert ns.goal_timeout > 0
    assert ns.goal_min_tests == 1


def test_goal_absent_by_default():
    """不给 --goal 时是 None，保持既有行为不变。"""
    ns = _parser().parse_args(["prompt"])
    assert ns.goal is None


def test_goal_rounds_can_be_lowered():
    """轮次可收紧——默认有限，用户还能调更小。"""
    ns = _parser().parse_args(["--goal", "x", "--goal-max-rounds", "1", "prompt"])
    assert ns.goal_max_rounds == 1


def test_goal_rounds_rejects_zero():
    """0 轮没有意义，要拒绝而不是当成「不跑」静默成功。"""
    with pytest.raises(SystemExit):
        _parser().parse_args(["--goal", "x", "--goal-max-rounds", "0", "prompt"])


def test_exit_codes_cover_goal_states():
    """goal 的停止原因必须映射到既有退出码，不能各自为政。"""
    from ee.core import goal as G

    # met 复用 completed 的 0
    assert EXIT_CODES["completed"] == 0
    for k in (G.BUDGET, G.INTERRUPTED, G.ERROR):
        assert k in EXIT_CODES, f"{k} 没有对应退出码"
    # exhausted 与 interrupted 是两个不同的终态，不能共用同一个码
    assert G.EXHAUSTED != G.INTERRUPTED


def test_goal_verdict_to_exit_code_mapping():
    """停止原因 → 退出码的映射函数：exhausted 用 3（与步数上限同义）。"""
    from ee.headless import goal_exit_code

    assert goal_exit_code("met") == 0
    assert goal_exit_code("exhausted") == 3
    assert goal_exit_code("budget") == 5
    assert goal_exit_code("interrupted") == 130
    assert goal_exit_code("error") == 1
    # 未知原因不能悄悄变成 0
    assert goal_exit_code("something-else") == 1
