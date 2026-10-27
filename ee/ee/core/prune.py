"""修剪旧工具输出（不调模型）。默认关闭：[prune] enabled = true 才启用。

为什么默认关闭：修剪改写的是历史里较早的消息，从第一条被修剪的消息起，那一次请求后面的整段历史
都要按“未命中缓存”计费。DeepSeek 等前缀缓存很便宜的模型（命中价约为未命中价的 1/50，见
profiles/deepseek.toml 的 price.cache_read）上，省下的那点 token 远抵不上这一整段重新按全价算的钱，
修剪往往反而更贵；上下文真放不下时交给自动压缩（[compact]）。缓存不便宜、或窗口很小的模型可以在配置里打开。

上下文用量达到 [prune].threshold × 窗口（默认 0.6，低于自动压缩的 0.8）时，把较早的大块工具结果
（read / shell / grep 等）一次性换成一行占位：工具名、参数摘要、原长度、“已修剪，需要时重新调用”。

- 保护：最近 keep_turns 个用户回合内的一切；再往前最近 keep_tokens 的工具输出；仍被引用的读文件结果
  （最近回合里提到过这个路径的那个文件的最后一次 read）；不在 tools 名单里的工具（task 结论、skill、
  edit/write 的结果、todo、计划……）；本身很小的输出（< min_output_tokens）。
- 成批：可修剪的总量 < min_tokens 时什么都不做；修剪一次之后，要等拿到新的服务端用量，并且上下文比修剪后
  的实际大小再涨 min_tokens、仍在阈值以上才会再修剪。压缩摘要之前的内容不碰（压缩后历史里也没有旧工具结果）。
- 前缀缓存：修剪会改动历史里较早的消息，从第一条被修剪的消息起这一次请求缓存未命中（之前的部分
  仍命中）；占位文字是确定的，之后每一步又恢复只追加、照常命中。所以只在阈值处成批做一次，不逐步修剪。
- 持久化：会话 JSONL 里原始的 message 记录不动（界面条目本来就单独存了完整输出），另写一条
  {"kind": "prune", "items": [{i, id, content, chars, tokens}]}，恢复会话时按顺序重放。
"""
from __future__ import annotations

import json

from ee.core import compaction as C
from ee.core.events import Notice

DEFAULTS = {
    "enabled": False,          # 默认关闭：会让前缀缓存失效，DeepSeek 上往往更贵（见模块说明）
    "threshold": 0.6,          # 上下文用量 ≥ threshold × 窗口时修剪（必须低于 compact.threshold）
    "keep_turns": 2,           # 最近几个用户回合完全不碰
    "keep_tokens": 40000,      # 再往前最近这么多 token 的工具输出也保留
    "min_tokens": 20000,       # 一批至少能省这么多才修剪（太少不值得让缓存失效一次）
    "min_output_tokens": 300,  # 单个输出小于这个不修剪
    "tools": ["read", "shell", "shell_output", "grep", "glob", "web_fetch", "web_search", "repo_map", "mcp__*"],
}
MARK = "[已修剪的旧工具输出]"


class Pruned(Notice):
    """修剪了一批旧工具输出（是 Notice，界面按普通提示显示）。"""


def settings(data: dict | None) -> dict:
    s = {**DEFAULTS, **((data or {}).get("prune") or {})}
    comp = float(((data or {}).get("compact") or {}).get("threshold", 0.8) or 0.8)
    try:
        th = float(s.get("threshold") or DEFAULTS["threshold"])
    except (TypeError, ValueError):
        th = DEFAULTS["threshold"]
    s["threshold"] = max(0.05, min(th, comp - 0.05))     # 一定在自动压缩之前
    for k in ("keep_turns", "keep_tokens", "min_tokens", "min_output_tokens"):
        try:
            s[k] = max(0, int(s.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            s[k] = DEFAULTS[k]
    tools = s.get("tools")
    s["tools"] = [str(t) for t in tools] if isinstance(tools, (list, tuple)) else list(DEFAULTS["tools"])
    s["enabled"] = bool(s.get("enabled", False))
    return s


def _tool_ok(name: str, pats: list[str]) -> bool:
    for p in pats:
        if p.endswith("*") and name.startswith(p[:-1]):
            return True
        if name == p:
            return True
    return False


def _calls(history: list[dict]) -> dict[str, tuple[str, dict, str]]:
    """tool_call_id → (工具名, 参数 dict, 原始参数串)。"""
    out = {}
    for m in history:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or ""
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {}
            out[tc.get("id")] = (fn.get("name") or "", args if isinstance(args, dict) else {}, str(raw))
    return out


def args_summary(name: str, args: dict, limit: int = 160) -> str:
    keys = ("path", "command", "pattern", "query", "url", "id", "offset", "limit", "pages", "sheet", "focus")
    parts = []
    for k in keys:
        if k in args and args[k] not in (None, "", []):
            v = args[k]
            v = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            parts.append(f"{k}={v}")
    if not parts:
        parts = [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in list(args.items())[:3]]
    s = ", ".join(parts)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def placeholder(name: str, args: dict, chars: int, tokens: int) -> str:
    return (f"{MARK} {name}({args_summary(name, args)})：原输出 {chars:,} 字符（约 {tokens:,} tokens），"
            "为节省上下文已从对话里移除。需要其中内容时请重新调用工具获取（文件可能已经变化）。")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lstrip("./")


def plan(history: list[dict], cfg: dict) -> list[dict]:
    """算出这一批要修剪的消息：[{i, id, name, args, chars, tokens, content}]。不够 min_tokens 返回 []。"""
    real = [i for i, m in enumerate(history) if C.is_real_user(m)]
    if len(real) <= cfg["keep_turns"]:
        return []
    cut = real[-cfg["keep_turns"]] if cfg["keep_turns"] > 0 else len(history)
    floor = 0                                     # 不越过压缩摘要
    for i, m in enumerate(history[:cut]):
        if m.get("_kind") == "summary":
            floor = i + 1
    calls = _calls(history)
    # 最近回合里提到过的路径（助手正文 + 工具参数）：这些文件最后一次 read 的结果仍被引用
    recent = []
    for m in history[cut:]:
        if m.get("role") == "assistant":
            recent.append(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                recent.append(str((tc.get("function") or {}).get("arguments") or ""))
    recent_text = _norm("\n".join(recent))
    last_read: dict[str, int] = {}
    for i, m in enumerate(history):
        if m.get("role") == "tool":
            name, args, _ = calls.get(m.get("tool_call_id"), ("", {}, ""))
            if name == "read" and args.get("path"):
                last_read[_norm(str(args["path"]))] = i
    protected = cfg["keep_tokens"]
    out: list[dict] = []
    for i in range(cut - 1, floor - 1, -1):
        m = history[i]
        if m.get("role") != "tool" or m.get("_pruned"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        t = C.estimate_tokens(content)
        if protected > 0:                         # 最近的一段工具输出保留
            protected -= t
            continue
        name, args, _ = calls.get(m.get("tool_call_id"), ("", {}, ""))
        if not name or not _tool_ok(name, cfg["tools"]) or t < cfg["min_output_tokens"]:
            continue
        if name == "read" and args.get("path"):
            p = _norm(str(args["path"]))
            if last_read.get(p) == i and p and p in recent_text:
                continue
        out.append({"i": i, "id": m.get("tool_call_id"), "name": name, "args": args,
                    "chars": len(content), "tokens": t})
    if sum(x["tokens"] for x in out) < max(1, cfg["min_tokens"]):
        return []
    out.reverse()
    for x in out:
        x["content"] = placeholder(x["name"], x["args"], x["chars"], x["tokens"])
    return out


def apply(history: list[dict], items: list[dict]) -> int:
    """把占位写进历史（换新 dict，不原地改；只认 tool_call_id 对得上的）。返回实际修剪条数。用于重放。"""
    n = 0
    for x in items:
        i = int(x.get("i", -1))
        if not (0 <= i < len(history)):
            continue
        m = history[i]
        if m.get("role") != "tool" or (x.get("id") and m.get("tool_call_id") != x.get("id")):
            continue
        history[i] = {**m, "content": x["content"], "_pruned": {"chars": x.get("chars", 0), "tokens": x.get("tokens", 0)}}
        n += 1
    return n


def due(session, cfg: dict) -> bool:
    """到阈值了吗。修剪过一次之后：先等拿到新的服务端用量，记下修剪后的实际大小，
    之后要比它再涨 min_tokens 才会再修剪（避免占用仍高于阈值时每一步都修剪、每一步都让缓存失效）。"""
    if not cfg["enabled"]:
        return False
    window = session.profile.context_window
    used = session.stats.last_prompt + session.stats.last_completion
    if window <= 0 or used <= 0:
        return False
    mark = getattr(session, "_prune_mark", None)
    if mark is not None and (session.stats.requests < mark or
                             getattr(session, "_prune_comp", 0) != getattr(session, "compactions", 0)):
        mark = session._prune_mark = None      # /new 或压缩之后：历史已重建，重新开始
    if mark is not None:
        if session.stats.requests <= mark:
            return False                       # 修剪后还没有新的用量
        if getattr(session, "_prune_floor", None) is None:
            session._prune_floor = used        # 修剪后第一次真实用量
            return False
        if used < session._prune_floor + max(1, cfg["min_tokens"]):
            return False
    return used >= cfg["threshold"] * window


async def maybe_prune(session, emit) -> int:
    """达到阈值时成批修剪一次。返回省下的 token 估计（没修剪返回 0）。"""
    cfg = settings(session.config.data)
    if not due(session, cfg):
        return 0
    items = plan(session.history, cfg)
    if not items:
        return 0
    session._prune_mark = session.stats.requests
    session._prune_floor = None
    session._prune_comp = getattr(session, "compactions", 0)
    apply(session.history, items)
    session.store.append("prune", {"items": [{k: x[k] for k in ("i", "id", "content", "chars", "tokens")}
                                             for x in items]})
    saved = sum(x["tokens"] - C.estimate_tokens(x["content"]) for x in items)
    saved = max(0, saved)
    if any(x["name"] == "read" for x in items):
        for rec in session.state.files.values():
            rec.reads.clear()         # 被修剪的读结果看不到了：下次 read 给全文，不给“与上次相同”
    st = session.stats
    st.last_prompt = max(0, st.last_prompt - saved)
    st.pruned_tokens = getattr(st, "pruned_tokens", 0) + saved
    st.prunes = getattr(st, "prunes", 0) + 1
    emit(Pruned(f"已修剪 {len(items)} 个较早的工具输出（约省 {saved:,} tokens）。原文仍可在对话里查看，模型需要时会重新读取。",
                "info"))
    return saved
