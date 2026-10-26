"""上下文压缩。

1. 让同一个模型基于完整历史写一份交接摘要：请求 = 冻结前缀 + 原历史 + 一条压缩指令（复用前缀缓存）；
   失败时退回侧路请求：把历史渲染成文字（超长则掐头留尾）单独发给模型。
2. 新历史 = AGENTS.md 消息（原样保留） + 最近几条用户原话（有 token 预算） + 摘要消息（带全量环境附录）。
   system/tools 不变，压缩后继续只追加，前缀依然稳定。
"""
from __future__ import annotations

import json

from ee.core.context import load_prompt

CTX_MARK = "\n\n<context-update"
META_KINDS = ("agents", "summary", "note")


def estimate_tokens(text: str) -> int:
    """粗估 token：ASCII 约 4 字符 1 token，其他（中文等）约 1 字符 1 token。"""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return ascii_n // 4 + (len(text) - ascii_n) + 1


def message_text(m: dict) -> str:
    c = m.get("content")
    parts = [c if isinstance(c, str) else ("" if c is None else json.dumps(c, ensure_ascii=False))]
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        parts.append(f"{fn.get('name', '')}({fn.get('arguments', '')})")
    if m.get("reasoning"):
        parts.append(m["reasoning"])
    return "\n".join(p for p in parts if p)


def estimate_messages(msgs: list[dict]) -> int:
    return sum(estimate_tokens(message_text(m)) + 4 for m in msgs)


def strip_context_update(text: str) -> str:
    return (text or "").split(CTX_MARK)[0]


def is_real_user(m: dict) -> bool:
    return m.get("role") == "user" and m.get("_kind") not in META_KINDS


def clip_middle(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    # 按比例估算字符数，掐掉中间
    ratio = max_tokens / max(1, estimate_tokens(text))
    keep = max(200, int(len(text) * ratio))
    head = keep // 3
    tail = keep - head
    return text[:head] + "\n…（中间省略）…\n" + text[-tail:]


def recent_user_messages(history: list[dict], max_count: int, max_tokens: int) -> list[str]:
    """从新到旧挑用户原话，最多 max_count 条、合计不超过 max_tokens；返回时按时间正序。"""
    out: list[str] = []
    budget = max_tokens
    for m in reversed(history):
        if len(out) >= max_count or budget <= 0:
            break
        if not is_real_user(m):
            continue
        text = strip_context_update(m.get("content") or "").strip()
        if not text:
            continue
        t = estimate_tokens(text)
        if t > budget:
            text = clip_middle(text, budget)
            t = budget
        out.append(text)
        budget -= t
    out.reverse()
    return out


def render_transcript(history: list[dict], max_tokens: int, tool_chars: int = 2000) -> str:
    """侧路压缩用：历史 → 文本记录，超长掐头留尾（保留开头 1/4、结尾 3/4）。"""
    lines = []
    for m in history:
        role = m.get("role")
        if role == "user":
            lines.append("【用户】" + strip_context_update(m.get("content") or ""))
        elif role == "assistant":
            body = m.get("content") or ""
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                body += f"\n→ 调用 {fn.get('name')}({str(fn.get('arguments', ''))[:500]})"
            lines.append("【助手】" + body)
        elif role == "tool":
            c = m.get("content") or ""
            if len(c) > tool_chars:
                c = c[:tool_chars // 2] + "\n…（输出省略）…\n" + c[-tool_chars // 2:]
            lines.append("【工具结果】" + c)
    text = "\n\n".join(lines)
    if estimate_tokens(text) <= max_tokens:
        return text
    ratio = max_tokens / max(1, estimate_tokens(text))
    keep = max(1000, int(len(text) * ratio))
    head = keep // 4
    return text[:head] + "\n\n…（较早的对话省略）…\n\n" + text[-(keep - head):]


def compact_prompt() -> str:
    return load_prompt("compact.md")


def summary_prefix() -> str:
    return load_prompt("compact_prefix.md")


def build_history(old: list[dict], summary: str, appendix: str, max_user: int, user_tokens: int) -> list[dict]:
    new: list[dict] = [dict(m) for m in old if m.get("_kind") == "agents"]
    for text in recent_user_messages(old, max_user, user_tokens):
        new.append({"role": "user", "content": text})
    body = summary_prefix() + "\n\n<summary>\n" + summary.strip() + "\n</summary>"
    if appendix:
        body += "\n\n" + appendix
    new.append({"role": "user", "content": body, "_kind": "summary"})
    return new
