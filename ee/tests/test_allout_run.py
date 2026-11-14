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
        async def run_turn(text, images=None, **_kw):
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


# ---------------------------------------------------------------- 候选审批


def test_candidate_approval_reaches_main_thread(ee_home, workspace):
    """候选的审批必须落到主会话（出卡片 / 走 auto_approve），不能一律拒绝。

    真模型演练里主会话能自动批准 pytest，而候选被一律拒绝——分叉路径会因此
    永远跑不完。这里守的是接线本身。
    """
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    fut = asyncio.new_event_loop().create_future()
    ev = ApprovalRequest("c1", "shell", "运行命令：python -m pytest -q", detail="python -m pytest -q",
                         prefix="python -m pytest", future=fut)
    handled = th._allout_candidate_event(ev, "allout-1")
    assert handled is True
    key = "allout-1/c1"
    assert key in th.approvals, "审批要注册到主会话，用户才能回应"
    assert th.approvals[key][0] is fut, "回填的必须是候选等待的那个 future"
    item = th.approvals[key][1]
    assert item["type"] == "approval"
    assert item["call_id"] == key
    assert "全力以赴候选" in item["title"]


def test_candidate_approval_decision_resolves_future(ee_home, workspace):
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    fut = asyncio.new_event_loop().create_future()
    ev = ApprovalRequest("c2", "shell", "运行命令：pytest", future=fut)
    th._allout_candidate_event(ev, "allout-2")
    th.notify = lambda m, p: None
    th.respond("allout-2/c2", "once")
    assert fut.done()
    assert fut.result().choice == "once"


def test_candidate_question_does_not_block(ee_home, workspace):
    """候选的 ask_user 已在配置里关掉；万一出现也不能卡到超时。"""
    from ee.core.events import QuestionRequest

    th = _thread(ee_home, workspace)
    fut = asyncio.new_event_loop().create_future()
    ev = QuestionRequest("q1", "用哪个方案？", future=fut)
    assert th._allout_candidate_event(ev, "allout-1") is True
    assert fut.done() and "自行判断" in fut.result()


def test_normal_event_not_consumed(ee_home, workspace):
    """非阻塞事件不能被吞掉，否则候选的工具输出不会进主会话。"""
    from ee.core.events import Notice

    th = _thread(ee_home, workspace)
    assert th._allout_candidate_event(Notice("hi", "info"), "allout-1") is False


def test_candidate_approval_follows_main_session_auto_approve(ee_home, workspace):
    """无人值守下，候选的普通命令审批由 auto_approve 自动批准（与主会话同一档位）。

    守的是「提高权限」这条路径：主会话开 auto，候选就该跟着 auto——
    否则候选连 pytest 都跑不了，分叉永远失败（2026-11-09 实测过）。
    """
    import asyncio as aio

    from ee import headless
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    th.notify = lambda m, p: None
    fut = aio.new_event_loop().create_future()
    # 走真实路径：候选事件先改名注册到主会话，再由 auto_approve 按主会话档位决定
    ev = ApprovalRequest("c3", "shell", "运行命令：python -m pytest -q",
                         prefix="python -m pytest", future=fut)
    th._allout_candidate_event(ev, "allout-1")
    renamed = next(it for k, (f, it) in th.approvals.items() if f is fut)
    appro = ApprovalRequest("allout-1/c3", "shell", "运行命令：python -m pytest -q",
                            prefix="python -m pytest", future=fut)
    headless.auto_approve(th, appro, "auto")
    assert renamed["call_id"] == "allout-1/c3"
    assert fut.done() and fut.result().choice == "once", "普通命令应被自动批准"


def test_candidate_approval_force_still_denied_under_auto(ee_home, workspace):
    """危险命令（force=True）在无人值守下仍被拒绝，不因 allout 放宽。"""
    import asyncio as aio

    from ee import headless
    from ee.core.events import ApprovalRequest

    th = _thread(ee_home, workspace)
    th.notify = lambda m, p: None
    fut = aio.new_event_loop().create_future()
    ev = ApprovalRequest("allout-1/c4", "shell", "运行命令：git push --force",
                         prefix="git push", future=fut, force=True)
    th._on_event(ev)
    decision, why = headless.auto_approve(th, ev, "auto")
    assert decision == "denied"
    assert fut.done() and fut.result().choice == "deny_with_reason"


def _notify_with_answers(th, notes, answers, choice):
    """收通知；遇到审批卡片就排一个回复。

    必须用 call_soon 延后：`_on_event` 先 `_add()`（同步发 item/started 通知）
    再登记 `self.approvals`，在通知回调里同步 `respond()` 会撞「已失效」。
    延后的回调在 `_confirm_probe_goal` await future 让出控制权后执行，此时已登记。
    """
    def notify(m, p):
        notes.append((m, p))
        if m == "item/started" and p["item"].get("type") == "approval":
            cid = p["item"]["call_id"]
            answers.append(cid)
            asyncio.get_running_loop().call_soon(th.respond, cid, choice)
    th.notify = notify


def _turn_end_reason(th) -> str:
    ends = [it for it in th.items if it.get("type") == "turn_end"]
    assert ends, "没有 turn_end 条目"
    return ends[-1]["reason"]


def test_probe_goal_asked_once_then_confirmed(ee_home, workspace, monkeypatch):
    """没填目标时探测到判据：先弹一次确认，确认后走完整流程并写回 meta。"""
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3, "3 passed")])
    _stub_turn(th, ["completed"])
    notes, answers = [], []
    _notify_with_answers(th, notes, answers, "once")

    asyncio.run(th._consume("修好它", None, None, {"enabled": True, "candidates": 1}))

    assert _turn_end_reason(th) == "met"
    assert len(answers) == 1, "探测到的判据必须先问一次"
    assert th.meta.get("allout", {}).get("goal_confirmed") == "python -m pytest -q"
    plans = [p for m, p in notes if m == "allout_plan"]
    assert plans[0]["probe"]["command"] == "python -m pytest -q"
    # 确认前推的是降级版（单路、未验证）；确认后必须重新推导并再推一条完整版，
    # 否则界面会一直显示「全力以赴 · 1 路」而实际按完整火力执行。
    assert len(plans) == 2, f"确认后应重新推导并再推一条 allout_plan，实际 {len(plans)} 条"
    assert plans[0]["plan"]["verified"] is False and plans[0]["plan"]["candidates"] == 1
    assert plans[1]["plan"]["verified"] is True
    assert plans[1]["goal"] == "python -m pytest -q"


def test_probe_goal_denied_degrades_without_judge(ee_home, workspace, monkeypatch):
    """用户这次不确认：按单路跑，不跑判据，也不写确认标记。"""
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    th = _thread(ee_home, workspace)
    import ee.core.goal as G

    called = []

    async def fake(spec, **_kw):
        called.append(1)
        return _Verdict(True, 1, "1 passed")

    monkeypatch.setattr(G, "judge", fake)
    _stub_turn(th, ["completed"])
    notes, answers = [], []
    _notify_with_answers(th, notes, answers, "deny")

    asyncio.run(th._consume("修好它", None, None, {"enabled": True, "candidates": 1}))

    assert _turn_end_reason(th) == "completed"
    assert not called, "没确认判据就不能执行它"
    assert th.meta.get("allout", {}).get("goal_confirmed") is None
    notices = [p["item"]["text"] for m, p in notes if m == "item/started"
               and p["item"].get("type") == "notice"]
    assert any("没有确认" in x for x in notices), notices


def test_plan_allout_applies_effort_on_new_session(ee_home, workspace, monkeypatch):
    """开了全力以赴，新会话必须真的切到推导出的最高强度，而不是只写在文案里。"""
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    th.ensure_session()
    monkeypatch.setattr(A, "derive", lambda **_kw: A.AlloutPlan(
        candidates=1, repair_rounds=1, max_rounds=2, effort="high", max_steps=50,
        temperature_ladder=(0.0,), verified=True, reasons=(), degradations=()))
    asyncio.run(th._plan_allout({"enabled": True, "candidates": 1},
                                {"command": "pytest -q", "max_rounds": 3}))
    from ee.core import reasoning as RZ
    want = RZ.canonical(th.session.profile, "high")   # 档案可能把 high 映射成 max
    assert th.session.effort == want, "新会话必须切到推导出的最高强度（按档案规整后）"
    assert th.meta.get("effort") == want


def test_plan_allout_leaves_used_session_effort_alone(ee_home, workspace, monkeypatch):
    """已有会话中途换档会断前缀缓存（effort_switch），保持原档不动。"""
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    th.ensure_session()
    th.session.set_effort("low")        # 先落到一个与 plan.effort 不同的档，守卫才有可观测效果
    th.session.turns = 7
    before = th.session.effort
    monkeypatch.setattr(A, "derive", lambda **_kw: A.AlloutPlan(
        candidates=1, repair_rounds=1, max_rounds=2, effort="high", max_steps=50,
        temperature_ladder=(0.0,), verified=True, reasons=(), degradations=()))
    asyncio.run(th._plan_allout({"enabled": True, "candidates": 1},
                                {"command": "pytest -q", "max_rounds": 3}))
    assert th.session.effort == before, "已有会话不能被中途换档"
    assert th.meta.get("effort") is None


def test_plan_allout_degraded_path_also_maxes_effort(ee_home, workspace):
    """没有判据的降级路径（不走 _run_allout）也要拉满：用户开了全力以赴就该拿到最高强度。"""
    from ee.core import reasoning as RZ
    from ee.core.allout import max_effort

    th = _thread(ee_home, workspace)
    th.ensure_session()
    lv = max_effort(th.session.profile)
    assert lv, "测试环境的默认模型档案应支持调节强度"
    asyncio.run(th._plan_allout({"enabled": True, "candidates": 3}, None))
    want = RZ.canonical(th.session.profile, lv)
    assert th.session.effort == want
    assert th.meta.get("effort") == want


def test_candidate_ok_requires_met_and_not_regressed():
    from ee.core.allout import candidate_ok

    class _V:
        def __init__(self, met): self.met = met

    assert candidate_ok({"verdict": _V(True), "regressed": False})
    assert not candidate_ok({"verdict": _V(True), "regressed": True}), "弄坏旧测试的候选不能算通过"
    assert not candidate_ok({"verdict": _V(False), "regressed": False})
    assert not candidate_ok({"verdict": None, "regressed": False})


def test_run_allout_regression_gate_forces_repair(ee_home, workspace, monkeypatch):
    """首轮目标判据过了但回归命令失败：不能报 met，必须进修复轮。"""
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3), _Verdict(False, 1, "regressed"),
                               _Verdict(True, 3), _Verdict(True, 5)])
    calls = _stub_turn(th, ["completed", "completed"])
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    reason = asyncio.run(th._run_allout("修好它", None,
                                        {"command": "pytest -q", "max_rounds": 3,
                                         "pass_to_pass": "pytest -q tests/"},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "met"
    assert len(calls) == 2, "回归失败必须触发修复轮"
    how = [p.get("how") for m, p in notes if m == "allout_progress" and p.get("phase") == "done"]
    assert how == ["repair"]


def test_run_allout_regression_suite_larger_than_target_suite(ee_home, workspace, monkeypatch):
    """回归套件比目标判据大时，不能被 keep-policy 误判成「测试数下降」而丢弃修复。

    回归命令跑 100 个测试、目标判据只跑 3 个；修复后目标仍是 3 个——
    比较必须发生在「同一命令」的前后，否则正常修复会被永久丢弃。
    """
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3), _Verdict(False, 100),
                               _Verdict(True, 3), _Verdict(True, 200)])
    calls = _stub_turn(th, ["completed", "completed"])
    reason = asyncio.run(th._run_allout("修好它", None,
                                        {"command": "pytest -q tests/test_x.py", "max_rounds": 3,
                                         "pass_to_pass": "pytest -q"},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "met", "回归套件更大不该把修复误判为退步"
    assert len(calls) == 2


def test_run_allout_regressed_result_never_reported_as_met(ee_home, workspace, monkeypatch):
    """修复始终弄坏旧测试时：即使目标判据是绿的，最终也只能报 exhausted，不能报 met。"""
    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3), _Verdict(False, 100),
                               _Verdict(True, 3), _Verdict(False, 100)])
    _stub_turn(th, ["completed", "completed"])
    reason = asyncio.run(th._run_allout("修好它", None,
                                        {"command": "pytest -q tests/test_x.py", "max_rounds": 3,
                                         "pass_to_pass": "pytest -q"},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "exhausted", "回归门没通过就不能算达成"


def test_run_allout_filters_regressed_candidates_before_rank(ee_home, workspace, monkeypatch):
    """回归门拦下的候选不进 A/B 排序；pass_to_pass 必须透传给分叉。"""
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(False, 1)])
    _stub_turn(th, ["completed", "completed"])
    monkeypatch.setattr(A, "derive", lambda **_kw: A.AlloutPlan(
        candidates=2, repair_rounds=1, max_rounds=3, effort="", max_steps=50,
        temperature_ladder=(0.0, 0.3), verified=True, reasons=(), degradations=()))
    seen = {}

    async def fake_fanout(spec, plan, task_text, cap, pass_to_pass=None):
        seen["p2p"] = pass_to_pass
        bad = {"id": "cand-1", "verdict": _Verdict(True, 3), "regressed": True,
               "cwd": str(workspace), "base": "x"}
        good = {"id": "cand-2", "verdict": _Verdict(True, 3), "regressed": False,
                "cwd": str(workspace), "base": "x"}
        return [bad, good]

    ranked = []

    async def fake_rank(passed, task, judge_output):
        ranked.extend(c["id"] for c in passed)
        return None

    monkeypatch.setattr(th, "_allout_fanout", fake_fanout)
    monkeypatch.setattr(th, "_allout_rank", fake_rank)
    reason = asyncio.run(th._run_allout("修好它", None,
                                        {"command": "pytest -q", "max_rounds": 3,
                                         "pass_to_pass": "pytest -q tests/"},
                                        {"enabled": True, "candidates": 2}))
    assert seen["p2p"] == "pytest -q tests/", "回归命令必须透传到分叉"
    assert ranked == ["cand-2"], "回归的候选不能进排序"
    assert reason == "exhausted"



# ---------------------------------------------------------------- 取消时回收候选

def _fanout_fakes(monkeypatch, workspace, hang_judge=True):
    """把 worktree / Session / judge 全换成替身，返回记录用的容器。"""
    import ee.core.goal as G
    from ee.core import worktree as W

    seen = {"created": [], "finished": []}

    def fake_create(cwd, label="", home=None):
        seen["created"].append(label)
        return {"cwd": str(workspace), "agent_base": "b", "base": "b",
                "path": str(workspace / label)}

    def fake_finish(info, message, home=None):
        seen["finished"].append(info["path"])
        return {"file_count": 0, "files": [], "additions": 0, "deletions": 0}

    monkeypatch.setattr(W, "agent_eligible", lambda cwd: (True, ""))
    monkeypatch.setattr(W, "create_for_agent", fake_create)
    monkeypatch.setattr(W, "finish_agent", fake_finish)

    if hang_judge:
        async def hang(spec, **_kw):
            await asyncio.sleep(3600)          # 卡在判据：候选活着且占着 worktree
        monkeypatch.setattr(G, "judge", hang)
    return seen


def _patch_fake_child(monkeypatch):
    """候选 Session 用替身：不进真实模型回合，直接落到判据。"""
    from types import SimpleNamespace

    class _FakeChild:
        def __init__(self, *a, **kw):
            self.spec = SimpleNamespace(alias="m-effort-unknown")
            self.stats = SimpleNamespace(totals=lambda: {"cost": 0.0})

        async def run_turn(self, text, images=None, **_kw):
            if False:              # 空异步生成器
                yield None

    monkeypatch.setattr("ee.server.threads.Session", _FakeChild)


def test_cancel_reaps_candidate_tasks(ee_home, workspace, monkeypatch):
    """父回合被取消时，候选任务必须一起停下并回收 worktree。

    候选是独立 asyncio task：取消外层 task 不会传下去。`await watch()` 抛
    CancelledError 时，原来的 `gather` 在它后面根本不会执行——候选继续跑模型、
    跑判据、占着 worktree（2026-11 独立审查实测：父回合结束后仍有候选在跑）。
    """
    import pytest
    from ee.core.goal import GoalSpec

    th = _thread(ee_home, workspace)
    th.ensure_session()
    th._allout_judge_lock = asyncio.Lock()
    seen = _fanout_fakes(monkeypatch, workspace)
    _patch_fake_child(monkeypatch)

    async def main():
        plan = type("P", (), {"temperature_ladder": [0.1, 0.2, 0.3]})()
        task = asyncio.ensure_future(th._allout_fanout(
            GoalSpec(command="pytest -q", cwd=str(workspace)), plan, "修好它", 2))
        await asyncio.sleep(0.1)               # 让两个候选都进到判据
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(seen["created"]) == 2
    assert len(seen["finished"]) == 2, "取消后每个候选的 worktree 都要收尾，不能留半个"


def test_cancel_during_worktree_creation_reaps_it(ee_home, workspace, monkeypatch):
    """创建还在工作线程里时被取消：创建停不下来，但落地后必须立刻回收。

    `asyncio.to_thread` 不能被取消。直接丢掉 future，`git worktree add` 照样跑完，
    留下一个没有主人的 worktree。
    """
    import threading

    import pytest
    from ee.core.goal import GoalSpec
    from ee.core import worktree as W

    th = _thread(ee_home, workspace)
    th.ensure_session()
    th._allout_judge_lock = asyncio.Lock()
    seen = _fanout_fakes(monkeypatch, workspace)
    _patch_fake_child(monkeypatch)

    gate = threading.Event()

    def slow_create(cwd, label="", home=None):
        gate.wait(5)
        seen["created"].append(label)
        return {"cwd": str(workspace), "agent_base": "b", "base": "b",
                "path": str(workspace / label)}

    monkeypatch.setattr(W, "create_for_agent", slow_create)

    async def main():
        plan = type("P", (), {"temperature_ladder": [0.1]})()
        task = asyncio.ensure_future(th._allout_fanout(
            GoalSpec(command="pytest -q", cwd=str(workspace)), plan, "修好它", 1))
        await asyncio.sleep(0.05)              # 此时卡在 to_thread 的创建里
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.sleep(0.3)               # 等工作线程落地 + 回收任务跑完

    asyncio.run(main())
    assert len(seen["created"]) == 1
    assert seen["finished"] == [str(workspace / "allout-1")], "创建完的 worktree 必须被回收"


# ---------------------------------------------------------------- 丢弃修复要真回滚

def test_discarded_repair_is_reverted_in_workspace(ee_home, workspace, monkeypatch):
    """keep-policy 说「丢弃」时，磁盘上的修复也必须撤掉。

    修复轮直接跑在主工作区：只改 kept / 判定结果的话，界面说「未保留」、回合以
    exhausted 收尾，工作区里留着的却是那份没通过判据的改动（2026-11 独立审查实测）。
    """
    th = _thread(ee_home, workspace)
    th.ensure_session()
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(False, 1), _Verdict(False, 1)])
    _stub_turn(th, ["completed", "completed", "completed"])

    trees = iter([f"tree-{i}" for i in range(10)])
    th.session.disk_capture = lambda: next(trees)
    reverted: list[str | None] = []
    monkeypatch.setattr(th, "_revert_repair", lambda s, tree: reverted.append(tree) or ["a.txt"])
    notes = []
    th.notify = lambda m, p: notes.append((m, p))

    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "exhausted"
    assert reverted, "没通过判据的修复必须真的撤回"
    assert all(x is not None for x in reverted), "回滚必须拿到「修复前」的快照"
    repairs = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "repair"]
    assert repairs and all(p["kept"] is False for p in repairs)
    assert repairs[0]["reverted"] == 1, "回滚结果要报给界面，不能只写 kept"


def test_kept_repair_is_not_reverted(ee_home, workspace, monkeypatch):
    """保留的修复不能被回滚（否则「已保留」是假的）。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(True, 12)])
    _stub_turn(th, ["completed", "completed"])
    trees = iter([f"tree-{i}" for i in range(10)])
    th.session.disk_capture = lambda: next(trees)
    reverted: list[object] = []
    monkeypatch.setattr(th, "_revert_repair", lambda s, tree: reverted.append(tree) or ["a.txt"])
    th.notify = lambda m, p: None

    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "met"
    assert reverted == [], "保留的修复不该被动"


def test_revert_repair_files_restores_snapshot(ee_home, workspace, monkeypatch):
    """回滚本体：把工作区恢复到修复前，并把「尝试过的修复」存成恢复点。"""
    th = _thread(ee_home, workspace)
    th.ensure_session()
    s = th.session
    files = {"a.txt": "fix"}

    class _FakeDisk:
        def changed(self, a, b):
            return ["a.txt"] if a != b else []

        def capture(self):
            return "tree-" + repr(sorted(files.items()))

        def restore(self, tree, paths):
            files["a.txt"] = "before"
            return paths

    monkeypatch.setattr(s, "disk", lambda: _FakeDisk())
    monkeypatch.setattr("ee.server.checkpoint_rpc.add_restore_point",
                        lambda t, tree, label: {"id": "rp-1", "tree": tree, "label": label})
    monkeypatch.setattr(s, "_after_restore", lambda restored, note: None)
    restored = th._revert_repair(s, "tree-before")
    assert restored == ["a.txt"]
    assert files["a.txt"] == "before"


def test_failed_apply_never_reports_met(ee_home, workspace, monkeypatch):
    """胜者补丁写回失败时，回合不能报 met。

    写回走的是 `git apply`（全有或全无）；主目录脏了/冲突时它一个文件都不动。
    此时工作区里既没有候选的改动，第一轮的判据也还是红的——必须如实报 exhausted，
    否则界面显示「完成」而工作区什么都没成。
    """
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    th.ensure_session()
    _patch_judge(monkeypatch, [_Verdict(False, 1), _Verdict(False, 1)])   # 首轮 + 修复轮都红
    _stub_turn(th, ["completed", "completed"])
    th.session.disk_capture = lambda: None

    winner = {"id": "cand-1", "verdict": _Verdict(True, 3), "regressed": False,
              "cwd": str(workspace), "base": "b", "worktree": str(workspace)}

    async def fake_fanout(spec, plan, task_text, cap, pass_to_pass=None):
        return [winner]

    async def fake_rank(passed, task, judge_output):
        return passed[0]

    monkeypatch.setattr(th, "_allout_fanout", fake_fanout)
    monkeypatch.setattr(th, "_allout_rank", fake_rank)
    monkeypatch.setattr(A, "apply_winner", lambda *a, **k: {"applied": False, "files": 2,
                                                           "error": "补丁应用失败（主目录未改动）：冲突"})
    notes = []
    th.notify = lambda m, p: notes.append((m, p))

    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 2}))
    assert reason == "exhausted", f"写回失败却报 {reason}"
    applies = [p for m, p in notes if m == "allout_progress" and p.get("phase") == "apply"]
    assert applies and applies[-1]["result"]["applied"] is False
    assert "冲突" in applies[-1]["result"]["error"]
    # 状态文字收尾就清空了；失败原因与善后路径必须落进对话，否则用户只剩一个「未达成」
    notices = [it for it in th.items if it.get("type") == "notice"]
    assert any("没能写回主目录" in str(it.get("text")) and "worktree" in str(it.get("text"))
               for it in notices), f"缺少写回失败的说明：{notices}"
