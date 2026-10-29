"""测试计数判定的反证（Wave 2 / T08，对应 ACC-12）。

判据只有一个方向：**只有证据充分才算成立**。最常见的假成功是
「命令退出 0 且收集到了测试」，但那些测试其实一个都没跑（全 skipped），
或者压根没执行（只 collected）。

本文件直接测生产的 `parse_tests` 与 `Verdict.met`，用真实 runner 的输出形态。
"""
from __future__ import annotations

from ee.core.goal import Verdict, parse_tests


def _met(output: str, exit_code: int = 0, min_tests: int = 1) -> bool:
    return Verdict(exit=exit_code, tests=parse_tests(output), timed_out=False,
                   output=output, min_tests=min_tests).met


class TestCollectedIsNotExecuted:
    def test_all_skipped_does_not_count_as_executed(self):
        """collected 3 items / 3 skipped：一个都没跑，不能算通过。"""
        out = "collected 3 items\n\n3 skipped in 0.12s"
        assert parse_tests(out) == 0, f"collected 把 skipped 算成了执行数：{parse_tests(out)}"
        assert not _met(out)

    def test_collected_alone_is_not_executed(self):
        """只打印了 collected、没有跑完任何测试（例如中途 collection error 前）。"""
        out = "collected 0 items"
        assert parse_tests(out) == 0
        assert not _met(out)

    def test_two_skipped_one_passed_counts_one(self):
        """9 collected / 1 passed / 2 skipped：真跑过的只有 1 个。"""
        out = "collected 3 items\n\n1 passed, 2 skipped in 0.20s"
        assert parse_tests(out) == 1
        assert _met(out, min_tests=1)
        assert not _met(out, min_tests=2), "只跑了 1 个却满足 min_tests=2"


class TestPassedAndFailedStillSum:
    def test_failed_plus_passed_counts_both(self):
        """2 failed + 3 passed = 跑了 5 个（部分修好时不能低估进度）。"""
        out = "2 failed, 3 passed in 0.5s"
        assert parse_tests(out) == 5

    def test_only_failed_counts(self):
        out = "4 failed in 0.3s"
        assert parse_tests(out) == 4
        assert not _met(out, exit_code=1)


class TestUnknownStaysUnknown:
    def test_unrecognized_output_is_zero(self):
        """读不出计数就当没跑，宁可多跑几轮也不假成功。"""
        assert parse_tests("build succeeded") == 0
        assert not _met("build succeeded")

    def test_empty_output_is_zero(self):
        assert parse_tests("") == 0
        assert not _met("")


class TestExitCodeAndTimeout:
    def test_nonzero_exit_never_met(self):
        assert not _met("5 passed in 0.2s", exit_code=1)

    def test_timeout_never_met(self):
        v = Verdict(exit=0, tests=99, timed_out=True, output="99 passed", min_tests=1)
        assert not v.met, "超时不算通过，即使打印了 passed"

    def test_no_exit_code_never_met(self):
        v = Verdict(exit=None, tests=99, timed_out=False, output="99 passed", min_tests=1)
        assert not v.met
