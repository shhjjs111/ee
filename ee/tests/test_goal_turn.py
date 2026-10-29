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
        _norm_goal("pytest -q")


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
