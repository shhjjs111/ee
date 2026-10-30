"""错峰折扣判定：把配置里的 offpeak 时段/折扣换算成「当前该打几折」。"""
from __future__ import annotations

from datetime import datetime


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


def discount_for(cfg: dict | None, now: datetime) -> float:
    """返回当前时刻的折扣系数（0<d<=1）。没配错峰 → 1.0。

    cfg 形如 {"offpeak_discount": 0.5, "offpeak_hours": "0-8"}。
    offpeak_hours 是 "start-end"（0-23，允许跨午夜如 "22-6"）；空或非法 → 全价。
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
