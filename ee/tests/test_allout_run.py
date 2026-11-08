"""全力以赴的编排（allout 阶段 5）：`Thread._run_allout` 的闭环行为。

用假的判据函数与桩回合验证「预算不是配额」的核心承诺：
- 首回合就通过判据 → 只跑一个回合、零修复、零分叉；
- 修复没通过 → 不保留（keep-policy），最终如实报 exhausted；
- 用户中断 → 立即返回 interrupted，不继续烧钱；
- 判据调用串行（两个判定不会同时在跑）。

不做真实模型调用：`run_turn` 与 `judge` 都在模块导入点替换。
"""
from __future__ import annotations

import asyncio


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t1", home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


class _Verdict:
    def __init__(self, met: bool, tests: int = 3, output: str = "", reason: str = ""):
        self.met = met
        self.tests = tests
        self.output = output
        self.reason = reason or (f"ok：{tests} 个测试通过" if met else "exit=1：命令以非零退出")
        self.exit = 0 if met else 1
        self.timed_out = False
        self.min_tests = 1


def _stub_turn(th, script):
    """把 session.run_turn 换成按脚本产事件的桩，返回被调用的消息列表。"""
    from ee.core.events import TurnDone

    calls: list[str] = []

    def make(session):
        async def run_turn(text, images=None):
            calls.append(text)
            step = script.pop(0) if script else "completed"
            yield TurnDone(step, "")
        return run_turn

    th.ensure_session()
    th.session.run_turn = make(th.session)
    return calls


def _patch_judge(monkeypatch, results):
    """替换 ee.core.goal.judge（_run_allout 在调用时才 import，所以替换生效）。"""
    import ee.core.goal as G

    seq = list(results)

    async def fake(spec, **_kw):
        if seq:
            return seq.pop(0)
        return _Verdict(False, 1, "no more script")

    monkeypatch.setattr(G, "judge", fake)


def test_first_try_met_runs_exactly_one_turn(ee_home, workspace, monkeypatch):
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 12, "12 passed")])
    calls = _stub_turn(th, ["completed"])
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 3}))
    assert reason == "met"
    assert len(calls) == 1, "一次就过时不该跑任何额外回合"
    phases = [p.get("phase") for m, p in notes if m == "allout_progress"]
    assert "done" in phases


def test_repair_discarded_when_judge_still_fails(ee_home, workspace, monkeypatch):
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(False, 1), _Verdict(False, 1)])
    _stub_turn(th, ["completed", "completed", "completed"])
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "exhausted"
    repairs = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "repair"]
    assert repairs and all(p["kept"] is False for p in repairs), "没通过判据的修复不能保留"


def test_repair_kept_and_met(ee_home, workspace, monkeypatch):
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(True, 12)])
    calls = _stub_turn(th, ["completed", "completed"])
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "met"
    assert len(calls) == 2
    repairs = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "repair"]
    assert repairs and repairs[0]["kept"] is True


def test_interrupt_returns_immediately(ee_home, workspace, monkeypatch):
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(False, 1)])
    calls = _stub_turn(th, ["interrupted"])
    th.notify = lambda m, p: None
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 3}))
    assert reason == "interrupted"
    assert len(calls) == 1


def test_judge_calls_are_serialized(ee_home, workspace, monkeypatch):
    """两个判定同时发起时必须互斥（并发跑测试会造出假失败）。"""
    import ee.core.goal as G

    th = _thread(ee_home, workspace)
    _stub_turn(th, [])
    th.notify = lambda m, p: None
    inside = []
    peak = [0]

    async def fake(spec, **_kw):
        inside.append(1)
        peak[0] = max(peak[0], len(inside))
        await asyncio.sleep(0.01)
        inside.pop()
        return _Verdict(True, 3)

    monkeypatch.setattr(G, "judge", fake)

    async def go():
        await th._run_allout("x", None, {"command": "c", "max_rounds": 1},
                             {"enabled": True, "candidates": 1})

    asyncio.run(go())
    assert peak[0] == 1, "判据调用必须串行"
