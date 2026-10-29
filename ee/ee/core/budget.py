"""花费上限与告警（D5-9）。

配置 [budget]：session / daily / monthly（0 = 不限）、action = warn（仅提醒）| pause（暂停并询问是否继续）、
warn_ratio（默认 0.8）、currency（默认 ¥，与模型单价的币种一致才计入）。

- 每日 / 每月花费跨会话、跨进程累计在 ~/.ee/state/spend.json：进程内锁 + 锁文件，临时文件 + os.replace 原子写。
  日期按本机时区（Windows 上即系统时区）。
- 每次模型请求前检查（主会话每一步、子代理每一步）：到 warn_ratio 提示一次；到 100% 时
  warn → 提示一次后继续；pause → 发审批卡片问用户（继续这一回合 / 本会话不再询问 / 停止）。
- 无人值守（ee exec、自动化）到 100% 一律停止，原因写进 stop_reason（运行记录里能看到）。
- 主会话和它的子代理共用一个 BudgetGuard：并行子代理同时到线时只问一次。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

SCOPES = ("session", "daily", "monthly")
LABEL = {"session": "本会话", "daily": "今日", "monthly": "本月"}
ACTIONS = ("warn", "pause")
KEEP_DAYS = 62
KEEP_MONTHS = 24
_PLOCK = threading.Lock()


def settings(data: dict) -> dict:
    b = dict((data or {}).get("budget") or {})
    out = {}
    for k in SCOPES:
        try:
            v = float(b.get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        out[k] = max(0.0, v)
    out["action"] = b.get("action") if b.get("action") in ACTIONS else "warn"
    try:
        r = float(b.get("warn_ratio") or 0.8)
    except (TypeError, ValueError):
        r = 0.8
    out["warn_ratio"] = min(0.99, max(0.1, r))
    out["currency"] = str(b.get("currency") or "¥").strip() or "¥"
    out["unattended"] = bool(b.get("unattended", False))
    return out


def day_key(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def month_key(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m")


class _FileLock:
    """跨进程互斥：O_EXCL 建锁文件；超过 stale 秒的锁视为残留并清掉。拿不到锁超时后照样继续（宁可少算不卡住）。"""

    def __init__(self, path: Path, timeout: float = 5.0, stale: float = 30.0):
        self.path, self.timeout, self.stale = path, timeout, stale
        self.fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    return self
                time.sleep(0.02)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None
                try:
                    self.path.unlink()
                except OSError:
                    pass


class Ledger:
    """每日 / 每月花费账本：{"days": {"2026-10-26": {"¥": 1.2}}, "months": {"2026-10": {"¥": 3.4}}}。"""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time):
        self.path = Path(home) / "state" / "spend.json"
        self.clock = clock

    def _read(self) -> dict:
        for i in range(20):
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.setdefault("days", {})
                    d.setdefault("months", {})
                    return d
                break
            except PermissionError:           # Windows：另一个进程正在 replace，稍等再读
                time.sleep(0.02 * (i + 1))
            except (OSError, ValueError):
                break
        return {"days": {}, "months": {}}

    def _write(self, d: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        for i in range(20):                   # Windows：别的进程正好在读时 replace 会 PermissionError，稍等重试
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.02 * (i + 1))
        os.replace(tmp, self.path)

    def add(self, cost: float, currency: str = "¥", when: float | None = None) -> None:
        if not cost or cost <= 0:
            return
        t = self.clock() if when is None else when
        dk, mk = day_key(t), month_key(t)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _PLOCK, _FileLock(self.path.with_name(self.path.name + ".lock")):
            d = self._read()
            day = d["days"].setdefault(dk, {})
            day[currency] = round(float(day.get(currency, 0.0)) + cost, 10)
            mon = d["months"].setdefault(mk, {})
            mon[currency] = round(float(mon.get(currency, 0.0)) + cost, 10)
            for key, keep in (("days", KEEP_DAYS), ("months", KEEP_MONTHS)):
                for old in sorted(d[key])[:-keep]:
                    d[key].pop(old, None)
            self._write(d)

    def totals(self, currency: str = "¥", when: float | None = None) -> tuple[float, float]:
        t = self.clock() if when is None else when
        d = self._read()
        return (float((d["days"].get(day_key(t)) or {}).get(currency, 0.0)),
                float((d["months"].get(month_key(t)) or {}).get(currency, 0.0)))


Ask = Callable[[str, str], Awaitable[str]]      # (scope, 说明) → "once" | "always" | "deny"


class BudgetGuard:
    def __init__(self, data: dict, home: Path, clock: Callable[[], float] = time.time):
        self.cfg = settings(data)
        self.ledger = Ledger(home, clock)
        self.warned: set[tuple[str, str, str]] = set()     # (scope, warn|full, 周期) 已提示过
        self.allowed: set[str] = set()                     # 本会话不再询问的范围
        self.allowed_turn: set[str] = set()                # 本回合继续的范围
        self.denied: set[str] = set()                      # 本回合用户选了停止的范围
        self.stop_reason: str | None = None
        # 已花金额是否可信（False = 有请求花了钱但金额不可知：缺 usage 或缺单价）。
        # 由调用方在记账时设置；默认 True 以保证既有行为不变。
        self.accounting_complete: bool = True
        self._incomplete_warned: set[tuple[str, str]] = set()
        self._lock: asyncio.Lock | None = None

    def reload(self, data: dict) -> None:
        unattended = self.cfg.get("unattended")
        self.cfg = settings(data)
        self.cfg["unattended"] = self.cfg["unattended"] or unattended

    @property
    def active(self) -> bool:
        return any(self.cfg[k] > 0 for k in SCOPES)

    def new_turn(self) -> None:
        self.allowed_turn.clear()
        self.denied.clear()
        self.stop_reason = None

    def record(self, cost: float | None, currency: str) -> None:
        if cost:
            try:
                self.ledger.add(float(cost), currency or "¥")
            except OSError:
                pass

    def status(self, session_spent: float) -> dict:
        cur = self.cfg["currency"]
        day, mon = self.ledger.totals(cur)
        spent = {"session": session_spent, "daily": day, "monthly": mon}
        return {"currency": cur, "action": self.cfg["action"], "warn_ratio": self.cfg["warn_ratio"],
                "scopes": {k: {"spent": round(spent[k], 6), "limit": self.cfg[k]} for k in SCOPES}}

    def _period(self, scope: str) -> str:
        t = self.ledger.clock()
        return day_key(t) if scope == "daily" else month_key(t) if scope == "monthly" else ""

    async def check(self, session_spent: Callable[[str], float], notify: Callable[[str], None], ask: Ask) -> str | None:
        """模型请求前调用。返回 None 继续；返回字符串 = 停止的原因。"""
        if not self.active:
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:                     # 并行子代理同时到线：只问一次
            return await self._check(session_spent, notify, ask)

    async def _check(self, session_spent, notify, ask) -> str | None:
        cfg = self.cfg
        cur = cfg["currency"]
        day, mon = await asyncio.to_thread(self.ledger.totals, cur)
        spent = {"session": session_spent(cur), "daily": day, "monthly": mon}
        incomplete = not self.accounting_complete
        for scope in SCOPES:
            limit = cfg[scope]
            if limit <= 0:
                continue
            used = spent[scope]
            period = self._period(scope)
            text = f"{LABEL[scope]}花费 {cur}{used:.4f}，上限 {cur}{limit:g}"
            if incomplete:
                # 缺 usage / 未知单价时，已知金额小于真实花费。无人值守不能当 0 继续放行，
                # 否则用户设的上限形同虚设；有人值守只提醒，不擅自停机。
                if cfg.get("unattended"):
                    self.stop_reason = (
                        f"{LABEL[scope]}花费金额不可知（有请求缺用量或未配单价），"
                        f"无法确认是否在 {cur}{limit:g} 的内限额内，无人值守运行已停止。"
                    )
                    return self.stop_reason
                key = (scope, period)
                if key not in self._incomplete_warned:
                    self._incomplete_warned.add(key)
                    notify(f"{LABEL[scope]}花费金额不完整（有请求缺用量或未配单价），"
                           f"已花 {cur}{used:.4f} 只是已知部分，可能高于实际消费。")
            if used >= limit:
                if scope in self.allowed or scope in self.allowed_turn:
                    continue
                if cfg.get("unattended"):
                    self.stop_reason = f"{text}：已达到花费上限，无人值守运行已停止。"
                    return self.stop_reason
                if scope in self.denied:
                    self.stop_reason = f"{text}：已达到花费上限，已停止。"
                    return self.stop_reason
                if cfg["action"] == "warn":
                    key = (scope, "full", period)
                    if key not in self.warned:
                        self.warned.add(key)
                        notify(f"{text}：已达到花费上限（设置为仅提醒，继续运行）。")
                    continue
                choice = await ask(scope, f"{text}：已达到花费上限，是否继续？")
                if choice == "always":
                    self.allowed.add(scope)
                elif choice == "once":
                    self.allowed_turn.add(scope)
                else:
                    self.denied.add(scope)
                    self.stop_reason = f"{text}：已达到花费上限，已按你的选择停止。"
                    return self.stop_reason
            elif used >= limit * cfg["warn_ratio"]:
                key = (scope, "warn", period)
                if key not in self.warned:
                    self.warned.add(key)
                    notify(f"{text}：已用到 {int(cfg['warn_ratio'] * 100)}%。")
        return None
