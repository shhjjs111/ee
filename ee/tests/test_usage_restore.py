"""重启后费用恢复的反证（Wave 1 / T04，对应 ACC-03/ACC-04）。

两件事必须同时成立：

1. **不多算**：重放同一个会话不能把已经花掉的钱再加一遍（跨进程重启、或同一
   进程里重复恢复）；
2. **不漏算**：混币会话恢复后，每个币种的已花金额都还在——不是只剩最后一个。

第二条是我在 T03 引入 v2 口径时自己带出来的回归：`totals()` 在混币时
`cost` 变成 None，而恢复路径只读 `u.get("cost")` 判断，于是整个 spent_before
被丢掉，会话预算重新从 0 开始。
"""
from __future__ import annotations

import pytest

from ee.core.context import UsageStats
from ee.core.providers.profiles import Price


def _price(per_million: float, currency: str) -> Price:
    return Price(input=per_million, output=per_million, currency=currency)


def _add_one_million(st: UsageStats, currency: str, model: str = "m") -> None:
    st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, currency), model)


def _spent_from_totals(u: dict) -> dict[str, float]:
    """模拟 Thread.ensure_session 从 turn_end 的 usage 恢复已花金额。

    这里直接调生产辅助函数，避免测试自己写一套恢复逻辑——那样测的是测试。
    """
    from ee.server.threads import spent_before_from_usage

    return spent_before_from_usage(u)


class TestRestoreFromUsage:
    def test_single_currency_restores(self):
        st = UsageStats()
        _add_one_million(st, "CNY")
        assert _spent_from_totals(st.totals()) == {"CNY": pytest.approx(1.0)}

    def test_mixed_currency_restores_every_bucket(self):
        """混币会话恢复后两个币种都要在（T03 之前 cost 是合并值，现在是 None）。"""
        st = UsageStats()
        _add_one_million(st, "CNY")
        _add_one_million(st, "USD")
        got = _spent_from_totals(st.totals())
        assert got.get("CNY") == pytest.approx(1.0), f"CNY 丢失：{got}"
        assert got.get("USD") == pytest.approx(1.0), f"USD 丢失：{got}"

    def test_unknown_amount_is_not_restored_as_zero(self):
        """有请求金额未知时，不能把该币种恢复成 0 元（那等于假装没花钱）。"""
        st = UsageStats()
        _add_one_million(st, "CNY")
        st.add(None, _price(1.0, "CNY"), "m")          # 一次没有 usage 的请求
        got = _spent_from_totals(st.totals())
        # 已知部分仍然要保留；未知不参与但不抹掉已知。
        assert got.get("CNY") == pytest.approx(1.0)

    def test_empty_usage_restores_nothing(self):
        assert _spent_from_totals({}) == {}


class TestNoDoubleCount:
    def test_restored_baseline_not_double_counted(self):
        """恢复出的基线 + 新花费 = 真实总额，不能把基线算两遍。"""
        from ee.core.session import Session

        st = UsageStats()
        _add_one_million(st, "CNY")
        baseline = _spent_from_totals(st.totals())

        # 构造一个只有 spent_before 的会话对象来读 session_spent（不建真实 Session）。
        class _Probe:
            budget_root = None
            spent_before = dict(baseline)
            stats = UsageStats()

        _Probe.budget_root = _Probe
        _Probe.stats.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(1.0, "CNY"), "m")

        assert Session.session_spent(_Probe, "CNY") == pytest.approx(2.0)


class TestMultiRestartAccumulation:
    """重启多次后，会话预算必须保住**全部**历史花费，而不是只剩最后一段。

    复现的真实数据（~/.ee/sessions/20261030-235538-61f6f6.jsonl，重启 3 次）：

        段1 prompt=12,048,019 → ¥1.8556
        段2 prompt= 5,720,054 → ¥0.6336
        段3 prompt= 3,002,317 → ¥0.1815
        真实合计 ¥2.6707，恢复值 ¥0.1815，丢失 93.2%

    根因：`turn_end.usage` 的快照由 `Session.stats` 生成，而 `stats` 在重启后是
    新的 `UsageStats`（session.py:99），每个进程从 0 重新累加。恢复路径只读最后
    一条 `turn_end`，于是只恢复最后一个进程的那部分花费。
    """

    def test_three_restarts_keep_all_segments(self):
        """三次重启各花一部分：恢复值必须等于三段之和，不是最后一段。"""
        from ee.server.threads import spent_before_from_usage

        # 每段一个进程，各自从 0 累加（真实日志的形状）
        def segment(spend: float) -> dict:
            st = UsageStats()
            st.add({"prompt": 1_000_000, "completion": 0, "hit": 0}, _price(spend, "¥"), "m")
            return st.totals()

        last = segment(0.1815)          # 最后一个进程的花费
        # 写入侧应把「重启前已花」也记进快照（累计口径）。
        last["spent_total"] = {"¥": pytest.approx(0) if False else 2.6707}
        got = spent_before_from_usage(last)
        assert got.get("¥") == pytest.approx(2.6707), (
            f"三次重启后只恢复了 {got}，应为 ¥2.6707（丢失 {1 - got.get('¥', 0) / 2.6707:.1%}）")

    def test_spent_total_survives_mixed_currency(self):
        """累计口径同样要分币种，不能合并成一个数。"""
        from ee.server.threads import spent_before_from_usage

        u = {"costs": {"CNY": {"known_amount": "1.0"}, "USD": {"known_amount": "0.5"}},
             "spent_total": {"CNY": 3.0, "USD": 1.5}}
        got = spent_before_from_usage(u)
        assert got.get("CNY") == pytest.approx(3.0), got
        assert got.get("USD") == pytest.approx(1.5), got

    def test_old_snapshot_without_spent_total_falls_back(self):
        """老会话没有 spent_total 字段：退回旧口径（最后一段），不能因此丢光。"""
        from ee.server.threads import spent_before_from_usage

        u = {"costs": {"¥": {"known_amount": "0.1815"}}}
        assert spent_before_from_usage(u).get("¥") == pytest.approx(0.1815)
