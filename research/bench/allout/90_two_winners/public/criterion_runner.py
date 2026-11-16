"""判据入口：前 3 次判据**人为失败**，让单路 + 两轮修复都过不了，从而稳定进入分叉。

计数器路径由基准脚本写进命令的第一个参数（项目外的临时文件）：主目录与候选 worktree
共享同一个，不会因为 worktree 里没有这个未跟踪文件而重新从 0 开始。第 4 次起才是真实验收。
"""
import os
import subprocess
import sys
from pathlib import Path

raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EE_BENCH_COUNTER")
state = Path(raw) if raw else Path(__file__).with_name(".runs")
n = int(state.read_text(encoding="utf-8")) if state.is_file() else 0
n += 1
state.write_text(str(n), encoding="utf-8")
if n <= 3:
    print(f"criterion: not yet (run {n})")
    raise SystemExit(1)
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "public/test_public.py"]))
