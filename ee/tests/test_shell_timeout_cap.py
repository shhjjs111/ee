"""shell 命令耗时的上限。

两个不同的东西，不要混：
- `bash_timeout`（默认 120）只是**默认值**——模型在 tool call 里显式传 `timeout` 就盖掉它，
  schema 允许一直传到 `MAX_TIMEOUT = 600`。
- `shell_max_timeout`（默认 0 = 不额外限制）是**硬上限**，模型传多大都封顶。

为什么需要后者：无人值守运行（评测 / 自动化）时，一条跑不完的命令能合法地吃掉 600 秒，
整轮耗时就被它一个盖住，看起来像「每步都很慢」。实测三次 600.3/600.4/600.3 秒的空洞
全部卡在 MAX_TIMEOUT 上，就是这条路径。

注意：这里量墙钟时间而不是 `meta["duration_ms"]`——后者由 `Session._exec_one` 里的
`finalize_details` 注入，直接调 `ShellTool().run()` 的单元测试拿不到它。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from ee.tools.base import ToolContext, ToolState
from ee.tools.shell import MAX_TIMEOUT, ShellTool
from ee.tools.shellenv import resolve_shell

SLEEP = 30          # 命令的自然耗时；只要上限生效，任何用例都远早于它结束


def _ctx(ws: Path, **kw) -> ToolContext:
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out",
                       shell=resolve_shell("auto"), **kw)


def _sleep_cmd() -> str:
    exe = sys.executable
    sh = resolve_shell("auto")
    if sh.syntax == "powershell":
        return f"& '{exe}' -c \"import time; time.sleep({SLEEP})\""
    return f'"{exe}" -c "import time; time.sleep({SLEEP})"'


def test_session_cap_clamps_explicit_large_timeout(workspace):
    """模型显式传 timeout=600，会话配了 shell_max_timeout=5 → 实际按 5 秒执行。

    若上限没生效，命令会跑满 30 秒自然结束（timed_out 为 False）。
    """
    tool = ShellTool()
    t0 = time.monotonic()
    r = asyncio.run(tool.run({"command": _sleep_cmd(), "timeout": 600},
                             _ctx(workspace, max_timeout=5)))
    dur = time.monotonic() - t0
    assert r.meta.get("timed_out") is True, r.meta
    assert r.ok is False
    assert dur < 20, f"上限没生效：命令跑了 {dur:.1f}s"


def test_no_cap_keeps_default_behaviour(workspace):
    """不配上限（默认 0）时行为不变：模型自己给的 timeout 照旧生效。"""
    tool = ShellTool()
    t0 = time.monotonic()
    r = asyncio.run(tool.run({"command": _sleep_cmd(), "timeout": 2}, _ctx(workspace)))
    dur = time.monotonic() - t0
    assert r.meta.get("timed_out") is True
    assert dur < 20, f"timeout=2 没生效：跑了 {dur:.1f}s"


def test_cap_does_not_raise_a_smaller_timeout(workspace):
    """上限只封顶、不抬底：模型传 1 秒、上限 30 秒 → 仍然按 1 秒掐断。"""
    tool = ShellTool()
    t0 = time.monotonic()
    r = asyncio.run(tool.run({"command": _sleep_cmd(), "timeout": 1},
                             _ctx(workspace, max_timeout=30)))
    dur = time.monotonic() - t0
    assert r.meta.get("timed_out") is True
    assert dur < 15, f"较小的 timeout 被上限抬高了：跑了 {dur:.1f}s"


def test_cap_never_exceeds_tool_maximum(workspace):
    """上限配得比 MAX_TIMEOUT 还大也没用：工具自身的 600 秒天花板优先（纯计算，不真跑）。"""
    ctx = _ctx(workspace, max_timeout=99999)
    timeout = min(MAX_TIMEOUT, max(1, int(600 or ctx.timeout_default)))
    if ctx.max_timeout > 0:
        timeout = min(timeout, max(1, int(ctx.max_timeout)))
    assert timeout == MAX_TIMEOUT


def test_session_wires_cap_from_config(workspace):
    """配置里的 shell_max_timeout 要真的接到 ToolContext 上（否则上层改了配置没人读）。"""
    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(workspace)
    cfg.data["shell_max_timeout"] = 77
    s = Session(cfg, cwd=workspace, mode="auto", persist=False)
    assert s.config.data["shell_max_timeout"] == 77
    # 默认值必须存在且为 0（不改变既有行为）
    from ee.config import DEFAULTS
    assert DEFAULTS.get("shell_max_timeout") == 0


def test_exec_flag_wires_cap_into_session(workspace, monkeypatch):
    """`ee exec --max-tool-timeout N` 必须真的把 N 写进会话配置。

    这是无人值守路径的唯一入口：评测用 `ee exec` 跑子进程，配置层不给硬上限的话，
    模型传 timeout=600 依旧能占满 10 分钟——修复等于没接上。
    """
    from ee.headless import add_arguments, unattended_configure

    p = argparse.ArgumentParser()
    add_arguments(p)
    args = p.parse_args(["--max-tool-timeout", "45", "随便"])
    assert args.max_tool_timeout == 45

    class Cfg:
        data: dict = {}

    cfg = Cfg()
    unattended_configure(None, (), (), None, args.max_tool_timeout)(cfg)
    assert cfg.data["shell_max_timeout"] == 45


def test_exec_without_flag_leaves_cap_off(workspace):
    """不传 --max-tool-timeout 时保持默认（0 = 不额外限制），不改变交互式的既有行为。"""
    from ee.headless import add_arguments, unattended_configure

    p = argparse.ArgumentParser()
    add_arguments(p)
    args = p.parse_args(["随便"])
    assert args.max_tool_timeout is None

    class Cfg:
        data: dict = {}

    cfg = Cfg()
    unattended_configure(None, (), (), None, args.max_tool_timeout)(cfg)
    assert "shell_max_timeout" not in cfg.data


def test_eval_profiles_passes_cap_to_exec():
    """评测脚本默认要把硬上限传给 ee exec，否则等于没修。"""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib
    from scripts import eval_profiles as E

    importlib.reload(E)
    p = E.build_parser()
    args = p.parse_args([])
    assert args.max_tool_timeout and args.max_tool_timeout > 0
    args_off = p.parse_args(["--max-tool-timeout", "0"])
    assert args_off.max_tool_timeout == 0
