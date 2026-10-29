"""评测汇总的费用口径反证（Wave 1 / T05，对应 ACC-31）。

两个毛病都会让"省钱"这件事无法被证明：

1. `avg_cost` 用 `all(c is not None for c in costs)` 把关：**只要有一条记录缺
   费用，整组的平均花费就变成 None**。评测越不稳定，越看不见钱。
2. 只取单值 `cost`：混币或 v2 口径下它是 None，明明有分币种金额也不用。

修好后的口径：按币种给出已知合计，并显式给出"有多少条缺费用"，而不是整组消失。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import eval_profiles as E  # noqa: E402


def _row(**kw) -> dict:
    base = {"task": "t1", "profile": "full", "group": "full", "passed": True,
            "steps": 3, "tool_calls": 2, "prompt_tokens": 1000, "completion_tokens": 100,
            "cache_hit_tokens": 500, "duration_s": 10.0, "tool_s": 2.0, "edit_failures": 0}
    base.update(kw)
    return base


class TestCostCoverage:
    def test_one_missing_cost_does_not_hide_the_rest(self):
        """一条缺费用不能把整组金额藏起来，但也不能因此给出假完整的单值平均。"""
        rows = [
            _row(cost=0.02, currency="CNY"),
            _row(cost=None, currency="CNY", reason="usage_missing"),
            _row(cost=0.04, currency="CNY"),
        ]
        agg = E.aggregate(rows)["full"]
        # 已知部分可见：总额与已报价的条数（原来整组直接看不见）
        assert agg["costs"]["CNY"]["known_total"] == pytest.approx(0.06)
        assert agg["costs"]["CNY"]["known_runs"] == 2
        # 缺失显式计数，不藏
        assert agg["cost_missing_runs"] == 1
        assert agg["cost_known_runs"] == 2
        # 组内有缺失：不给单值平均，避免假完整（与 ACC-02 同一条不变量）
        assert agg["avg_cost"] is None

    def test_mixed_currency_reported_per_currency(self):
        """混币时按币种分别给出，不能合成一个数。"""
        rows = [
            _row(cost=0.02, currency="CNY"),
            _row(cost=0.03, currency="USD"),
        ]
        agg = E.aggregate(rows)["full"]
        buckets = agg.get("costs") or {}
        assert "CNY" in buckets and "USD" in buckets, f"没有分币种金额：{buckets}"

    def test_all_known_still_works(self):
        """全都有费用时照旧给出平均值（不能为了新口径把常规情况弄坏）。"""
        rows = [_row(cost=0.02, currency="CNY"), _row(cost=0.04, currency="CNY")]
        agg = E.aggregate(rows)["full"]
        assert agg["avg_cost"] == pytest.approx(0.03)
        assert agg["cost_missing_runs"] == 0


class TestSummarizeKeepsV2Fields:
    def test_summarize_reads_v2_costs_bucket(self):
        """turn_done 的 usage 已经是 v2 时，汇总要能读到分币种金额。"""
        import json

        lines = [
            json.dumps({"type": "session", "profile": "full"}),
            json.dumps({"type": "turn_done", "reason": "completed", "duration_ms": 1000,
                        "usage": {"schema_version": 2, "requests": 1, "prompt": 100, "completion": 10,
                                  "hit": 50, "hit_known_prompt": 100, "usage_missing": 0,
                                  "costs": {"CNY": {"known_amount": "0.05", "estimated_amount": "0",
                                                    "unknown_requests": 0, "requests": 1, "complete": True}},
                                  "cost_complete": True, "cost": 0.05, "currency": "CNY"}}),
        ]
        s = E.summarize_events(lines)
        assert s["cost"] == pytest.approx(0.05)
        assert (s.get("costs") or {}).get("CNY"), f"v2 分币种金额没被读到：{s.get('costs')}"
