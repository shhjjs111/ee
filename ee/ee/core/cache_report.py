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
"""
from __future__ import annotations

from ee.core.usage import UsageRollup

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
    """是不是没有 wire 基线的首发。

    新记录有显式 `cold`；旧账本没有这个字段，只能保守推断：`new==0` 且
    `hit==0` 或 gap 很大，才当冷启动。不能把所有 `new==0` 都算进来——
    wire 估算变小（取消/失败重试）也会得到 new==0，但那是正常回合。
    """
    if r.get("cold"):
        return True
    if "baseline" in r:
        return False
    if r.get("break") or r.get("new") != 0:
        return False
    return _num(r.get("hit"), -1) == 0 or _num(r.get("gap")) >= 5000


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

    breaks = [{"turn": r.get("turn"), "reason": r.get("break"),
               "prompt": _num(r.get("prompt")), "gap": _num(r.get("gap")),
               "new": _num(r.get("new"))}
              for r in main if r.get("break")]
    cold = [r for r in main if _is_cold(r)]
    legacy_cold = [r for r in cold if not r.get("cold")]
    causes = [r for r in main if r.get("cause")]
    by_cause: dict[str, int] = {}
    for r in causes:
        key = str(r.get("cause"))
        by_cause[key] = by_cause.get(key, 0) + 1
    gaps = sorted(_num(r.get("gap")) for r in main
                  if "gap" in r and not r.get("break") and not _is_cold(r))

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
        },
        "breaks": {"count": len(breaks), "items": breaks},
        # 冷启动/无基线：不计入 normal_gap 与压缩水位，但单独给出来，
        # 否则用户会以为「这些未命中凭空消失了」。
        "cold": {"count": len(cold), "gap": sum(_num(r.get("gap")) for r in cold),
                 "legacy": len(legacy_cold)},
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


def _buckets(main: list[dict]) -> list[dict]:
    """按输入规模分桶看命中率：小 prompt 的抖动会被大 prompt 摊平，混在一起看不出问题。"""
    out = []
    for lo, hi, label in _BUCKETS:
        sel = [r for r in main if (hi is None or _num(r.get("prompt")) < hi) and _num(r.get("prompt")) >= lo]
        known = [r for r in sel if r.get("hit") is not None and not _is_cold(r)]
        denom = sum(_num(r.get("prompt")) for r in known)
        out.append({"label": label, "requests": len(sel),
                    "hit_rate": _rate(sum(_num(r.get("hit")) for r in known), denom),
                    "prompt": sum(_num(r.get("prompt")) for r in sel),
                    "cold": sum(1 for r in sel if _is_cold(r))})
    return out
