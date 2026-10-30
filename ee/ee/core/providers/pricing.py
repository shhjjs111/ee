"""错峰折扣判定：把配置里的 offpeak 时段/折扣换算成「当前该打几折」。

DeepSeek 家族内置官方峰谷规则（api-docs.deepseek.com 2026-10 官方）：
高峰 = 北京时间周一至周五（不含中国法定节假日）9:00-12:00、14:00-18:00；
空闲 = 其余（含周末与法定节假日全天），价格为高峰的一半。
"""
from __future__ import annotations

from datetime import datetime, time

# DeepSeek 高峰时段（北京时间，半开区间 [start, end)）
_DEEPSEEK_PEAK_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))

# 中国法定节假日（2026 已知；每年需更新）。放假日期全天按空闲（半价）。
_CN_HOLIDAYS_2026 = {
    "2026-01-01",  # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",  # 春节
    "2026-04-06",  # 清明
    "2026-05-01", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19",  # 端午
    "2026-09-25",  # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
}


def _parse_hour(s: str) -> int | None:
    try:
        n = int(s)
        return n if 0 <= n <= 23 else None
    except (TypeError, ValueError):
        return None


def _in_window(hour: int, start: int, end: int) -> bool:
    """[start, end) 半开区间；跨午夜时 end < start。"""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _is_cn_holiday(now: datetime) -> bool:
    return now.strftime("%Y-%m-%d") in _CN_HOLIDAYS_2026


def _deepseek_is_peak(now: datetime) -> bool:
    """DeepSeek 官方：工作日（非节假日）的 9-12、14-18 是高峰。"""
    if now.weekday() >= 5:          # 周六/周日
        return False
    if _is_cn_holiday(now):
        return False
    t = now.time()
    return any(start <= t < end for start, end in _DEEPSEEK_PEAK_WINDOWS)


def discount_for(cfg: dict | None, now: datetime, family: str | None = None) -> float:
    """返回当前时刻的折扣系数（0<d<=1）。没配错峰 → 1.0。

    family="deepseek" 时用官方峰谷规则（高峰全价、空闲按 offpeak_discount，通常 0.5）；
    其余家族用 offpeak_hours 显式配置（"start-end"，允许跨午夜如 "22-6"）。
    """
    if not cfg:
        return 1.0
    discount = cfg.get("offpeak_discount")
    try:
        d = float(discount)
    except (TypeError, ValueError):
        return 1.0
    if not (0 < d <= 1):
        return 1.0

    # DeepSeek 官方规则：高峰全价，空闲打折
    if family == "deepseek":
        return 1.0 if _deepseek_is_peak(now) else d

    # 其他家族：offpeak_hours 显式时段
    hours = cfg.get("offpeak_hours") or ""
    parts = str(hours).split("-")
    if len(parts) != 2:
        return 1.0
    start, end = _parse_hour(parts[0].strip()), _parse_hour(parts[1].strip())
    if start is None or end is None:
        return 1.0
    if _in_window(now.hour, start, end):
        return d
    return 1.0
