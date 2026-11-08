"""parse_tests 对各家 runner 汇总行的解析（allout 阶段 0）。

**为什么这个文件值得存在**：Goal / best-of-n / 全力以赴全部靠
「命令真的跑了多少个测试」这一个数来判成败。2026-11-09 实测：go / dotnet /
mocha / rspec / phpunit / bun 的输出一律读成 0，vitest 把 `Test Files 3 passed`
误读成 3（真实是 `Tests 12 passed`）。后果不是少显示一个数字——
这些项目里 `go test ./...` 全绿也会判「zero_tests 不通过」，续跑循环会
无限重试直到烧光预算。

样本全部取自各 runner 的真实输出形态（含失败形态），不是编的。
"""
from __future__ import annotations

import pytest

from ee.core.goal import parse_tests

# (名字, 输出, 期望「真跑过」的测试数)
SAMPLES = [
    # ---- vitest / jest：汇总行必须以 `Tests` 开头；`Test Files` 不是测试数
    ("vitest_ok", " Test Files  3 passed (3)\n      Tests  12 passed (12)\n", 12),
    ("vitest_fail", " Test Files  3 passed (3)\n      Tests  1 failed | 11 passed (12)\n", 12),
    ("jest_ok", "Test Suites: 3 passed, 3 total\nTests:       12 passed, 12 total\nSnapshots:   0 total\n", 12),
    ("jest_fail", "Test Suites: 1 failed, 2 passed, 3 total\nTests:       1 failed, 11 passed, 12 total\n", 12),
    # ---- go test：成功只打 `ok  pkg  0.12s`，没有计数
    ("go_ok", "ok  \texample.com/pkg\t0.012s\nok  \texample.com/pkg2\t0.003s\n", 2),
    ("go_fail", "--- FAIL: TestX (0.00s)\nFAIL\nFAIL\texample.com/pkg\t0.01s\n", 0),
    ("go_verbose", "=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n--- FAIL: TestB (0.00s)\nFAIL\n", 2),
    # ---- dotnet test：Failed 在前、Passed 在后，两者都跑过
    ("dotnet_ok", "Passed!  - Failed:     0, Passed:    12, Skipped:     0, Total:    12\n", 12),
    ("dotnet_fail", "Failed!  - Failed:     1, Passed:    11, Skipped:     0, Total:    12\n", 12),
    # ---- mocha：passing / failing 要相加
    ("mocha_ok", "  12 passing (34ms)\n", 12),
    ("mocha_fail", "  11 passing (34ms)\n  1 failing\n", 12),
    # ---- rspec：examples 是跑过的总数，failures 是其中失败的
    ("rspec_ok", "5 examples, 0 failures\n", 5),
    ("rspec_fail", "5 examples, 1 failure\n", 5),
    # ---- phpunit：两种汇总形态
    ("phpunit_ok", "OK (12 tests, 34 assertions)\n", 12),
    ("phpunit_fail", "Tests: 12, Assertions: 34, Failures: 1.\n", 12),
    # ---- bun test
    ("bun_ok", " 12 pass\n 0 fail\nRan 12 tests across 3 files. [12.00ms]\n", 12),
    ("bun_fail", " 11 pass\n 1 fail\nRan 12 tests across 3 files. [12.00ms]\n", 12),
    # ---- maven surefire：Tests run 已含 failures，不能重复相加
    ("maven_ok", "[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0\n", 12),
    ("maven_fail", "[ERROR] Tests run: 12, Failures: 1, Errors: 0, Skipped: 0\n", 12),
    # ---- pytest 原有形态不能被破坏（回归）
    ("pytest_ok", "===== 15 passed in 1.2s =====\n", 15),
    ("pytest_mixed", "2 failed, 3 passed in 0.5s\n", 5),
    ("pytest_collected_only", "collected 7 items\n", 7),
]


@pytest.mark.parametrize("name,output,want", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_parse_tests_recognizes_runner_summaries(name, output, want):
    got = parse_tests(output)
    assert got == want, f"{name}: 期望 {want}，实际 {got}\n输出：{output!r}"


def test_test_files_line_alone_is_not_a_test_count():
    """只有 Test Files 行没有 Tests 行时，不能拿文件数当测试数。"""
    assert parse_tests(" Test Files  3 passed (3)\n") == 0


def test_go_all_failed_non_verbose_reads_zero_not_a_false_success():
    """go 全失败（非 -v）没有任何计数：按 0 处理是保守方向，
    退出码非零本来就会判不通过；这里只守「不许凭空报数」。"""
    out = "FAIL\texample.com/pkg\t0.01s\nFAIL\n"
    assert parse_tests(out) == 0
