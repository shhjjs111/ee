"""判据入口：前 3 次判据**人为失败**，让单路 + 修复轮都过不了，从而稳定进入分叉。

这是"多路择优"的机器冒烟：不是质量样本（判据被设计成先失败）。第 4 次起才是真实验收。
"""
import subprocess
import sys
from pathlib import Path

state = Path(__file__).with_name(".runs")
n = int(state.read_text(encoding="utf-8")) if state.is_file() else 0
n += 1
state.write_text(str(n), encoding="utf-8")
if n <= 3:
    print(f"criterion: not yet (run {n})")
    raise SystemExit(1)
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "public/test_public.py"]))
