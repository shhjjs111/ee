"""交付闸门（2026-11 第一批）：所有 `met` 出口都必须经过同一道验收。

守的是三件事：
1. 交付状态（用户实际拿到的）通过验收，才允许 `met`；
2. 验收依据变了（改断言/改测试配置/改命令）→ 不能自动 `met`；
3. 判据或写回期间的状态变化 → 结果作废，如实报告，不能拿"某次跑过"当证据。
"""
from __future__ import annotations

import asyncio

import pytest

from ee.core import acceptance
from ee.core.goal import GoalSpec, Verdict


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t-dv", home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


def _v(met: bool = True, tests: int = 3, timed_out: bool = False) -> Verdict:
    return Verdict(exit=0 if met else 1, tests=tests, timed_out=timed_out,
                   output="3 passed" if met else "1 failed", min_tests=1)


def _delivery(th) -> dict:
    d = th._delivery
    assert d is not None, "交付结论没写下来"
    return d


def _gate(th, s, spec, p2p_spec=None, **kw) -> str:
    return asyncio.run(th._delivery_gate(s, spec, p2p_spec, **kw))


def test_reuses_recent_judge_and_reports_unconfigured_regression(ee_home, workspace):
    """能复用的判据不重复跑；回归未配置要单列状态，不能伪装成"回归通过"。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    tree = s.disk_capture()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    th._judge_recent = {"target": {"verdict": _v(True), "tree": tree, "source_unchanged": True}}

    spec = GoalSpec(command="pytest -q", cwd=str(th.cwd))
    reason = _gate(th, s, spec, source="first_try")
    assert reason == "met"
    d = _delivery(th)
    assert d["status"] == "verified"
    assert d["target"]["reused"] is True
    assert d["regression"]["status"] == "unconfigured"


def test_judge_that_modifies_source_invalidates_result(ee_home, workspace, monkeypatch):
    """判据命令自己写了文件 → 这次结果不能用来证明交付状态通过。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")

    async def dirty_judge(spec, **_kw):
        (workspace / "generated.txt").write_text("x", encoding="utf-8")
        return _v(True)

    monkeypatch.setattr("ee.core.goal.judge", dirty_judge)
    spec = GoalSpec(command="pytest -q", cwd=str(th.cwd))
    reason = _gate(th, s, spec, source="first_try")
    assert reason == "exhausted"
    assert _delivery(th)["status"] == "failed"
    assert _delivery(th)["target"]["source_unchanged"] is False


def test_acceptance_basis_change_blocks_met(ee_home, workspace):
    """本轮里 conftest.py 被改过 → needs_review，且点名变了哪个文件。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    (workspace / "conftest.py").write_text("# 放宽了断言\n", encoding="utf-8")

    spec = GoalSpec(command="pytest -q", cwd=str(th.cwd))
    reason = _gate(th, s, spec, source="first_try")
    assert reason == "exhausted"
    d = _delivery(th)
    assert d["status"] == "needs_review"
    assert "conftest.py" in d["acceptance"]["detail"]


def test_regression_timeout_blocks_met(ee_home, workspace, monkeypatch):
    """目标过了但回归命令超时 → 不能报 met，状态要能区分成 timeout。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    th._judge_recent = {"target": {"verdict": _v(True), "tree": s.disk_capture(), "source_unchanged": True}}

    async def timed_out_judge(spec, **_kw):
        return _v(met=False, timed_out=True)

    monkeypatch.setattr("ee.core.goal.judge", timed_out_judge)
    p2p = GoalSpec(command="pytest -q tests/", cwd=str(th.cwd), min_tests=1)
    reason = _gate(th, s, GoalSpec(command="pytest -q", cwd=str(th.cwd)), p2p, source="first_try")
    assert reason == "exhausted"
    assert _delivery(th)["regression"]["status"] == "timeout"


def test_delivered_state_changed_before_verify(ee_home, workspace):
    """写回后工作区又被改过 → 不能拿"现在"去验证写回结果。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    reason = _gate(th, s, GoalSpec(command="pytest -q", cwd=str(th.cwd)),
                   source="apply", delivered_tree="tree-that-is-not-current")
    assert reason == "exhausted"
    assert _delivery(th)["state"]["changed_after_delivery"] is True


def test_apply_content_mismatch_blocks_met(ee_home, workspace):
    """预期状态对不上（外部改了同一文件的别处）→ 不 met。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    info = {"expected": {"ok": True, "uncovered": []},
            "mismatches": [{"path": "a.txt", "kind": "content"}], "result": {"applied": True}}
    reason = _gate(th, s, GoalSpec(command="pytest -q", cwd=str(th.cwd)),
                   source="apply", apply_info=info)
    assert reason == "exhausted"
    assert _delivery(th)["status"] == "failed"


def test_apply_without_expected_state_blocks_met(ee_home, workspace):
    """构造不出预期状态（快照覆盖不到 / git 出错）→ 宁可说"没验证到"，也不报 met。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    info = {"expected": {"ok": False, "reason": "快照未覆盖 a.txt"}, "mismatches": [],
            "result": {"applied": True}}
    reason = _gate(th, s, GoalSpec(command="pytest -q", cwd=str(th.cwd)),
                   source="apply", apply_info=info)
    assert reason == "exhausted"


def test_user_stop_during_verify_marks_interrupted(ee_home, workspace, monkeypatch):
    """复验期间点停止：判据被取消，交付状态记 interrupted，绝不 met。"""
    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th._accept0 = acceptance.fingerprint(th.cwd, "pytest -q")
    started = asyncio.Event()

    async def slow_judge(spec, **_kw):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("ee.core.goal.judge", slow_judge)

    async def main():
        task = asyncio.ensure_future(th._delivery_gate(
            s, GoalSpec(command="pytest -q", cwd=str(th.cwd)), None, source="first_try"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert _delivery(th)["status"] == "interrupted"
