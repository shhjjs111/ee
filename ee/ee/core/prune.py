"""修剪旧工具输出（不调模型），并清掉旧轮次的推理（不改历史，只在发送时裁）。默认开启。

为什么默认开：实测同一个接口、同一时段、缓存同样 99% 命中时，prompt 32k 首包 5.2 秒、
96k 13.9 秒、160k 19.8 秒——延迟几乎只由 prompt 的绝对 token 数决定，窗口还剩多少无所谓。
1M 窗口的模型按百分比算则永远到不了水位（60% = 60 万），长会话会一路慢下去。

代价：修剪会让后面整段历史的前缀缓存失效（从第一条被修剪的消息起这一次请求未命中，
之后又恢复只追加）；所以在水位处成批做一次，并保留最近 keep_tokens 的工具输出作最近上下文。

上下文用量到水位时，把较早的大块工具结果
（read / shell / grep 等）一次性换成一行占位：工具名、参数摘要、原长度、“已修剪，需要时重新调用”。

- 保护：最近 keep_turns 个用户回合内的一切；再往前最近 keep_tokens 的工具输出；仍被引用的读文件结果
  （最近回合里提到过这个路径的那个文件的最后一次 read）；不在 tools 名单里的工具（task 结论、skill、
  edit/write 的结果、todo、计划……）；本身很小的输出（< min_output_tokens）。
- 成批：可修剪的总量 < min_tokens 时什么都不做；修剪一次之后，要等拿到新的服务端用量，并且上下文比修剪后
  的实际大小再涨 min_tokens、仍在阈值以上才会再修剪。压缩摘要之前的内容不碰（压缩后历史里也没有旧工具结果）。
- 前缀缓存：修剪会改动历史里较早的消息，从第一条被修剪的消息起这一次请求缓存未命中（之前的部分
  仍命中）；占位文字是确定的，之后每一步又恢复只追加、照常命中。所以只在阈值处成批做一次，不逐步修剪。
- 绝对上限：threshold × 窗口 对 1M 窗口的模型没有意义（60% = 60 万，对话到不了就先被长上下文拖慢）。
  [prune].max_tokens > 0 时取 min(threshold × 窗口, max_tokens)：上游按 token 数承受延迟，与窗口还剩多少无关。
- 持久化：会话 JSONL 里原始的 message 记录不动（界面条目本来就单独存了完整输出），另写一条
  {"kind": "prune", "items": [{i, id, content, chars, tokens}]}，恢复会话时按顺序重放。
"""
from __future__ import annotations

import json

from ee.core import compaction as C
from ee.core.events import Notice

DEFAULTS = {
    # 默认开启：实测 prompt 越大首包越慢（32k→5.2s / 96k→13.9s / 160k→19.8s，缓存命中 99% 也救不回来），
    # 而 1M 窗口按 percentages 算永远够不着。只在 max_tokens 水位以上才会真动手，短会话零影响。
    "enabled": True,
    "threshold": 0.6,          # 小窗口模型用：上下文用量 ≥ threshold × 窗口时修剪
    "max_tokens": 100000,      # 绝对水位（总 prompt token）：取 min(threshold × 窗口, max_tokens)，0 = 只看百分比
    "keep_turns": 2,           # 最近几个用户回合完全不碰
    "keep_tokens": 16000,      # 再往前最近这么多 token 的工具输出也保留
    "min_tokens": 20000,       # 一批至少能省这么多才修剪（太少不值得让缓存失效一次）
    "min_output_tokens": 300,  # 单个输出小于这个不修剪
    "strip_reasoning": True,   # 旧轮次推理不发给服务端（实测占长会话 prompt 的 26%）
    "keep_reasoning_turns": 2,  # 最近几个带推理的助手轮次保留（再往前清空）
    "tools": ["read", "shell", "shell_output", "grep", "glob", "web_fetch", "web_search", "repo_map", "mcp__*"],
}
MARK = "[已修剪的旧工具输出]"


class Pruned(Notice):
    """修剪了一批旧工具输出（是 Notice，界面按普通提示显示）。"""


def settings(data: dict | None) -> dict:
    s = {**DEFAULTS, **((data or {}).get("prune") or {})}
    # 压缩阈值现在也可能是绝对值（compact.max_tokens）：阈值比较按同一尺度做。
    comp_cfg = (data or {}).get("compact") or {}
    comp = float(comp_cfg.get("threshold", 0.8) or 0.8)
    comp_cap = int(comp_cfg.get("max_tokens", 0) or 0)
    try:
        th = float(s.get("threshold") or DEFAULTS["threshold"])
    except (TypeError, ValueError):
        th = DEFAULTS["threshold"]
    s["threshold"] = max(0.05, min(th, comp - 0.05))     # 一定在自动压缩之前
    for k in ("keep_turns", "keep_tokens", "min_tokens", "min_output_tokens", "max_tokens", "keep_reasoning_turns"):
        try:
            s[k] = max(0, int(s.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            s[k] = DEFAULTS[k]
    tools = s.get("tools")
    s["tools"] = [str(t) for t in tools] if isinstance(tools, (list, tuple)) else list(DEFAULTS["tools"])
    s["enabled"] = bool(s.get("enabled", DEFAULTS["enabled"]))
    # 绝对水位必须比压缩更早：两边都有上限时，修剪线不高于压缩线
    if comp_cap > 0 and s["max_tokens"] > comp_cap:
        s["max_tokens"] = comp_cap
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
    return used >= _limit(cfg, window)


def _limit(cfg: dict, window: int) -> float:
    """触发线：threshold × 窗口；配了 max_tokens 再按绝对值封顶。"""
    limit = cfg["threshold"] * window
    if cfg["max_tokens"] > 0:
        limit = min(limit, cfg["max_tokens"])
    return limit


def reasoning_target(history: list[dict], keep: int) -> int:
    """该把推理游标推到哪：返回最后一个要清空的 assistant 下标，-1 = 不用清。

    只算下标、不改历史：真正的裁剪在 wire_messages 里做，需要回传时把游标调回去就恢复。

    切法必须按**用户回合**，不能按 assistant 条数：DeepSeek 要求同一个用户问题内的
    思考链完整回传（闽一个就 400，见 api-docs.deepseek.com/zh-cn/guides/thinking_mode），
    而下一个用户问题开始时应当删掉之前的思维链（news251201：「需删除之前的思维链」）。
    所以一个回合要么整段保留、要么整段清掉；从回合中间截既不合规也省不了多少。
    """
    real = [i for i, m in enumerate(history) if C.is_real_user(m)]
    if keep < 0 or len(real) <= keep:
        return -1                             # 回合数还不够：全保留
    cut = real[-keep] if keep > 0 else len(history)
    last = -1
    for i in range(cut):                      # 只看 cut 之前的旧回合
        m = history[i]
        if m.get("role") == "assistant" and m.get("reasoning"):
            last = i
    return last


async def maybe_prune(session, emit) -> int:
    """达到水位时做一次：清旧推理（发送时裁）+ 修剪旧工具输出。返回省下的 token 估计。"""
    cfg = settings(session.config.data)
    if not due(session, cfg):
        return 0
    saved = 0
    session._prune_mark = session.stats.requests
    session._prune_floor = None
    session._prune_comp = getattr(session, "compactions", 0)
    # 旧轮次推理：实测占长会话 prompt 的 26%，清掉后首包 25.6s→12.8s。上游接受空串。
    if cfg["strip_reasoning"] and not getattr(session, "_reasoning_strip_disabled", False) \
            and getattr(session.spec.profile, "reasoning_passback", "none") != "none":
        adv = reasoning_target(session.history, cfg["keep_reasoning_turns"])
        if adv > getattr(session, "_reasoning_strip_to", -1):
            old = getattr(session, "_reasoning_strip_to", -1)
            session._reasoning_strip_to = adv
            cut = sum(len(session.history[i].get("reasoning") or "") for i in range(old + 1, adv + 1))
            if cut:
                saved += C.estimate_tokens("x" * cut)
                session.store.append("reasoning_strip", {"to": adv})
                emit(Pruned(f"较早轮次的推理不再回传（约省 {C.estimate_tokens('x' * cut):,} tokens）。"
                            f"会话内容本身没有改动。", "info"))
    items = plan(session.history, cfg)
    if items:
        apply(session.history, items)
        session.store.append("prune", {"items": [{k: x[k] for k in ("i", "id", "content", "chars", "tokens")}
                                                 for x in items]})
        part = sum(x["tokens"] - C.estimate_tokens(x["content"]) for x in items)
        saved += max(0, part)
        if any(x["name"] == "read" for x in items):
            for rec in session.state.files.values():
                rec.reads.clear()     # 被修剪的读结果看不到了：下次 read 给全文，不给“与上次相同”
        emit(Pruned(f"已修剪 {len(items)} 个较早的工具输出（约省 {max(0, part):,} tokens）。"
                    f"原文仍可在对话里查看，模型需要时会重新读取。", "info"))
    if not saved:
        return 0
    st = session.stats
    st.last_prompt = max(0, st.last_prompt - saved)
    st.pruned_tokens = getattr(st, "pruned_tokens", 0) + saved
    st.prunes = getattr(st, "prunes", 0) + 1
    return saved
