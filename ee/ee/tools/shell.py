"""shell 工具（设计里的 bash 工具）。schema 名字和描述保持通用，具体用哪种 shell 由环境块告诉模型。"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

from ee.proc import kill_tree
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
            await asyncio.to_thread(kill_tree, proc.pid)   # 不能在事件循环上同步跑 taskkill
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
            "background": {"type": "boolean", "description": "true = 后台运行（dev server、watch、长测试、交互程序），立即返回后台命令 id，之后用 shell_output 看输出、shell_write 输入、shell_kill 结束"},
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
        if args.get("background"):
            return await run_background(command, shell, cwd, ctx)
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



BG_YIELD = 3.0      # 后台启动后先等这么久收集开头输出（很快结束的命令直接拿到结果）


async def run_background(command: str, shell: ShellInfo, cwd, ctx: ToolContext) -> ToolResult:
    sess = ctx.extra.get("session")
    mgr = getattr(sess, "bg", None)
    if mgr is None:
        raise ToolError("当前会话不支持后台命令")
    try:
        p = await mgr.start(command, shell, cwd, child_env())
    except FileNotFoundError as e:
        raise ToolError(f"无法启动 {shell.display}：{e}")
    except RuntimeError as e:
        raise ToolError(str(e))
    await mgr.wait_output(p, BG_YIELD)
    if p.running:   # 开头的输出多等一会儿收齐（例如 dev server 打印地址）
        await asyncio.sleep(0.2)
    text, notes = mgr.read_new(p)
    if text:
        ctx.emit(text)
    head = f"[后台命令 {p.id} · pid {p.pid}]"
    if p.running:
        state = (f"\n[仍在运行。用 shell_output(id=\"{p.id}\") 看后续输出，shell_write 输入，shell_kill 结束；"
                 f"完整日志：{p.log_path}]")
    else:
        state = f"\n[已结束 · exit {p.exit_code}]"
    body = text.rstrip("\n") or "（暂无输出）"
    return ToolResult(f"{head}\n{body}{state}", ok=(p.running or p.exit_code == 0), summary=f"Started {command}",
                      meta={"kind": "shell", "command": command, "background": True, "bg_id": p.id,
                            "exit": p.exit_code, "running": p.running, "output": text[-4000:]})
