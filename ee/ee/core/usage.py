"""用量与费用口径 v2（Wave 1 / T03）。

**为什么单独一层**：`UsageStats` 用一个 float 累加所有花费，币种取最后一条记录，
缺 usage 的请求直接跳过不记账。结果有两类误导：

1. 混币被相加：CNY 1 + USD 1 显示成 "2 USD"（币种是最后一条的）；
2. 缺用量仍报完整：一次请求没返回 usage，总额仍显示成确定金额。

这一层只做纯计算，不碰 IO、不读配置、不发请求。`UsageStats` 通过它得出
`totals()` 的 v2 字段，同时保留旧的单值 `cost/currency` 以兼容现有调用方。

**口径约定（不要悄悄改）**：

- 币种不换算。不同币种各自累计，只有"唯一币种且全部已知"才给出单值 `cost`。
- `known_amount` 只累加"有单价且金额有效"的请求；`estimated_amount` 留给以后
  用估算替代缺失单价的场景，当前恒为 0，但字段先占位，避免以后改结构。
- `hit_rate` 的分母是"返回了命中字段的请求的 prompt 合计"；同时给出
  `hit_coverage`（这部分请求占总请求输入 token 的比例），否则命中率会误导。
- 缺 usage 也算一次请求，记入 `usage_missing`，不从账里消失。
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 2
DEFAULT_CURRENCY = "¥"


def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def unbaselined_miss(r: dict) -> bool:
    """老账本里「分不清是冷启动还是整段未命中」的行。

    本函数只用于**排除**，不用来冒充冷启动：
      - 新记录带 `baseline`：冷启动有 `cold`，断点有 `break`，两条路都明确；
      - 老记录两者都没有，只能保守推断：`new==0` 且 `hit==0` / `gap` 很大，
        说明这次请求从头就没接上，跟「前缀稳不稳」无关。
    把它算成冷启动会让冷启动次数虚高；直接算正常回合又会把一次整段重算当成前缀不稳，
    把压缩水位往下推。所以单独一类，两侧都不进。
    """
    if not isinstance(r, dict) or "baseline" in r or r.get("cold") or r.get("break"):
        return False
    if _num(r.get("new"), -1) != 0:
        return False
    return _num(r.get("hit"), -1) == 0 or _num(r.get("gap")) >= 5000


@dataclass
class CurrencyBucket:
    """单币种的累计结果。"""
    known_amount: float = 0.0
    estimated_amount: float = 0.0
    unknown_requests: int = 0      # 该币种下金额未知的请求数
    requests: int = 0

    @property
    def complete(self) -> bool:
        """该币种金额是否完整可信：没有任何未知金额的请求。"""
        return self.unknown_requests == 0

    def as_dict(self) -> dict:
        return {
            "known_amount": f"{self.known_amount:.10f}".rstrip("0").rstrip(".") or "0",
            "estimated_amount": f"{self.estimated_amount:.10f}".rstrip("0").rstrip(".") or "0",
            "unknown_requests": self.unknown_requests,
            "requests": self.requests,
            "complete": self.complete,
        }


@dataclass
class UsageRollup:
    """把一批请求记录汇总成对外口径。records 的元素形状：
    {prompt, completion, hit, reasoning, cost, currency, model}
    cost 为 None 表示"这条请求的金额未知"（没配单价）
    """
    records: list = field(default_factory=list)

    def add(self, record: dict) -> None:
        self.records.append(dict(record))

    # ---------------------------------------------------------------- 汇总
    def buckets(self) -> dict[str, CurrencyBucket]:
        out: dict[str, CurrencyBucket] = {}
        for r in self.records:
            cur = str(r.get("currency") or DEFAULT_CURRENCY)
            b = out.setdefault(cur, CurrencyBucket())
            b.requests += 1
            cost = r.get("cost")
            if cost is None:
                b.unknown_requests += 1
            else:
                try:
                    b.known_amount += float(cost)
                except (TypeError, ValueError):
                    b.unknown_requests += 1
        return out

    def roles(self) -> dict:
        """按请求来源（role）分组汇总。

        为什么要分：侧路请求（标题 / commit / 压缩摘要）每次都是两三条全新消息、命中恒 0，
        挤进同一个分母会把主对话的命中率无端拉低。主对话命中率只看 role=main。
        缺 role 的老记录算 main（它们是这次改动之前唯一存在的来源）。
        """
        out: dict[str, dict] = {}
        for r in self.records:
            name = str(r.get("role") or "main")
            b = out.setdefault(name, {"requests": 0, "prompt": 0, "completion": 0,
                                      "hit": 0, "hit_known_prompt": 0, "hit_requests": 0,
                                      "usage_missing": 0})
            b["requests"] += 1
            b["prompt"] += _num(r.get("prompt"))
            b["completion"] += _num(r.get("completion"))
            if r.get("usage_missing"):
                b["usage_missing"] += 1
            h = r.get("hit")
            if h is not None:
                b["hit"] += _num(h)
                b["hit_known_prompt"] += _num(r.get("prompt"))
                b["hit_requests"] += 1
        for b in out.values():
            b["hit_rate"] = (b["hit"] / b["hit_known_prompt"]) if b["hit_known_prompt"] > 0 else None
            b["hit_coverage"] = (b["hit_known_prompt"] / b["prompt"]) if b["hit_known_prompt"] > 0 else None
            # 请求口径：缺 usage 的行 prompt=0，永远进不了 token 分母（它不可能把覆盖率拉低）。
            # 所以再给一个按请求数算的覆盖率，两者都看才知道样本是不是真的全。
            b["hit_coverage_requests"] = (b["hit_requests"] / b["requests"]) if b["requests"] > 0 else None
        return out

    def totals_v2(self, *, requests: int, prompt: int, completion: int, hit: int,
                  hit_known_prompt: int, usage_missing: int, by_role: dict | None = None) -> dict:
        """给出 v2 字段。requests/prompt/... 由调用方从自身累加器传入，
        保证与既有 `UsageStats` 的其它字段完全一致（这一层不重复实现累加）。

        by_role：按来源分组的汇总（见 roles()）；主对话口径从 `main_hit_rate` 读，
        而不是总体的 `hit_rate`——后者把命中恒 0 的侧路请求也算进了分母。
        """
        buckets = self.buckets()
        costs = {cur: b.as_dict() for cur, b in buckets.items()}

        # 单值 cost 只在"唯一币种且金额完整"时给出；其余一律 None，绝不冒充确定总额。
        single_cost = None
        single_cur = None
        if len(buckets) == 1:
            cur, b = next(iter(buckets.items()))
            if b.complete:
                single_cost = round(b.known_amount, 10)
                single_cur = cur

        hit_rate = (hit / hit_known_prompt) if hit_known_prompt > 0 else None
        # 覆盖率：报过命中字段的请求，占总输入 token 的多少。
        # 没有任何请求报命中字段（hit_known_prompt==0）时是「未知」（None），
        # 不是 0——0 会被前端解读成「已知零命中」，掩盖「服务端没给字段」。
        hit_coverage = (hit_known_prompt / prompt) if hit_known_prompt > 0 else None

        out = {
            "schema_version": SCHEMA_VERSION,
            "requests": requests,
            "prompt": prompt,
            "completion": completion,
            "usage_missing": usage_missing,
            "costs": costs,
            "cost_complete": bool(buckets) and all(b.complete for b in buckets.values()) and usage_missing == 0,
            "hit": hit,
            "hit_known_prompt": hit_known_prompt,
            "hit_rate": hit_rate,
            "hit_coverage": hit_coverage,
            "cost": single_cost,
            "currency": single_cur,
        }
        if by_role:
            out["by_role"] = by_role
            main = by_role.get("main") or {}
            out["main_requests"] = main.get("requests", 0)
            out["main_hit_rate"] = main.get("hit_rate")
            out["main_hit_coverage"] = main.get("hit_coverage")
            out["main_hit_coverage_requests"] = main.get("hit_coverage_requests")
            out["main_usage_missing"] = main.get("usage_missing", 0)
            out["main_hit_known_prompt"] = main.get("hit_known_prompt", 0)
        return out
