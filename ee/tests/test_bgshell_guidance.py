"""后台命令轮询陷阱的引导修复（05-full 实测缺陷）。

实测现象（eval-full，任务 05 full 模式）：模型用 background=true 跑测试，
进程 7.5s 就退出（exit 0），但模型连续 6 次调 shell_output(wait=120)，
每次只看到「没有新输出 + [b2 已退出 · exit 0]」，仍然继续等，耗尽 782s 超时。

根因：shell_output 在进程已退出时，返回文案没有明确告诉模型「不要再轮询了」。
「已退出 · exit 0」这行状态太弱，模型把它当成普通状态行忽略掉。

修复：进程已退出时，输出显式引导「命令已结束，不要再等待，直接基于输出继续」。

注意：BgManager 的锁绑定事件循环，全部工具调用必须在同一个 asyncio.run 里。
"""
from __future__ import annotations

import asyncio
import sys

from ee.core.bgproc import BgManager
from ee.tools.base import ToolContext, ToolState
from ee.tools.bgshell import ShellOutputTool
from ee.tools.shell import ShellTool


def test_finished_process_output_tells_model_to_stop_waiting(tmp_path):
    """进程已退出时，shell_output 的返回必须明确引导模型停止等待。"""
    async def go():
        sess = type("S", (), {"bg": BgManager(tmp_path)})()
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          extra={"session": sess})
        start = await ShellTool().run({"command": "echo done-12345", "background": True}, ctx)
        bg_id = start.details["bg_id"]
        # 模拟模型行为：wait 5s 等输出（echo 早该退出），再读——应看到「已退出」+ 引导
        out = await ShellOutputTool().run({"id": bg_id, "wait": 5}, ctx)
        return out.output

    text = asyncio.run(go())
    assert "已退出" in text, f"应显示已退出状态：{text[:200]}"
    assert ("不要再" in text) and ("等待" in text or "轮询" in text), (
        f"进程已退出时必须引导模型停止等待/轮询，否则模型会反复 shell_output 空转。实际：{text[:300]}"
    )


def test_running_process_does_not_add_stop_guidance(tmp_path):
    """还在运行时不加「停止等待」引导（模型此时确实应该等或稍后再查）。

    必须显式指定 shell 并按它的语法拼命令：CI 上 auto 探测可能选中 PowerShell，
    而 POSIX 的引号写法在 PowerShell 下语义不同，子进程会直接 exit 1
    （本地 Git Bash 通过、CI 挂——已踩过）。
    """
    from ee.tools.shellenv import resolve_shell

    sh = resolve_shell("auto")
    (tmp_path / "loop.py").write_text("import time\nwhile True:\n    time.sleep(0.2)\n", encoding="utf-8")
    exe = sys.executable
    cmd = (f"& '{exe}' -u loop.py" if sh.syntax == "powershell" else f'"{exe}" -u loop.py')

    async def go():
        sess = type("S", (), {"bg": BgManager(tmp_path)})()
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          shell=sh, extra={"session": sess})
        start = await ShellTool().run({"command": cmd, "background": True}, ctx)
        bg_id = start.details["bg_id"]
        out = await ShellOutputTool().run({"id": bg_id}, ctx)
        await sess.bg.kill(sess.bg.get(bg_id))
        return out.output

    text = asyncio.run(go())
    assert "运行中" in text
    assert "不要再" not in text, "运行中的命令不该出现停止等待引导"
