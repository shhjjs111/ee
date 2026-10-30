"""错峰折扣时段判定（pricing.offpeak）的行为契约。"""
from __future__ import annotations

from datetime import datetime

from ee.core.providers.pricing import discount_for


def test_no_offpeak_config_is_full_price():
    """没配错峰时段 → 折扣 1.0。"""
    assert discount_for({}, datetime(2026, 10, 30, 15, 0)) == 1.0


def test_offpeak_hours_apply_discount():
    """offpeak_hours="0-8" 且 discount=0.5，凌晨 3 点 → 0.5。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": "0-8"}
    assert discount_for(cfg, datetime(2026, 10, 30, 3, 0)) == 0.5


def test_peak_hours_full_price():
    """offpeak 之外（如 15 点）→ 1.0。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": "0-8"}
    assert discount_for(cfg, datetime(2026, 10, 30, 15, 0)) == 1.0


def test_crossing_midnight_hours():
    """跨午夜时段 22-6，凌晨 2 点应命中。"""
    cfg = {"offpeak_discount": 0.7, "offpeak_hours": "22-6"}
    assert discount_for(cfg, datetime(2026, 10, 30, 2, 0)) == 0.7
    assert discount_for(cfg, datetime(2026, 10, 30, 12, 0)) == 1.0
