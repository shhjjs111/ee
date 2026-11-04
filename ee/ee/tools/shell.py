"""shell 工具（设计里的 bash 工具）。schema 名字和描述保持通用，具体用哪种 shell 由环境块告诉模型。"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time

from ee.core import sandbox as SB
from ee.proc import child_environ, kill_tree
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult
from ee.core.events import Notice
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


BG_DESC = "后台运行，返回 id；用 shell_output 看输出。只给常驻命令（dev server）；测试等一次性命令前台跑"
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
            # lean 没有 grep/glob，无法「定位相关测试文件」，这条规则对 lean 无效，
            # 且 lean 预算已满（1200 上限）。只在 full 保留。
            self.rules = [r for r in self.rules if "跑测试" not in r]

    def summary(self, args: dict) -> str:
        return f"Ran {args.get('command', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("command 为空")
        shell = ctx.shell or resolve_shell("auto")
        sb = sandbox_plan(ctx)
        # 只读沙箱用的是受限令牌，Git Bash（cygwin）在它下面起不来（共享内存 DACL
        # 不满足 WRITE_RESTRICTED，CreateFileMapping 直接 Win32 error 5）；cmd /
        # powershell 不受影响。这里换成能用的那个，并把原因说出来——不静默换，
        # 因为命令语法会从 POSIX 变成 PowerShell。
        from ee.tools.sandbox_shell import readonly_shell_override
        new_kind, why = readonly_shell_override(shell.kind, sandbox_active=sb is not None)
        if why and new_kind != shell.kind:
            shell = resolve_shell(new_kind)
            ctx.emit(Notice(why, "warn"))
        timeout = min(MAX_TIMEOUT, max(1, int(args.get("timeout") or ctx.timeout_default)))
        if ctx.max_timeout > 0:                      # 会话级硬上限：模型显式传更大的 timeout 也不越界
            timeout = min(timeout, max(1, int(ctx.max_timeout)))
        cwd = ctx.resolve(args["workdir"]) if args.get("workdir") else ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f"workdir 不存在：{cwd}")
        if args.get("background"):
            return await run_background(command, shell, cwd, ctx)
        # 必然久等的命令（`sleep N; ...`、`gh run watch`）直接转后台：
        # 实测模型把它们写在前台，一条就能霍掉 600s 超时（新代码 91 回合里
        # 这类纯阻塞等 45 分钟）。显式传 background=false 的尊重模型。
        if args.get("background") is not False and long_wait_reason(command):
            sess = ctx.extra.get("session")
            if getattr(sess, "bg", None) is not None:
                ctx.emit(Notice(f"这条命令会阻塞很久（{long_wait_reason(command)}），已改走后台："
                                f"用 shell_output 等待结果", "info"))
                return await run_background(command, shell, cwd, ctx)
        t0 = time.monotonic()
        try:
            code, out, timed_out = await run_command(command, shell, cwd, timeout, ctx.emit, sandbox=sb)
        except FileNotFoundError as e:
            raise ToolError(f"无法启动 {shell.display}：{e}")
        if sb is not None and SB.is_helper_failure(code, out):
            raise ToolError(out.strip())
        dur = time.monotonic() - t0
        body, truncated = truncate_output_info(out.rstrip("\n"), ctx.output_dir, "shell")
        hint = full_pytest_hint(command)
        if hint:
            body = (body or "") + "\n" + hint
        # 超时也把已捕获的输出交回去：原先文案只说「已终止」，模型看不出还能接着用
        # （新代码 91 个回合里有 6 次撞 600s 超时，共 44 分钟，输出里其实已有进度）。
        # 输出里往往已有「跑到哪了」，能直接判断该换子集还是转后台。
        tail = (f"\n[超时：{timeout}s 后已终止，上面的输出是终止前已捕获的部分，可据此判断进度]"
                if timed_out else "")
        footer = f"\n[exit {code if code is not None else '?'} · {dur:.1f}s]" + _sb_note(sb)
        ok = (code == 0) and not timed_out
        return ToolResult((body or "（无输出）") + tail + footer, ok=ok, summary=f"Ran {command}",
                          meta={"kind": "shell", "command": command, "exit": code, "timed_out": timed_out,
                                **({"truncated": truncated} if truncated else {}), "output": out[-4000:]})



# 必然久等的命令：模型把它们写成前台，一条就吃掉 600s 超时。识别出来转后台。
_SLEEP_HEAD = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)\b", re.I)
_WATCH_PATTERNS = (
    (re.compile(r"\bgh\s+run\s+watch\b"), "等 CI 的 gh run watch"),
    (re.compile(r"\bgh\s+pr\s+checks\b[^;&|]*--watch\b"), "等 CI 的 gh pr checks --watch"),
)
SLEEP_BG_THRESHOLD = 10          # 睡 10 秒以上且后面还串了命令，就是「先等再查」的轮询写法


def long_wait_reason(command: str) -> str | None:
    """命令是不是「必然阻塞很久」，是则返回原因。检测不到返回 None。

    只看两类实测写法（新代码 91 回合里占了 45 分钟的纯阻塞）：
    - `sleep N; ...` / `sleep N && ...`（N>=10）：等完再查的轮询，本该走后台；
    - `gh run watch` / `gh pr checks --watch`：一直挂到 CI 结束，实测单次 600s。
    单独的 `sleep N` 不算（模型就是想等一下，前台让它等完更简单）。
    """
    m = _SLEEP_HEAD.match(command)
    if m and float(m.group(1)) >= SLEEP_BG_THRESHOLD and re.search(r"[;&|]|\n", command[m.end():]):
        return f"先 sleep {m.group(1)} 秒再继续"
    for pat, why in _WATCH_PATTERNS:
        if pat.search(command):
            return why
    return None


# 解释器的三种写法都要认：`python`、`python3`、`./.venv/Scripts/python.exe`（本机标准用法）。
# 只认裸 `python` 是不够的——实测 13 次全量 pytest 里 12 次写成带路径的 python.exe，
# 一次警示都没触发过。
_PY = r"[\w./\\~$-]*python[\w.]*(?:\.exe)?"
_PYTEST_CALL = re.compile(rf"(?:^|[\s;&|(])(?:{_PY}\s+-m\s+pytest|py\.test|pytest)(?=\s|$|[|;&)])")


def _is_pytest_invocation(command: str) -> bool:
    """命令是不是在跑 pytest（含 `python -m pytest`、带路径的 python.exe、裸 pytest）。"""
    return bool(_PYTEST_CALL.search(command))


def _strip_prefix_chain(command: str) -> str:
    """剥掉命令体前面的 `cd xxx &&` / `VAR=1 &&` 前缀，留下真正执行的那一段。

    没有这一步时 `cd repo && pytest` 的「第一个 token」是 cd，被判成「有范围」——
    实测这类写法的全量 pytest 全部漏检。
    """
    body = command
    while True:
        m = re.match(r"\s*(?:cd|pushd|set-location)\s+[^;&|]+\s*(?:&&|;|\|\||\n)\s*", body, re.I)
        if not m:
            m = re.match(r"\s*[\w.]+=[^\s;&|]*\s*(?:&&|;)\s*", body)
        if not m:
            return body
        body = body[m.end():]


# 这些参数说明「范围已缩小」或「根本没在执行」
_SCOPING_FLAGS = ("-k", "--deselect", "--ignore", "--lf", "--last-failed", "--co", "--collect-only",
                  "--sw", "--stepwise")

# 带值的选项：它们的值是配置/路径，不是「测试范围」。
# 漏掉这个时 `-c ee/pyproject.toml` 的 `ee/pyproject.toml` 会被当成选择器，
# 于是本机标准写法 `-c ee/pyproject.toml -q -n 8`（全量）全部漏检。
_VALUE_FLAGS = ("-c", "--config-file", "-p", "--rootdir", "-o", "--override-ini", "--basetemp",
                "--junitxml", "--timeout", "-W", "-m", "--numprocesses", "-n", "--tb", "--capture",
                "--maxfail", "-x", "--durations", "--log-level")


def _drop_option_values(body: str) -> str:
    """把「带值选项」连同它的值一起去掉，留下真正的测试选择器。

    `-c ee/pyproject.toml` 的 `ee/pyproject.toml` 是配置文件，不是测试范围；
    不去掉就会把它当成选择器，于是本机标准写法 `-c ee/pyproject.toml -q -n 8`
    （全量）全部漏检。
    """
    out: list[str] = []
    toks = body.split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _VALUE_FLAGS:
            i += 2                      # 连带值一起跳过（值不存在时也安全：i 越界即结束）
            continue
        if any(t.startswith(f + "=") for f in _VALUE_FLAGS):
            i += 1
            continue
        out.append(t)
        i += 1
    return " ".join(out)


def full_pytest_hint(command: str) -> str | None:
    """检测「无范围的全量 pytest」，返回给模型的即时提示；不是全量则返回 None。

    背景：本仓库全量约 1064 秒，而单次 shell 有超时上限——跑全量注定超时，
    再转后台轮询又是一轮浪费。与其让模型等到超时后知后觉，不如在结果里
    立刻告诉它「这是全量，改跑子集」。
    """
    if not _is_pytest_invocation(command):
        return None
    if any(flag in command for flag in _SCOPING_FLAGS):
        return None
    # 去掉 pytest 本身与常见无害参数，看还剩不剩「路径/节点选择器」
    body = re.split(r"[|;&]", _strip_prefix_chain(command))[0]   # 只看管道/串联前的部分
    body = re.sub(rf"^\s*(?:{_PY}\s+-m\s+)?(?:pytest|py\.test)\b", "", body)
    body = re.sub(r"\d*>&?\d*|\d*>|\d*<|\d*<<?&?\d*", " ", body)   # 剥掉重定向（2>&1、>/dev/null 等）
    body = _drop_option_values(body)
    for tok in body.split():
        if tok.startswith("-") or tok in ("python", "py", "/dev/null", "NUL", "nul"):
            continue
        # 还剩位置参数：测试文件（*.py / ::节点）说明范围已缩小；
        # 目录（tests、ee/tests）跑的是整个目录，等同于全量，不能算缩小。
        if tok.endswith(".py") or "::" in tok:
            return None
    return ("[提示：这是全量 pytest（本仓库 135 个测试文件，约 17 分钟），单次 shell 有超时上限，"
            "很可能跑不完。改用相关子集：pytest <测试文件> 或 -k <关键字>]")


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
