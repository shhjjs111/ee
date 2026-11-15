"""交付与成本指标（2026-11 第四批）。

口径边界是本文件的重点：无样本、零成功、金额不完整、混合币种都必须返回 None + 说明，
而不是 0 或一个看起来精确的平均值——那会让"这套改造有没有用"的判断建立在假数字上。
"""
from __future__ import annotations

import asyncio

from ee.core import delivery_metrics as M


def _turn(reason: str = "met", status: str = "verified", *, regression: str = "met",
          cost: float | None = 0.5, complete: bool = True, currency: str = "¥") -> dict:
    return {"type": "turn_end", "reason": reason,
            "delivery": {"status": status, "regression": {"status": regression}},
            "usage": {"cost": cost, "cost_complete": complete, "currency": currency, "requests": 3}}


def test_non_delivery_turns_are_ignored():
    """普通对话（没有交付结论）不参与统计：混进来会把复验通过率稀释成假的。"""
    plain = {"type": "turn_end", "reason": "completed", "usage": {"cost": 0.1}}
    got = M.aggregate([plain])
    assert got["samples"] == 0
    assert got["verify_rate"] is None
    assert any("无样本" in n for n in got["notes"])


def test_verified_and_met_is_consistent():
    got = M.aggregate([_turn(), _turn()])
    assert got["samples"] == 2 and got["solved"] == 2
    assert got["verify_rate"] == 1.0
    assert got["consistency_error_rate"] == 0.0
    assert got["cost_total"] == 1.0
    assert got["cost_per_solved"] == 0.5


def test_met_but_unverified_counts_as_consistency_error():
    """报 met 却没通过复验：这是"成功状态不一致"，必须能被指标抓出来。"""
    got = M.aggregate([_turn(), _turn(status="failed", regression="failed")])
    assert got["reported_met"] == 2
    assert got["inconsistent"] == 1
    assert got["consistency_error_rate"] == 0.5
    assert got["verify_rate"] == 0.5


def test_no_met_turns_means_no_consistency_sample():
    got = M.aggregate([_turn(reason="exhausted", status="failed")])
    assert got["consistency_error_rate"] is None, "没有 met 回合就没有分母，不能报 0"


def test_zero_solved_reports_spend_without_average():
    got = M.aggregate([_turn(status="failed"), _turn(status="needs_review")])
    assert got["cost_per_solved"] is None
    assert got["cost_total"] == 1.0, "零成功时也要能看到总支出"
    assert any("没有已复验通过" in n for n in got["notes"])


def test_mixed_currency_refuses_to_sum():
    got = M.aggregate([_turn(currency="¥"), _turn(currency="$")])
    assert got["cost_total"] is None
    assert any("币种" in n for n in got["notes"])


def test_incomplete_cost_is_flagged():
    got = M.aggregate([_turn(cost=None, complete=False), _turn()])
    assert got["cost_total"] == 0.5
    assert got["cost_complete"] is False
    assert any("没有金额" in n for n in got["notes"])


def test_unconfigured_regression_is_not_a_failure():
    """未配置回归命令是口径缺失，不是失败：不能因此把 verified 的回合算成没过。"""
    got = M.aggregate([_turn(regression="unconfigured")])
    assert got["solved"] == 1 and got["verify_rate"] == 1.0
    assert got["regression_unconfigured"] == 1
    assert any("未配置回归命令" in n for n in got["notes"])


def test_rpc_aggregates_loaded_threads(ee_home, workspace):
    """RPC 入口：只统计带交付结论的回合，参数按会话过滤。"""
    from test_server import make_backend

    async def go():
        b, _, _ = make_backend(ee_home, [])
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        t = b.threads[tid]
        t._add({"type": "turn_end", "reason": "met", "delivery": {"status": "verified",
                                                                "regression": {"status": "met"}},
                "usage": {"cost": 0.25, "cost_complete": True, "currency": "¥"}})
        t._add({"type": "turn_end", "reason": "met", "delivery": {"status": "failed",
                                                                "regression": {"status": "failed"}},
                "usage": {"cost": 0.25, "cost_complete": True, "currency": "¥"}})
        all_ = await b.call("metrics/summary", {})
        one = await b.call("metrics/summary", {"thread_id": tid})
        assert all_["samples"] == 2 and one["samples"] == 2
        assert one["consistency_error_rate"] == 0.5 and one["verify_rate"] == 0.5

    asyncio.run(go())


def test_unverified_turns_are_excluded_from_verify_rate_denominator():
    """没有独立判据的回合（unverified）不算复验样本：分母只放有判据的回合。"""
    got = M.aggregate([_turn(status="unverified", reason="completed", regression="unconfigured"),
                       _turn()])
    assert got["samples"] == 2
    assert got["applicable"] == 1 and got["unverified"] == 1
    assert got["verify_rate"] == 1.0, "未验收回合不能稀释复验通过率"
    assert any("未独立验收" in n for n in got["notes"])


def test_all_unverified_has_no_verify_rate_but_shows_spend():
    got = M.aggregate([_turn(status="unverified", reason="completed", regression="unconfigured")])
    assert got["verify_rate"] is None and got["applicable"] == 0
    assert got["cost_total"] == 0.5 and got["cost_per_solved"] is None
