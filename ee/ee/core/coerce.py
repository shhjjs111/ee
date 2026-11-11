"""把不可信的值折成数字，而不是抛异常。

为什么需要这层：`int()` / `float()` 遇到 `"abc"` / `None` / `[]` 会抛
`ValueError` / `TypeError`，而这里要被转换的值常常不是代码自己算出来的：

- 用户手写的配置（`~/.ee/config.toml`、项目级 `.ee/config.toml`）；
- 会话文件里的字段（`seq` / `hist` 可能来自老版本或被手改过）；
- RPC 参数（`tail` / `before_seq` / `min_tests` 由桌面端或脚本传入）；
- 模型或网关返回的用量字段。

这些位置的正确行为是「用默认值继续」，不是让一整个回合、或者打开一个会话
（`thread/get`）直接崩掉。要注意区分另一类：`threads._norm_goal` 这种**校验**
需要把坏值报成用户可见的错误，它们自己包 `try/except` 并抛带说明的 ValueError，
不该改成静默默认。

集中在这里还有一个好处：pi-lens 的 `unchecked-numeric-parse-python` 规则
（找的就是裸 `int()` / `float()`）第一次有统一的、可读的替代写法，而不是在每个
调用点复制一遍 try/except。
"""
from __future__ import annotations

from typing import Any


def as_int(value: Any, default: int = 0) -> int:
    """转成 int；转不了就用 `default`。

    与 `int(x)` 的区别只在坏值：`"3"` 会得到 3（和 `int` 一样），`"3.7"` / `"abc"` /
    `None` / `[]` 得到默认值。`float("inf")` 这类会抛 OverflowError 的情况也归到默认值。
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """转成 float；转不了就用 `default`（用法与 `as_int` 对称）。"""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default
