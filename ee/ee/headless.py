"""无界面执行：`ee exec`（D4-9）。一次跑完一个回合就退出，给脚本、CI、定时任务（D4-10）用。

用法：
  ee exec "任务"                       提示词作为参数
  ee exec -  /  ee exec               从 stdin 读提示词（UTF-8，自动去 BOM）
  ee exec --json "任务"                stdout 每行一个 JSON 事件（JSONL）
  ee exec --resume <会话id> "继续"     续接已有会话；--last 续接当前目录最近的会话

输出：
  不加 --json：stdout 只有最终回答；进度（工具调用、自动审批、提示、会话 id）打到 stderr（-q 关掉）。
  加 --json：stdout 只有 JSONL 事件，见 EVENT_TYPES；人看的进度一律不打。

会话：与桌面端共用 ~/.ee/sessions（同一套 SessionStore、同样的条目格式），meta.source="exec"，
桌面端左栏能看到、能接着聊、能 /undo；--no-persist 不落盘。

权限：--mode 默认 read-only（最安全）。default/auto 模式下遇到需要审批的操作，没人能批：
  --approve deny（默认）一律拒绝，并把原因告诉模型让它换做法；
  --approve auto  自动批准，但危险命令（强删根目录、强制推送等强制询问类）仍然拒绝。
ask_user 提问工具在 exec 下关闭（配置 ask_user=false）。

退出码：0 完成；1 出错（模型/网络/配置错误、会话不存在等）；2 参数错误（没有提示词等）；
  3 到达步数上限；4 防死循环停下；130 被 Ctrl+C 中断。
Ctrl+C：第一次优雅中断当前回合（已产生的历史会保存），第二次立即退出。
结束时（无论成败）结束本次启动的所有后台命令、关闭 MCP 连接。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, TextIO

from ee.core.events import (ApprovalRequest, AssistantMessage, PlanUpdate, SubagentEnd, SubagentStart, TextDelta,
                            ToolEnd, ToolStart, TurnStart, Usage)
from ee.core.permissions import MODE_LABEL, MODES

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_MAX_STEPS, EXIT_LOOP, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
EXIT_CODES = {"completed": EXIT_OK, "error": EXIT_ERROR, "max_steps": EXIT_MAX_STEPS, "loop": EXIT_LOOP,
              "interrupted": EXIT_INTERRUPTED}

# --json 时的事件类型（每行一个对象，都有 "type"）：
EVENT_TYPES = (
    "session",          # {id, cwd, model, mode, resumed, persist}           第一行
    "turn_start",       # {turn}
    "assistant_delta",  # {text}                                              仅 --stream
    "assistant",        # {text, reasoning?}                                  每步一条（完整文本）
    "tool_start",       # {call_id, name, summary, args}
    "tool_end",         # {call_id, name, ok, status: ok|error|denied, summary, output, truncated, diff?}
    "approval",         # {call_id, tool, title, decision: approved|denied, reason}
    "plan",             # {items}                                             todo 清单
    "subagent_start",   # {call_id, agent, description}
    "subagent_end",     # {call_id, status, steps}
    "notice",           # {text, level}
    "usage",            # {prompt_tokens, completion_tokens, cache_hit, cost, totals}
    "error",            # {message}
    "turn_done",        # {reason, text, session_id, duration_ms, usage, exit_code}  最后一行
)

JSON_OUTPUT_LIMIT = 4000          # JSON 里单个工具输出 / diff 的上限（首尾各留一半）
DENY_REASON = ("现在是无人值守的 ee exec，没有人能批准这个操作，已自动拒绝。"
               "请换不需要审批的做法；做不到就在最终回答里说明需要用户手动做什么。")
FORCE_DENY_REASON = ("这是危险操作，无人值守的 ee exec 不会自动批准，已拒绝。"
                     "请换安全的做法，或在最终回答里说明需要用户手动执行什么。")
_SID_RE = re.compile(r"^[\w\-.]+$")


class ExecError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def _clip(s: str, n: int = JSON_OUTPUT_LIMIT) -> tuple[str, bool]:
    s = s or ""
    if len(s) <= n:
        return s, False
    half = n // 2
    return s[:half] + f"\n…（省略 {len(s) - n} 个字符）…\n" + s[-half:], True


# ------------------------------------------------------------------ 参数
def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("prompt", nargs="?", help="提示词；省略或写 - 时从 stdin 读")
    p.add_argument("-C", "--cd", metavar="目录", help="工作目录（默认当前目录；续接会话时默认用会话原来的目录）")
    p.add_argument("-m", "--model", metavar="模型", help="模型名（默认用配置里的默认模型；续接时沿用会话的模型）")
    p.add_argument("--mode", choices=MODES, default="read-only",
                   help="权限模式，默认 read-only（只读）")
    p.add_argument("--approve", choices=["deny", "auto"], default="deny",
                   help="需要审批的操作怎么处理：deny 一律拒绝（默认）；auto 自动批准（危险命令仍拒绝）")
    p.add_argument("--allow", action="append", default=[], metavar="规则",
                   help="追加 allow 规则（同 config.toml permissions.allow 的写法，如 'shell(git status*)'），可多次")
    p.add_argument("--deny", action="append", default=[], metavar="规则", help="追加 deny 规则，可多次")
    p.add_argument("--max-steps", type=int, metavar="N", help="本回合最多几步（默认用配置 max_steps）")
    p.add_argument("--json", action="store_true", help="stdout 输出 JSONL 事件流")
    p.add_argument("--stream", action="store_true", help="--json 时额外输出 assistant_delta 流式片段")
    p.add_argument("-o", "--output-last-message", metavar="文件", help="把最终回答写进这个文件（UTF-8）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--resume", metavar="会话id", help="续接这个会话（与桌面端同一套会话）")
    g.add_argument("--last", action="store_true", help="续接工作目录下最近的一个会话")
    p.add_argument("--no-persist", action="store_true", help="不保存会话（桌面端看不到，之后也不能续接）")
    p.add_argument("-q", "--quiet", action="store_true", help="不加 --json 时也不往 stderr 打进度")


# ------------------------------------------------------------------ 输出
class Printer:
    """把事件写出去：--json 写 JSONL 到 stdout；否则人看的进度写 stderr。"""

    def __init__(self, json_mode: bool, stream: bool, quiet: bool, stdout: TextIO, stderr: TextIO):
        self.json_mode, self.stream, self.quiet = json_mode, stream, quiet
        self.out, self.err = stdout, stderr

    def event(self, type_: str, **data) -> None:
        if not self.json_mode:
            return
        try:
            self.out.write(json.dumps({"type": type_, **data}, ensure_ascii=False, default=str) + "\n")
            self.out.flush()
        except (OSError, ValueError):     # 管道被关（如 | head）：不再写
            pass

    def progress(self, text: str) -> None:
        if self.json_mode or self.quiet:
            return
        try:
            self.err.write(text + "\n")
            self.err.flush()
        except (OSError, ValueError):
            pass

    def final(self, text: str) -> None:
        if self.json_mode or not text:
            return
        try:
            self.out.write(text + ("" if text.endswith("\n") else "\n"))
            self.out.flush()
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------ 线程（复用桌面端的 Thread：条目格式、落盘完全一致）
def _make_thread_class():
    from ee.server.threads import Thread

    class ExecThread(Thread):
        """在桌面端 Thread 上加两件事：事件转给 Printer；审批按 --approve 立即自动决定。"""

        printer: Printer
        approve: str = "deny"
        final_text: str = ""

        def _add(self, it: dict, persist: bool = True) -> dict:
            it = super()._add(it, persist)
            t = it.get("type")
            if t == "notice":
                self.printer.event("notice", text=it.get("text", ""), level=it.get("level", "info"))
                self.printer.progress(f"[提示] {it.get('text', '')}")
            elif t == "error":
                self.printer.event("error", message=it.get("message", ""))
                self.printer.progress(f"[错误] {it.get('message', '')}")
            return it

        def _on_event(self, ev) -> None:
            super()._on_event(ev)
            p = self.printer
            if isinstance(ev, TurnStart):
                p.event("turn_start", turn=ev.turn)
            elif isinstance(ev, TextDelta):
                if p.stream:
                    p.event("assistant_delta", text=ev.text)
            elif isinstance(ev, AssistantMessage):
                if ev.text or ev.reasoning:
                    d = {"text": ev.text or ""}
                    if ev.reasoning:
                        d["reasoning"] = ev.reasoning
                    p.event("assistant", **d)
            elif isinstance(ev, ToolStart):
                p.event("tool_start", call_id=ev.call_id, name=ev.name, summary=ev.summary, args=ev.args)
                p.progress(f"→ {ev.summary or ev.name}")
            elif isinstance(ev, ToolEnd):
                status = "ok" if ev.ok else ("denied" if ev.meta.get("denied") else "error")
                out, cut = _clip(ev.output or "")
                d = {"call_id": ev.call_id, "name": ev.name, "ok": ev.ok, "status": status,
                     "summary": ev.summary, "output": out, "truncated": cut}
                if ev.diff:
                    d["diff"] = _clip(ev.diff)[0]
                if ev.meta.get("kind") == "plan_proposal":      # 计划模式提交的方案：最终回答就是它
                    d["plan"] = ev.meta.get("plan", "")
                    self.final_text = ev.meta.get("plan", "")
                p.event("tool_end", **d)
                if not ev.ok:
                    first = (ev.output or "").strip().splitlines()[:1]
                    p.progress(f"  {'已拒绝' if status == 'denied' else '失败'}：{first[0][:200] if first else ''}")
            elif isinstance(ev, ApprovalRequest):
                if self.approve == "auto" and not ev.force:
                    choice, reason = "once", ""
                else:
                    choice, reason = "deny", (FORCE_DENY_REASON if ev.force else DENY_REASON)
                if ev.call_id in self.approvals:
                    self.respond(ev.call_id, choice, reason)
                elif ev.future is not None and not ev.future.done():
                    from ee.core.events import ApprovalDecision
                    ev.future.set_result(ApprovalDecision("deny_with_reason" if choice == "deny" else choice, reason))
                decision = "approved" if choice == "once" else "denied"
                p.event("approval", call_id=ev.call_id, tool=ev.tool, title=ev.title, decision=decision,
                        reason=reason)
                p.progress(f"[审批] {'自动批准' if decision == 'approved' else '自动拒绝'}：{ev.title}")
            elif isinstance(ev, PlanUpdate):
                p.event("plan", items=ev.items)
            elif isinstance(ev, SubagentStart):
                p.event("subagent_start", call_id=ev.call_id, agent=ev.agent, description=ev.description)
            elif isinstance(ev, SubagentEnd):
                p.event("subagent_end", call_id=ev.call_id, status=ev.status, steps=ev.steps)
            elif isinstance(ev, Usage):
                p.event("usage", prompt_tokens=ev.prompt_tokens, completion_tokens=ev.completion_tokens,
                        cache_hit=ev.cache_hit, cost=ev.cost, totals=ev.totals)

    return ExecThread


# ------------------------------------------------------------------ 主流程
def _read_prompt(args, stdin: TextIO | None) -> str:
    if args.prompt not in (None, "-"):
        return args.prompt
    src = stdin if stdin is not None else sys.stdin
    if src is None:
        raise ExecError("没有提示词：请把提示词写在参数里，或用 - 从 stdin 传入。", EXIT_USAGE)
    if args.prompt is None and stdin is None and src.isatty():
        raise ExecError("没有提示词：请把提示词写在参数里，或通过管道从 stdin 传入（ee exec - < 文件）。", EXIT_USAGE)
    buf = getattr(src, "buffer", None)
    if buf is not None:     # 按字节读再按 UTF-8 解码：不受 Windows 控制台代码页影响；去掉 PowerShell 可能加的 BOM
        text = buf.read().decode("utf-8-sig", errors="replace")
    else:
        text = src.read()
        text = text[1:] if text.startswith("\ufeff") else text
    if not text.strip():
        raise ExecError("stdin 里没有读到提示词。", EXIT_USAGE)
    return text.strip("\r\n")


def _find_last(home: Path, cwd: Path) -> str:
    from ee.core.store import SessionStore
    norm = os.path.normcase(str(cwd))
    for p in SessionStore.list_sessions(home):
        try:
            c = SessionStore.summary(p).get("cwd") or ""
        except Exception:  # noqa: BLE001
            continue
        if c and os.path.normcase(os.path.abspath(c)) == norm:
            return p.stem
    raise ExecError(f"目录 {cwd} 下没有可续接的会话。")


def _load_thread(cls, home: Path, sid: str, **kw):
    from ee.core.store import SessionStore
    if not _SID_RE.match(sid or ""):
        raise ExecError(f"会话 id 不合法：{sid}")
    path = home / "sessions" / f"{sid}.jsonl"
    if not path.is_file():
        raise ExecError(f"会话不存在：{sid}")
    d = SessionStore.load(path)
    return cls(sid, home, lambda m, p: None, d["meta"], messages=d["messages"], items=d["items"], **kw)


async def _run(args, prompt: str, printer: Printer, home: Path, client_factory: Callable | None) -> tuple[int, str]:
    from ee.core.store import SessionStore

    cls = _make_thread_class()

    def configure(cfg) -> None:
        d = cfg.data
        d["ask_user"] = False                          # 没人回答问题
        if args.max_steps:
            d["max_steps"] = max(1, int(args.max_steps))
        if args.allow or args.deny:
            perms = dict(d.get("permissions") or {})
            perms["allow"] = list(perms.get("allow") or []) + list(args.allow)
            perms["deny"] = list(perms.get("deny") or []) + list(args.deny)
            d["permissions"] = perms

    kw = dict(client_factory=client_factory, persist=not args.no_persist, configure=configure)
    resumed = bool(args.resume or args.last)
    if resumed:
        sid = args.resume or _find_last(home, Path(args.cd or os.getcwd()).resolve())
        th = await asyncio.to_thread(_load_thread, cls, home, sid, **kw)
        if args.cd:
            th.meta["cwd"] = str(Path(args.cd).resolve())
        if not Path(th.meta.get("cwd") or "").is_dir():
            raise ExecError(f"会话的工作目录不存在：{th.meta.get('cwd')}（用 -C 指定）")
    else:
        cwd = Path(args.cd or os.getcwd()).expanduser()
        if not cwd.is_dir():
            raise ExecError(f"目录不存在：{cwd}")
        store = SessionStore(home)
        meta = {"cwd": str(cwd.resolve()), "model": "", "mode": args.mode, "title": "新对话",
                "created": time.time(), "source": "exec"}
        th = cls(store.id, home, lambda m, p: None, meta, **kw)
    th.printer, th.approve = printer, args.approve

    try:
        s = await th.ensure_session_async()
    except ValueError as e:
        raise ExecError(f"配置错误：{e}")
    if args.model and args.model != s.spec.alias:
        try:
            s.set_model(args.model)
        except ValueError as e:
            raise ExecError(f"模型不可用：{e}")
        th.meta["model"] = s.spec.alias
    printer.event("session", id=th.id, cwd=th.meta["cwd"], model=s.spec.alias, mode=args.mode,
                  resumed=resumed, persist=not args.no_persist)
    printer.progress(f"ee exec · 会话 {th.id}{'（续接）' if resumed else ''} · {s.spec.alias} · "
                     f"{MODE_LABEL.get(args.mode, args.mode)}模式 · {th.meta['cwd']}")

    loop = asyncio.get_running_loop()
    hits = {"n": 0}
    restore = _install_sigint(loop, th, hits)
    reason, t0 = "error", time.monotonic()
    try:
        th.start_turn(prompt, mode=args.mode)
        task = th.task
        assert task is not None
        await task
        last = next((it for it in reversed(th.items) if it.get("type") == "turn_end"), None)
        reason = (last or {}).get("reason") or "error"
    finally:
        restore()
        try:
            await asyncio.wait_for(s.bg.close(), 8)           # 后台命令一律结束（exec 不留进程）
        except Exception:  # noqa: BLE001
            s.bg.kill_all_sync()
        try:
            from ee.mcp.manager import shutdown as mcp_shutdown
            await asyncio.wait_for(mcp_shutdown(), 8)
        except Exception:  # noqa: BLE001
            pass
    text = th.final_text or _last_text(th)
    code = EXIT_CODES.get(reason, EXIT_ERROR)
    printer.event("turn_done", reason=reason, text=text, session_id=th.id,
                  duration_ms=int((time.monotonic() - t0) * 1000), usage=th.usage, exit_code=code)
    if reason != "completed":
        label = {"error": "出错", "max_steps": "到达步数上限", "loop": "检测到重复操作，已停止",
                 "interrupted": "已中断"}.get(reason, reason)
        printer.progress(f"[结束] {label}（退出码 {code}）")
    printer.progress(f"会话 id：{th.id}" + ("" if args.no_persist else f"（续接：ee exec --resume {th.id} \"…\"）"))
    return code, text


def _last_text(th) -> str:
    """本回合最后一条非空助手回复。"""
    for it in reversed(th.items):
        if it.get("type") == "user":
            break
        if it.get("type") == "assistant" and (it.get("text") or "").strip():
            return it["text"]
    return ""


def _install_sigint(loop, th, hits: dict) -> Callable[[], None]:
    """Ctrl+C：第一次中断当前回合（历史保存），第二次立即退出。Windows/POSIX 都用 signal.signal（控制台 Ctrl+C 可用）。"""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def handler(signum, frame):  # noqa: ARG001
        hits["n"] += 1
        if hits["n"] >= 2:
            try:
                if th.session is not None:
                    th.session.bg.kill_all_sync()
            finally:
                os._exit(EXIT_INTERRUPTED)
        loop.call_soon_threadsafe(th.interrupt)

    old = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(signal.SIGINT, old)
        except (ValueError, OSError):
            pass
    return restore


def run(args, *, home: Path | None = None, client_factory: Callable | None = None,
        stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """执行 `ee exec`，返回退出码。测试可注入 client_factory / stdin / stdout / stderr / home。"""
    from ee.config import ee_home
    out, err = stdout or sys.stdout, stderr or sys.stderr
    printer = Printer(args.json, args.stream, args.quiet, out, err)
    home = home or ee_home()

    def fail(msg: str, code: int) -> int:
        if args.json:
            printer.event("error", message=msg)
            printer.event("turn_done", reason="error", text="", session_id=None, duration_ms=0, usage={},
                          exit_code=code)
        else:
            try:
                err.write(f"ee exec：{msg}\n")
                err.flush()
            except (OSError, ValueError):
                pass
        return code

    try:
        prompt = _read_prompt(args, stdin)
    except ExecError as e:
        return fail(str(e), e.code)
    try:
        code, text = asyncio.run(_run(args, prompt, printer, home, client_factory))
    except ExecError as e:
        return fail(str(e), e.code)
    except KeyboardInterrupt:
        return fail("已中断", EXIT_INTERRUPTED)
    printer.final(text)
    if args.output_last_message:
        try:
            p = Path(args.output_last_message)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
        except OSError as e:
            fail(f"写入 {args.output_last_message} 失败：{e}", EXIT_ERROR)
            return EXIT_ERROR if code == EXIT_OK else code
    return code
