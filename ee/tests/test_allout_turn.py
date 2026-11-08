"""全力以赴接入 turn/start（allout 阶段 2，界面侧入口）。

**本文件不启动真实回合**，沿用 `test_goal_turn.py` 的 `_no_loop`：
只验校验与用户条目写入。「跑起来会怎样」由后续阶段的编排测试覆盖。

守三件事：
- `allout` 只从这条入口来，参数非法时明确拒绝（不静默降级）；
- 只有用户真的开了才写入（`enabled=False` 与不传等价，不落条目）；
- 模型只有一个：参数里没有任何模型字段。
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
    real = asyncio.create_task

    class _Done:
        def done(self) -> bool:
            return True

        def cancel(self) -> None:
            pass

    def _stub(coro, **_kw):
        coro.close()
        return _Done()

    asyncio.create_task = _stub
    try:
        yield
    finally:
        asyncio.create_task = real


def _users(th) -> list[dict]:
    return [i for i in th.items if i.get("type") == "user"]


# ---------------------------------------------------------------- 校验


def test_rejects_non_dict():
    from ee.server.threads import _norm_allout

    with pytest.raises(ValueError):
        _norm_allout("on")


def test_rejects_non_bool_enabled():
    from ee.server.threads import _norm_allout

    with pytest.raises(ValueError):
        _norm_allout({"enabled": "yes"})


def test_rejects_bad_candidates():
    from ee.server.threads import _norm_allout

    with pytest.raises(ValueError):
        _norm_allout({"enabled": True, "candidates": "many"})
    with pytest.raises(ValueError):
        _norm_allout({"enabled": True, "candidates": 0})


def test_candidates_capped():
    from ee.server.threads import _norm_allout

    assert _norm_allout({"enabled": True, "candidates": 99})["candidates"] <= 3


def test_defaults_applied():
    from ee.server.threads import _norm_allout

    out = _norm_allout({"enabled": True})
    assert out["enabled"] is True
    assert out["candidates"] == 3


def test_rejects_model_field():
    """模型固定为用户所选：参数里带 model 必须拒绝，不能悄悄换模型。"""
    from ee.server.threads import _norm_allout

    with pytest.raises(ValueError):
        _norm_allout({"enabled": True, "model": "gpt-5"})


# ---------------------------------------------------------------- 记录


def test_disabled_is_not_recorded(ee_home, workspace):
    th = _thread(ee_home, workspace)
    with _no_loop():
        th.start_turn("修好它", allout={"enabled": False})
    assert not any("allout" in i for i in _users(th)), "没开就不该在界面上显示"


def test_enabled_recorded_on_user_item(ee_home, workspace):
    th = _thread(ee_home, workspace)
    with _no_loop():
        th.start_turn("修好它", allout={"enabled": True, "candidates": 2})
    u = _users(th)[0]
    assert u["allout"]["enabled"] is True
    assert u["allout"]["candidates"] == 2


def test_rejects_when_running(ee_home, workspace):
    """运行中改 allout 等于中途换跑法：拒绝，和 goal 的排队语义一致。"""
    th = _thread(ee_home, workspace)

    class _Running:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            pass

    th.task = _Running()
    with pytest.raises(ValueError):
        th.start_turn("修好它", allout={"enabled": True})


# ---------------------------------------------------------------- 推导通知


def _plan_notices(ee_home, workspace, *, goal=None):
    """调 _plan_allout，收集它推的通知。判据探测只读文件，可以真跑。"""
    th = _thread(ee_home, workspace)
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    th.ensure_session()
    asyncio.run(th._plan_allout({"enabled": True, "candidates": 3}, goal))
    return [p for m, p in notes if m == "allout_plan"]


def test_plan_allout_with_user_goal_is_verified(ee_home, workspace):
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    got = _plan_notices(ee_home, workspace, goal={"command": "pytest -q", "max_rounds": 3})
    assert len(got) == 1
    p = got[0]
    assert p["thread_id"] == "t1"
    assert p["goal"] == "pytest -q"
    assert p["plan"]["verified"] is True
    assert p["probe"] is None, "用户已给判据时不再探测"


def test_plan_allout_without_goal_probes_and_flags_unconfirmed(ee_home, workspace):
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    got = _plan_notices(ee_home, workspace)
    p = got[0]
    assert p["goal"] is None
    assert p["plan"]["verified"] is False
    assert p["plan"]["candidates"] == 1
    assert p["probe"]["command"] == "python -m pytest -q"
    assert p["probe"]["confirmed"] is False, "探测来的判据未经确认不能执行"
    assert p["plan"]["degradations"], "降级必须带原因"


def test_plan_allout_without_any_goal_degrades(ee_home, workspace):
    got = _plan_notices(ee_home, workspace)
    p = got[0]
    assert p["probe"]["command"] is None
    assert p["plan"]["verified"] is False
    assert any("目标" in d for d in p["plan"]["degradations"])


def test_plan_allout_user_cap_shrinks_shown_plan(ee_home, workspace):
    """用户设的上限小于推导值时，界面显示的路数必须等于实际执行的路数。"""
    th = _thread(ee_home, workspace)
    notes = []
    th.notify = lambda m, p: notes.append((m, p))
    th.ensure_session()
    # 16 核 / 128k+ 上下文下推导值是 3；用户把上限设成 1
    asyncio.run(th._plan_allout({"enabled": True, "candidates": 1},
                                {"command": "pytest -q", "max_rounds": 3}))
    p = [x for m, x in notes if m == "allout_plan"][0]
    if p["plan"]["candidates"] >= 1 and p["candidates_cap"] == 1:
        assert p["plan"]["candidates"] == 1, "显示的路数必须被用户上限钳住"
        assert len(p["plan"]["temperature_ladder"]) == 1, "温度阶梯要跟着截断"
