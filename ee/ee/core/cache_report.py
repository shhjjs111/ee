"""缓存命中率的离线诊断：把会话 JSONL 里的逐请求 usage 行算成可验收的数字。

**为什么要单独一层**：`UsageStats.totals()` 只在会话进程存活期间可读；`turn_end` 的
累计快照只告诉你「到现在一共花了多少」，回答不了「哪一次请求漏了多少、漏之前刚发生
什么」。能回答这个的只有 `SessionStore.load(path)["usage"]` 里那些逐请求的行——但那是
原始数据，不是结论。这一层只做纯计算：输入一批 record，输出验收需要的量。

不读磁盘、不读配置、不发请求，所以能单测，也能被 CLI 与 RPC 复用。

**口径**（与 `UsageStats` 保持一致，不要另立一套）：
- 命中率分母只含「服务端真的报了 hit」的请求，覆盖率给出这部分占总输入的比例；
- 主对话口径只看 `role == "main"`，侧路/子代理/压缩各自分组；
- 断点（`break`）是"这次未命中远超新增"，压缩计数单独给，用来对照
  「断点数 ≤ 压缩次数」这条不变量；
- `cold` 是首次请求 / 重启恢复后的首发（没有上一次 wire 基线）。它的未命中
  不能说明前缀稳不稳，因此不进 `normal_gap`，也 (在 UsageStats 里) 不进
  压缩水位的命中率样本；`main.hit_rate` 的总口径仍然包含它。
- `cold_ttl`：空闲超过 provider 声明的缓存 TTL（OpenAI 600s、Anthropic 300s…）。
  服务端过期不是前缀不稳，同样不进 `normal_gap` 与水位样本。
- `uncacheable`：prompt 低于服务商最短可缓存长度（OpenAI 1024、Anthropic 4096…），
  整个 prompt 都进不了缓存，单独计数。
- 尾块（`main.tail`）：服务端按块匹配，末尾不满一块的部分必然 miss；
  `adjusted_hit_rate` 扣掉它之后才算「前缀稳不稳」。
- 客户端前缀稳定率（`main.wire`）：靠逐条 wire 指纹算，不依赖服务商上报 cache；
  服务商不报 hit 时，这是唯一还能看的指标。
- 空闲间隔（`main.idle`）：服务端缓存过期不是缺陷，先分桶测量再谈预热。
"""
from __future__ import annotations

from ee.core.usage import UsageRollup, unbaselined_miss

# 按输入规模分桶：命中率与 prompt 大小强相关（小 prompt 抖动大、大 prompt 摊薄快）
_BUCKETS = ((0, 40_000, "<40k"), (40_000, 80_000, "40–80k"),
            (80_000, 150_000, "80–150k"), (150_000, None, ">150k"))


def _num(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rate(hit: int, denom: int) -> float | None:
    return (hit / denom) if denom > 0 else None


def _is_cold(r: dict) -> bool:
    """是不是**明确记着**「没有 wire 基线」的首发（新记录才有这个标记）。

    老账本没有 `cold` 字段，不能靠猜：猜对了没收益，猜错了会让冷启动次数虚高、
    把一次整段未命中隐藏在「重启」背后。分不清的那一类走 `unbaselined_miss()`，
    两边都不进、单独给一类。
    """
    return bool(r.get("cold"))


def summarize(rows: list[dict]) -> dict:
    """把逐请求 usage 行汇总成诊断结论（纯函数）。"""
    rows = [r for r in rows if isinstance(r, dict)]
    roll = UsageRollup(rows)
    by_role = roll.roles()
    main = [r for r in rows if (r.get("role") or "main") == "main"]
    known = [r for r in main if r.get("hit") is not None]

    main_prompt = sum(_num(r.get("prompt")) for r in main)
    known_prompt = sum(_num(r.get("prompt")) for r in known)
    main_hit = sum(_num(r.get("hit")) for r in known)
    missing = [r for r in main if r.get("usage_missing")]

    breaks = [{"turn": r.get("turn"), "reason": r.get("break"),
               "prompt": _num(r.get("prompt")), "gap": _num(r.get("gap")),
               "new": _num(r.get("new"))}
              for r in main if r.get("break")]
    by_break: dict[str, int] = {}
    for b in breaks:
        key = str(b["reason"])
        by_break[key] = by_break.get(key, 0) + 1
    cold = [r for r in main if _is_cold(r)]
    # 服务端缓存过期：空闲超过 provider 声明的 TTL。和冷启动一样是「已知的整段未命中」，
    # 但原因不同——不算成前缀不稳，也不进压缩水位样本。
    cold_ttl = [r for r in main if r.get("cold_ttl")]
    legacy = [r for r in main if unbaselined_miss(r)]
    causes = [r for r in main if r.get("cause")]
    by_cause: dict[str, int] = {}
    for r in causes:
        key = str(r.get("cause"))
        by_cause[key] = by_cause.get(key, 0) + 1
    gaps = sorted(_num(r.get("gap")) for r in main
                  if "gap" in r and not r.get("break") and not _is_cold(r)
                  and not r.get("cold_ttl")
                  and not unbaselined_miss(r))

    return {
        "requests": len(rows),
        "usage_missing": sum(1 for r in rows if r.get("usage_missing")),
        "by_role": by_role,
        "main": {
            "requests": len(main),
            "hit": main_hit,
            "prompt": main_prompt,
            # 分母只含报过 hit 的请求；coverage 说明这部分占总输入多少，
            # 两者一起看才知道「99%」是真命中还是样本太少。
            "hit_rate": _rate(main_hit, known_prompt),
            "hit_coverage": _rate(known_prompt, main_prompt),
            # 请求口径的覆盖率。token 口径看不见「缺用量」的请求（它们 prompt=0，进不了任何
            # 分母），所以单独给一个「报过 hit 的请求 / 全部主对话请求」——只有两个都是 1.0
            # 才是真的全量样本。
            "hit_coverage_requests": _rate(len(known), len(main)),
            "usage_missing": len(missing),
            # 尾块：服务端按块匹配（DeepSeek 64 token），末尾不满一块的部分必然 miss。
            # 把它扣掉后的命中率才是「前缀稳不稳」的可比口径。
            **_tail(known),
            # 客户端前缀稳定率：靠逐条 wire 指纹算，不依赖服务商上报 cache 字段。
            # 服务商不报 hit 的会话里，只有这个数还能说明「前缀有没有被我们自己改」。
            **_wire(main),
            # 空闲间隔分桶：服务端缓存会过期，隔太久的整段未命中不该算成前缀不稳。
            "idle": _idle(main),
        },
        "breaks": {"count": len(breaks), "by_reason": by_break, "items": breaks},
        # 冷启动/无基线：不计入 normal_gap 与压缩水位，但单独给出来，
        # 否则用户会以为「这些未命中凭空消失了」。
        "cold": {"count": len(cold), "gap": sum(_num(r.get("gap")) for r in cold)},
        # 空闲超过服务端 TTL 的整段未命中：不是前缀被改坏。DeepSeek 不声明 TTL，
        # 所以这个分类主要出现在 OpenAI / Anthropic / Gemini / 通义上。
        "cold_ttl": {"count": len(cold_ttl), "gap": sum(_num(r.get("gap")) for r in cold_ttl)},
        # 老账本（baseline 字段上线前）里分不清「冷启动」与「整段未命中」的行。
        # 不算成冷启动（那会让次数虚高），也不进正常回合（那是把重算当前缀不稳）。
        "legacy": {"count": len(legacy), "gap": sum(_num(r.get("gap")) for r in legacy)},
        # 已知原因（compact / prune / mcp_attach / model_switch ...）不一定都会触发 break：
        # gap 没超过阈值时只记 cause。单独汇总，用户才看得到「换了模型但仍命中」这类事件。
        "causes": {"count": len(causes), "by_reason": by_cause},
        # 压缩次数 = 压缩摘要请求的条数，用来对照「断点数不应多于压缩次数」。
        # 侧路/子代理的断点不计：它们本来就不共享主对话前缀。
        "compactions": sum(1 for r in rows if r.get("role") == "compact"),
        "normal_gap": {
            "count": len(gaps),
            "median": _pct(gaps, 0.5),
            "p90": _pct(gaps, 0.9),
            "max": gaps[-1] if gaps else None,
        },
        "buckets": _buckets(main),
    }


def _pct(sorted_vals: list[int], q: float) -> int | None:
    """有序列表的分位数（最近秩，够用且不会被线性插值编出不存在的小数）。"""
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def _tail(known: list[dict]) -> dict:
    """尾块占比与扣掉尾块后的命中率。

    为什么单算：`hit / prompt` 在小请求上永远到不了 99%——末尾不满一块的部分按定义就是
    miss。实测 <40k 桶只有 95%，把这部分扣掉后才是「前缀是否稳定」的证据；不扣就会把结构性
    损耗当成「缓存策略不好」，进而错误地调压缩水位。只统计有 `tail` 字段的新记录。
    """
    rows = [r for r in known if isinstance(r.get("tail"), int) and r["tail"] >= 0
            and _num(r.get("prompt")) > _num(r.get("tail"))]
    below = [r for r in known if r.get("uncacheable")]
    if not rows:
        return {"tail": None, "uncacheable": {"requests": len(below),
                                               "prompt": sum(_num(r.get("prompt")) for r in below)}}
    prompt = sum(_num(r.get("prompt")) for r in rows)
    tail = sum(_num(r.get("tail")) for r in rows)
    # 命中不可能超过「可缓存部分」：超过说明服务商上报自相矛盾（或把块大小记错了）。
    # 夹到分母而不是放任 >100%，否则一个脏数据点会把整段口径带歪。
    hit = sum(min(_num(r.get("hit")), _num(r.get("prompt")) - _num(r.get("tail"))) for r in rows)
    denom = prompt - tail
    return {"tail": {"requests": len(rows), "tokens": tail, "share": _rate(tail, prompt),
                      "adjusted_hit_rate": _rate(hit, denom)},
            # 低于最短可缓存长度的请求：整个 prompt 都进不了缓存。单独计数，
            # 不能算成「前缀不稳」，也不能当尾块（会把尾块占比说大）。
            "uncacheable": {"requests": len(below),
                            "prompt": sum(_num(r.get("prompt")) for r in below)}}


def _wire(main: list[dict]) -> dict:
    """客户端前缀稳定率：相邻请求的 wire 指纹是否只是追加。

    与命中率解耦：服务商不报 cache_read 时命中率是 None，但指纹仍然是本地事实。
    `rewritten` 说明有东西改写了已发历史（压缩/修剪以外的就是 bug）；
    `generation_changed` 说明 system/tools/选项/端点换了，等于换了一条缓存线。
    """
    rows = [r for r in main if r.get("wire_cmp") and r["wire_cmp"] != "baseline_missing"]
    if not rows:
        return {"wire": None}
    by_cmp: dict[str, int] = {}
    for r in rows:
        key = str(r.get("wire_cmp"))
        by_cmp[key] = by_cmp.get(key, 0) + 1
    stable = sum(n for k, n in by_cmp.items() if k in ("equal", "appended"))
    return {"wire": {"observed": len(rows), "stable": stable, "rate": _rate(stable, len(rows)),
                     "by_cmp": by_cmp}}


_IDLE_BUCKETS = ((0, 60, "<1m"), (60, 300, "1–5m"), (300, 1800, "5–30m"), (1800, None, ">30m"))


def _idle(main: list[dict]) -> list[dict]:
    """按「距上一次主对话请求多久」分桶看命中率：服务端缓存过期是正常现象，不是缺陷。

    不猜 TTL 阈值：阈值因供应商/负载而异，写死一个数字会让报告替你下结论。
    先分桶测量，真的看到「>30m 才掉」，才谈要不要在空闲后预热。
    """
    out = []
    for lo, hi, label in _IDLE_BUCKETS:
        sel = [r for r in main if isinstance(r.get("idle_s"), (int, float))
               and r["idle_s"] >= lo and (hi is None or r["idle_s"] < hi)]
        known = [r for r in sel if r.get("hit") is not None]
        denom = sum(_num(r.get("prompt")) for r in known)
        out.append({"label": label, "requests": len(sel),
                    "hit_rate": _rate(sum(_num(r.get("hit")) for r in known), denom)})
    return out


def _buckets(main: list[dict]) -> list[dict]:
    """按输入规模分桶看命中率：小 prompt 的抖动会被大 prompt 摊平，混在一起看不出问题。"""
    out = []
    for lo, hi, label in _BUCKETS:
        sel = [r for r in main if (hi is None or _num(r.get("prompt")) < hi) and _num(r.get("prompt")) >= lo]
        known = [r for r in sel if r.get("hit") is not None and not _is_cold(r)
                 and not r.get("cold_ttl") and not r.get("uncacheable")
                 and not unbaselined_miss(r)]
        denom = sum(_num(r.get("prompt")) for r in known)
        out.append({"label": label, "requests": len(sel),
                    "hit_rate": _rate(sum(_num(r.get("hit")) for r in known), denom),
                    "prompt": sum(_num(r.get("prompt")) for r in sel),
                    "cold": sum(1 for r in sel if _is_cold(r)),
                    # 过短请求不参与命中率（结构上不可能命中），单独给个数，不藏在分母里。
                    "uncacheable": sum(1 for r in sel if r.get("uncacheable"))})
    return out
