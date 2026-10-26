"""自动化的调度规则（D4-10）：每隔 N 分钟 / 每天 HH:MM（可选星期几）/ 5 段 cron。

时间一律按本机本地时间理解（naive datetime ⇄ time.time() 时间戳用 datetime.fromtimestamp / .timestamp() 互转，
夏令时由系统处理）。不依赖第三方库。

调度对象（JSON）：
  {"kind": "interval", "minutes": 30}
  {"kind": "daily", "time": "09:00", "weekdays": [1, 2, 3, 4, 5]}   weekdays 用 cron 习惯：0=周日 … 6=周六；空 = 每天
  {"kind": "cron", "expr": "0 9 * * 1-5"}

cron 5 段：分 时 日 月 星期。每段支持 *、数字、a-b、列表 a,b、步长 */n 与 a-b/n、a/n；
月份可写 jan…dec，星期可写 sun…sat（0 和 7 都是周日）。日和星期都受限时按 cron 惯例取“或”。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

MIN_INTERVAL = 1                  # 分钟
MAX_INTERVAL = 7 * 24 * 60        # 一周
_SEARCH_DAYS = 366 * 5            # 找下次时间最多往后看 5 年（如 2 月 30 日这种永远不会到的会报错）

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DOWS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_WEEKDAY_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


class ScheduleError(ValueError):
    pass


# ------------------------------------------------------------------ cron
def _parse_field(text: str, lo: int, hi: int, names: dict[str, int] | None, label: str) -> set[int]:
    out: set[int] = set()

    def num(tok: str) -> int:
        t = tok.strip().lower()
        if names and t in names:
            return names[t]
        if not t.isdigit():
            raise ScheduleError(f"cron 的{label}字段里有看不懂的值：{tok}")
        return int(t)

    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"cron 的{label}字段有空项")
        step = 1
        if "/" in part:
            part, _, st = part.partition("/")
            if not st.isdigit() or int(st) <= 0:
                raise ScheduleError(f"cron 的{label}字段步长不合法：/{st}")
            step = int(st)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            a, b = num(x), num(y)
        else:
            a = num(part)
            b = hi if step > 1 else a        # “5/15”= 从 5 开始每 15
        if a < lo or b > hi or a > b:
            raise ScheduleError(f"cron 的{label}字段超出范围（{lo}–{hi}）：{part}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """解析好的 5 段 cron 表达式。"""

    def __init__(self, expr: str):
        expr = (expr or "").strip()
        fields = expr.split()
        if len(fields) != 5:
            raise ScheduleError("cron 表达式要正好 5 段：分 时 日 月 星期（例如 0 9 * * 1-5）")
        self.expr = " ".join(fields)
        self.minutes = _parse_field(fields[0], 0, 59, None, "分钟")
        self.hours = _parse_field(fields[1], 0, 23, None, "小时")
        self.doms = _parse_field(fields[2], 1, 31, None, "日期")
        self.months = _parse_field(fields[3], 1, 12, _MONTHS, "月份")
        dows = _parse_field(fields[4], 0, 7, _DOWS, "星期")
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_any = fields[2].startswith("*")
        self.dow_any = fields[4].startswith("*")

    def _day_ok(self, t: datetime) -> bool:
        dom_ok = t.day in self.doms
        dow_ok = ((t.weekday() + 1) % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok                   # 两个都限定：任一满足即可（cron 惯例）

    def next_after(self, after: datetime) -> datetime:
        """严格晚于 after 的下一个触发时刻（本地 naive 时间，精确到分钟）。"""
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=_SEARCH_DAYS)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        while t <= limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                nh = next((h for h in hours if h > t.hour), None)
                t = (t.replace(hour=nh, minute=0) if nh is not None
                     else t.replace(hour=0, minute=0) + timedelta(days=1))
                continue
            if t.minute not in self.minutes:
                nm = next((m for m in minutes if m > t.minute), None)
                t = (t.replace(minute=nm) if nm is not None
                     else t.replace(minute=0) + timedelta(hours=1))
                continue
            return t
        raise ScheduleError(f"cron 表达式 {self.expr} 在 5 年内都不会触发")


# ------------------------------------------------------------------ 调度对象
def _parse_hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})\s*", str(s or ""))
    if not m:
        raise ScheduleError("时间要写成 HH:MM，例如 09:00")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ScheduleError("时间超出范围（00:00–23:59）")
    return h, mi


def normalize(sched) -> dict:
    """校验并规范化调度对象；不合法抛 ScheduleError（中文原因）。"""
    if not isinstance(sched, dict):
        raise ScheduleError("调度设置缺失")
    kind = sched.get("kind")
    if kind == "interval":
        try:
            n = int(sched.get("minutes"))
        except (TypeError, ValueError):
            raise ScheduleError("间隔分钟数要是整数")
        if not (MIN_INTERVAL <= n <= MAX_INTERVAL):
            raise ScheduleError(f"间隔要在 {MIN_INTERVAL}–{MAX_INTERVAL} 分钟之间")
        return {"kind": "interval", "minutes": n}
    if kind == "daily":
        h, mi = _parse_hhmm(sched.get("time"))
        days = sched.get("weekdays") or []
        if not isinstance(days, list):
            raise ScheduleError("星期几要是列表")
        try:
            wd = sorted({0 if int(d) == 7 else int(d) for d in days})
        except (TypeError, ValueError):
            raise ScheduleError("星期几要是 0–6 的数字（0=周日）")
        if any(d < 0 or d > 6 for d in wd):
            raise ScheduleError("星期几要是 0–6 的数字（0=周日）")
        if len(wd) == 7:
            wd = []
        return {"kind": "daily", "time": f"{h:02d}:{mi:02d}", "weekdays": wd}
    if kind == "cron":
        c = Cron(str(sched.get("expr") or ""))
        c.next_after(datetime.now())             # 永远不会触发的（如 2 月 30 日）这里就报错
        return {"kind": "cron", "expr": c.expr}
    raise ScheduleError("调度类型要是 interval（每隔）/ daily（每天）/ cron 之一")


def _daily_cron(sched: dict) -> Cron:
    h, mi = _parse_hhmm(sched["time"])
    days = sched.get("weekdays") or []
    return Cron(f"{mi} {h} * * {','.join(str(d) for d in days) if days else '*'}")


def next_run(sched: dict, after_ts: float, anchor_ts: float | None = None) -> float:
    """下一次运行的时间戳（严格晚于 after_ts）。
    interval：从 anchor（上次开始运行的时刻，没有就是创建时刻）起每 N 分钟一次，已过去的格子跳过；
    daily / cron：本地时间的下一个匹配时刻。"""
    kind = sched.get("kind")
    if kind == "interval":
        step = int(sched["minutes"]) * 60
        base = anchor_ts if anchor_ts is not None else after_ts
        if base > after_ts:
            return base
        k = int((after_ts - base) // step) + 1
        return base + k * step
    after = datetime.fromtimestamp(after_ts)
    cron = _daily_cron(sched) if kind == "daily" else Cron(sched["expr"])
    return cron.next_after(after).timestamp()


def next_runs(sched: dict, after_ts: float, n: int = 3, anchor_ts: float | None = None) -> list[float]:
    out: list[float] = []
    t = after_ts
    for _ in range(max(1, n)):
        t = next_run(sched, t, anchor_ts)
        out.append(t)
    return out


def describe(sched: dict) -> str:
    """中文调度说明（日志、通知里用；界面另有同逻辑的 TS 版本）。"""
    kind = sched.get("kind")
    if kind == "interval":
        n = int(sched.get("minutes") or 0)
        if n % 1440 == 0:
            return f"每 {n // 1440} 天"
        if n % 60 == 0:
            return f"每 {n // 60} 小时"
        return f"每 {n} 分钟"
    if kind == "daily":
        days = sched.get("weekdays") or []
        if not days:
            return f"每天 {sched.get('time')}"
        if days == [1, 2, 3, 4, 5]:
            return f"工作日 {sched.get('time')}"
        return f"每{'、'.join(_WEEKDAY_CN[d] for d in days)} {sched.get('time')}"
    if kind == "cron":
        return f"cron：{sched.get('expr')}"
    return "未知调度"
