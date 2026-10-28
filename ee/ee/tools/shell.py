"""shell 工具（设计里的 bash 工具）。schema 名字和描述保持通用，具体用哪种 shell 由环境块告诉模型。"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

from ee.core import sandbox as SB
from ee.proc import child_environ, kill_tree
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult
from ee.tools.shellenv import IS_WINDOWS, ShellInfo, resolve_shell, strip_clixml
from ee.tools.textdecode import StreamDecoder
from ee.tools.truncate import truncate_output_info

MAX_TIMEOUT = 600
CAPTURE_CHARS = 4_000_000      # 内存里最多留这么多字符（开头一半 + 结尾一半），防止输出巨大的命令撑爆内存


class CappedText:
    """只保留开头和结尾各 limit/2 个字符，记下中间丢了多少。"""

    def __init__(self, limit: int):
        self.half = max(1, limit // 2)
        self.head: list[str] = []
        self.head_len = 0
        self.tail: list[str] = []
        self.tail_len = 0
        self.dropped = 0

    def add(self, s: str) -> None:
        if self.head_len < self.half:
            take = s[: self.half - self.head_len]
            self.head.append(take)
            self.head_len += len(take)
            s = s[len(take):]
            if not s:
                return
        self.tail.append(s)
        self.tail_len += len(s)
        if self.tail_len > self.half * 2:          # 攒够两倍再裁，避免每块都拼接
            whole = "".join(self.tail)
            cut = len(whole) - self.half
            self.dropped += cut
            self.tail, self.tail_len = [whole[cut:]], self.half

    def text(self) -> str:
        tail = "".join(self.tail)
        if len(tail) > self.half:
            self.dropped += len(tail) - self.half
            tail = tail[-self.half:]
            self.tail, self.tail_len = [tail], len(tail)
        head = "".join(self.head)
        if not self.dropped:
            return head + tail
        return f"{head}\n\n…[输出过大，中间省略约 {self.dropped} 字符]…\n\n{tail}"


def child_env() -> dict:
    env = child_environ()
    env.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "TERM": env.get("TERM", "dumb"), "NO_COLOR": "1", "GH_PAGER": "cat"})
    return env


def sandbox_plan(ctx: ToolContext):
    """按会话配置得到沙箱计划（未开启 → None）；沙箱准备失败 → ToolError（不静默回退成不隔离）。

    只读 / 计划模式下默认启用：命令白名单只能防误操作，不是安全边界。
    """
    from ee.core.permissions import READONLY_MODES
    cfg = ctx.extra.get("config")
    data = getattr(cfg, "data", None) or {}
    session = ctx.extra.get("session")
    mode = getattr(getattr(session, "policy", None), "mode", "") if session is not None else ""
    try:
        return SB.plan(data, ctx.cwd, readonly=mode in READONLY_MODES)
    except SB.SandboxError as e:
        raise ToolError(str(e))


def _sb_note(sb) -> str:
    return f"\n{sb.notice}" if sb is not None and sb.notice else ""


async def run_command(command: str, shell: ShellInfo, cwd, timeout: float, emit=lambda s: None,
                      sandbox=None) -> tuple[int | None, str, bool]:
    """跑命令，返回 (退出码, 合并输出, 是否超时)。取消时杀掉整个进程树。sandbox：core/sandbox.SandboxPlan。"""
    kw: dict = {}
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    argv, script = shell.prepare(command)
    env = child_env()
    if sandbox is not None:
        argv, env = sandbox.wrap(argv, env)
    try:
        return await _run(argv, cwd, timeout, emit, kw, env)
    finally:
        if script is not None:
            try:
                script.unlink()
            except OSError:
                pass


async def _run(argv, cwd, timeout, emit, kw, env=None) -> tuple[int | None, str, bool]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env if env is not None else child_env(), **kw)
    buf = CappedText(CAPTURE_CHARS)
    dec = StreamDecoder()
    timed_out = False

    async def pump():
        assert proc.stdout
        while True:
            b = await proc.stdout.read(65536)
            s = dec.decode(b, final=not b)
            if s:
                buf.add(s)
                emit(s)
            if not b:
                break

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill(proc)
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    out = strip_clixml(buf.text().replace("\r\n", "\n"))
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


BG_DESC = "后台运行，立即返回 id；之后用 shell_output 看输出、shell_write 输入、shell_kill 结束"
BG_DESC_LEAN = "后台运行，立即返回 pid 和日志路径（用 read 看日志）"


class ShellTool(Tool):
    name = "shell"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": f"超时秒数，默认 120，最大 {MAX_TIMEOUT}"},
            "workdir": {"type": "string", "description": "执行目录，默认工作目录"},
            "description": {"type": "string", "description": "5～10 字说明"},
            "background": {"type": "boolean", "description": BG_DESC},
        },
        "required": ["command"],
    }

    def __init__(self, bg_tools: bool = True) -> None:
        """bg_tools=False（没有 shell_output/shell_write/shell_kill，如精简模式）：background 改成用 read 看日志，结果里给出结束进程的命令。"""
        super().__init__()
        if not bg_tools:
            props = dict(self.parameters["properties"])
            props.pop("description", None)          # 界面不用它，精简模式省掉
            props["background"] = {"type": "boolean", "description": BG_DESC_LEAN}
            self.parameters = {**self.parameters, "properties": props}

    def summary(self, args: dict) -> str:
        return f"Ran {args.get('command', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("command 为空")
        shell = ctx.shell or resolve_shell("auto")
        timeout = min(MAX_TIMEOUT, max(1, int(args.get("timeout") or ctx.timeout_default)))
        if ctx.max_timeout > 0:                      # 会话级硬上限：模型显式传更大的 timeout 也不越界
            timeout = min(timeout, max(1, int(ctx.max_timeout)))
        cwd = ctx.resolve(args["workdir"]) if args.get("workdir") else ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f"workdir 不存在：{cwd}")
        if args.get("background"):
            return await run_background(command, shell, cwd, ctx)
        sb = sandbox_plan(ctx)
        t0 = time.monotonic()
        try:
            code, out, timed_out = await run_command(command, shell, cwd, timeout, ctx.emit, sandbox=sb)
        except FileNotFoundError as e:
            raise ToolError(f"无法启动 {shell.display}：{e}")
        if sb is not None and SB.is_helper_failure(code, out):
            raise ToolError(out.strip())
        dur = time.monotonic() - t0
        body, truncated = truncate_output_info(out.rstrip("\n"), ctx.output_dir, "shell")
        tail = f"\n[超时：{timeout}s 后已终止]" if timed_out else ""
        footer = f"\n[exit {code if code is not None else '?'} · {dur:.1f}s]" + _sb_note(sb)
        ok = (code == 0) and not timed_out
        return ToolResult((body or "（无输出）") + tail + footer, ok=ok, summary=f"Ran {command}",
                          meta={"kind": "shell", "command": command, "exit": code, "timed_out": timed_out,
                                **({"truncated": truncated} if truncated else {}), "output": out[-4000:]})



BG_YIELD = 3.0      # 后台启动后先等这么久收集开头输出（很快结束的命令直接拿到结果）


async def run_background(command: str, shell: ShellInfo, cwd, ctx: ToolContext) -> ToolResult:
    sess = ctx.extra.get("session")
    mgr = getattr(sess, "bg", None)
    if mgr is None:
        raise ToolError("当前会话不支持后台命令")
    sb = sandbox_plan(ctx)
    try:
        p = await mgr.start(command, shell, cwd, child_env(), sandbox=sb)
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
    tmap = getattr(sess, "tool_map", None) or {}
    if p.running and "shell_output" not in tmap:      # 没有后台命令工具（精简模式）：用 read 看日志、用 shell 结束
        sl = "//" if getattr(shell, "syntax", "posix") == "posix" else "/"     # Git Bash 下 / 开头的参数会被当成路径
        kill = f"taskkill {sl}T {sl}F {sl}PID {p.pid}" if IS_WINDOWS else f"kill {p.pid}"
        state = f"\n[仍在运行。完整日志：{p.log_path}（用 read 查看）；用完用 shell 执行 `{kill}` 结束]"
    elif p.running:
        state = (f"\n[仍在运行。用 shell_output(id=\"{p.id}\") 看后续输出，shell_write 输入，shell_kill 结束；"
                 f"完整日志：{p.log_path}]")
    else:
        state = f"\n[已结束 · exit {p.exit_code}]"
        if sb is not None and SB.is_helper_failure(p.exit_code, text):
            raise ToolError(text.strip())
    state += _sb_note(sb)
    body = text.rstrip("\n") or "（暂无输出）"
    return ToolResult(f"{head}\n{body}{state}", ok=(p.running or p.exit_code == 0), summary=f"Started {command}",
                      meta={"kind": "shell", "command": command, "background": True, "bg_id": p.id,
                            "exit": p.exit_code, "running": p.running, "output": text[-4000:]})
