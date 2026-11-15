from types import SimpleNamespace

from ee.core.allout import derive
from ee.core.hwprobe import Hardware, probe


def plan(hardware, gate=6, **kwargs):
    return derive(config={}, profile=SimpleNamespace(context_window=500000),
                  hardware=hardware, has_goal=True, candidate_gate=gate, **kwargs)


def test_resources_really_limit_parallelism():
    assert plan(Hardware(32, 32, 100)).candidates == 6
    assert plan(Hardware(32, 4, 100)).candidates == 1
    assert plan(Hardware(32, 32, 4)).candidates == 1
    assert plan(Hardware(32, None, 100)).candidates == 1
    assert plan(Hardware(32, 32, None)).candidates == 1
    assert plan(Hardware(32, 32, 100), max_candidates=2).candidates == 2


def test_probe_is_bounded_and_handles_missing_disk(tmp_path):
    hw = probe(tmp_path)
    assert hw.cpu_count >= 1
    assert hw.disk_free_gb is not None and hw.disk_free_gb >= 0
    assert probe(tmp_path / "missing").disk_free_gb is None


def test_context_budget_reaches_feedback():
    from ee.core.goal_feedback import build_feedback

    budget = plan(Hardware(32, 32, 100)).feedback_chars
    verdict = SimpleNamespace(output="x" * 16000)
    assert "x" * 16000 in build_feedback(verdict, SimpleNamespace(), attempt=1,
                                         max_checks=3, output_limit=budget)


def test_no_goal_review_runs_after_implementation_and_respects_stop(ee_home, workspace):
    import asyncio
    from test_allout_run import _thread, _stub_turn

    th = _thread(ee_home, workspace)
    calls = _stub_turn(th, ["completed", "completed"])
    assert asyncio.run(th._run_allout_review("任务", None, True)) == "completed"
    assert len(calls) == 2
    assert all("不得执行" in prompt for prompt in calls)
    calls = _stub_turn(th, ["interrupted"])
    assert asyncio.run(th._run_allout_review("任务", None)) == "interrupted"
    assert len(calls) == 1


def test_generated_validation_reuses_implementation_and_freezes_approval(ee_home, workspace, monkeypatch):
    import asyncio
    from ee.core.events import TurnDone
    from test_allout_run import _thread

    th = _thread(ee_home, workspace)
    session = th.ensure_session()
    tests = workspace / "test_generated.py"
    tests.write_text("def test_item(): assert True\n")
    calls = []

    async def implementation(*args, **kwargs):
        calls.append("implementation")
        yield TurnDone("completed", 'EE_VALIDATION: {"command":"pytest -q","files":["test_generated.py"]}')

    async def approve(proposal):
        assert "test_generated.py" in proposal.note
        tests.write_text("def test_item(): assert False\n")
        return True

    async def judge(spec):
        raise AssertionError("验收文件在批准期间变化，不能执行")

    monkeypatch.setattr(session, "run_turn", implementation)
    monkeypatch.setattr(th, "_confirm_probe_goal", approve)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    assert asyncio.run(th._run_allout_review("任务", None)) == "exhausted"
    assert calls == ["implementation"]


def test_generated_validation_failure_feeds_repair(ee_home, workspace, monkeypatch):
    import asyncio
    from ee.core.events import TurnDone
    from ee.core.goal import Verdict
    from test_allout_run import _thread

    th = _thread(ee_home, workspace)
    session = th.ensure_session()
    (workspace / "test_generated.py").write_text("def test_item(): assert True\n")
    prompts = []
    calls = []

    async def run_turn(prompt, *args, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            yield TurnDone("completed", 'EE_VALIDATION: {"command":"pytest -q","files":["test_generated.py"]}')
        else:
            yield TurnDone("completed")

    async def approve(_proposal):
        return True

    async def judge(spec):
        calls.append(spec.command)
        if len(calls) == 1:
            return Verdict(exit=1, tests=1, output="FAILED: missing boundary")
        return Verdict(exit=0, tests=1, output="1 passed")

    async def deliver(*args, **kwargs):
        return "met"

    monkeypatch.setattr(session, "run_turn", run_turn)
    monkeypatch.setattr(th, "_confirm_probe_goal", approve)
    monkeypatch.setattr(th, "_delivery_gate", deliver)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    assert asyncio.run(th._run_allout_review("实现功能", None)) == "met"
    assert len(prompts) == 2 and "missing boundary" in prompts[1]
    assert set(calls) == {"pytest -q"}


def test_parallel_validation_is_opt_in_and_resource_capped():
    prof = SimpleNamespace(context_window=500000)
    cfg = {"max_steps": 50, "max_steps_segments": 3}
    assert derive(config=cfg, profile=prof, has_goal=True,
                  hardware=Hardware(32, 32, 100), candidate_gate=6).judge_slots == 1
    high = derive(config=cfg, profile=prof, has_goal=True,
                  hardware=Hardware(32, 32, 100), candidate_gate=6, parallel_validation=True)
    assert high.judge_slots == 3
    assert high.explore_slots == 3
    assert high.max_steps == 92
    low = derive(config=cfg, profile=prof, has_goal=True,
                 hardware=Hardware(32, 4, 100), candidate_gate=6, parallel_validation=True)
    assert low.judge_slots == 1


# ---------------------------------------------------------------- 资源矩阵与项目信号

def test_project_size_raises_per_lane_estimate():
    """重工程提高每路估算：同样的内存，能跑的路数更少（不是靠"语言类型"猜）。"""
    light = plan(Hardware(32, 8, 100, project_gb=0.2, project_complete=True))
    heavy = plan(Hardware(32, 8, 100, project_gb=4.0, project_complete=True))
    assert light.per_lane_gb == 2.0 and heavy.per_lane_gb == 3.0
    assert light.candidates >= heavy.candidates, (light.candidates, heavy.candidates)


def test_observed_judge_time_raises_per_lane_estimate():
    """上次判据很慢（>=120s）：按重工程估，且理由里写明这是观测到的耗时。"""
    got = plan(Hardware(32, 8, 100, project_gb=0.1, project_complete=True),
               observed_judge_ms=150_000)
    assert got.per_lane_gb == 3.0
    assert any("150s" in r for r in got.reasons)


def test_limiting_factor_is_explicit():
    """限制因素要能被界面说清：CPU / 上下文 / 闸门 / 用户上限 / 资源。"""
    assert plan(Hardware(4, 32, 100)).limiting == "cpu"
    assert plan(Hardware(32, 32, 100), gate=1).limiting == "gate"
    assert plan(Hardware(32, 32, 100), max_candidates=2).limiting == "user"
    low_ram = plan(Hardware(32, 4, 100))
    assert low_ram.limiting == "resource" and low_ram.candidates == 1


def test_resource_drop_reduces_candidates():
    """资源下降时计划必须跟着收紧（同一项目、同一配置）。"""
    plenty = plan(Hardware(32, 16, 100, project_gb=0.1, project_complete=True))
    tight = plan(Hardware(32, 5, 100, project_gb=0.1, project_complete=True))
    assert plenty.candidates > tight.candidates


def test_unknown_hardware_stays_single_lane():
    assert plan(Hardware(32, None, None)).candidates == 1


def test_project_size_scan_is_bounded(tmp_path):
    from ee.core.hwprobe import project_size

    (tmp_path / "a.bin").write_bytes(b"0" * 2048)
    gb, complete = project_size(tmp_path)
    assert complete and gb > 0
    gb2, complete2 = project_size(tmp_path, max_entries=1)
    assert complete2 is False and gb2 >= 0


def test_plan_has_no_show_only_max_rounds():
    """轮次语义：全力以赴 = 1 次首判 + repair_rounds；不再有只用于展示的 max_rounds。"""
    got = plan(Hardware(32, 32, 100))
    assert "max_rounds" not in got.as_dict()
