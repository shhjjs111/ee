"""复现脚本：父进程用管道当 stdin 拉起子进程（与 Electron 拉起 ee-backend 相同），
子进程里一个线程阻塞读 stdin（--exit-on-stdin-eof 的做法），另一个线程跑 git。
观察 git 子进程是否卡住（Windows 同步管道上挂着 ReadFile 时，继承了同一管道的子进程启动时会被卡住）。

用法：python ee/scripts/win_stdin_repro.py
"""
import os
import shutil
import subprocess
import sys
import threading
import time


def child(variant: str) -> None:
    if variant != "no-reader":
        threading.Thread(target=lambda: sys.stdin.buffer.read(1024), daemon=True).start()
        time.sleep(0.5)
    git = shutil.which("git")
    t0 = time.time()
    res = {}

    def run():
        try:
            kw = {"stdin": subprocess.DEVNULL} if variant == "devnull" else {}
            p = subprocess.run([git, "--version"], capture_output=True, timeout=5, **kw)
            res["out"] = f"rc={p.returncode} {p.stdout.decode(errors='replace').strip()}"
        except subprocess.TimeoutExpired:
            res["out"] = "TimeoutExpired（git 5 秒没返回）"
        except Exception as e:  # noqa: BLE001
            res["out"] = repr(e)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(20)
    state = res.get("out") or "卡死：subprocess.run 20 秒还没返回（超时后 communicate() 也在等）"
    print(f"[{variant}] git={git} {time.time() - t0:.1f}s -> {state}", flush=True)
    os._exit(0)


def main() -> int:
    bad = False
    for variant in ("no-reader", "reader", "devnull"):
        p = subprocess.Popen([sys.executable, __file__, "--child", variant], stdin=subprocess.PIPE)
        try:
            p.wait(40)
        except subprocess.TimeoutExpired:
            print(f"[{variant}] 子进程 40 秒没结束", flush=True)
            p.kill()
            bad = True
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        sys.exit(main())
