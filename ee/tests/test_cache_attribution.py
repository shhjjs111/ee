"""缓存未命中归因第一步：记录每条请求发生在第几回合（turn）。

目的：前缀缓存未命中集中在哪里，需要「回合」这个维度才能回答——
首回合必然冷启动（整段 prompt 都新），后续回合的未命中才是可优化的。
"""
from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------- 主对话口径与断点归因（P0-1）

def test_side_requests_do_not_lower_main_hit_rate():
    """侧路请求（标题 / commit / 压缩）命中恒 0，不能把用户看到的主对话命中率拉下来。"""
    from ee.core import subagent as SA  # noqa: F401  确保模块可导入（同进程口径一致）

    s = UsageStats()
    for _ in range(4):
        s.add({"prompt": 10_000, "completion": 100, "hit": 9_900}, _price(), model="m", role="main")
    for _ in range(4):
        s.add({"prompt": 3_000, "completion": 100, "hit": 0}, _price(), model="m", role="title")
    totals = s.totals()
    assert totals["hit_rate"] < 0.9, "总口径本来就会被侧路拉低（这正是要区分的原因）"
    assert totals["main_hit_rate"] == 0.99
    assert totals["main_requests"] == 4
    assert totals["by_role"]["title"]["requests"] == 4


def test_title_request_records_its_role():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 5, "hit": 0}, _price(), model="m", role="title")
    assert s.records[-1]["role"] == "title"


def test_recent_main_hit_rate_ignores_side_requests():
    """压缩水位自适应用的是「最近主对话请求」，侧路请求混进来会把水位推向错误方向。"""
    s = UsageStats()
    for _ in range(6):
        s.add({"prompt": 1000, "completion": 10, "hit": 995}, _price(), model="m", role="main")
        s.add({"prompt": 300, "completion": 10, "hit": 0}, _price(), model="m", role="title")
    assert s.recent_main_hit_rate() == pytest.approx(0.995)


def test_subagent_records_do_not_pollute_main_hit_rate():
    """子代理并入主会话后必须落成 role=subagent：它的前缀独立、命中率天然低。"""
    from ee.core import subagent as SA

    parent = type("S", (), {})()
    parent.stats = UsageStats()
    for _ in range(10):
        parent.stats.add({"prompt": 10_000, "completion": 100, "hit": 9_900}, _price(), model="m", role="main")
    child = type("S", (), {})()
    child.stats = UsageStats()
    for _ in range(10):
        child.stats.add({"prompt": 10_000, "completion": 100, "hit": 0}, _price(), model="m", role="main")

    SA._merge_usage(parent, child)
    totals = parent.stats.totals()
    assert totals["main_hit_rate"] == pytest.approx(0.99), "子代理不能把主对话命中率拉到 0.495"
    assert totals["by_role"]["subagent"]["requests"] == 10
    assert parent.stats.recent_main_hit_rate() == pytest.approx(0.99)


# ---------------------------------------------------------------- 断点判定（wire / gap）

def test_gap_within_normal_growth_is_not_a_break():
    """纯追加回合：未命中约等于这次新增的量，不该标成断点。"""
    s = UsageStats()
    s.note_wire(10_000)
    s.add({"prompt": 10_000, "completion": 50, "hit": 0}, _price(), model="m", role="main")
    s.note_wire(12_000)                       # 追加 2000
    s.add({"prompt": 12_000, "completion": 50, "hit": 10_400}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["new"] == 2_000
    assert r["gap"] == 1_600
    assert "break" not in r


def test_gap_far_beyond_growth_is_a_break():
    """历史被整段改写（压缩）：未命中远超新增，必须标成断点并带上原因。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 50, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(30_000)                       # 压缩后历史变短了
    s.add({"prompt": 30_000, "completion": 50, "hit": 3_000}, _price(), model="m", role="main",
          break_reason="compact")
    r = s.records[-1]
    assert r["gap"] == 27_000
    assert r["break"] == "compact"


def test_side_request_never_reports_a_break():
    """侧路请求的消息和主对话没有公共前缀，拿它算断点会把主对话的归因全带偏。"""
    s = UsageStats()
    s.note_wire(50_000)                       # 主对话刚发过
    s.add({"prompt": 3_000, "completion": 50, "hit": 0}, _price(), model="m", role="title")
    r = s.records[-1]
    assert "gap" not in r and "break" not in r
    assert s.last_wire_tokens == 0, "侧路不动主对话的 wire 游标"
