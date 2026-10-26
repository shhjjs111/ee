---
id: 09-refactor-loopguard-text
title: 拆出防死循环提示文案
kind: multi-file
check: python -m pytest -q tests/test_loopguard.py tests/test_loop.py tests/_eval_check.py
---
把 `ee/ee/core/loopguard.py` 里的三段提示文案常量 `REPEAT_NOTE`、`REPEAT_STOP`、`NO_PROGRESS_NOTE` 挪到新文件 `ee/ee/core/loopguard_text.py`，
`loopguard.py` 改为从那里导入；原来 `from ee.core.loopguard import REPEAT_NOTE` 这类写法要继续能用，文案内容一字不改。改完跑相关测试。

## 验收测试
```python
import ast
from pathlib import Path

from ee.core import loopguard, loopguard_text


def test_moved():
    for k in ("REPEAT_NOTE", "REPEAT_STOP", "NO_PROGRESS_NOTE"):
        assert getattr(loopguard, k) == getattr(loopguard_text, k)
    src = Path(loopguard.__file__).read_text(encoding="utf-8")
    assigned = {t.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name)}
    assert not assigned & {"REPEAT_NOTE", "REPEAT_STOP", "NO_PROGRESS_NOTE"}
    assert "结果不会变" in loopguard_text.REPEAT_NOTE
