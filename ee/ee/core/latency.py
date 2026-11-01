"""模型接口首包延迟的进程内统计（按 base_url）。

**为什么要有它**：首包慢的时候，用户只看到一个不动的转圈，分不清是「这次特别慢」
还是「这个接口一直这么慢」。实测本机某中转站首包中位 18.8 秒、p90 35.9 秒，而同一天
另一些请求只有 3.8 秒——同一个接口的抖动比接口之间的差距还大。不给参照的数字
（「已等待 15 秒」）只会让人更焦虑，给了中位数才知道该等还是该换接口。

**为什么只放内存，不落盘**：

- 这是「这次运行里这个接口给人的感觉」，重启后重新积累几十次请求就够用；
- 落盘意味着每个测试用例都可能去写用户真实的 `~/.ee`（EE_HOME 没隔离时），
  而这条统计的全部价值只体现在提示文案里，不值得引入这个污染面。
"""
from __future__ import annotations

KEEP = 20          # 每个接口只留最近这么多次
MIN_SAMPLES = 3    # 少于这么多次不给中位：2 个样本算出来的“中位”是误导

_data: dict[str, list[int]] = {}


def key_for(base_url: str | None, model: str = "") -> str:
    """同一个 base_url 的不同模型共用一份统计：排队/网络慢通常是接口级现象。"""
    return (base_url or "").strip() or f"(model){model}"


def record(key: str, ms: int | None) -> None:
    """记一次首包耗时（毫秒）。None / 负数（没测到）不记，避免把“没数据”当成“很快”。"""
    if ms is None or ms < 0:
        return
    bucket = _data.setdefault(key, [])
    bucket.append(int(ms))
    del bucket[:-KEEP]


def samples(key: str) -> int:
    return len(_data.get(key) or [])


def median_ms(key: str) -> int | None:
    """该接口最近的首包中位数；样本不足时返回 None（宁可不给，也不给假参照）。"""
    xs = sorted(_data.get(key) or [])
    if len(xs) < MIN_SAMPLES:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def reset() -> None:
    """清空统计（测试用）。"""
    _data.clear()


def heartbeat_text(alias: str, waited: float, median: int | None) -> str:
    """等待首包期间的状态行。有中位数就带上参照，没有就只说等了多久。"""
    if median:
        return f"{alias} 还没有返回首包，已等待 {waited:.0f} 秒（该接口最近中位 {median / 1000:.1f} 秒）…"
    return f"{alias} 还没有返回首包，已等待 {waited:.0f} 秒…"
