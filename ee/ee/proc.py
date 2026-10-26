"""安全地跑短命令（git / rg / taskkill …）并拿输出。

为什么不用 subprocess.run(capture_output=True, timeout=…)：
- 子进程默认继承本进程的 stdin。桌面端用管道当 ee-backend 的 stdin，并有线程阻塞在上面读
  （--exit-on-stdin-eof）。Windows 上同步管道挂着 ReadFile 时，继承了同一管道的子进程会在启动阶段卡住。
  所以这里一律 stdin=DEVNULL（或显式给 input）。
- Windows 上 subprocess.run 超时后会 kill 子进程再无限期 communicate()；git.exe（Git\\cmd 里的包装器）
  会再起一个真正的 git.exe 继承输出管道，kill 包装器后管道不关，communicate() 永远等不到 EOF。
  这里超时后杀整棵进程树，并且收尾的 communicate() 也有超时。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def popen_kwargs() -> dict:
    """所有后台子进程的公共参数：不弹窗、POSIX 下单独进程组（便于整组杀掉）。"""
    if IS_WINDOWS:
        return {"creationflags": no_window_flags()}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def run_capture(argv: list[str], cwd: str | os.PathLike | None = None, timeout: float = 10,
                input: bytes | None = None, env: dict | None = None) -> tuple[int, bytes, bytes]:
    """跑命令，返回 (returncode, stdout, stderr)。找不到程序 → (127, b"", 错误)；超时 → (124, 已有输出, b"timeout")。

    绝不无限期阻塞：超时杀进程树，收尾最多再等 3 秒。
    """
    try:
        p = subprocess.Popen(argv, cwd=str(cwd) if cwd else None,
                             stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **popen_kwargs())
    except OSError as e:
        return 127, b"", str(e).encode("utf-8", "replace")
    try:
        out, err = p.communicate(input=input, timeout=timeout)
        return p.returncode, out or b"", err or b""
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)
        try:
            p.kill()
        except OSError:
            pass
        out = b""
        try:
            out, _ = p.communicate(timeout=3)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            for f in (p.stdout, p.stderr, p.stdin):
                try:
                    if f:
                        f.close()
                except OSError:
                    pass
        return 124, out or b"", b"timeout"
