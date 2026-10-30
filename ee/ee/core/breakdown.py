"""上下文构成（A12）：当前发给模型的上下文里，各部分各占多少 token。

只读、不改任何状态：不冻结前缀、不动历史，所以随时查看都不影响前缀缓存。
token 是按字符粗估的（与压缩触发用的估算一致）；服务端返回过用量时另给出实际值，界面以实际值为准显示占用。
"""
from __future__ import annotations

import json

from ee.core import compaction as C

IMAGE_TOKENS = 1000          # 每张图片的估算 token（各家按分辨率计费，这里取 2048px 长边的大致量级）

LABELS = {
    "system": "系统提示",
    "tools": "工具定义",
    "agents": "AGENTS.md",
    "memory": "记忆",
    "skills": "技能",
    "repomap": "仓库地图",
    "summary": "压缩摘要",
    "history": "历史消息",
    "tool_output": "工具输出",
    "images": "图片",
}
ALWAYS = ("system", "tools", "agents", "memory", "skills", "history", "tool_output", "images")


def _category(m: dict) -> str:
    kind = m.get("_kind")
    if kind == "agents":
        sub = m.get("_sub")
        return sub if sub in ("memory", "skills", "repomap") else "agents"
    if kind == "summary":
        return "summary"
    if m.get("role") == "tool":
        return "tool_output"
    return "history"


def _msg_tokens(m: dict, wire: dict) -> int:
    c = m.get("content")
    text = c if isinstance(c, str) else ("" if c is None else json.dumps(c, ensure_ascii=False))
    parts = [text]
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        parts.append(f"{fn.get('name', '')}({fn.get('arguments', '')})")
    if wire.get("reasoning_content"):          # 只有按回传规则真的发回去的推理才算
        parts.append(wire["reasoning_content"])
    return C.estimate_tokens("\n".join(p for p in parts if p)) + 4


def compute(system: str, tools: list[dict], history: list[dict], profile, window: int,
            used: int | None = None, actual: bool = False, stats=None) -> dict:
    from ee.core.providers import transforms as T
    counts = {k: 0 for k in LABELS}
    counts["system"] = C.estimate_tokens(system)
    per_tool = []
    for t in tools:
        n = C.estimate_tokens(json.dumps(t, ensure_ascii=False))
        per_tool.append({"name": (t.get("function") or {}).get("name", ""), "tokens": n})
    counts["tools"] = sum(x["tokens"] for x in per_tool)
    wires = T.wire_messages([{k: v for k, v in m.items() if k != "_images"} for m in history], profile)
    images = 0
    pruned_now = 0
    for m, w in zip(history, wires):
        counts[_category(m)] += _msg_tokens(m, w)
        if m.get("_images"):
            n = len(m["_images"])
            images += n
            if getattr(profile, "vision", False):
                counts["images"] += n * IMAGE_TOKENS
        if m.get("_pruned"):
            pruned_now += 1
    total = sum(counts.values())
    items = []
    for k, label in LABELS.items():
        if k not in ALWAYS and counts[k] == 0:
            continue
        items.append({"key": k, "label": label, "tokens": counts[k],
                      "percent": round(100.0 * counts[k] / total, 1) if total else 0.0})
    per_tool.sort(key=lambda x: -x["tokens"])
    savings = {"pruned": int(getattr(stats, "pruned_tokens", 0) or 0),
               "prunes": int(getattr(stats, "prunes", 0) or 0),
               "pruned_outputs": pruned_now,
               "compacted": int(getattr(stats, "compacted_tokens", 0) or 0)}
    return {"items": items, "tools": per_tool, "total": total, "window": int(window or 0),
            "used": int(used if used is not None else total), "actual": bool(actual),
            "images": images, "messages": len(history), "savings": savings, "estimated": True}


def for_session(s) -> dict:
    """会话当前的上下文构成。前缀已冻结就用冻结的那份（真正发出去的），否则用将要冻结的那份（不冻结）。"""
    if s.prefix is not None:
        prefix = s.prefix
    else:                       # 只是预览：不把系统提示词来源缓存进会话（改 SYSTEM.md 后发首条消息仍按新文件）
        had = getattr(s, "_sys_src", None)
        prefix = s.current_prefix()
        if had is None:
            s._sys_src = None
    history = list(s.history)
    actual_used = s.stats.last_prompt + s.stats.last_completion
    d = compute(prefix.system["content"], prefix.tools, history, s.profile, s.profile.context_window,
                used=actual_used or None, actual=actual_used > 0, stats=s.stats)
    d["compactions"] = int(getattr(s, "compactions", 0) or 0)
    d["frozen"] = s.prefix is not None
    # T19：缓存命中要带覆盖率。hit_rate 的分母只含"返回了 hit 字段的请求"，
    # 会漏掉没报 hit 的请求；hit_coverage 摊到全部输入 token，两者都要给，
    # 且未知时是 None（前端据此区分「没有命中」和「不知道」）。
    try:
        totals = s.stats.totals()
    except Exception:  # noqa: BLE001  统计异常不影响查看上下文构成
        totals = {}
    d["cache"] = {
        "hit": int(totals.get("hit", 0) or 0),
        "hit_rate": totals.get("hit_rate"),
        "hit_coverage": totals.get("hit_coverage"),
    }
    cfg = s.config.data.get("compact", {}) or {}
    from ee.core import prune as P
    pcfg = P.settings(s.config.data)
    d["thresholds"] = {"compact": float(cfg.get("threshold", 0.8)) if cfg.get("auto", True) else None,
                       "prune": float(pcfg["threshold"]) if pcfg.get("enabled", True) else None}
    d["model"] = s.spec.alias
    return d
