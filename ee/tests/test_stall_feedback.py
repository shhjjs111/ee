"""长等待期间必须「看得见」（P0-1 / P0-2 / P1）。

实测（2026-11-08，会话 20261108-093000-4ae8a7）：模型发了一次 `shell_output(wait=500)`，
到 324 秒时用户以为卡死了强杀 ee。回看后端日志，那次等待**完全正常**——只是这 5 分 24 秒
里界面零事件，用户分不清「在等后台命令」和「卡死了」。

同时，进程退出导致的中断被写成了「用户中断」，用户与后续 agent 都被误导。

这组用例守三件事：
1. 长等待期间持续发 Status 进度，结束后必须清掉（不能停在「等待中」）；
2. 单个工具调用超过阈值没返回，主动发一条提示（只报警，不取消）；
3. 中断原因能区分「用户停止」与「进程退出」，不再一律写成用户中断。
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from ee.core import session as S
from ee.core.bgproc import BgManager
from ee.core.events import Notice, Status
from ee.tools import ToolContext, ToolState
from ee.tools.base import Tool, ToolResult
from ee.tools.bgshell import _clear_status, _wait_tick
from ee.tools.shell import ShellTool
from ee.tools.shellenv import resolve_shell


def _py(sh, script: str) -> str:
    exe = sys.executable
    if sh.syntax == "powershell":
        return f"& '{exe}' -u {script}"
    return f'"{exe}" -u {script}'


# ---------------------------------------------------------------- 1. 长等待的可见进度

def test_long_wait_emits_progress_and_clears_it(workspace, tmp_path):
    """等一会儿、期间没有新输出：必须发过「已等 Ns」，结束后把状态清掉。

    没有这些 status 时，界面上就是一段零事件的空白——用户没法判断是还在跑还是卡死了。
    """
    (workspace / "quiet.py").write_text(
        "import time\ntime.sleep(30)\nprint('done', flush=True)\n", encoding="utf-8")
    sh = resolve_shell("auto")
    seen: list = []

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / ".out", shell=sh,
                          extra={"session": SimpleNamespace(bg=mgr), "emit_event": seen.append})
        await ShellTool().run({"command": _py(sh, "quiet.py"), "background": True}, ctx)
        p = mgr.get("b1")
        # tick_s 压到 0.2：默认 5 秒一次会让用例白等
        await mgr.wait_output(p, 1, tick=_wait_tick(ctx, p, 1), tick_s=0.2)
        _clear_status(ctx)
        await mgr.close()

    asyncio.run(go())
    texts = [e.text for e in seen if isinstance(e, Status)]
    assert any("已等" in t for t in texts), f"长等待期间应有进度提示，实际：{texts}"
    assert texts and texts[-1] == "", f"等待结束后必须清掉状态，实际末尾：{texts[-1]!r}"


def test_wait_tick_absent_without_emit_event(workspace, tmp_path):
    """没有事件通道时不能抛（子代理 / 测试桩可能不传 emit_event）。"""
    ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=tmp_path, extra={})
    assert _wait_tick(ctx, SimpleNamespace(id="b1"), 10) is None


def test_wait_output_returns_at_deadline_without_output(workspace, tmp_path):
    """到点必须返回（哪怕一条新输出都没有），不能无限等下去。"""

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        p = SimpleNamespace(running=True, total=0, cursor=0, _changed=asyncio.Event())
        started = asyncio.get_running_loop().time()
        await mgr.wait_output(p, 0.3, tick_s=0.1)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(go()) < 5.0


# ---------------------------------------------------------------- 2. 工具调用看门狗

class _Slow(Tool):
    name = "slow_tool"
    read_only = True
    parameters = {"type": "object", "properties": {}}

    def __init__(self, secs: float):
        # 不调 super().__init__()：它会去读 prompts/tools/slow_tool.md（并不存在）。
        # _run_watched 只用到 tool.secs，不需要描述文本。
        self.description, self.rules = "", []
        self.secs = secs

    async def run(self, args, ctx):        # pragma: no cover - 不直接走 run
        await asyncio.sleep(self.secs)
        return ToolResult("ok")


def _bare_session() -> S.Session:
    """只借 _run_watched 用的最小壳：它只用到 _guarded_run 和 emit。"""
    s = object.__new__(S.Session)

    async def fake_guarded(cid, tool, args, emit, pre):
        await asyncio.sleep(tool.secs)
        return ToolResult("ok", summary="done")

    s._guarded_run = fake_guarded     # type: ignore[method-assign]
    return s


def test_stall_watchdog_warns_but_does_not_cancel(monkeypatch):
    """超过阈值没返回就发提示，但**不取消**工具：长 CI / 大仓库 to_thread 本来就慢。

    原来的行为是彻底静默——一个调用卡住时界面没有任何线索。
    """
    monkeypatch.setattr(S, "TOOL_STALL_WARN", 0.05)
    monkeypatch.setattr(S, "TOOL_STALL_EVERY", 0.05)
    evs: list = []

    async def go():
        s = _bare_session()
        return await s._run_watched("c1", "slow_tool", _Slow(0.4), {}, evs.append, None)

    r = asyncio.run(go())
    assert r.ok, "看门狗只报警，不能把结果吞掉"
    warns = [e for e in evs if isinstance(e, Notice) and "还没有返回" in e.text]
    assert warns, f"应有“已跑 N 秒”的提示，实际事件：{evs}"
    assert "slow_tool" in warns[0].text, "提示里要写明是哪个工具"


def test_stall_watchdog_is_silent_for_fast_calls(monkeypatch):
    """正常快调用不能被加噪音：默认阈值 120 秒，快调用一条提示都不该发。"""
    assert S.TOOL_STALL_WARN >= 60, "阈值太小会把正常慢工具也报成卡住"
    evs: list = []

    async def go():
        s = _bare_session()
        return await s._run_watched("c1", "slow_tool", _Slow(0.0), {}, evs.append, None)

    r = asyncio.run(go())
    assert r.ok
    assert not [e for e in evs if isinstance(e, Notice)], f"快调用不该报警：{evs}"


def test_exec_one_actually_wires_the_watchdog(monkeypatch, workspace):
    """守接线：`_exec_one` 必须真的经由 `_run_watched` 跑工具（上面两例直接调 `_run_watched`，
    接线断了也照样绿）。

    为什么单独立一条：实测报告里的静默发生时，看门狗代码完全可能已经存在，但 `_exec_one`
    还直接调 `_guarded_run`——光靠看门狗自己的用例塑不出这类“写了但没接上”的问题。
    """
    from ee.core.events import ToolEnd, ToolStart
    from ee.core import hooks as H
    from tests.conftest import make_session

    monkeypatch.setattr(S, "TOOL_STALL_WARN", 0.05)
    monkeypatch.setattr(S, "TOOL_STALL_EVERY", 0.05)
    # 这条只塑「看门狗有没有接上」：hooks 会去读配置、起子进程，跟本意无关。
    async def _pre(session, cid, tool, args, emit):
        return H.PreResult(args)

    async def _post(session, cid, tool, args, res, emit):
        return res

    monkeypatch.setattr(H, "pre_tool_use", _pre)
    monkeypatch.setattr(H, "post_tool_use", _post)
    s, _ = make_session(workspace, [])
    # 权限闸门直接放行：否则 _ask 会等满 APPROVAL_TIMEOUT（300s），用例看上去就像卡死。
    from ee.core.permissions import Decision
    s.policy.check = lambda *a, **k: Decision("allow", "测试放行")   # type: ignore[method-assign]
    evs: list = []

    async def go():
        tc = {"id": "c1", "type": "function",
              "function": {"name": "slow_tool", "arguments": "{}"}}
        await s._exec_one(tc, _Slow(0.4), {}, None, evs.append)

    asyncio.run(go())
    assert any(isinstance(e, ToolStart) for e in evs), f"工具应已开始：{evs}"
    warns = [e for e in evs if isinstance(e, Notice) and "还没有返回" in e.text]
    assert warns, f"_exec_one 没有接上看门狗（事件：{evs}）"
    assert any(isinstance(e, ToolEnd) for e in evs), "工具结果仍要回写"
    assert s._results["c1"], "结果必须进历史"


# ---------------------------------------------------------------- 3. 中断原因要分清

def test_interrupt_reason_defaults_to_user_stop():
    """不传 why = 用户点了停止：历史里仍写「用户中断」，不改变原有语义。"""
    s = object.__new__(S.Session)
    s._task = None
    s._stop_why = ""
    assert s.interrupt() is False          # 没有在跑的回合
    assert s._stop_why == ""


def test_process_exit_reason_is_not_blamed_on_user():
    """进程退出（关桌面端 / 强杀 / 内核结束）不能写成「用户中断」。

    实测报告 P1：用户根本没点停止，却被告知是自己中断了；自动化据此记成用户取消。
    """
    assert "用户中断" not in S.INTERRUPTED_EXIT
    assert "退出" in S.INTERRUPTED_EXIT


def test_interrupt_carries_reason_into_history():
    """interrupt(why=…) 的原因要原样落到工具结果里，而不是固定那句「用户中断」。"""

    async def go():
        s = object.__new__(S.Session)
        s._task = asyncio.ensure_future(asyncio.sleep(10))
        s._stop_why = ""
        try:
            assert s.interrupt(why=S.INTERRUPTED_EXIT) is True
            assert s._stop_why == S.INTERRUPTED_EXIT
        finally:
            s._task.cancel()

    asyncio.run(go())
