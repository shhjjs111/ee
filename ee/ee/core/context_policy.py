"""成本驱动的上下文策略——影子模式（Wave 6 / T20）。

**这个模块只观察和记录，不改变任何实际行为。** 这是刻意的：

改写早期历史（修剪/压缩）会让后续请求按"未命中缓存"计费。本仓库的 DeepSeek 缓存
命中率实测 92-96%，一次改动可能省了 token 却涨了钱。所以计划要求：先记录
"本可以怎么做"，用配对评测证明"成功任务总成本下降且完成率不降"之后，才谈开启。

分层：
- `decide()` 是**纯决策**：输入一份用量快照，输出建议 + 理由，`applied` 恒为 False。
- `settings()` 读配置，默认关闭；坏配置回落默认。
- 实际执行（修剪/压缩）仍在 `prune.py` / `session.py` 的既有开关后面，不受本模块影响。

决策输入只用实测可得的数据；缺失时保守维持现状（`hold`），不猜。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# 保守默认：默认关闭，阈值沿用既有取舍（修剪在压缩之前）
DEFAULTS = {
    "enabled": False,
    "threshold": 0.6,            # 达到窗口这个比例时「本可以修剪」
    "compact_threshold": 0.8,    # 达到这个比例时「本可以压缩」
    "max_failures": 3,           # 失败/重试超过这个数就保守不动
}

# 建议取值
SUGGEST_PRUNE = "prune"
SUGGEST_COMPACT = "compact"
SUGGEST_HOLD = "hold"


def settings(data: dict | None) -> dict:
    """读 [context_policy]，默认关闭；坏值回落默认，不崩。

    只接受真正的布尔 True 才开启——字符串 "yes"/1 都不算（避免配置写错悄悄打开实验）。
    """
    raw = (data or {}).get("context_policy")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)

    comp = 0.8
    compact_cfg = (data or {}).get("compact")
    if isinstance(compact_cfg, dict):
        try:
            comp = float(compact_cfg.get("threshold", 0.8) or 0.8)
        except (TypeError, ValueError):
            comp = 0.8
    out["compact_threshold"] = comp

    if raw.get("enabled") is True:                 # 只认布尔真值
        out["enabled"] = True

    try:
        th = float(raw.get("threshold", DEFAULTS["threshold"]))
    except (TypeError, ValueError):
        th = DEFAULTS["threshold"]
    # 修剪阈值一定落在压缩之前，否则「先修剪后压缩」的既有顺序会被打乱
    out["threshold"] = max(0.05, min(th, comp - 0.05))

    try:
        out["max_failures"] = max(0, int(raw.get("max_failures", DEFAULTS["max_failures"])))
    except (TypeError, ValueError):
        out["max_failures"] = DEFAULTS["max_failures"]
    return out


@dataclass(frozen=True)
class Usage:
    """做决策需要的全部输入。缺失的项用 0/False，由 decide() 保守处理。"""
    window: int = 0              # 模型上下文窗口
    used: int = 0                # 当前占用 token
    prefix_stable: bool = True   # 前缀是否稳定（不稳定时改写历史代价更大）
    pruned: int = 0              # 已修剪累计（token 估计）
    compacted: int = 0           # 已压缩累计
    failures: int = 0            # 最近失败次数
    retries: int = 0             # 最近重试次数
    enabled: bool = False        # 配置开关（仅用于记录，不影响决定是否执行）


@dataclass(frozen=True)
class Decision:
    suggest: str                 # prune | compact | hold
    reason: str                  # 可解释的理由
    applied: bool = False        # 影子模式：恒为 False
    used_ratio: float = 0.0
    threshold: float = 0.0
    compact_threshold: float = 0.0
    pruned: int = 0
    compacted: int = 0
    prefix_stable: bool = True

    def as_record(self) -> dict:
        """可直接写进 JSONL 的纯字典（回放/分析/审计都用它）。"""
        return asdict(self)


def decide(u: Usage, *, threshold: float, compact_threshold: float) -> Decision:
    """给出「本可以怎么做」的建议。纯函数，无副作用，永不执行。"""
    ratio = (u.used / u.window) if u.window > 0 else 0.0

    def hold(reason: str) -> Decision:
        return Decision(suggest=SUGGEST_HOLD, reason=reason, applied=False,
                        used_ratio=round(ratio, 4), threshold=threshold,
                        compact_threshold=compact_threshold, pruned=u.pruned,
                        compacted=u.compacted, prefix_stable=u.prefix_stable)

    # 数据不足：不猜
    if u.window <= 0:
        return hold("缺少上下文窗口大小，无法判断压力，维持现状")
    if u.used <= 0:
        return hold("还没有用量数据，维持现状")
    # 前缀不稳定：改写历史会让后续按未命中计费，代价可能高于收益
    if not u.prefix_stable:
        return hold("前缀不稳定：改写历史会让后续请求按未命中计费，维持现状")
    # 失败/重试多：模型已经不顺，再改写历史更糟
    if u.failures >= 3 or u.retries >= 8:
        return hold(f"最近失败 {u.failures} 次、重试 {u.retries} 次，先维持现状")

    if ratio >= compact_threshold:
        return Decision(suggest=SUGGEST_COMPACT,
                        reason=f"占用 {ratio:.0%} 已达压缩阈值 {compact_threshold:.0%}，本可以压缩",
                        applied=False, used_ratio=round(ratio, 4), threshold=threshold,
                        compact_threshold=compact_threshold, pruned=u.pruned,
                        compacted=u.compacted, prefix_stable=u.prefix_stable)
    if ratio >= threshold:
        return Decision(suggest=SUGGEST_PRUNE,
                        reason=f"占用 {ratio:.0%} 已达修剪阈值 {threshold:.0%}，本可以修剪旧工具输出",
                        applied=False, used_ratio=round(ratio, 4), threshold=threshold,
                        compact_threshold=compact_threshold, pruned=u.pruned,
                        compacted=u.compacted, prefix_stable=u.prefix_stable)
    return hold(f"占用 {ratio:.0%} 未达修剪阈值 {threshold:.0%}，维持现状")


def observe(session, emit=None) -> Decision | None:
    """在会话上加一次影子观察：只记录，不执行。关闭时不做任何事。

    接线点由 session 侧调用；本函数保证：
    - 开关关闭 → 返回 None，零副作用（连统计都不写）；
    - 开关打开 → 写一条 usage_record 到会话日志，供后续配对评测分析。
    """
    cfg = settings(session.config.data)
    if not cfg["enabled"]:
        return None
    stats = session.stats
    prefix = getattr(session, "prefix", None)
    u = Usage(
        window=int(getattr(getattr(session, "profile", None), "context_window", 0) or 0),
        used=int(stats.last_prompt + stats.last_completion),
        prefix_stable=prefix is not None,
        pruned=int(getattr(stats, "pruned_tokens", 0) or 0),
        compacted=int(getattr(stats, "compacted_tokens", 0) or 0),
        enabled=True,
    )
    d = decide(u, threshold=cfg["threshold"], compact_threshold=cfg["compact_threshold"])
    session.store.append("context_policy", d.as_record())
    if emit is not None:
        emit(d)
    return d
