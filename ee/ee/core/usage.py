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

    def totals_v2(self, *, requests: int, prompt: int, completion: int, hit: int,
                  hit_known_prompt: int, usage_missing: int) -> dict:
        """给出 v2 字段。requests/prompt/... 由调用方从自身累加器传入，
        保证与既有 `UsageStats` 的其它字段完全一致（这一层不重复实现累加）。"""
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

        return {
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
