"""「压缩会不会变傻」的可测指标（roadmap §7 的三个指标，做成能跑、能断言的形式）。

**为什么需要这一层**：压缩的代价是双重的——既丢信息（摘要覆盖不足），又丢工具输出
（模型被迫重读）。之前只有一次性手工实验（压缩 2 次后答对 24/24），验不了回归：
谁把 `retained_suffix` 改坏、把 `keep_tools` 调小，测试不会响。

三个指标都能从**已经在磁盘上的数据**算出来，不需要额外发请求：
1. `identifier_coverage`：压缩前历史里的函数名/路径/命令/报错，多少出现在摘要里。
   低 = 摘要把细节吃了，模型只能重读去补。
2. `reread_after_compaction`：压缩之后 N 轮内重复读同一文件的次数。**核心指标**，
   直接量化重读损失——它是唯一能把「保留段太小」变成数字的东西。
3. `step_overhead`：配对任务的步数差。压缩前后同一任务差多少步。

不读磁盘、不发请求的纯函数，输入就是 `SessionStore` 的两样东西：消息列表 + 逐请求账本。
"""
from __future__ import annotations

import json
import re

# 从代码/命令/报错里抽「值得被摘要记住」的标识符。
# 只收三类高信号：带下划线/驼峰的函数名、文件路径、shell 命令的第一个词。
_IDENT_PATTERNS = (
    # 函数名 / 变量名：必须带下划线或 camelCase，纯小写单词（read/write/path）噪声太大
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b"),
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),
    # 文件路径：至少一级目录 + 带扩展名，或常见的点号文件名
    re.compile(r"\b[\w.-]+[/\\][\w./\\-]*\.[A-Za-z0-9]{1,8}\b"),
    re.compile(r"\b\w+\.(?:py|ts|tsx|js|json|toml|yaml|yml|md|txt|rs|go|java|sh)\b"),
    # 报错/异常名
    re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception|Warning)\b"),
)

# 路径里这些太泛，出现在任何摘要里都不算「记住了这个文件的细节」
_STOP = frozenset("""
ok error true false none null self cls init main app index test tests src lib core
utf json yaml md txt py ts js file path data args kwargs value values item items
""".split())


def identifiers(text: str, limit: int = 400) -> list[str]:
    """从一段文本里抽出「具体到能被重读」的标识符（去重、保序）。"""
    seen: dict[str, None] = {}
    for pat in _IDENT_PATTERNS:
        for m in pat.finditer(text or ""):
            tok = m.group(0).strip()
            if len(tok) < 4 or len(tok) > 80:
                continue
            if tok.lower() in _STOP:
                continue
            seen.setdefault(tok, None)
            if len(seen) >= limit:
                return list(seen)
    return list(seen)


def ident_digest(before: str, summary: str, limit: int = 400) -> dict:
    """压缩那一刻算出覆盖率并只留数字：压缩前的原文不落盘（太大且含私密内容）。

    为什么必须在压缩时算：压缩会把原文整段替换掉，事后只剩摘要，离线再也算不出
    「当时丢了多少」。留 `[total, kept]` 两个整数就够复盘，也不占空间。
    """
    c = identifier_coverage(before, summary, limit)
    return {"total": c["total"], "kept": c["kept"]}


def read_digest(messages: list[dict], turns: int = 3, limit: int = 60) -> list[list]:
    """压缩前最后几轮读过哪些区间：`[[路径, offset, limit], ...]`。

    **必须在这一刻记**：压缩会把原文整段换掉，之后历史里再也找不到「压缩前读过什么」。
    没有这份清单，`reread_after_compaction` 在真实会话上永远返回 0——不是健康，
    是它根本无从比较（这一点是拿 5 个真实会话验证时发现的）。
    """
    regs = _reads(_last_turns(messages, turns))[-limit:]
    return [[r[0], r[1], r[2]] for r in regs]


def _last_turns(messages: list[dict], turns: int) -> list[dict]:
    """从尾部往前取最后 `turns` 轮（每轮以真实用户消息开头）。"""
    starts = [i for i, m in enumerate(messages)
              if m.get("role") == "user" and not m.get("_kind")]
    if not starts:
        return messages
    return messages[starts[max(0, len(starts) - turns)]:]


def identifier_coverage(before: str, summary: str, limit: int = 400) -> dict:
    """压缩前的标识符有多少落进了摘要。

    返回 `{"total": n, "kept": k, "rate": k/n, "missed": [...]}`。
    `missed` 给前若干个，用来判断「丢的是关键路径还是无关噪声」。
    """
    want = identifiers(before, limit)
    if not want:
        return {"total": 0, "kept": 0, "rate": None, "missed": []}
    have = summary or ""
    kept = [x for x in want if x in have]
    missed = [x for x in want if x not in have]
    return {"total": len(want), "kept": len(kept),
            "rate": len(kept) / len(want), "missed": missed[:20]}


def _reads(msgs: list[dict]) -> list[tuple]:
    """按顺序列出这段历史里「读了哪块内容」：`(路径, offset, limit)`。

    必须带区间而不是只有路径：编辑一个文件时会分很多段读它（`offset=160 limit=50`、
    `offset=204 limit=12`…），那是正常推进，不是「把已经看过的东西再看一遍」。
    只按路径去重会把这些全算成重读——实测真实会话里 24 次「重读」全是这种情况。
    """
    out = []
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}).get("name") or ""
            if "read" not in fn.lower():
                continue
            arg = (tc.get("function") or {}).get("arguments") or ""
            try:
                a = json.loads(arg) if arg.strip().startswith("{") else {}
            except ValueError:
                a = {}
            path = a.get("path") or a.get("file_path") or a.get("filename") or ""
            if not path:
                mt = re.search(r'"(?:path|file_path|filename)"\s*:\s*"([^"]+)"', arg)
                path = mt.group(1) if mt else ""
            if not path:
                continue
            out.append((str(path), a.get("offset"), a.get("limit")))
    return out


def _turns_after(messages: list[dict], start: int, retained_to: int | None = None
                 ) -> list[tuple[int, list[dict]]]:
    """从 `start` 往后切成「轮」：每遇到一条真实用户消息就是新一轮。

    `retained_to` 是摘要消息上的 `_retained_to`（压缩后历史长度）：那之前的内容是
    压缩**当时原样保留**的旧往来，不是压缩后的新动作，要从扫描里排除——
    否则保留段里的 read 会被误算成「压缩后又读了一遍」。
    """
    out: list[tuple[int, list[dict]]] = []
    cur: list[dict] = []
    seen_summary = False
    for i, m in enumerate(messages[start:], start):
        if not seen_summary:
            if m.get("_kind") == "summary":
                seen_summary = True
            continue
        if retained_to is not None and i < retained_to:
            continue                   # 保留段：压缩当时就在，不是压缩后的新读取
        if m.get("role") == "user" and not m.get("_kind"):
            if cur:
                out.append((len(out), cur))
            cur = [m]
        else:
            cur.append(m)
    if cur:
        out.append((len(out), cur))
    return out


def reread_after_compaction(messages: list[dict], window: int = 3,
                            before_reads: list[list] | None = None) -> dict:
    """压缩之后 `window` 轮内，重读了「压缩前看过、却被压掉」的区间几次。

    口径（比「同一文件读了两次」严格得多，也必须严格）：
    - 区间三元组 `(路径, offset, limit)` 完全相同才算同一块内容。编辑一个文件时按
      `offset` 分段读是正常推进（真实会话实测 24 次「重读」全是这种情况），不是损失；
    - 只算**压缩前读过的**区间——压缩后第一次读的东西本来就要读；
    - 保留段里那些「压缩当时就在」的原始往来不算新读取（`_retained_to` 之前）；
    - 同一轮里重复调用只算一次。

    `rereads=0` 是健康值：说明保留段把模型刚看过的内容原样留着了。
    """
    total = 0
    detail = []
    starts = [i for i, m in enumerate(messages) if m.get("_kind") == "summary"]
    for s in starts:
        # 优先用压缩时落盘的清单：压缩后的历史里已经没有「压缩前读过什么」了，
        # 从 messages[:s] 现算只能在「压缩还没真发生时」骗出 0。
        if before_reads is not None:
            before = {tuple(x) for x in before_reads}
        else:
            before = set(_reads(messages[:s]))
        if not before:
            continue
        bad: dict[tuple, int] = {}
        for turn, msgs in _turns_after(messages, s, messages[s].get("_retained_to")):
            if turn >= window:
                break
            for reg in set(_reads(msgs)):
                if reg in before:
                    bad[reg] = bad.get(reg, 0) + 1
        if bad:
            total += sum(bad.values())
            detail.append({"at": s, "files": sorted({k[0] for k in bad})[:5],
                           "extra": sum(bad.values())})
    return {"compactions": len(starts), "rereads": total, "detail": detail}


def step_overhead(pairs: list[tuple[int, int]]) -> dict:
    """配对任务步数差：`[(压缩前步数, 压缩后步数), ...]`。

    目标「压缩后继续不该多走一步」（差 ≤ +1）。返回中位数与超阈值对数，
    免得一条极端样本掩盖整体。
    """
    if not pairs:
        return {"pairs": 0, "median": None, "over": 0}
    diffs = sorted(b - a for a, b in pairs)
    med = diffs[len(diffs) // 2] if len(diffs) % 2 else (
        diffs[len(diffs) // 2 - 1] + diffs[len(diffs) // 2]) / 2
    return {"pairs": len(pairs), "median": med, "over": sum(1 for d in diffs if d > 1)}


def from_store(records: list[dict]) -> dict:
    """从落盘的 compact 记录里取出历次压缩的摘要覆盖率（`session._compact` 写入）。"""
    items = []
    for rec in records:
        if rec.get("kind") != "compact":
            continue
        cov = rec.get("coverage") or {}
        total, kept = int(cov.get("total") or 0), int(cov.get("kept") or 0)
        items.append({"t": rec.get("t"), "reason": rec.get("reason"),
                      "total": total, "kept": kept,
                      "rate": (kept / total) if total else None})
    return {"count": len(items), "items": items}


def audit(messages: list[dict], usage_rows: list[dict] | None = None,
          records: list[dict] | None = None) -> dict:
    """一次算齐指标（供 CLI 与测试共用）。

    `records` 是 `SessionStore._records()` 的原始行；给了就能读落盘的摘要覆盖率
    （压缩前的原文已经被替换掉，事后算不出来，只能靠压缩当时记下的那两个整数）。
    """
    # 只遍历一次：调用方可能传的是 SessionStore._records() 生成器（一次即尽）。
    records = list(records) if records else []
    digests = [rec.get("reads") for rec in records
               if rec.get("kind") == "compact" and rec.get("reads") is not None]
    out = {"reread": reread_after_compaction(
        messages, before_reads=digests[-1] if digests else None)}
    cov = from_store(records) if records else None
    if cov and cov["count"]:
        rates = [x["rate"] for x in cov["items"] if x["rate"] is not None]
        cov["mean_rate"] = (sum(rates) / len(rates)) if rates else None
        out["identifier_coverage"] = cov
    else:
        out["identifier_coverage"] = None
    if usage_rows is not None:
        from ee.core.cache_report import summarize

        out["cache"] = summarize(usage_rows)["main"]
        out["compactions_recorded"] = sum(1 for m in messages if m.get("_kind") == "summary")
    return out
