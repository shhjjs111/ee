"""第三批：进展判定驱动的修复循环（2026-11 设计评审）。

核心规则：
- 只看**同一批用例的身份变化**：失败面缩小且没有新增失败 → 保留这版继续修；
- 失败面没动 → 回滚并记一次停滞；结果不完整/读不出 → 回滚但**不算停滞**（没有证据别换路）；
- 有明确进展时多给一轮修复（预算内），而不是把最有价值的中间成果丢掉。
"""
from __future__ import annotations

import asyncio

from ee.core.goal import Verdict
from ee.core.testreport import Report


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t-prog", home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


def _v(met: bool, failed: list[str], passed_count: int = 0, *, complete: bool = True) -> Verdict:
    """构造带逐用例结果的判定。failed 为空时按"全绿"处理。"""
    counts = {"passed": passed_count + max(0, 10 - len(failed) - 1), "failed": len(failed)}
    rep = Report(kind="pytest", counts=counts,
                 cases={f"t::{f}": "failed" for f in failed},
                 complete=complete and bool(counts))
    return Verdict(exit=0 if met else 1, tests=counts["passed"] + counts["failed"],
                   output="\n".join(f"FAILED t::{f}" for f in failed) or f"{counts['passed']} passed",
                   min_tests=1, report=rep, timed_out=False)


def _run(th, verdicts, monkeypatch, *, rounds: int = 1):
    """跑 _run_allout：judge 按脚本返回；记录回滚与修复通知。"""
    from test_allout_run import _stub_turn  # 复用既有的桩回合

    _stub_turn(th, ["completed"] * 6)
    seq = list(verdicts)

    async def fake_judge(spec, **_kw):
        return seq.pop(0) if seq else _v(True, [], 10)

    monkeypatch.setattr("ee.core.goal.judge", fake_judge)
    th.session.disk_capture = lambda: "tree-stable"
    reverted: list[object] = []
    monkeypatch.setattr(th, "_revert_repair", lambda s, tree: reverted.append(tree) or ["a.txt"])
    notes: list[tuple[str, dict]] = []
    th.notify = lambda m, p: notes.append((m, p))
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    return reason, reverted, notes


def test_improved_attempt_is_kept_and_buys_another_round(ee_home, workspace, monkeypatch):
    """8 个失败修到 2 个：保留这版继续修（不回滚），并多给一轮。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    verdicts = [
        _v(False, ["a", "b", "c", "d", "e", "f", "g", "h"]),   # 首轮
        _v(False, ["a", "b"]),                                 # 修复轮 1：失败面缩小
        _v(True, []),                                           # 修复轮 2：通过
    ]
    reason, reverted, notes = _run(th, verdicts, monkeypatch)
    assert reason == "met", (reason, notes[-3:])
    assert reverted == [], "有明确进展的修复不能被回滚"
    repairs = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "repair"]
    assert repairs and repairs[0]["progress"] == "improved", repairs
    assert len(repairs) == 2, f"有进展应多给一轮修复，实际 {len(repairs)} 轮"


def test_stalled_attempt_is_reverted_and_counted(ee_home, workspace, monkeypatch):
    """失败集合没变化：回滚这一次尝试，并记一次停滞。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    verdicts = [
        _v(False, ["a", "b"]),
        _v(False, ["a", "b"]),        # 修复轮：失败面没动
        _v(False, ["a", "b"]),        # 分叉前的判据（若走到）
    ]
    reason, reverted, notes = _run(th, verdicts, monkeypatch)
    assert reason == "exhausted"
    assert reverted, "没有进展的修复必须回滚"
    assert th._stalled >= 1
    repairs = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "repair"]
    assert repairs and repairs[0]["progress"] == "stalled"


def test_unknown_progress_reverts_but_does_not_count_as_stall(ee_home, workspace, monkeypatch):
    """读不出逐用例结果：回滚，但不能据此判"停滞"（没有证据就别换路）。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    no_report = lambda: Verdict(exit=1, tests=1, output="1 failed", min_tests=1, report=None)  # noqa: E731
    verdicts = [no_report(), no_report(), no_report()]
    reason, reverted, notes = _run(th, verdicts, monkeypatch)
    assert reason == "exhausted"
    assert reverted, "无法确认进展时仍要回滚（不能把不可比的结果留在工作区）"
    assert th._stalled == 0, "unknown 不该被记成停滞"


def test_candidates_split_into_continuation_and_independent(ee_home, workspace, monkeypatch):
    """候选分思路：第一条带失败证据继续修，其余从原始需求重新定位。"""
    from types import SimpleNamespace

    from ee.core import worktree as W
    from ee.core.goal import GoalSpec

    th = _thread(ee_home, workspace)
    th.ensure_session()
    th._allout_judge_lock = asyncio.Lock()

    prompts: list[str] = []

    class _FakeChild:
        def __init__(self, *a, **kw):
            self.spec = SimpleNamespace(alias="m")
            self.stats = SimpleNamespace(totals=lambda: {"cost": 0.0})

        async def run_turn(self, text, images=None, **_kw):
            prompts.append(text)
            if False:
                yield None

    monkeypatch.setattr("ee.core.session.Session", _FakeChild)  # _allout_fanout 是函数内导入
    monkeypatch.setattr(W, "agent_eligible", lambda cwd: (True, ""))
    monkeypatch.setattr(W, "create_for_agent",
                        lambda cwd, label="", home=None: {"cwd": str(workspace), "agent_base": "b",
                                                          "base": "b", "path": str(workspace / label)})
    monkeypatch.setattr(W, "finish_agent",
                        lambda info, message, home=None: {"file_count": 0, "files": [],
                                                          "additions": 0, "deletions": 0})

    async def no_judge(spec, **_kw):
        raise AssertionError("分叉用例不该跑判据（候选还没结束）")

    monkeypatch.setattr("ee.core.goal.judge", no_judge)

    failure = _v(False, ["a", "b"])
    plan = type("P", (), {"temperature_ladder": [0.0, 0.3], "repair_rounds": 1})()
    spec = GoalSpec(command="pytest -q", cwd=str(workspace))

    async def main():
        task = asyncio.ensure_future(th._allout_fanout(spec, plan, "修好它", 2, failure=failure))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert len(prompts) == 2, f"两个候选都应跑起来：{len(prompts)}"
    assert "上一轮判定没有通过" in prompts[0], "第一条候选要带着剩余失败（延续路线）"
    assert "上一轮判定没有通过" not in prompts[1], "第二条候选不该看到上一轮的失败（独立路线）"
