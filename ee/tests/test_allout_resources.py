from types import SimpleNamespace

from ee.core.allout import derive
from ee.core.hwprobe import Hardware, probe


def plan(hardware, **kwargs):
    return derive(config={}, profile=SimpleNamespace(context_window=500000),
                  hardware=hardware, has_goal=True, max_parallel=6, **kwargs)


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
    settings = dict(config={"max_steps": 50, "max_steps_segments": 3},
                    profile=SimpleNamespace(context_window=500000), has_goal=True,
                    hardware=Hardware(32, 32, 100), max_parallel=6)
    assert derive(**settings).judge_slots == 1
    high = derive(**settings, parallel_validation=True)
    assert high.judge_slots == 3
    assert high.explore_slots == 3
    assert high.max_steps == 92
    assert derive(**{**settings, "hardware": Hardware(32, 4, 100)},
                  parallel_validation=True).judge_slots == 1
