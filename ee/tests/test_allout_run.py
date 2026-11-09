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


def test_run_allout_applies_plan_effort_on_new_session(ee_home, workspace, monkeypatch):
    """推理强度是设计里最大的杠杆：新会话必须真的切到 plan.effort，而不是只写在文案里。"""
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3, "3 passed")])
    _stub_turn(th, ["completed"])
    monkeypatch.setattr(A, "derive", lambda **_kw: A.AlloutPlan(
        candidates=1, repair_rounds=1, max_rounds=2, effort="high", max_steps=50,
        temperature_ladder=(0.0,), verified=True, reasons=(), degradations=()))
    reason = asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                                        {"enabled": True, "candidates": 1}))
    assert reason == "met"
    from ee.core import reasoning as RZ
    want = RZ.canonical(th.session.profile, "high")   # 档案可能把 high 映射成 max
    assert th.session.effort == want, "新会话必须切到推导出的最高强度（按档案规整后）"
    assert th.meta.get("effort") == want


def test_run_allout_leaves_used_session_effort_alone(ee_home, workspace, monkeypatch):
    """已有会话中途换档会断前缀缓存（effort_switch），保持原档不动。"""
    import ee.core.allout as A

    th = _thread(ee_home, workspace)
    _patch_judge(monkeypatch, [_Verdict(True, 3, "3 passed")])
    _stub_turn(th, ["completed"])
    th.session.set_effort("low")        # 先落到一个与 plan.effort 不同的档，守卫才有可观测效果
    th.session.turns = 7
    before = th.session.effort
    monkeypatch.setattr(A, "derive", lambda **_kw: A.AlloutPlan(
        candidates=1, repair_rounds=1, max_rounds=2, effort="high", max_steps=50,
        temperature_ladder=(0.0,), verified=True, reasons=(), degradations=()))
    asyncio.run(th._run_allout("修好它", None, {"command": "pytest -q", "max_rounds": 3},
                               {"enabled": True, "candidates": 1}))
    assert th.session.effort == before, "已有会话不能被中途换档"
    assert th.meta.get("effort") is None

