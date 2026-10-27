"""后端的生命线：桌面端没了就退出，退出时不留孤儿进程。只用标准库（Windows 走 ctypes），不需要原生依赖。

三道保险（由 `ee serve` 组合使用）：
1. stdin EOF：桌面端退出时管道关闭。
2. ParentWatch：盯住桌面端主进程 pid（Windows 上持有进程句柄等它结束，不怕 pid 复用；POSIX 轮询）。
   桌面端被强杀、而 stdin 的写端意外被别的进程继承时，仍然能发现。
3. hard_exit_after：要求退出后若干秒还没正常退完（事件循环卡死、清理卡住），
   就直接结束所有子孙进程（后台命令、MCP 服务器、shell）然后 os._exit。
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Callable

from ee.proc import IS_WINDOWS, kill_tree

log = logging.getLogger("ee.server")

__all__ = ["pid_alive", "list_children", "kill_descendants", "kill_tree", "ParentWatch", "hard_exit_after"]

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102


def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        k = _k32()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == 5     # 拒绝访问 = 进程存在
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # 僵尸进程（已退出、未被回收）也算没了
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            st = f.read().decode("utf-8", "replace")
        return st[st.rindex(")") + 2:][:1] not in ("Z", "X")
    except (OSError, ValueError):
        return True


def _process_table() -> list[tuple[int, int]]:
    """[(pid, ppid)]，失败返回空列表。"""
    out: list[tuple[int, int]] = []
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID)))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out
    if os.path.isdir("/proc"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    st = f.read().decode("utf-8", "replace")
                out.append((int(name), int(st[st.rindex(")") + 2:].split()[1])))
            except (OSError, ValueError, IndexError):
                continue
        return out
    try:                                               # macOS 等
        import subprocess
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        for line in r.stdout.splitlines():
            a = line.split()
            if len(a) == 2 and a[0].isdigit() and a[1].isdigit():
                out.append((int(a[0]), int(a[1])))
    except Exception:  # noqa: BLE001
        pass
    return out


def list_children(pid: int) -> list[int]:
    return [p for p, pp in _process_table() if pp == pid and p != pid]


def _descendants(pid: int) -> list[int]:
    table = _process_table()
    kids: dict[int, list[int]] = {}
    for p, pp in table:
        if p != pp:
            kids.setdefault(pp, []).append(p)
    out, todo, seen = [], [pid], {pid}
    while todo:
        cur = todo.pop()
        for c in kids.get(cur, []):
            if c not in seen:
                seen.add(c)
                out.append(c)
                todo.append(c)
    return out


def kill_descendants(pid: int | None = None) -> int:
    """结束 pid（默认自己）的所有子孙进程，自己不动。返回结束的个数（尽力而为）。"""
    root = pid or os.getpid()
    if IS_WINDOWS:
        kids = list_children(root)
        for c in kids:
            kill_tree(c)                               # taskkill /T：连同它的子孙
        return len(kids)
    desc = _descendants(root)
    for c in desc:
        try:
            if os.getpgid(c) == c:                     # 进程组长（start_new_session 起的）：整组杀
                os.killpg(c, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(c, signal.SIGKILL)
        except OSError:
            pass
    return len(desc)


class ParentWatch:
    """父进程（桌面端）退出时回调一次 on_gone。daemon 线程，不阻塞退出。"""

    def __init__(self, pid: int, on_gone: Callable[[], None], interval: float = 1.0):
        self.pid = pid
        self.on_gone = on_gone
        self.interval = interval
        self._stop = threading.Event()

    def start(self) -> "ParentWatch":
        threading.Thread(target=self._run, name="ee-parent-watch", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            if IS_WINDOWS:
                self._run_windows()
            else:
                self._run_posix()
        except Exception:  # noqa: BLE001
            log.warning("父进程看门狗异常", exc_info=True)
            return
        if not self._stop.is_set():
            log.info("桌面端进程 %s 已退出，后端退出", self.pid)
            try:
                self.on_gone()
            except Exception:  # noqa: BLE001
                log.warning("父进程退出回调失败", exc_info=True)

    def _run_windows(self) -> None:
        import ctypes
        k = _k32()
        h = k.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not h:
            if ctypes.get_last_error() == 5:           # 拒绝访问：退回轮询
                while not self._stop.wait(self.interval):
                    if not pid_alive(self.pid):
                        return
            return                                     # 打不开 = 已经不在了
        try:
            ms = max(50, int(self.interval * 1000))
            while not self._stop.is_set():
                r = k.WaitForSingleObject(h, ms)
                if r != _WAIT_TIMEOUT:                 # 进程结束（或句柄出错）
                    return
        finally:
            k.CloseHandle(h)

    def _run_posix(self) -> None:
        ppid0 = os.getppid()
        while True:
            if not pid_alive(self.pid):
                return
            if self.pid == ppid0 and os.getppid() != ppid0:   # 被过继给 init：父进程没了
                return
            if self._stop.wait(self.interval):
                return


_hard_lock = threading.Lock()
_hard_started = False


def hard_exit_after(seconds: float, code: int = 0, reason: str = "") -> None:
    """seconds 秒后若进程还在：结束所有子孙进程并 os._exit(code)。只启动一次。"""
    global _hard_started
    with _hard_lock:
        if _hard_started:
            return
        _hard_started = True

    def run():
        time.sleep(seconds)
        try:
            log.warning("后端 %.0f 秒内没有正常退出（%s），强制结束子进程并退出", seconds, reason or "退出")
        except Exception:  # noqa: BLE001
            pass
        try:
            kill_descendants()
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(code)

    threading.Thread(target=run, name="ee-hard-exit", daemon=True).start()
