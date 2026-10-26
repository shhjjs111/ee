"""shell 工具（设计里的 bash 工具）。schema 名字和描述保持通用，具体用哪种 shell 由环境块告诉模型。"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult
from ee.tools.shellenv import IS_WINDOWS, ShellInfo, resolve_shell, strip_clixml
from ee.tools.truncate import truncate_output

MAX_TIMEOUT = 600


def child_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "TERM": env.get("TERM", "dumb"), "NO_COLOR": "1", "GH_PAGER": "cat"})
    return env


async def run_command(command: str, shell: ShellInfo, cwd, timeout: float, emit=lambda s: None) -> tuple[int | None, str, bool]:
    """跑命令，返回 (退出码, 合并输出, 是否超时)。取消时杀掉整个进程树。"""
    kw: dict = {}
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    argv, script = shell.prepare(command)
    try:
        return await _run(argv, cwd, timeout, emit, kw)
    finally:
        if script is not None:
            try:
                script.unlink()
            except OSError:
                pass


async def _run(argv, cwd, timeout, emit, kw) -> tuple[int | None, str, bool]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=child_env(), **kw)
    chunks: list[str] = []
    timed_out = False

    async def pump():
        assert proc.stdout
        while True:
            b = await proc.stdout.read(4096)
            if not b:
                break
            s = b.decode("utf-8", errors="replace")
            chunks.append(s)
            emit(s)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill(proc)
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    out = strip_clixml("".join(chunks).replace("\r\n", "\n"))
    return proc.returncode, out, timed_out


async def _kill(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


class ShellTool(Tool):
    name = "shell"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令（使用环境信息里给出的当前 shell 的语法）"},
            "timeout": {"type": "integer", "description": f"超时秒数，默认 120，最大 {MAX_TIMEOUT}"},
            "workdir": {"type": "string", "description": "在哪个目录执行，默认工作目录。优先用这个参数而不是 cd"},
            "description": {"type": "string", "description": "5-10 个字说明这条命令做什么"},
        },
        "required": ["command"],
    }

    def summary(self, args: dict) -> str:
        return f"Ran {args.get('command', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("command 为空")
        shell = ctx.shell or resolve_shell("auto")
        timeout = min(MAX_TIMEOUT, max(1, int(args.get("timeout") or ctx.timeout_default)))
        cwd = ctx.resolve(args["workdir"]) if args.get("workdir") else ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f"workdir 不存在：{cwd}")
        t0 = time.monotonic()
        try:
            code, out, timed_out = await run_command(command, shell, cwd, timeout, ctx.emit)
        except FileNotFoundError as e:
            raise ToolError(f"无法启动 {shell.display}：{e}")
        dur = time.monotonic() - t0
        body, truncated = truncate_output(out.rstrip("\n"), ctx.output_dir, "shell")
        tail = f"\n[超时：{timeout}s 后已终止]" if timed_out else ""
        footer = f"\n[exit {code if code is not None else '?'} · {dur:.1f}s]"
        ok = (code == 0) and not timed_out
        return ToolResult((body or "（无输出）") + tail + footer, ok=ok, summary=f"Ran {command}",
                          meta={"kind": "shell", "command": command, "exit": code, "timed_out": timed_out,
                                "truncated": truncated, "output": out[-4000:]})

