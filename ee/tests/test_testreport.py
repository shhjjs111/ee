"""逐用例结果解析与进展判定（2026-11 第三批）。

关键立场：**看不懂就说看不懂**。把"少跑了几条"当成"修好了几条"，
正是这套机制要防的事，所以每个不确定的分支都要求 `state=unknown`。
"""
from __future__ import annotations

from ee.core import testreport as T

PY_FAIL = """\
FAILED tests/test_a.py::test_one - AssertionError: 1 != 2
FAILED tests/test_a.py::test_two - assert False
=========================== short test summary info ===========================
2 failed in 0.12s
"""

PY_ONE_FIXED = """\
FAILED tests/test_a.py::test_two - assert False
=========================== short test summary info ===========================
1 failed in 0.10s
"""

PY_ALL_GREEN = """\
=========================== short test summary info ===========================
3 passed in 0.08s
"""

PY_SKIPPED_INSTEAD = """\
FAILED tests/test_a.py::test_two - assert False
=========================== short test summary info ===========================
1 failed, 1 skipped in 0.10s
"""

PY_VERBOSE = """\
tests/test_a.py::test_one PASSED                                            [ 50%]
tests/test_a.py::test_two FAILED                                            [100%]
FAILED tests/test_a.py::test_two - assert False
1 failed, 1 passed in 0.12s
"""

PY_SETUP_ERROR = """\
ERROR at setup of tests/test_a.py::test_three
FAILED tests/test_a.py::test_one - assert False
=========================== short test summary info ===========================
1 failed, 1 error in 0.12s
"""


def test_pytest_parses_failures_and_summary():
    rep = T.parse(PY_FAIL)
    assert rep.kind == "pytest" and rep.complete
    assert rep.failed == {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}
    assert rep.counts["failed"] == 2


def test_pytest_verbose_keeps_passing_ids():
    rep = T.parse(PY_VERBOSE)
    assert rep.cases["tests/test_a.py::test_one"] == T.PASSED
    assert rep.cases["tests/test_a.py::test_two"] == T.FAILED


def test_pytest_setup_error_keeps_phase():
    """setup 阶段报错和断言失败不是一回事，阶段要留着（别被当成"用例失败"混进修复反馈）。"""
    rep = T.parse(PY_SETUP_ERROR)
    assert rep.cases["tests/test_a.py::test_three"] == "error:setup"


def test_progress_confirmed_when_failure_vanishes_and_count_drops():
    got = T.progress(T.parse(PY_FAIL), T.parse(PY_ONE_FIXED))
    assert got["state"] == "improved", got
    assert got["fixed"] == ["tests/test_a.py::test_one"]


def test_progress_regressed_on_new_failure():
    got = T.progress(T.parse(PY_ONE_FIXED), T.parse(PY_FAIL))
    assert got["state"] == "regressed"
    assert got["new_failures"]


def test_progress_unknown_when_skipped_instead_of_fixed():
    """失败用例变成 skipped：跳过计数变了 → 不能算修好（这正是"藏测试"的形态）。"""
    got = T.progress(T.parse(PY_FAIL), T.parse(PY_SKIPPED_INSTEAD))
    assert got["state"] == "unknown", got
    assert "skip" in got["why"]


def test_progress_unknown_when_counts_do_not_add_up():
    """失败数没跟着减少 → 消失的用例可能只是没被收集，不能算进展。"""
    before = T.parse(PY_FAIL)
    after = T.parse(PY_FAIL)                 # 同样的失败列表，但把汇总改成 1 failed
    after.counts["failed"] = 1
    got = T.progress(before, after)
    assert got["state"] in ("unknown", "stalled"), got


def test_progress_all_green_is_improved():
    got = T.progress(T.parse(PY_FAIL), T.parse(PY_ALL_GREEN))
    assert got["state"] == "improved"


def test_go_verbose_uses_package_qualified_ids():
    out = """\
=== RUN   TestA
--- PASS: TestA (0.00s)
=== RUN   TestB
--- FAIL: TestB (0.00s)
FAIL
FAIL\texample.com/pkg\t0.01s
ok  \texample.com/other\t0.02s
"""
    rep = T.parse(out)
    assert rep.kind == "go" and rep.complete
    assert rep.failed == {"example.com/pkg::TestB"}, rep.cases


def test_go_without_verbose_is_incomplete():
    """没有 -v 只有包级结果：能说包红/包绿，不能说逐用例没有回归。"""
    out = "ok  \texample.com/pkg\t0.01s\n"
    rep = T.parse(out)
    assert rep.kind == "go" and not rep.complete


def test_unknown_output_is_not_evidence():
    rep = T.parse("hello world")
    assert rep.kind == "" and not rep.complete
    assert T.progress(rep, rep)["state"] == "unknown"
