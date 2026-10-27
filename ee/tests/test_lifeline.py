"""后端生命线：桌面端（父进程）没了就退出、事件循环卡死也能硬退出、退出时不留孤儿子进程。"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from ee.server import lifeline as L

EE_ROOT = str(Path(__file__).resolve().parents[1])
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _env(home: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = EE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = env["USERPROFILE"] = env["APPDATA"] = str(home)
    env["EE_LOG_DIR"] = str(home / "logs")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _wait_dead(pid: int, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not L.pid_alive(pid):
            return True
        time.sleep(0.1)
    return not L.pid_alive(pid)


def _new_session_kw() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def test_pid_alive():
    assert L.pid_alive(os.getpid())
    p = subprocess.Popen(SLEEPER)
    assert L.pid_alive(p.pid)
    p.kill()
    p.wait()
    assert not L.pid_alive(p.pid)
    assert not L.pid_alive(0) and not L.pid_alive(-5)


def test_list_children_sees_new_session_child():
    p = subprocess.Popen(SLEEPER, **_new_session_kw())
    try:
        assert p.pid in L.list_children(os.getpid())
    finally:
        p.kill()
        p.wait()


def test_parent_watch_fires_when_parent_exits():
    parent = subprocess.Popen(SLEEPER)
    fired = []
    w = L.ParentWatch(parent.pid, lambda: fired.append(time.monotonic()), interval=0.1)
    w.start()
    time.sleep(0.3)
    assert not fired
    parent.kill()
    parent.wait()
    t0 = time.monotonic()
    while not fired and time.monotonic() - t0 < 5:
        time.sleep(0.05)
    assert fired, "父进程退出后看门狗没有触发"


def test_parent_watch_fires_immediately_for_dead_pid():
    p = subprocess.Popen(SLEEPER)
    p.kill()
    p.wait()
    fired = []
    L.ParentWatch(p.pid, lambda: fired.append(1), interval=0.1).start()
    t0 = time.monotonic()
    while not fired and time.monotonic() - t0 < 3:
        time.sleep(0.05)
    assert fired


def test_hard_exit_kills_descendants_even_when_main_thread_is_stuck(tmp_path):
    # 模拟：事件循环卡死（主线程睡死），但有一个新进程组里的孙进程（像后台命令 / MCP 服务器）
    script = textwrap.dedent(f"""
        import subprocess, sys, time
        from ee.server import lifeline as L
        kw = {_new_session_kw()!r}
        g = subprocess.Popen({SLEEPER!r}, **kw)
        print(g.pid, flush=True)
        L.hard_exit_after(0.5, reason="test")
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=_env(tmp_path), text=True)
    gpid = int(p.stdout.readline())
    try:
        assert p.wait(timeout=15) == 0
        assert _wait_dead(gpid, 10), "硬退出后孙进程还活着（孤儿进程）"
    finally:
        if L.pid_alive(gpid):
            L.kill_tree(gpid)
        if p.poll() is None:
            p.kill()


def _start_serve(tmp_path, extra_args, stdin=subprocess.PIPE):
    p = subprocess.Popen([sys.executable, "-m", "ee", "serve", *extra_args], stdin=stdin,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env(tmp_path), text=True,
                         encoding="utf-8")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        line = p.stdout.readline()
        if not line:
            break
        if line.startswith("EE_SERVE_READY"):
            return p, json.loads(line.split(" ", 1)[1])
    p.kill()
    raise AssertionError("ee serve 没有启动")


def test_serve_exits_when_parent_pid_dies(tmp_path):
    parent = subprocess.Popen(SLEEPER)
    # stdin 由测试进程一直握着（不会 EOF）：只能靠父进程看门狗退出
    p, _ = _start_serve(tmp_path, ["--exit-on-stdin-eof", "--parent-pid", str(parent.pid)])
    try:
        time.sleep(0.5)
        assert p.poll() is None
        parent.kill()
        parent.wait()
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()


def test_serve_exits_on_stdin_eof(tmp_path):
    p, _ = _start_serve(tmp_path, ["--exit-on-stdin-eof"])
    try:
        p.stdin.close()
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()
