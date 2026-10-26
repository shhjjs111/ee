---
id: 10-bugfix-style-title
title: 修回答风格标题解析
kind: bugfix
check: python -m pytest -q tests/test_style_budget.py tests/_eval_check.py
inject: [{"file": "ee/ee/core/styles.py", "old": "if lines and lines[0].startswith(\"#\"):", "new": "if lines and lines[0].startswith(\"##\"):"}]
revert_passes: true
---
用户在 `~/.ee/styles/` 里放了一个风格文件，首行是 `# 我的风格`，设置页里显示的名字却不是「我的风格」，而且标题那一行被当成了正文。
按 `ee/ee/core/styles.py` 开头的说明，首行 `# 显示名` 是标题。请修好并确认相关测试通过。

## 验收测试
```python
from ee.core.styles import _parse


def test_parse_title():
    assert _parse("# 我的风格\n正文", "x") == ("我的风格", "正文")
    assert _parse("没有标题", "fb") == ("fb", "没有标题")
