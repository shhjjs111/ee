"""会话 id 的合法性校验。

会话 id 会拼进 ~/.ee/sessions/<id>.jsonl 和 ~/.ee/sessions/<id>/ 的路径，必须限制成
单段文件名：字母/数字开头，后续只允许字母、数字、点、下划线、短横线，最长 128。
"""
from __future__ import annotations

import re

_THREAD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_thread_id(value: object) -> bool:
    return isinstance(value, str) and _THREAD_ID_RE.fullmatch(value) is not None


def check_thread_id(value: object) -> str:
    if not is_thread_id(value):
        raise ValueError("thread_id 不合法")
    return value  # type: ignore[return-value]
