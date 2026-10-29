"""Goal 的真实闭环验收（T10-b 任务书「验收（唯一标准）」的落地）。

不同于 test_goal_runner 里的收敛用例（那里 turn 直接改文件），这里走完整链路：
`ee exec --goal` → 跑真实 pytest 判红 → agent 用 edit 工具修 → 再判 → 绿 → 退出 0。

守的正是任务书写死的那条：**agent 真的把失败的测试修好了才允许报 met**。
"""
from __future__ import annotations

import io
import json
import sys

from conftest import FakeClient, finish, text_chunks, tool_chunks

from ee import headless
from ee.cli import build_parser
from ee.core.providers.openai_chat import ChatClient
from ee.tools.shellenv import resolve_shell


def _run(home, workspace, argv, script):
    fake = FakeClient(script)
    args = build_parser().parse_args(["exec", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = headless.run(args, home=home, client_factory=lambda spec: ChatClient(spec, client=fake),
                        stdin=None, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue(), fake


def _pycmd(*parts) -> str:
    body = " ".join(f'"{p}"' if " " in str(p) else str(p) for p in parts)
    return f"& {body}" if resolve_shell("auto").syntax == "powershell" else body


def test_goal_closes_loop_red_then_green(ee_home, workspace):
    """完整闭环：判据先红 → agent 编辑修好 → 判据转绿 → 退出 0 且报 met。

    这是任务书的唯一验收标准。用真实 pytest 做判据，用真实 edit 工具改文件，
    只有模型是假的——其余每一层都是生产代码。
    """
    target = workspace / "test_calc.py"
    target.write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    goal = _pycmd(sys.executable, "-m", "pytest", "-q",
                  str(target), "-p", "no:cacheprovider")

    # 假模型第一轮：先 read（编辑前必须读过，见 tools/edit.py:42 的约束），
    # 再 edit 把 a-b 改成 a+b；第二轮：收尾
    script = [
        tool_chunks(0, "c_read", "read", {"path": "calc.py"}) + finish("tool_calls"),
        tool_chunks(0, "c_edit", "edit", {
            "path": "calc.py",
            "old_string": "return a - b",
            "new_string": "return a + b",
        }) + finish("tool_calls"),
        text_chunks("修好了") + finish("stop"),
    ]

    code, out, err, fake = _run(
        ee_home, workspace,
        ["-C", str(workspace), "--goal", goal, "--goal-max-rounds", "3",
         # read-only 模式下 edit 会被权限直接 deny——闭环验收必须允许写，
         # 否则测的是"只读模式挡住了编辑"，不是"改对了就通过"。
         "--mode", "default", "--approve", "auto", "修好 calc.py 的 add"],
        script)

    assert code == 0, f"应当 met 并退出 0，实际 {code}\n--- stderr ---\n{err}"
    # 判据第一次必须是红的：否则这条用例没验到"先红后绿"
    assert "判据] 未通过" in err or "未通过" in err, err
    assert "met" in err
    # 文件真的被改对了
    assert "a + b" in (workspace / "calc.py").read_text(encoding="utf-8")
    # agent 确实被调用过（不是靠零轮跳过蒙混过关）
    assert fake.calls, "模型一次都没被调用"


def test_goal_exhausted_keeps_real_output(ee_home, workspace):
    """修不好时：跑满轮次报 exhausted，退出 3，且报告里带真实的 pytest 失败输出。

    这条防的是"报个 exhausted 就完事"——人得能从这个输出看出到底什么没过。
    """
    target = workspace / "test_never.py"
    target.write_text("def test_x():\n    assert 1 == 2, '永远失败'\n", encoding="utf-8")
    goal = _pycmd(sys.executable, "-m", "pytest", "-q",
                  str(target), "-p", "no:cacheprovider")

    script = [text_chunks("我改不了") + finish("stop")]

    code, out, err, _ = _run(
        ee_home, workspace,
        ["-C", str(workspace), "--goal", goal, "--goal-max-rounds", "2", "试试"],
        script)

    assert code == 3, f"应当是 exhausted(3)，实际 {code}\n{err}"
    # 最后一次判据的真实输出要在报告里
    assert "永远失败" in err or "1 failed" in err, err
    assert "exhausted" in err
