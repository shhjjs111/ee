"""费用与用量统计的反证（Wave 0 / T02，对应 ACC-01/02/05）。

本文件的用例描述的是**当前实现下的缺陷**：混币被相加、缺用量仍报完整总额。
每条都用 `pytest.xfail(strict=True)` 标记为"已知失败、待修复"：

- 现在跑：标记 XFAIL，CI 不红，但缺陷被可重复地钉住；
- Wave 1 修好后：会变成 XPASS，strict 模式下直接 FAIL —— 强制实施者回来
  把 xfail 标记删掉，而不是把缺陷悄悄当成"已通过"。

断言对象是生产的 `UsageStats` / `Price`，不是复制一份实现来测自己。
"""
from __future__ import annotations

import pytest

from ee.core.context import UsageStats
from ee.core.providers.profiles import Price


def _price(per_million: float, currency: str) -> Price:
    """单价按每百万 token 计；输入 1_000_000 token 时金额恰好等于 per_million。"""
    return Price(input=per_million, output=per_million, currency=currency)


def _one_request(cost_per_million: float, currency: str) -> dict:
    """一条恰好消耗 1M prompt token 的用量记录，未命中缓存。"""
    return {"prompt": 1_000_000, "completion": 0, "hit": 0, "currency": currency,
            "cost": cost_per_million}


def test_mixed_currency_totals_must_not_collapse_into_one_currency():
    """CNY 1 + USD 1 不能合并成一个数字（当前实现合并成 2，币种取最后一条）。"""
    st = UsageStats()
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "CNY"), "model-a")
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "USD"), "model-b")
    totals = st.totals()

    # 现状：cost == 2.0 且 currency 是单值。正解要求混币时不能给出单一确定总额。
    assert not (totals.get("cost") == 2.0), (
        f"混币被相加：cost={totals.get('cost')} currency={totals.get('currency')}"
    )


def test_mixed_currency_keeps_per_currency_buckets():
    """混币时必须能分别读出每个币种的金额，而不是只留一个。"""
    st = UsageStats()
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "CNY"), "model-a")
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "USD"), "model-b")
    totals = st.totals()

    costs = totals.get("costs") or {}
    assert "CNY" in costs and "USD" in costs, f"缺少分币种金额：{costs}"
    assert float(costs["CNY"]["known_amount"]) == pytest.approx(1.0)
    assert float(costs["USD"]["known_amount"]) == pytest.approx(1.0)


def test_missing_usage_must_mark_total_incomplete():
    """有一条请求没返回 usage 时，总额不能被当成完整已知金额（当前会照常给一个确定值）。"""
    st = UsageStats()
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "CNY"), "model-a")
    st.add(None, _price(1.0, "CNY"), "model-a")     # 服务端没给 usage 的那种情况
    totals = st.totals()

    assert totals.get("usage_missing") == 1
    # 现状：cost 仍是 1.0，看起来像"完整且确定"。正解要么 cost=None，要么带明确的完整性标记。
    assert totals.get("cost") is None or totals.get("cost_complete") is False, (
        f"缺用量仍报完整总额：cost={totals.get('cost')} cost_complete={totals.get('cost_complete')}"
    )


def test_unknown_price_must_not_look_free():
    """没配单价的模型不能被当成 0 元（当前走 cost_known=False，但需要确认展示层口径）。"""
    st = UsageStats()
    st.add({"prompt": 1000, "completion": 100, "hit": 0}, None, "unpriced")
    totals = st.totals()

    assert totals.get("cost") is None, f"未知单价被算成金额：{totals.get('cost')}"


def test_hit_rate_reports_coverage():
    """命中率必须有覆盖率，否则"50% 命中"在只有一半请求报命中字段时会误导。"""
    st = UsageStats()
    st.add({"prompt": 1000, "completion": 10, "hit": 500}, _price(1.0, "CNY"), "model-a")
    st.add({"prompt": 1000, "completion": 10, "hit": None}, _price(1.0, "CNY"), "model-a")
    totals = st.totals()

    # 现状只给 hit_rate，没有 hit_coverage；命中率 0.5 是"有命中字段的那一半"的口径。
    assert "hit_coverage" in totals, "缺少 hit_coverage：用户无法判断命中率代表多少请求"
    assert totals["hit_coverage"] == pytest.approx(0.5)


def test_hit_coverage_unknown_when_no_request_reports_hit():
    """T19：没有任何请求报 hit 字段时，覆盖率是「未知」（None），不是 0。

    0 会被前端解读成「已知零命中」，掩盖「服务端根本没给命中字段」这个事实。
    这是「未知不显示 0」在数据源层的根因。
    """
    st = UsageStats()
    st.add({"prompt": 1000, "completion": 10, "hit": None}, _price(1.0, "CNY"), "model-a")
    st.add({"prompt": 1000, "completion": 10, "hit": None}, _price(1.0, "CNY"), "model-a")
    totals = st.totals()
    assert totals["hit_rate"] is None, "没有任何请求报命中字段，命中率应为未知"
    assert totals["hit_coverage"] is None, "覆盖率也应为未知，而不是 0"
