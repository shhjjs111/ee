---
id: 08-homework-primes
title: 作业：埃氏筛
kind: homework
check: python -m pytest -q ee/tests/_eval_check.py
check_cwd: .
---
我在做作业：在仓库根目录新建 `homework/primes.py`，实现 `sieve(n: int) -> list[int]`，用埃拉托斯特尼筛法返回所有 ≤ n 的素数（n < 2 时返回空列表），
直接运行这个文件时打印 100 以内的素数。再写 `homework/README.md`，用中文讲清思路、为什么只需要筛到 √n、时间复杂度。跑一下确认结果对。

## 验收测试
```python
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("primes", ROOT / "homework" / "primes.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_sieve():
    m = _load()
    assert m.sieve(1) == [] and m.sieve(2) == [2]
    assert m.sieve(30) == [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
    assert len(m.sieve(10000)) == 1229


def test_main_and_readme():
    out = subprocess.run([sys.executable, str(ROOT / "homework" / "primes.py")], capture_output=True, text=True).stdout
    assert "97" in out and "2" in out
    t = (ROOT / "homework" / "README.md").read_text(encoding="utf-8")
    assert "√" in t or "sqrt" in t.lower() or "平方根" in t
