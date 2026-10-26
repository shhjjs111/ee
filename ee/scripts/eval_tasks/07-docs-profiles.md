---
id: 07-docs-profiles
title: 写精简模式说明文档
kind: docs
check: python -m pytest -q tests/_eval_check.py
---
新建 `ee/docs/profiles.md`，用中文给用户写一页说明：完整模式和精简模式有什么区别（工具、系统提示词、各自适合什么场景），
怎么切换（设置页「工具与记忆 → 模式」、`~/.ee/config.toml` 的 `profile`、`ee exec --profile`），切换从什么时候生效，
以及哪些东西不受影响（权限审批、Hooks）。先读代码确认这些说法属实（ee/ee/tools/__init__.py、ee/ee/config.py、ee/ee/headless.py）。

## 验收测试
```python
from pathlib import Path


def test_doc():
    p = Path(__file__).parents[1] / "docs" / "profiles.md"
    t = p.read_text(encoding="utf-8")
    for kw in ("lean", "profile", "--profile", "read", "edit", "write", "shell", "权限", "新对话"):
        assert kw in t, kw
    assert len(t) > 300
```
