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


# 按字符类别估 token 的系数（每字符约合多少 token）。用真实 DeepSeek API 标定得出，
# 见 research/2026-11-03-cache-hit-rate.md §2.7。原来一刀切「ASCII 4 字符 1 token」
# 在 JSON / 路径 / ls / diff 这类工具输出上会低估 1.7–2.1 倍——而工具输出正是长对话里
# 占比最大的内容，所以整体系统性低估约 10–16%。
#
# 为什么按类别而不是按内容类型猜：同一个文件里代码、路径、JSON 混在一起，只有逐字符
# 分类才能既不过拟合又不失真。标定集覆盖中英散文、代码、JSON、路径、diff、表格、日志，
# 训练集与测试集分开，平均相对误差从 0.340 降到 0.193。
# 系数用「纯样本」单独标定，每类只含有该类字符：
#   纯中文 1000 字 -> 562 token（1.78 字符/token）
#   纯字母 1000 字 -> 300 token（3.33）
#   纯空格 1000 个 -> 9 token（111，空白几乎不单独成 token，只跟着词）
#   纯标点  870 字 -> 600 token（1.45）
#   纯数字 1000 字 -> 334 token（2.99）
# **不能拿混合语料一起做最小二乘**：各类字符在不同文本里共线，拟合会把空格系数拉到
# 0.245（实际 0.009）、中文拉到 0.786（实际 0.562）——看似能拟合总量，单类却错得离谱。
_TK_WORD = 1 / 3.33    # 英文等字母
_TK_SPACE = 1 / 111.0  # 空白（几乎不占 token）
_TK_PUNCT = 1 / 1.45   # 标点符号：JSON、路径全是符号，token 消耗大
_TK_DIGIT = 1 / 2.99   # 数字
_TK_CJK = 1 / 1.78     # 中日韩


def estimate_tokens(text: str) -> int:
    """估 token：按字符类别加权（系数见上，真实 API 标定）。

    比「ASCII/4 + 其余/1」准得多的地方：JSON、文件路径、ls 输出、git diff、表格——
    这些都是工具输出里最常见的内容，它们的标点密度极高，旧公式会低估近一倍。
    """
    if not text:
        return 0
    word = space = punct = digit = cjk = 0
    for ch in text:
        o = ord(ch)
        if o >= 0x2E80:                 # CJK 统一表意文字及更宽的字符集
            cjk += 1
        elif ch.isdigit():
            digit += 1
        elif ch.isspace():
            space += 1
        elif ch.isalpha():
            word += 1
        else:
            punct += 1                  # 含 ASCII 标点与非 ASCII 标点/符号
    return int(word * _TK_WORD + space * _TK_SPACE + punct * _TK_PUNCT
               + digit * _TK_DIGIT + cjk * _TK_CJK) + 1


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


def compact_update_prompt() -> str:
    """增量压缩指令（已有上次摘要时用）。"""
    return load_prompt("compact_update.md")


def last_summary(history: list[dict]) -> tuple[int, str] | None:
    """最近一条压缩摘要消息 → (下标, 摘要正文)；没有返回 None。"""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("_kind") == "summary":
            c = m.get("content") or ""
            a, b = c.find("<summary>\n"), c.rfind("\n</summary>")
            text = c[a + len("<summary>\n"):b] if a >= 0 and b > a else c
            return i, text.strip()
    return None


def after_summary(history: list[dict], idx: int) -> int:
    """上次压缩「没被概括」的部分从哪开始。

    压缩后的历史是 [AGENTS, 用户原话, 摘要, 保留段]，保留段是**原文**、还没进摘要。
    下一次压缩要把「保留段 + 之后的新内容」一起概括进去，然后把保留段换成新的。
    所以边界 = 上次保留段的末尾（摘要消息的 `_retained_to`）；没有这个标记时退回摘要下一条。
    """
    m = history[idx] if 0 <= idx < len(history) else {}
    end = m.get("_retained_to")
    if not isinstance(end, int) or end < idx + 1:
        end = idx + 1
    return min(end, len(history))


def has_new_since(history: list[dict], idx: int) -> bool:
    """上次压缩之后有没有真正的新内容（不算系统备注、也不算当时刻意保留的原文）。

    不能简单地「摘要之后有东西就算新内容」：压缩后的历史是
    [AGENTS, 用户原话, 摘要, 保留段]，保留段本来就排在摘要后面。按「摘要之后」判断
    会把刚压缩完的会话立刻判成「有新内容」，于是每次都白压一遍（每次压缩都作废整段缓存）。
    压缩时把当时的长度记在摘要消息的 `_retained_to` 上，只数它之后的。
    """
    return any(x.get("_kind") not in META_KINDS for x in history[after_summary(history, idx):])


def incremental_side_request(previous: str, new_msgs: list[dict], max_tokens: int) -> str:
    """侧路增量压缩的 user 内容：旧摘要 + 之后的新对话记录。"""
    return ("<previous-summary>\n" + previous.strip() + "\n</previous-summary>\n\n"
            "以下是上次摘要之后的新对话记录：\n\n" + render_transcript(new_msgs, max_tokens))


RETENTION_TOKENS = 64_000      # 保留的最近往来总预算（参考 Codex 的 RETAINED_MESSAGE_TOKEN_BUDGET）
RETENTION_COUNT = 20           # 同时最多保留几条


def retained_suffix(old: list[dict], max_tokens: int = RETENTION_TOKENS,
                    max_count: int = RETENTION_COUNT, floor: int = 0) -> tuple[int, list[dict]]:
    """从历史尾部截出一段**连续**的最近往来 -> (起始下标, 消息)。

    为什么必须保留：压缩把历史换成「几条用户原话 + 摘要」后，模型手里就再也没有刚读过的文件、
    刚跑过的命令输出。它只能重新 read/shell——每一次重读都是几千 token 的全新内容（必然未命中），
    于是压缩之后的几十步每步都在为自己刚刚看过的东西重新付钱。实测压缩后回合的每次请求
    未命中从 ~500 涨到 2,500–6,600，其中大部分是这部分。

    为什么是**连续**后缀而不是「挑出所有工具消息」：逐条挑会打乱时间顺序——较早的 assistant
    会排到较新的 user 后面，模型读到的是「先看到新问题、再看到旧回答」。连续后缀天然保持
    原始顺序，且和 Codex「从头裁、保留尾部」（compact.rs:362-371）的做法一致。

    预算二者先到：总 token 达 max_tokens 或消息数达 max_count 就停，避免压缩后 prompt 又顶回水位。
    floor = 保留段的下界（增量压缩时传上次摘要的下标）：摘要之前的内容已经被概括过，
    再把原文带回来等于没压缩。截断点落在工具往来中间时要去掉孤儿（缺 assistant 的 tool、
    缺 tool 的 tool_calls），否则请求 400。
    """
    if max_tokens <= 0 or max_count <= 0:
        return len(old), []
    used = 0
    count = 0
    start = len(old)
    for i in range(len(old) - 1, max(-1, floor - 1), -1):
        m = old[i]
        if m.get("_kind") in META_KINDS:
            # 系统备注 / 旧摘要 / AGENTS：不是真实往来，不占预算。注意**不能**把 start 往前推，
            # 否则 AGENTS 消息会被收进保留段，而 build_history 已经单独保留过它 → 重复一份。
            continue
        cost = estimate_messages([m])
        if count >= max_count or used + cost > max_tokens:
            break
        used += cost
        count += 1
        start = i
    seg = [dict(m) for m in old[start:] if m.get("_kind") not in META_KINDS]
    # 开头的孤儿：assistant 被截掉了，只剩它的 tool 结果
    while seg and seg[0].get("role") == "tool":
        seg.pop(0)
    # assistant 的 tool_calls 里，有些 tool 结果在截断处被切掉了：这些调用必须删掉，否则请求 400
    answered = {m.get("tool_call_id") for m in seg if m.get("role") == "tool"}
    cleaned: list[dict] = []
    for m in seg:
        if m.get("role") == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                keepc = [tc for tc in calls if tc.get("id") in answered]
                if len(keepc) != len(calls):
                    if keepc:
                        m = {**m, "tool_calls": keepc}
                    elif m.get("content") or m.get("reasoning"):
                        m = {k: v for k, v in m.items() if k != "tool_calls"}
                    else:
                        continue
        cleaned.append(m)
    return start, cleaned


def build_history(old: list[dict], summary: str, appendix: str, max_user: int, user_tokens: int,
                  keep_tools: int = 0, keep_tokens: int = RETENTION_TOKENS, floor: int = 0) -> list[dict]:
    """压缩后的新历史：agents + 较早的用户原话 + 摘要（带环境附录） + 最近这段原始往来。

    顺序 = 时间顺序，这是硬要求：摘要概括的是「最近往来之前」的部分，所以摘要必须排在
    保留段之前；保留段是历史真实的尾部，排在最后（Codex compact.rs 也是「摘要在前、
    保留消息在后」）。反过来写会让模型先读到最新内容、再读到对更早内容的概括。

    keep_tools = 保留段的消息条数上限（0 = 不保留，退回只留用户原话 + 摘要的旧行为）；
    keep_tokens = 保留段的 token 预算（调用方按模型窗口和压缩前大小下调）。
    floor = 保留段下界（增量压缩时是上次摘要的下标），保证不会把已概括的内容再捞回来。
    用户原话只从保留段**之前**的历史里挑：保留段里已经含有最近的用户消息原文，
    再从整段里挑会把更早的原话插到更晚的对话之后，时间就错了。
    """
    start, seg = ((len(old), []) if keep_tools <= 0
                  else retained_suffix(old, max_tokens=keep_tokens, max_count=keep_tools, floor=floor))
    new: list[dict] = [dict(m) for m in old if m.get("_kind") == "agents"]
    for text in recent_user_messages(old[:start], max_user, user_tokens):
        new.append({"role": "user", "content": text})
    body = summary_prefix() + "\n\n<summary>\n" + summary.strip() + "\n</summary>"
    if appendix:
        body += "\n\n" + appendix
    new.append({"role": "user", "content": body, "_kind": "summary"})
    new.extend({k: v for k, v in m.items() if k != "_pruned"} for m in seg)
    return new
