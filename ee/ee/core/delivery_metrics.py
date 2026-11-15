"""交付与成本指标（2026-11 第四批）。

**为什么单独成模块**：`delivery` 与 `usage` 都是逐回合的事实，散在条目里。要判断
"这套改造有没有让模型发挥得更好"，必须先把它们折成四个能互相比较的口径，而且
口径里的边界（未配置回归、无样本、零成功、金额不完整）必须写死，不能每个调用方
自己解释一遍。

四个指标（与设计评审对齐）：
- `verify_rate`：目标工作区复验通过率 = verified / 有交付结论的回合；
- `consistency_error_rate`：报 met 但交付未验证的比例（确定性测试里应为 0）；
- `solved` / `cost_per_solved`：每解决任务成本（分母是 verified 数）；
- `regression_unconfigured_rate`：回归命令未配置的比例（**不是失败**，只是口径缺失）。

注意与 `ee/core/metrics.py` 的区别：那个模块是**模型请求的分段计时**（连接/首字/总时长），
命名相近但用途完全不同；这里只做交付与成本的汇总。
"""
from __future__ import annotations


def _num(value, default=None):
    """容错取值：会话文件是外部可改的，一个坏字段不该让整份指标报错。"""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def turn_sample(item: dict) -> dict | None:
    """`turn_end` 条目 → 一条样本。没有 `delivery` 的回合（普通对话）不参与统计。"""
    if not isinstance(item, dict) or item.get("type") != "turn_end":
        return None
    delivery = item.get("delivery") or {}
    if not isinstance(delivery, dict) or not delivery:
        return None
    usage = item.get("usage") or {}
    reg = delivery.get("regression") or {}
    status = str(delivery.get("status") or "")
    reason = item.get("reason")
    cost = usage.get("cost")
    return {
        "reason": reason,
        "delivery": status,
        "verified": status == "verified",
        # 是否适用"独立验收"口径：unverified（无判据的证据审阅）不进复验通过率的分母。
        "applicable": status in ("verified", "failed", "needs_review"),
        "reported_met": reason == "met",
        "regression": str(reg.get("status") or ""),
        "cost": _num(cost),
        "cost_complete": bool(usage.get("cost_complete", False)),
        "currency": str(usage.get("currency") or ""),
        "requests": _int(usage.get("requests")),
    }


def aggregate(items: list[dict] | None) -> dict:
    """把一批 `turn_end` 条目折成四个指标。所有除零都返回 None，并给出 `notes` 说明原因。"""
    samples = [s for s in (turn_sample(x) for x in (items or [])) if s]
    notes: list[str] = []
    n = len(samples)
    applicable = [s for s in samples if s["applicable"]]
    unverified = sum(1 for s in samples if s["delivery"] == "unverified")
    solved = sum(1 for s in samples if s["verified"])
    met = [s for s in samples if s["reported_met"]]
    inconsistent = sum(1 for s in met if not s["verified"])
    unconfigured = sum(1 for s in samples if s["regression"] == "unconfigured")

    costs = [s["cost"] for s in samples if s["cost"] is not None]
    currencies = {s["currency"] for s in samples if s["cost"] is not None}
    cost_total: float | None
    if not costs:
        cost_total = None
        notes.append("没有可用的金额（模型未配单价或用量缺失）")
    elif len(currencies) > 1:
        cost_total = None
        notes.append(f"金额跨了多个币种（{'、'.join(sorted(currencies))}），不做合计")
    else:
        cost_total = round(sum(costs), 6)
    cost_complete = bool(samples) and all(s["cost_complete"] for s in samples)
    missing = sum(1 for s in samples if s["cost"] is None)
    if missing:
        notes.append(f"{missing} 个回合没有金额，合计只是已知部分")

    if n == 0:
        notes.append("无样本：还没有带交付结论的回合")
    if unverified:
        notes.append(f"{unverified} 个回合没有独立判据（完成但未独立验收），不计入复验通过率分母")
    if len(applicable) != n:
        notes.append(f"复验通过率的分母是有独立判据的 {len(applicable)} 个回合；"
                     f"每解决成本按全部 {n} 个回合的支出除以已复验通过数")
    if met and inconsistent == 0:
        notes.append("成功状态一致")
    if solved == 0 and n:
        notes.append("没有已复验通过的任务：不给出「平均每个任务的成本」")
    if unconfigured:
        notes.append(f"{unconfigured} 个回合未配置回归命令（口径缺失，不算失败）")

    return {
        "samples": n,
        "applicable": len(applicable),
        "unverified": unverified,
        "solved": solved,
        "verify_rate": None if not applicable else round(solved / len(applicable), 4),
        "reported_met": len(met),
        "inconsistent": inconsistent,
        "consistency_error_rate": None if not met else round(inconsistent / len(met), 4),
        "regression_unconfigured": unconfigured,
        "cost_total": cost_total,
        "cost_complete": cost_complete,
        "cost_per_solved": None if solved == 0 or cost_total is None else round(cost_total / solved, 6),
        "notes": notes,
    }
