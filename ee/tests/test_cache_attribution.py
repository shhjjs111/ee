"""缓存未命中归因第一步：记录每条请求发生在第几回合（turn）。

目的：前缀缓存未命中集中在哪里，需要「回合」这个维度才能回答——
首回合必然冷启动（整段 prompt 都新），后续回合的未命中才是可优化的。
"""
from __future__ import annotations

from ee.core.context import UsageStats
from ee.core.providers.profiles import Price


def _price() -> Price:
    return Price(input=2.0, output=8.0, cache_read=0.04, currency="¥")


def test_turn_recorded_in_request_record():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 10, "hit": 0}, _price(), model="m", turn=3)
    assert s.records[-1]["turn"] == 3


def test_turn_defaults_to_none_when_not_given():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 10, "hit": 0}, _price(), model="m")
    assert s.records[-1].get("turn") is None
