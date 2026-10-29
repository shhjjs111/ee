"""`ee exec --goal` 端到端（T10-b / U3）：用假模型跑真实续跑。

单元测试只验参数与映射；这里验用户真正会看到的：退出码、收敛行为、
以及"零测试不许报成功"在整条链路上是否真的成立。
"""
from __future__ import annotations

import io
import json
import sys

from conftest import FakeClient, finish, text_chunks

from ee import headless
from ee.cli import build_parser
from ee.core.providers.openai_chat import ChatClient
from ee.tools.shellenv import resolve_shell


def _run(home, workspace, argv, script=None):
    fake = FakeClient(script or ([text_chunks("改好了") + finish("stop")]))
    args = build_parser().parse_args(["exec", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = headless.run(args, home=home, client_factory=lambda spec: ChatClient(spec, client=fake),
                        stdin=None, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _pycmd(*parts) -> str:
    """拼一条在解析出的 shell 里合法的命令（PowerShell 需要调用运算符）。"""
    body = " ".join(f'"{p}"' if " " in str(p) else str(p) for p in parts)
    return f"& {body}" if resolve_shell("auto").syntax == "powershell" else body


def test_goal_already_met_runs_no_turn(ee_home, workspace):
    """判据一开始就成立 → 不跑回合，退出 0。"""
    green = workspace / "test_ok.py"
    green.write_text("def test_it():\n    assert True\n", encoding="utf-8")
    goal = _pycmd(sys.executable, "-m", "pytest", "-q", str(green))

    code, out, err = _run(ee_home, workspace, ["-C", str(workspace), "--goal", goal,
                                              "做点事"])
    assert code == 0, err
    assert "[判据] 通过" in err or "met" in err
    # 假模型一次都没被调用（没产出回答）
    assert "改好了" not in out


def test_goal_zero_tests_never_exits_zero(ee_home, workspace):
    """判据 exit 0 但零测试 → 必须非零退出，不能报成功。

    这是整条链路最危险的一条：命令"成功"了，但什么也没验证。
    """
    empty = workspace / "test_empty.py"
    empty.write_text("# 没有 test 函数\n", encoding="utf-8")
    # pytest 对零测试通常 exit 5；用一个一定 exit 0 且零测试的命令覆盖那条路径
    script = workspace / "noop.py"
    script.write_text("print('collected 0 items'); print('no tests ran')\n", encoding="utf-8")
    goal = _pycmd(sys.executable, str(script))

    code, out, err = _run(ee_home, workspace, ["-C", str(workspace), "--goal", goal,
                                              "--goal-max-rounds", "1", "做点事"])
    assert code != 0, f"零测试竟然退出 0：{err}"
    assert code == 3, f"应该是 exhausted(3)，实际 {code}"


def test_goal_exhausted_exit_three(ee_home, workspace):
    """判据一直不成立 → 跑满轮次，退出 3。"""
    red = workspace / "test_bad.py"
    red.write_text("def test_it():\n    assert False\n", encoding="utf-8")
    goal = _pycmd(sys.executable, "-m", "pytest", "-q", str(red))

    code, out, err = _run(ee_home, workspace, ["-C", str(workspace), "--goal", goal,
                                              "--goal-max-rounds", "1", "做点事"])
    assert code == 3, err
    assert "exhausted" in err


def test_goal_json_emits_check_events(ee_home, workspace):
    """--json 时每轮判定都有 goal_check 事件，末行 turn_done 带 goal 摘要。"""
    green = workspace / "test_ok2.py"
    green.write_text("def test_it():\n    assert True\n", encoding="utf-8")
    goal = _pycmd(sys.executable, "-m", "pytest", "-q", str(green))

    code, out, err = _run(ee_home, workspace, ["-C", str(workspace), "--goal", goal,
                                              "--json", "做点事"])
    evs = [json.loads(line) for line in out.splitlines() if line.strip()]
    checks = [e for e in evs if e.get("type") == "goal_check"]
    assert checks, f"没有 goal_check 事件：{evs}"
    assert checks[0]["met"] is True
    done = [e for e in evs if e.get("type") == "turn_done"][-1]
    assert done["exit_code"] == 0
    assert done["goal"]["stop_reason"] == "met"


def test_goal_not_set_keeps_old_behavior(ee_home, workspace):
    """不给 --goal 时行为完全不变：一个回合就退出。"""
    code, out, err = _run(ee_home, workspace, ["-C", str(workspace), "普通问题"])
    assert code == 0
    assert out.strip() == "改好了"
