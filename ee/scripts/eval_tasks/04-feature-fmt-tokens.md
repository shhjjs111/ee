---
id: 04-feature-fmt-tokens
title: 加 token 数格式化函数
kind: feature
check: python -m pytest -q tests/_eval_check.py
---
在 `ee/ee/core/context.py` 里加一个函数 `fmt_tokens(n: int) -> str`，把 token 数格式化成简短文字：
- 小于 1000 原样输出，如 `999`；
- 小于 100 万用 k，保留一位小数，如 `1234` → `1.2k`，`1000` → `1k`（小数是 0 就去掉）；
- 100 万及以上用 M，如 `3_400_000` → `3.4M`；
- 负数按 0 处理。
再给它写几条单元测试放进 ee/tests 下合适的文件，跑通。

## 验收测试
```python
from ee.core.context import fmt_tokens


def test_fmt_tokens():
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1000) == "1k"
    assert fmt_tokens(1234) == "1.2k"
    assert fmt_tokens(3_400_000) == "3.4M"
    assert fmt_tokens(2_000_000) == "2M"
    assert fmt_tokens(-5) == "0"
