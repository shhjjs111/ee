"""后台长命令（D4-6）：dev server、watch、长测试、交互程序。

- shell 工具带 background=true 时在这里启动：stdin 是独立管道（可写），stdout+stderr 合并读进环形缓冲，
  同时完整写进日志文件 <home>/tool-output/bg-<会话>-<id>.log。
- 每个会话一个 BgManager（子代理共用父会话的）；回合结束、用户中断都不杀，会话删除 / 内核退出时杀整棵进程树。
- 模型侧：shell_output 读增量输出（可等待新输出、可按正则过滤）、shell_write 写 stdin、shell_kill 结束。
- 界面侧：on_change 回调把启动 / 退出事件交给 server（shell/updated 通知），shell/list|read|kill RPC。
"""
from __future__ import annotations

import asyncio
import atexit
import codecs
import itertools
import logging
import os
import re
import signal
import subprocess
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ee.proc import IS_WINDOWS, kill_tree

log = logging.getLogger("ee.bgproc")

BUFFER_CHARS = 400_000          # 内存里每个进程最多留这么多字符（更早的只在日志文件里）
READ_LIMIT = 30_000             # 一次回给模型的输出上限
MAX_PROCS = 8                   # 每个会话同时运行的后台命令上限

_ALL: "weakref.WeakSet[BgManager]" = weakref.WeakSet()


@dataclass
class BgProc:
    id: str
    command: str
    cwd: str
    shell: str
    log_path: Path
    started: float = field(default_factory=time.time)
    ended: float | None = None
    exit_code: int | None = None
    status: str = "running"          # running | exited | killed | failed
    pid: int | None = None
    total: int = 0                   # 累计输出字符数
    cursor: int = 0                  # 模型已读到的位置（累计字符数）
    _buf: list[str] = field(default_factory=list)
    _buf_start: int = 0              # _buf 第一个字符对应的累计位置
    _buf_len: int = 0
    _proc: asyncio.subprocess.Process | None = None
    _changed: asyncio.Event | None = None
    _pump: asyncio.Task | None = None
    _script: Path | None = None
    _log = None

    # ---- 输出缓冲
    def _append(self, s: str) -> None:
        if not s:
            return
        self._buf.append(s)
        self._buf_len += len(s)
        self.total += len(s)
        while self._buf_len > BUFFER_CHARS and len(self._buf) > 1:
            drop = self._buf.pop(0)
            self._buf_len -= len(drop)
            self._buf_start += len(drop)
        if self._log is not None:
            try:
                self._log.write(s)
                self._log.flush()
            except Exception:  # noqa: BLE001
                pass
        if self._changed is not None:
            self._changed.set()

    def text_from(self, pos: int) -> tuple[str, int]:
        """从累计位置 pos 起的输出；返回 (文本, 因为缓冲溢出丢掉的字符数)。"""
        lost = max(0, self._buf_start - pos)
        start = max(pos, self._buf_start)
        whole = "".join(self._buf)
        return whole[start - self._buf_start:], lost

    def tail(self, n: int = 20_000) -> str:
        whole = "".join(self._buf)
        return whole[-n:]

    @property
    def running(self) -> bool:
        return self.status == "running"

    def info(self) -> dict:
        end = self.ended or time.time()
        return {"id": self.id, "command": self.command, "cwd": self.cwd, "status": self.status,
                "exit_code": self.exit_code, "pid": self.pid, "started": self.started, "ended": self.ended,
                "duration": round(end - self.started, 1), "output_chars": self.total,
                "unread": max(0, self.total - self.cursor), "log_path": str(self.log_path)}


class BgManager:
    def __init__(self, log_dir: Path, owner: str = "s"):
        self.log_dir = Path(log_dir)
        self.owner = re.sub(r"[^\w-]", "_", owner)[:24] or "s"
        self.procs: dict[str, BgProc] = {}
        self._ids = itertools.count(1)
        self.on_change: Callable[[dict], None] | None = None
        _ALL.add(self)

    # ---- 启动
    async def start(self, command: str, shell, cwd: Path, env: dict) -> BgProc:
        if sum(1 for p in self.procs.values() if p.running) >= MAX_PROCS:
            raise RuntimeError(f"后台命令已达上限 {MAX_PROCS} 个，先用 shell_kill 结束不需要的")
        pid_ = f"b{next(self._ids)}"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"bg-{self.owner}-{pid_}.log"
        p = BgProc(id=pid_, command=command, cwd=str(cwd), shell=getattr(shell, "display", ""), log_path=log_path)
        p._changed = asyncio.Event()
        try:
            p._log = open(log_path, "w", encoding="utf-8", errors="replace", newline="")
        except OSError:
            p._log = None
        kw: dict = {}
        if IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        argv, script = shell.prepare(command)
        p._script = script
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env, **kw)
        except Exception:
            self._cleanup_files(p)
            raise
        p._proc = proc
        p.pid = proc.pid
        self.procs[p.id] = p
        p._pump = asyncio.get_running_loop().create_task(self._pump_output(p))
        log.info("后台命令 %s 启动（pid %s）：%s", p.id, p.pid, command[:200])
        self._emit(p)
        return p

    async def _pump_output(self, p: BgProc) -> None:
        proc = p._proc
        assert proc is not None and proc.stdout is not None
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        cr = ""          # 上一块末尾的 \r 先扣着：\r\n 可能被拆在两次读取之间
        try:
            while True:
                b = await proc.stdout.read(4096)
                if not b:
                    break
                s = (cr + dec.decode(b)).replace("\r\n", "\n")
                cr = "\r" if s.endswith("\r") else ""
                p._append(s[:-1] if cr else s)
            p._append(cr + dec.decode(b"", final=True))
            code = await proc.wait()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            p._append(f"\n[读取输出出错：{e}]\n")
            code = proc.returncode
        p.exit_code = code
        if p.status == "running":
            p.status = "exited"
        p.ended = time.time()
        self._cleanup_files(p)
        if p._changed is not None:
            p._changed.set()
        log.info("后台命令 %s 结束：%s exit=%s", p.id, p.status, code)
        self._emit(p)

    def _cleanup_files(self, p: BgProc) -> None:
        if p._log is not None:
            try:
                p._log.close()
            except Exception:  # noqa: BLE001
                pass
            p._log = None
        if p._script is not None:
            try:
                p._script.unlink()
            except OSError:
                pass
            p._script = None

    def _emit(self, p: BgProc) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb(p.info())
            except Exception:  # noqa: BLE001
                log.exception("shell/updated 回调失败")

    # ---- 查询
    def get(self, pid: str) -> BgProc:
        p = self.procs.get(str(pid).strip())
        if p is None:
            known = "、".join(self.procs) or "无"
            raise KeyError(f"没有后台命令 {pid}（现有：{known}）")
        return p

    def list(self) -> list[dict]:
        return [p.info() for p in self.procs.values()]

    async def wait_output(self, p: BgProc, wait_s: float, pattern: re.Pattern | None = None) -> None:
        """最多等 wait_s 秒：有新输出（有 pattern 时要新输出里匹配）或进程结束就返回。"""
        deadline = time.monotonic() + max(0.0, wait_s)
        while p.running:
            if p.total > p.cursor:
                if pattern is None or pattern.search(p.text_from(p.cursor)[0]):
                    # 输出往往一阵一阵来：再等一小会儿把这一阵收齐
                    await asyncio.sleep(min(0.3, max(0.0, deadline - time.monotonic())))
                    return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            assert p._changed is not None
            p._changed.clear()
            try:
                await asyncio.wait_for(p._changed.wait(), timeout=left)
            except asyncio.TimeoutError:
                return

    def read_new(self, p: BgProc, limit: int = READ_LIMIT, pattern: re.Pattern | None = None) -> tuple[str, dict]:
        """取模型还没读过的输出并前移游标。超长保留开头和结尾；pattern 只留匹配的行。"""
        text, lost = p.text_from(p.cursor)
        p.cursor = p.total
        notes = {"lost": lost, "omitted": 0, "filtered": False}
        if pattern is not None:
            lines = [ln for ln in text.splitlines() if pattern.search(ln)]
            text = "\n".join(lines)
            notes["filtered"] = True
        if len(text) > limit:
            head = limit // 3
            notes["omitted"] = len(text) - limit
            text = text[:head] + f"\n…（省略 {notes['omitted']} 个字符）…\n" + text[-(limit - head):]
        return text, notes

    # ---- 写 stdin / 结束
    async def write(self, p: BgProc, data: str, close: bool = False) -> None:
        if not p.running or p._proc is None or p._proc.stdin is None:
            raise RuntimeError(f"后台命令 {p.id} 已经结束，不能再输入")
        try:
            if data:
                p._proc.stdin.write(data.encode("utf-8"))
                await asyncio.wait_for(p._proc.stdin.drain(), timeout=10)
            if close:
                p._proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise RuntimeError(f"写入失败，进程可能已关闭输入：{e}") from e

    async def kill(self, p: BgProc) -> None:
        if not p.running:
            return
        p.status = "killed"
        await asyncio.to_thread(_kill_pid, p.pid)
        proc = p._proc
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if p._pump is not None:
            try:
                await asyncio.wait_for(asyncio.shield(p._pump), timeout=3)
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        """会话结束：杀掉所有还在跑的后台命令。"""
        for p in list(self.procs.values()):
            if p.running:
                try:
                    await self.kill(p)
                except Exception:  # noqa: BLE001
                    log.exception("结束后台命令 %s 失败", p.id)

    def kill_all_sync(self) -> None:
        for p in list(self.procs.values()):
            if p.running and p.pid:
                p.status = "killed"
                _kill_pid(p.pid)


def _kill_pid(pid: int | None) -> None:
    if not pid:
        return
    if IS_WINDOWS:
        kill_tree(pid)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


async def close_all() -> None:
    for m in list(_ALL):
        await m.close()


@atexit.register
def _kill_everything() -> None:     # 兜底：内核退出时不留孤儿进程
    for m in list(_ALL):
        try:
            m.kill_all_sync()
        except Exception:  # noqa: BLE001
            pass
