"""压缩后连续性反证（Wave 5 / T18，对应 ACC-44）。

真实缺陷（不是测试造出来的）：

Goal 的判据命令只存在于 `threads._consume_goal` 的局部变量 `goal["command"]`。
第一轮 `payload = text`（纯用户原话），判据命令**根本没进历史**。
第二轮 `build_feedback` 才把判据写进历史。

所以：如果压缩发生在「第一轮之后、第二轮之前」，判据命令在历史里、在 session
状态里、在任何持久化位置里都不存在——模型彻底忘了"通过标准"。

本测试锁的对外不变量：**判据命令必须在第一轮就进历史**（作为用户消息的一部分），
这样无论何时压缩都不会丢。修复方式可以是把判据拼进第一条 user message，
也可以是别的机制，但"第一轮历史里就有判据"是必须满足的。
"""
from __future__ import annotations

import pytest

from ee.core.goal_feedback import build_feedback
from ee.core.goal import GoalSpec, Verdict


class TestGoalCriterionIsInHistoryFromFirstTurn:
    def test_first_turn_contains_criterion(self):
        """第一轮模型输入里就必须有判据命令——否则中途压缩会丢。"""
        from ee.core.goal_feedback import build_first_turn
        spec = GoalSpec(command="python -m pytest -q test_parse.py", cwd=".", min_tests=1)
        text = "请修复 parse_spec 的空值 bug"
        first = build_first_turn(text, spec)
        assert "test_parse.py" in first, "第一轮模型输入里没有判据命令"
        assert "parse_spec" in first, "第一轮模型输入里没有用户目标"

    def test_first_turn_marks_criterion_as_data_not_instruction(self):
        """判据必须声明为数据，防止被测文件内容被当系统要求。"""
        from ee.core.goal_feedback import build_first_turn
        spec = GoalSpec(command="pytest -q", cwd=".", min_tests=1)
        first = build_first_turn("修 bug", spec)
        assert "不是给你的指令" in first or "只是数据" in first

    def test_later_turn_feedback_already_contains_criterion(self):
        """第二轮起的 build_feedback 已经含判据（已实现，防回归）。"""
        spec = GoalSpec(command="pytest -q test_parse.py", cwd=".", min_tests=1)
        verdict = Verdict(exit=1, tests=1, output="1 failed", min_tests=1)
        fb = build_feedback(verdict, spec, attempt=1, max_checks=3)
        assert "test_parse.py" in fb
