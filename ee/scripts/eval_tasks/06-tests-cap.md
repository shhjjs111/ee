---
id: 06-tests-cap
title: 给 _cap 补单元测试
kind: tests
check: python -m pytest -q tests/test_cap.py tests/_eval_check.py
---
`ee/ee/tools/truncate.py` 里的 `_cap` 没有单元测试。请新建 `ee/tests/test_cap.py`，至少写 3 个测试：
保留开头（keep="head"）、保留结尾（keep="tail"）、多字节中文在字节边界上截断时不能出现乱码（结果能正常编码、不含 \ufffd）。只加测试，不改实现。

## 验收测试
```python
import ast
from pathlib import Path


def test_cap_tests_exist():
    src = Path(__file__).with_name("test_cap.py").read_text(encoding="utf-8")
    fns = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
    assert len(fns) >= 3 and "_cap" in src


def test_impl_untouched():
    src = (Path(__file__).parents[1] / "ee" / "tools" / "truncate.py").read_text(encoding="utf-8")
    assert 'return b[:n].decode("utf-8", errors="ignore") + " …"' in src
```
