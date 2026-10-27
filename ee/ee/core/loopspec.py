"""会话内 /loop 的参数解析：`/loop 10m 检查CI` → 间隔 10 分钟；`/loop 每天 9:00 总结` → 每天 09:00。

支持的写法（间隔可在开头或结尾）：
  10m / 30s / 2h / 1d、every 5m、每10分钟、每 2 小时、10分钟、2小时、1天
  每天 9:00 / 每天9点 / 每天 21点30 / 工作日 9:30
不写间隔默认每 10 分钟。秒向上取整到分钟（最短 1 分钟），间隔最长 7 天。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

DEFAULT_MINUTES = 10
MAX_MINUTES = 7 * 24 * 60
MAX_PROMPT = 4000

USAGE = ("用法：/loop [间隔] 提示词，例如 /loop 10m 检查CI是否通过、/loop 每天 9:00 总结昨天的提交；"
         "/loop list 查看，/loop stop <编号> 停止")


class LoopSpecError(ValueError):
    pass


@dataclass
class LoopSpec:
    schedule: dict
    prompt: str
    note: str = ""
    extra: dict = field(default_factory=dict)


_UNIT = {"s": 1, "sec": 1, "secs": 1, "秒": 1, "秒钟": 1,
         "m": 60, "min": 60, "mins": 60, "分": 60, "分钟": 60,
         "h": 3600, "hr": 3600, "hrs": 3600, "小时": 3600, "个小时": 3600, "时": 3600,
         "d": 86400, "day": 86400, "days": 86400, "天": 86400}
_UNITS = "|".join(sorted((re.escape(u) for u in _UNIT), key=len, reverse=True))
_IV = rf"(?:every\s*|每\s*(?:隔\s*)?)?(\d+)\s*({_UNITS})"
_LEAD = re.compile(rf"^{_IV}(?=\s|$|[，,：:])[\s，,：:]*", re.I)
_TRAIL = re.compile(rf"[\s，,]+{_IV}\s*[。.]?$", re.I)
_DAILY = re.compile(r"^(每天|每日|daily|工作日|weekdays)\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|点\s*(?:(\d{1,2})\s*分?|半)?)"
                    r"(?=\s|$|[，,])[\s，,]*", re.I)


def _interval(n: str, unit: str) -> tuple[dict, str]:
    secs = int(n) * _UNIT[unit.lower()]
    if secs <= 0:
        raise LoopSpecError("间隔要大于 0")
    minutes = max(1, math.ceil(secs / 60))
    if minutes > MAX_MINUTES:
        raise LoopSpecError("间隔最长 7 天；更长的周期请用「自动化」")
    note = f"间隔按分钟计，已取 {minutes} 分钟" if secs % 60 else ""
    return {"kind": "interval", "minutes": minutes}, note


def parse(text: str) -> LoopSpec:
    t = (text or "").strip()
    if not t:
        raise LoopSpecError(USAGE)
    note = ""
    m = _DAILY.match(t)
    if m:
        h = int(m.group(2))
        mi = m.group(3) or m.group(4)
        mm = int(mi) if mi else (30 if "半" in m.group(0) else 0)
        if h > 23 or mm > 59:
            raise LoopSpecError("时间要在 00:00–23:59 之间")
        wd = [1, 2, 3, 4, 5] if m.group(1).lower() in ("工作日", "weekdays") else []
        sched = {"kind": "daily", "time": f"{h:02d}:{mm:02d}", "weekdays": wd}
        prompt = t[m.end():].strip()
    elif (m := _LEAD.match(t)):
        sched, note = _interval(m.group(1), m.group(2))
        prompt = t[m.end():].strip()
    elif (m := _TRAIL.search(t)):
        sched, note = _interval(m.group(1), m.group(2))
        prompt = t[: m.start()].strip()
    else:
        if re.match(r"^(每天|每日|工作日)", t):
            raise LoopSpecError("时间写成 每天 9:00 或 每天9点")
        sched, prompt = {"kind": "interval", "minutes": DEFAULT_MINUTES}, t
        note = "没写间隔，默认每 10 分钟"
    if not prompt:
        raise LoopSpecError("请写上要重复发送的提示词。" + USAGE)
    return LoopSpec(sched, prompt[:MAX_PROMPT], note)
