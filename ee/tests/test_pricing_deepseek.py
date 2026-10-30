"""DeepSeek 官方峰谷价规则（内置）的行为契约。

官方规则（api-docs.deepseek.com/zh-cn/quick_start/pricing）：
- 高峰时段 = 北京时间周一至周五（不含中国法定节假日）9:00-12:00、14:00-18:00；
- 空闲时段 = 其余（含周末与法定节假日全天），价格为高峰时段的一半。
- 高峰全价，空闲半价。
"""
from __future__ import annotations

from datetime import datetime

from ee.core.providers.pricing import discount_for


def test_deepseek_weekday_peak_full_price():
    """周一 10 点（高峰窗口内）→ 全价 1.0。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": ""}
    # 2026-10-26 是周一
    assert discount_for(cfg, datetime(2026, 10, 26, 10, 0), family="deepseek") == 1.0


def test_deepseek_weekday_offpeak_half_price():
    """周一 8 点（高峰窗口外）→ 半价 0.5。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": ""}
    assert discount_for(cfg, datetime(2026, 10, 26, 8, 0), family="deepseek") == 0.5


def test_deepseek_weekend_all_offpeak():
    """周六（周末）全天 → 半价。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": ""}
    # 2026-10-31 是周六
    assert discount_for(cfg, datetime(2026, 10, 31, 10, 0), family="deepseek") == 0.5


def test_deepseek_lunch_gap_is_offpeak():
    """周一 13 点（两个高峰窗口之间的空档）→ 半价。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": ""}
    assert discount_for(cfg, datetime(2026, 10, 26, 13, 0), family="deepseek") == 0.5


def test_non_deepseek_uses_offpeak_hours_config():
    """非 deepseek 家族：仍走 offpeak_hours 显式配置，不套 deepseek 官方规则。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": "0-8"}
    # 周一 10 点，offpeak_hours=0-8 之外 → 全价（不受 deepseek 官方规则影响）
    assert discount_for(cfg, datetime(2026, 10, 26, 10, 0), family="qwen") == 1.0
    # 周一 3 点，在 0-8 内 → 半价
    assert discount_for(cfg, datetime(2026, 10, 26, 3, 0), family="qwen") == 0.5


def test_deepseek_holiday_all_offpeak():
    """法定节假日（非周末的工作日）全天半价。"""
    cfg = {"offpeak_discount": 0.5, "offpeak_hours": ""}
    # 2026-10-01 是周四，国庆假期
    assert discount_for(cfg, datetime(2026, 10, 1, 10, 0), family="deepseek") == 0.5
