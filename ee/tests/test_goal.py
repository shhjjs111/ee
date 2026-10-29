"""Goal 判据判定（T10-b / U1）。

这一层回答的问题只有一句：**这个条件现在成立吗，凭什么**。

守的都是「看起来成功实际没验证」的地方——本项目已经在 CI 上踩过一次
（1670 passed 才是绿，0 passed 不算）：
- exit 0 但一条测试都没收集到；
- 判据命令超时；
- 输出里读不到测试计数（解析失败不能当成功）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _spec(**kw):
    from ee.core.goal import GoalSpec

    d = {"command": "echo hi", "cwd": None, "min_tests": 1, "timeout": 30.0}
    d.update(kw)
    return GoalSpec(**d)


def test_exit_zero_with_zero_tests_is_not_met(tmp_path):
    """pytest 退出 0 但 collected 0 items：不许报成功。"""
    from ee.core.goal import Verdict, parse_tests

    n = parse_tests("collected 0 items\n\nno tests ran in 0.01s")
    assert n == 0
    v = Verdict(exit=0, tests=n, timed_out=False, output="", min_tests=1)
    assert v.met is False
    assert "zero" in v.reason or "没有测试" in v.reason or "0" in v.reason


def test_exit_zero_with_tests_is_met():
    """3 passed + exit 0 → 成立。"""
    from ee.core.goal import Verdict, parse_tests

    n = parse_tests("3 passed in 1.24s")
    assert n == 3
    v = Verdict(exit=0, tests=n, timed_out=False, output="", min_tests=1)
    assert v.met is True


def test_nonzero_exit_is_not_met():
    """exit != 0 一律不成立，哪怕有测试计数。"""
    from ee.core.goal import Verdict

    v = Verdict(exit=1, tests=5, timed_out=False, output="1 failed, 4 passed", min_tests=1)
    assert v.met is False


def test_timeout_is_not_met():
    """超时不是成功。"""
    from ee.core.goal import Verdict

    v = Verdict(exit=None, tests=99, timed_out=True, output="", min_tests=1)
    assert v.met is False
    assert "timeout" in v.reason.lower() or "超时" in v.reason


def test_unparsable_output_with_zero_exit_not_met():
    """退出 0 但读不出测试数：不可假成功（宁可判未达标）。"""
    from ee.core.goal import Verdict, parse_tests

    n = parse_tests("Build finished successfully.\nNothing to do.")
    v = Verdict(exit=0, tests=n, timed_out=False, output="", min_tests=1)
    assert v.met is False


def test_min_tests_honored():
    """min_tests=3 而只有 2 个 → 不成立。"""
    from ee.core.goal import Verdict

    v = Verdict(exit=0, tests=2, timed_out=False, output="", min_tests=3)
    assert v.met is False


def test_parse_tests_handles_multiple_formats():
    """pytest 的几种常见计数写法都要认得。"""
    from ee.core.goal import parse_tests

    assert parse_tests("5 passed, 1 skipped in 0.5s") == 5
    assert parse_tests("collected 7 items") == 7
    assert parse_tests("2 failed, 3 passed") == 5
    assert parse_tests("===== 12 passed in 3.4s =====") == 12


def test_parse_tests_prefers_passed_over_collected():
    """有 collected 也有 passed 时用实际跑过的数（collected 可能含 skip）。"""
    from ee.core.goal import parse_tests

    assert parse_tests("collected 10 items\n\n8 passed, 2 skipped") == 8


# ---------------------------------------------------------------- 真实执行

def _py(script: Path) -> str:
    """拼一条在解析出的 shell 里合法的「跑这个脚本」命令。

    Windows 上 `resolve_shell("auto")` 给的是 PowerShell，`"a.exe" "b.py"`
    这种并列引号在那里是语法错误，必须用调用运算符 `&`。
    """
    from ee.tools.shellenv import resolve_shell

    q = '"{}"' if " " in sys.executable else "{}"
    exe = q.format(sys.executable)
    body = f'{exe} "{script}"'
    return f"& {body}" if resolve_shell("auto").syntax == "powershell" else body


def test_judge_runs_real_command_and_reports_met(tmp_path):
    """真实跑一条命令：判据成立时的完整 Verdict。"""
    import asyncio
    from ee.core.goal import judge

    script = tmp_path / "fake_test.py"
    script.write_text("print('3 passed in 0.1s')\n", encoding="utf-8")
    spec = _spec(command=_py(script), cwd=str(tmp_path))

    v = asyncio.run(judge(spec))
    assert v.exit == 0, v.output
    assert v.tests == 3
    assert v.met is True
    assert v.output, "输出要保留，便于复核"


def test_judge_reports_not_met_for_real_failure(tmp_path):
    """真实失败命令 → 不成立，并带输出尾部。"""
    import asyncio
    from ee.core.goal import judge

    script = tmp_path / "fail_test.py"
    script.write_text("import sys; print('1 failed, 0 passed'); sys.exit(1)\n", encoding="utf-8")
    spec = _spec(command=_py(script), cwd=str(tmp_path))

    v = asyncio.run(judge(spec))
    assert v.met is False
    assert v.exit == 1, v.output
    assert "failed" in v.output


def test_judge_times_out(tmp_path):
    """真实超时：判未达标而不是卡住。"""
    import asyncio
    from ee.core.goal import judge

    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    spec = _spec(command=_py(script), cwd=str(tmp_path), timeout=1.0)

    v = asyncio.run(judge(spec))
    assert v.met is False
    assert v.timed_out is True


def test_judge_zero_tests_real(tmp_path):
    """真实场景：命令成功但没有测试——这正是 CI 上差点误判的形态。"""
    import asyncio
    from ee.core.goal import judge

    script = tmp_path / "empty.py"
    script.write_text("print('collected 0 items'); print('no tests ran')\n", encoding="utf-8")
    spec = _spec(command=_py(script), cwd=str(tmp_path))

    v = asyncio.run(judge(spec))
    assert v.exit == 0, v.output
    assert v.met is False, "exit 0 + 零测试绝不能算成功"
