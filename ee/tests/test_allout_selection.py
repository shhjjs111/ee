"""多候选择优必须真实等待其它候选，同时有时间边界。"""
import asyncio
from types import SimpleNamespace

import pytest

from ee.core.allout import AlloutPlan, max_effort
from ee.core.goal import GoalSpec
from test_allout_run import _thread, _fanout_fakes, _Verdict


def test_xhigh_is_normalized_to_effective_level():
    """xhigh 是 provider 的 wire 别名，不是计划档位；计划必须写实际会发的 high。"""
    assert max_effort(SimpleNamespace(effort_param="reasoning_effort",
                                     effort_levels=["high", "xhigh"])) == "high"


def test_selection_window_uses_judge_duration_and_caps():
    from ee.server.threads import Thread

    th = Thread("t", __import__("pathlib").Path("."), lambda m, p: None, {"cwd": "."}, persist=False)
    plan = AlloutPlan(selection_grace_seconds=5)
    th._allout_last_judge_ms = 60_000
    base, hard, judge_s = th._allout_selection_window(plan)
    assert base == 90.0, "1.5 × 60s，给已进入 testing 的候选交卷机会"
    assert hard == 240.0, "总等待上限 = 4 × 判据耗时，且不超过 300s"
    assert judge_s == 60.0
    th._allout_last_judge_ms = None
    base0, hard0, judge0 = th._allout_selection_window(plan)
    assert base0 == 5 and hard0 == 10 and judge0 is None


def test_side_review_inherits_effort_only_while_locked(workspace, monkeypatch):
    from conftest import make_session

    session, _ = make_session(workspace, [])
    calls = []

    class Client:
        async def stream(self, messages, tools, **kwargs):
            calls.append(kwargs)
            yield "text", "review"

    async def client(spec):
        return Client()

    monkeypatch.setattr(session, "ensure_role_client", client)
    monkeypatch.setattr(session, "effort_for", lambda spec: "high")
    session.lock_model()
    assert asyncio.run(session.side_complete("review", "patch", role="allout")) == "review"
    session.unlock_model()
    asyncio.run(session.side_complete("review", "patch", role="allout"))
    assert calls == [{"effort": "high"}, {}]


@pytest.mark.parametrize("slow_delay,expected", [(0.02, 2), (10, 1)])
def test_selection_grace_is_bounded(ee_home, workspace, monkeypatch, slow_delay, expected):
    th = _thread(ee_home, workspace)
    th.ensure_session()
    seen = _fanout_fakes(monkeypatch, workspace, hang_judge=False)
    prompts = []

    class Child:
        def __init__(self, *args, **kwargs):
            self.spec = th.session.spec
            self.stats = SimpleNamespace(totals=lambda: {"cost": 0})

        async def run_turn(self, text):
            prompts.append(text)
            if "根因路线" in text:
                await asyncio.sleep(slow_delay)
            if False:
                yield None

    async def judge(spec):
        return _Verdict(True)

    async def merge(*args):
        pass

    monkeypatch.setattr("ee.core.session.Session", Child)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    monkeypatch.setattr(th, "_allout_merge_usage", merge)

    async def run():
        th._allout_judge_lock = asyncio.Lock()
        plan = AlloutPlan(temperature_ladder=(0, 0.3), selection_grace_seconds=0.15)
        return await asyncio.wait_for(th._allout_fanout(
            GoalSpec(command="pytest", cwd=str(workspace)), plan, "修复", 2), 3)

    results = asyncio.run(run())
    completed = [r for r in results if not r.get("cancelled")]
    assert len(completed) == expected
    assert len(seen["finished"]) == 2
    assert len(set(prompts)) == 2
    assert all(r.get("verdict") and r["verdict"].met for r in completed)
    if slow_delay >= 1:
        cancelled = [r for r in results if r.get("cancelled")]
        assert cancelled, "被择优窗口取消的候选必须保留在 live/result 中供安全回收"
        assert not any(r.get("verdict") and r["verdict"].met for r in cancelled), \
            "取消候选即使有过绿色 verdict 也不能进入通过集"


def test_waiting_candidate_gets_time_after_it_enters_testing(ee_home, workspace, monkeypatch):
    """首个通过后，另一个还在 working 的候选随后进入 testing 也要被重新计算窗口。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    _fanout_fakes(monkeypatch, workspace, hang_judge=False)
    th._allout_last_judge_ms = 200

    class Child:
        def __init__(self, *args, **kwargs):
            self.spec = th.session.spec
            self.stats = SimpleNamespace(totals=lambda: {"cost": 0})

        async def run_turn(self, text, images=None, **_kw):
            if "根因路线" in text:
                await asyncio.sleep(0.25)
            if False:
                yield None

    calls = {"n": 0}

    async def judge(spec):
        calls["n"] += 1
        if calls["n"] == 2:
            await asyncio.sleep(0.5)
        return _Verdict(True)

    async def merge(*args):
        pass

    monkeypatch.setattr("ee.core.session.Session", Child)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    monkeypatch.setattr(th, "_allout_merge_usage", merge)
    th._allout_judge_lock = asyncio.Lock()
    plan = AlloutPlan(temperature_ladder=(0.0, 0.3), selection_grace_seconds=0.5)
    result = asyncio.run(asyncio.wait_for(
        th._allout_fanout(GoalSpec(command="pytest", cwd=str(workspace)), plan, "任务", 2), 3))
    assert len([r for r in result if not r.get("cancelled")]) == 2
    assert not any(r.get("cancelled") for r in result)


def test_candidate_verifiers_share_actual_slots(ee_home, workspace, monkeypatch):
    th = _thread(ee_home, workspace)
    th.ensure_session()
    _fanout_fakes(monkeypatch, workspace, hang_judge=False)
    active = peak = 0

    class Child:
        def __init__(self, *args, **kwargs):
            self.spec = th.session.spec

        async def run_turn(self, text):
            if False:
                yield None

    async def judge(spec):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return _Verdict(True)

    async def merge(*args):
        pass

    monkeypatch.setattr("ee.core.session.Session", Child)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    monkeypatch.setattr(th, "_allout_merge_usage", merge)

    async def run():
        th._allout_judge_lock = asyncio.Semaphore(2)
        th._allout_work_sem = asyncio.Semaphore(2)
        plan = AlloutPlan(temperature_ladder=(0, 0.3, 0.6), judge_slots=2)
        return await th._allout_fanout(GoalSpec("pytest", cwd=str(workspace)), plan, "任务", 3)

    assert len(asyncio.run(run())) == 3
    assert peak == 2
