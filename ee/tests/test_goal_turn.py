"""Goal 接入 turn/start 的校验与记录（T10-b 界面侧）。

界面走 turn/start（会话内发送），Goal 循环原先只在 `ee exec --goal` 里。

**本文件不启动真实回合。** 真实的 start_turn 末尾用 `asyncio.create_task` 起
`_consume`，而 Goal 模式下它会调 run_goal 反复跑真实回合——测试里没有模型，
会一直挂着（我第一版就是这么挂住的）。所以这里用 `_no_loop` 把 create_task
换成"关掉协程并返回占位"，只验校验与条目写入。

"跑起来会怎样"由 `test_goal_exec.py` 用假模型覆盖。
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t1", home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


@contextlib.contextmanager
def _no_loop():
    """拦掉 create_task：跑完 start_turn 的校验/记录，但不执行回合。

    替身要带 done()：Thread.running 会读 self.task.done()，返回字符串会炸。
    """
    real = asyncio.create_task

    class _Done:
        def done(self) -> bool:
            return True

        def cancel(self) -> None:
            pass

    def _stub(coro, **_kw):
        coro.close()          # 不执行，也不留 pending 任务
        return _Done()

    asyncio.create_task = _stub
    try:
        yield
    finally:
        asyncio.create_task = real


def _turns(th) -> list[dict]:
    return [i for i in th.items if i.get("type") == "user"]


# ---------------------------------------------------------------- 校验（纯函数）

def test_rejects_empty_command():
    from ee.server.threads import _norm_goal

    with pytest.raises(ValueError):
        _norm_goal({"command": "   "})


def test_rejects_missing_command():
    from ee.server.threads import _norm_goal

    with pytest.raises(ValueError):
        _norm_goal({})


def test_rejects_zero_rounds():
    """0 轮没有意义：拒绝，而不是静默当不跑。"""
    from ee.server.threads import _norm_goal

    with pytest.raises(ValueError):
        _norm_goal({"command": "pytest", "max_rounds": 0})


def test_rejects_non_numeric_rounds():
    from ee.server.threads import _norm_goal

    with pytest.raises(ValueError):
        _norm_goal({"command": "pytest", "max_rounds": "abc"})


def test_rejects_non_dict():
    from ee.server.threads import _norm_goal

    with pytest.raises(ValueError):
        _norm_goal("pytest -q")          # type: ignore[arg-type]  故意传错类型：界面坏数据要被挡下


def test_rounds_capped():
    """轮次有上限，防止界面传个巨大值把机器跑满。"""
    from ee.server.threads import MAX_GOAL_ROUNDS, _norm_goal

    g = _norm_goal({"command": "pytest", "max_rounds": 9999})
    assert g["max_rounds"] == MAX_GOAL_ROUNDS
    assert MAX_GOAL_ROUNDS <= 20


def test_defaults_applied():
    """只给命令时补默认轮次。"""
    from ee.server.threads import _norm_goal

    g = _norm_goal({"command": "pytest -q"})
    assert g["command"] == "pytest -q"
    assert g["max_rounds"] == 3


def test_command_trimmed_and_bounded():
    from ee.server.threads import _norm_goal

    assert _norm_goal({"command": "  pytest -q  "})["command"] == "pytest -q"
    assert len(_norm_goal({"command": "x" * 5000})["command"]) <= 2000


def test_timeout_optional_but_validated():
    from ee.server.threads import _norm_goal

    assert "timeout" not in _norm_goal({"command": "pytest"})
    assert _norm_goal({"command": "pytest", "timeout": 60})["timeout"] == 60
    assert _norm_goal({"command": "pytest", "timeout": 99999})["timeout"] <= 3600
    with pytest.raises(ValueError):
        _norm_goal({"command": "pytest", "timeout": "abc"})


def test_pass_to_pass_optional_and_trimmed():
    """回归命令可选；给了就裁剪空白，不给就不写入（老调用方行为不变）。"""
    from ee.server.threads import _norm_goal

    assert "pass_to_pass" not in _norm_goal({"command": "pytest"})
    g = _norm_goal({"command": "pytest", "pass_to_pass": " pytest -q tests/ "})
    assert g["pass_to_pass"] == "pytest -q tests/"


# ---------------------------------------------------------------- 条目写入

def test_goal_written_into_user_item(ee_home, workspace):
    """goal 要记到用户条目上，界面才知道这一回合在追什么目标。"""
    th = _thread(ee_home, workspace)
    with _no_loop():
        r = th.start_turn("修好它", goal={"command": "pytest -q", "max_rounds": 2})

    assert _turns(th)[0].get("goal") == {"command": "pytest -q", "max_rounds": 2}
    assert r.get("queued") is False


def test_no_goal_field_for_normal_turn(ee_home, workspace):
    """普通回合不写 goal 字段（老数据兼容，不塞空值）。"""
    th = _thread(ee_home, workspace)
    with _no_loop():
        th.start_turn("普通问题")

    assert "goal" not in _turns(th)[0]


def test_invalid_goal_rejected_before_starting_turn(ee_home, workspace):
    """坏判据在起回合之前就拒绝，不留下半截状态。"""
    th = _thread(ee_home, workspace)
    with _no_loop():
        with pytest.raises(ValueError):
            th.start_turn("x", goal={"command": ""})
    assert _turns(th) == [], "校验失败却写入了用户条目"


# ---------------------------------------------------------------- 回合内被停掉 / 检查点

def test_goal_loop_stops_when_model_turn_interrupted(ee_home, workspace, monkeypatch):
    """内核把「用户点停止」转成 `TurnDone("interrupted")`（不是异常）。

    `_consume_goal._one_turn` 必须把这个终态交给 run_goal，否则循环会接着判据、
    再起一轮模型回合：界面显示已停止，后台继续花钱。
    """
    from ee.core.events import TurnDone
    import ee.core.goal as G

    th = _thread(ee_home, workspace)
    th.ensure_session()
    calls: list[str] = []

    def make(session):
        async def run_turn(text, images=None, inner=False):
            calls.append(text)
            yield TurnDone("interrupted", "")
        return run_turn

    th.session.run_turn = make(th.session)
    judged: list[str] = []

    async def fake_judge(spec, **_kw):
        judged.append(spec.command)
        return G.Verdict(exit=1, tests=1, output="1 failed", min_tests=1)

    monkeypatch.setattr(G, "judge", fake_judge)
    notes: list[tuple[str, dict]] = []
    th.notify = lambda m, p: notes.append((m, p))

    reason = asyncio.run(th._consume_goal("修好它", None, {"command": "pytest -q", "max_rounds": 3}))
    assert reason == "interrupted"
    assert len(calls) == 1, "中断后不该再起模型回合"
    assert len(judged) == 1, "中断后不该再跑判据（首轮判定在前，属正常）"
    done = [p for m, p in notes if m == "goal_done"]
    assert done and done[-1]["stop_reason"] == "interrupted"


def test_goal_records_tree_after_from_inner_turn(ee_home, workspace, monkeypatch):
    """内层回合的 tree_after 要记到这一回合的用户条目上。

    不记的话 Goal 回合的 `tree_after` 永远是空的：检查点里看不到「回合结束后」的状态，
    /undo 只能退到「拿当前工作区推断改动」——用户在这之后的手改会被算进撤销范围。
    """
    from ee.core.events import TurnDone
    import ee.core.goal as G

    th = _thread(ee_home, workspace)
    th.ensure_session()
    usr = th._add({"type": "user", "text": "修好它", "goal": {"command": "pytest -q"}})
    usr["tree"] = "tree-before"          # 模拟回合开始时的快照已写入

    def make(session):
        async def run_turn(text, images=None, inner=False):
            yield TurnDone("completed", "", "tree-after")
        return run_turn

    th.session.run_turn = make(th.session)

    verdicts = [G.Verdict(exit=1, tests=1, output="1 failed", min_tests=1),
                G.Verdict(exit=0, tests=3, output="3 passed", min_tests=1)]

    async def fake_judge(spec, **_kw):
        return verdicts.pop(0)

    monkeypatch.setattr(G, "judge", fake_judge)
    th.notify = lambda m, p: None
    # 第一轮判定必须是红的，否则 run_goal 一个回合都不跑（先判后跑），也就没有 TurnDone
    reason = asyncio.run(th._consume_goal("修好它", None, {"command": "pytest -q", "max_rounds": 3}))
    assert reason == "met"
    assert usr.get("tree_after") == "tree-after", "Goal 回合必须记下回合后快照"


def test_goal_runs_regression_command(ee_home, workspace, monkeypatch):
    """普通 Goal 必须执行回归命令：目标判据过了、旧测试红了不能算 met。

    界面把「回归命令」标成在 Goal 里生效，前端也确实提交了 pass_to_pass；
    原来只有全力以赴用它，普通 Goal 静默丢掉整个字段（只收到目标判据就能 met）。
    """
    import ee.core.goal as G
    from ee.core.events import TurnDone

    th = _thread(ee_home, workspace)
    th.ensure_session()
    turns: list[str] = []

    def make(session):
        async def run_turn(text, images=None, inner=False):
            turns.append(text)
            yield TurnDone("completed", "")
        return run_turn

    th.session.run_turn = make(th.session)

    def verdict(met: bool, out: str, tests: int = 3):
        return G.Verdict(exit=0 if met else 1, tests=tests, output=out, min_tests=1)

    # 每一轮判定都会先跑目标、再跑回归：目标两轮都绿，回归先红后绿（修复轮之后）
    script = {"pytest -q tests/test_a.py": [verdict(True, "3 passed"), verdict(True, "3 passed")],
              "pytest -q tests/": [verdict(False, "2 failed, 5 passed"), verdict(True, "7 passed", 7)]}
    judged: list[str] = []

    async def fake_judge(spec, **_kw):
        judged.append(spec.command)
        return script[spec.command].pop(0)

    monkeypatch.setattr(G, "judge", fake_judge)
    notes = []
    th.notify = lambda m, p: notes.append((m, p))

    reason = asyncio.run(th._consume_goal("修好它", None, {
        "command": "pytest -q tests/test_a.py", "max_rounds": 2,
        "pass_to_pass": "pytest -q tests/"}))
    assert reason == "met"
    assert "pytest -q tests/" in judged, "回归命令必须真的被执行"
    assert turns, "回归失败后要有一个修复回合"
    assert "回归命令失败" in turns[0], "回归失败的输出要交给模型（否则它不知道该修什么）"
    notices = [it for it in th.items if it.get("type") == "notice"]
    assert any("回归命令未通过" in str(it.get("text")) for it in notices)


def test_goal_without_regression_command_keeps_old_behavior(ee_home, workspace, monkeypatch):
    """不填回归命令时不多跑任何判定（回归门是可选的）。"""
    import ee.core.goal as G

    th = _thread(ee_home, workspace)
    th.ensure_session()
    judged: list[str] = []

    async def fake_judge(spec, **_kw):
        judged.append(spec.command)
        return G.Verdict(exit=0, tests=3, output="3 passed", min_tests=1)

    monkeypatch.setattr(G, "judge", fake_judge)
    th.notify = lambda m, p: None
    reason = asyncio.run(th._consume_goal("修好它", None, {"command": "pytest -q", "max_rounds": 3}))
    assert reason == "met"
    assert judged == ["pytest -q"]


def test_inner_turn_keeps_queue_until_outer_turn_ends(workspace):
    """内层回合（Goal/全力以赴的一轮）不能把「待追加」提前退回界面。

    `run_turn` 在每个 TurnDone 处把没送进的排队消息退回界面；Goal 会连着调好几次
    `run_turn`，于是在第一轮结束就把用户「这一回合做完再追加」的消息蹦回输入框，
    而外层回合还在跑。现在内层回合用 `inner=True`，由外层收尾统一退一次。
    """
    from conftest import make_session
    from ee.core.events import QueueReturned, TurnDone

    async def run(inner: bool):
        s, _ = make_session(workspace, [])
        s.queue_input("待追加的话", "follow")

        async def fake_turn(text, put, images=None):
            put(TurnDone("completed", ""))       # 只测 run_turn 的收尾分支，不跑模型

        s._turn = fake_turn
        evs = [ev async for ev in s.run_turn("做点事", inner=inner)]
        return s, evs

    s_outer, evs_outer = asyncio.run(run(False))
    assert any(isinstance(e, QueueReturned) for e in evs_outer), "普通回合结束时该退回"
    assert not s_outer.queue.items

    s_inner, evs_inner = asyncio.run(run(True))
    assert not any(isinstance(e, QueueReturned) for e in evs_inner), \
        "内层回合不该把「待追加」提前退回"
    assert s_inner.queue.has("follow"), "消息要留在队列里给外层收尾"


def test_goal_turn_returns_leftover_queue_once(ee_home, workspace, monkeypatch):
    """Goal 跑完后，整回合里排的「待追加」在外层收尾时退回一次（且队列清空）。"""
    from ee.core.events import TurnDone
    import ee.core.goal as G

    th = _thread(ee_home, workspace)
    th.ensure_session()
    notes: list[tuple[str, dict]] = []
    th.notify = lambda m, p: notes.append((m, p))
    rounds = {"n": 0}

    def make(session):
        async def run_turn(text, images=None, inner=False):
            rounds["n"] += 1
            session.queue_input(f"待追加 {rounds['n']}", "follow")
            yield TurnDone("completed", "")
        return run_turn

    th.session.run_turn = make(th.session)
    # 两轮几回合才停：先判红 → 回合1 → 再判红 → 回合2 → 判绿
    verdicts = [G.Verdict(exit=1, tests=1, output="1 failed", min_tests=1),
                G.Verdict(exit=1, tests=1, output="1 failed", min_tests=1),
                G.Verdict(exit=0, tests=3, output="3 passed", min_tests=1)]

    async def fake_judge(spec, **_kw):
        return verdicts.pop(0) if verdicts else G.Verdict(exit=0, tests=3, output="3 passed", min_tests=1)

    monkeypatch.setattr(G, "judge", fake_judge)
    asyncio.run(th._consume("做点事", goal={"command": "pytest -q", "max_rounds": 3}))
    done = [p for m, p in notes if m == "turn/completed"]
    assert done and done[-1]["reason"] == "met", f"回合终态不对：{done}"
    assert rounds["n"] == 2, "前提：Goal 跑了两轮"
    returned = [p for m, p in notes if m == "turn/queue_returned"]
    assert len(returned) == 1, f"退回应该只发生一次（外层收尾），实际 {len(returned)} 次"
    assert [x["text"] for x in returned[0]["items"]] == ["待追加 1", "待追加 2"]
    assert not th.session.queue.items, "退回之后队列必须清空，否则下一回合会再送一次"
