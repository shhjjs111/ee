"""Wave 6 / T20：成本驱动的上下文策略——影子模式（只记录，不执行）。

计划原文的硬约束：
- 现有"修剪默认关闭"的取舍合理（改写早期历史会让后续按未命中计费），新策略**不得默认开启**；
- 决策输入缺失时**保守维持现状**；
- **先影子模式**：只记录"本可以怎么做"，不执行；
- 只有配对评测证明"成功任务总成本下降且完成率不降"后才在可撤销设置中开启；
- 不以"省 token 数"作唯一标准，以成功任务总成本与人工干预为最终判据。

所以本模块的对外契约是：**观察、记录、可解释，但绝不改变实际行为**。
本文件锁的就是这个契约——任何让影子模式产生副作用的改动都应该让这些测试变红。
"""
from __future__ import annotations

from ee.core import context_policy as P


def _state(**kw):
    base = dict(window=1_000_000, used=600_000, prefix_stable=True, pruned=0, compacted=0,
                failures=0, retries=0, enabled=False)
    base.update(kw)
    return P.Usage(**base)


# ---------------------------------------------------------------- 影子决策：只给建议

def test_shadow_suggests_prune_when_pressure_high():
    """用量超过修剪阈值时，影子建议「本可以修剪」——但只作为建议返回。"""
    d = P.decide(_state(used=700_000, window=1_000_000), threshold=0.6, compact_threshold=0.8)
    assert d.suggest == "prune"
    assert d.reason, "影子决策必须给出可解释的理由"
    assert d.applied is False, "影子模式绝不能真的执行"


def test_shadow_suggests_compact_when_near_window():
    """接近压缩阈值时建议压缩，且理由指向具体占用。"""
    d = P.decide(_state(used=850_000, window=1_000_000), threshold=0.6, compact_threshold=0.8)
    assert d.suggest == "compact"
    assert d.applied is False


def test_shadow_holds_when_prefix_unstable():
    """前缀不稳定时保守：不建改写历史（改写会让后续按未命中计费）。"""
    d = P.decide(_state(used=900_000, prefix_stable=False), threshold=0.6, compact_threshold=0.8)
    assert d.suggest == "hold"
    assert "前缀" in d.reason


def test_shadow_holds_when_failures_high():
    """失败/重试多时保守：此时再改写历史只会让模型更困惑。"""
    d = P.decide(_state(used=700_000, failures=5, retries=8), threshold=0.6, compact_threshold=0.8)
    assert d.suggest == "hold"
    assert "失败" in d.reason or "重试" in d.reason


def test_shadow_is_conservative_when_data_missing():
    """缺少关键数据（window 为 0 或 used 未知）时保守维持现状，不猜。"""
    assert P.decide(_state(window=0, used=0), threshold=0.6, compact_threshold=0.8).suggest == "hold"
    assert P.decide(_state(window=1_000_000, used=0), threshold=0.6, compact_threshold=0.8).suggest == "hold"


def test_shadow_never_applies_even_when_enabled_flag_true():
    """即便传入 enabled=True，decide() 也只是给建议——真正执行由调用方另走开关。

    这是分层：decide 是纯决策，执行是另一层的事。影子模式的价值正是不执行。
    """
    d = P.decide(_state(used=700_000, enabled=True), threshold=0.6, compact_threshold=0.8)
    assert d.applied is False


# ---------------------------------------------------------------- 记录：可解释、可回放

def test_record_is_plain_dict_for_replay():
    """记录可直接序列化（写进 JSONL 回放/分析），不含对象引用。"""
    import json
    d = P.decide(_state(used=700_000), threshold=0.6, compact_threshold=0.8)
    rec = d.as_record()
    text = json.dumps(rec, ensure_ascii=False)     # 不能抛
    back = json.loads(text)
    assert back["suggest"] == d.suggest
    assert back["applied"] is False
    assert "reason" in back and "used_ratio" in back


def test_record_captures_inputs_for_audit():
    """记录要带上决策依据，否则事后无法复核「当时为什么这么建议」。"""
    rec = P.decide(_state(used=700_000, window=1_000_000, pruned=100, compacted=200),
                   threshold=0.6, compact_threshold=0.8).as_record()
    assert rec["used_ratio"] == 0.7
    assert rec["threshold"] == 0.6
    assert rec["compact_threshold"] == 0.8
    assert rec["pruned"] == 100 and rec["compacted"] == 200
    assert rec["prefix_stable"] is True


# ---------------------------------------------------------------- 默认关闭（T21）

def test_policy_disabled_by_default():
    """新策略默认关闭：不给老用户带来任何行为变化。"""
    cfg = P.settings({})
    assert cfg["enabled"] is False, "影子策略必须默认关闭"


def test_settings_reads_config_and_clamps():
    """配置可开启，但阈值被夹到合法区间（不能把修剪阈值设到压缩之后）。"""
    cfg = P.settings({"context_policy": {"enabled": True, "threshold": 0.95}})
    assert cfg["enabled"] is True
    assert cfg["threshold"] <= 0.8 - 0.05, "修剪阈值必须在压缩阈值之前"


def test_settings_tolerates_garbage():
    """坏配置不崩、回落到默认（老 config.toml 里不会有这一段）。"""
    for bad in ({"context_policy": None}, {"context_policy": {"threshold": "abc"}},
                {"context_policy": {"enabled": "yes"}}):
        cfg = P.settings(bad)
        assert isinstance(cfg["threshold"], float)
        assert cfg["enabled"] is False, "非布尔真值不应被当成开启"


# ---------------------------------------------------------------- 接线：默认零副作用（T21）

def test_observe_writes_nothing_when_disabled(tmp_path):
    """默认关闭时，observe() 连日志都不写——老用户行为完全不变。"""
    import asyncio
    from pathlib import Path

    from ee.config import load_config
    from ee.core.session import Session
    from ee.core import context_policy as P

    ws = tmp_path / "ws"
    ws.mkdir()
    s = Session(load_config(ws), cwd=ws, persist=True)
    # persist=True 时 store 有目录；记录一次基线长度
    before = s.store.path.read_text(encoding="utf-8") if s.store.path.exists() else ""
    out = P.observe(s)
    assert out is None, "默认关闭时不应产生决策"
    after = s.store.path.read_text(encoding="utf-8") if s.store.path.exists() else ""
    assert after == before, "默认关闭时不应写任何记录"


def test_observe_records_when_enabled(tmp_path):
    """开启后写一条可回放的记录，且 applied=False（影子不执行）。"""
    from ee.config import load_config
    from ee.core.session import Session
    from ee.core import context_policy as P

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = load_config(ws)
    cfg.data["context_policy"] = {"enabled": True}
    s = Session(cfg, cwd=ws, persist=True)
    s.stats.last_prompt = 700_000
    s.stats.last_completion = 1_000
    s.profile.context_window = 1_000_000
    d = P.observe(s)
    assert d is not None and d.applied is False
    text = s.store.path.read_text(encoding="utf-8")
    assert "context_policy" in text, "开启后应落一条可回放记录"


def test_observe_never_rewrites_history(tmp_path):
    """影子模式绝不动历史——即便用量远超压缩阈值。"""
    import json

    from ee.config import load_config
    from ee.core.session import Session
    from ee.core import context_policy as P

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = load_config(ws)
    cfg.data["context_policy"] = {"enabled": True}
    s = Session(cfg, cwd=ws, persist=True)
    s.history = [{"role": "user", "content": "重要约束不要丢"}]
    before = json.dumps(s.history, ensure_ascii=False)
    s.stats.last_prompt = 990_000
    s.stats.last_completion = 5_000
    s.profile.context_window = 1_000_000
    # 前缀已冻结才进入稳定态（未冻结时 prefix_stable=False，策略本就该保守 hold）
    s.prefix = type("PF", (), {"sha256": "x", "system": {}, "tools": []})()
    d = P.observe(s)
    assert d.suggest == P.SUGGEST_COMPACT        # 建议压缩
    assert json.dumps(s.history, ensure_ascii=False) == before, "影子模式不得改写历史"


# ---------------------------------------------------------------- 安全：项目层不能开启（T21）

def test_project_layer_cannot_enable_policy(tmp_path):
    """项目 .ee/config.toml 不能开启这个实验开关——项目配置只能收紧，不能放宽。

    影子记录虽然只读，但它是实验性行为开关；由仓库内容决定是否记录用户会话，
    属于用户层权限。与 providers/hooks 同级处理。
    """
    from ee.config import sanitize_project_layer, load_config

    user_cfg = load_config(tmp_path / "empty")     # 一个干净的基线
    clean, dropped = sanitize_project_layer(
        {"context_policy": {"enabled": True, "threshold": 0.1}}, user_cfg)
    assert "context_policy" not in clean, "项目层不应能设置 context_policy"
    assert "context_policy" in dropped, "被丢弃的字段要记下来供设置页提示"


# ---------------------------------------------------------------- 回归：前缀未冻结 ≠ 不稳定

def test_fresh_session_without_prefix_still_advises(tmp_path):
    """刚开会话（prefix 未冻结）是正常初始态，不能因此永远 hold。

    真 bug（已修）：observe() 原来用 `prefix is not None` 判稳定，导致所有未冻结
    的会话（绝大多数）永远返回 hold，影子记录收集不到任何有效样本。
    """
    import tempfile
    from pathlib import Path

    from ee.config import load_config
    from ee.core.session import Session
    from ee.core import context_policy as P

    ws = Path(tempfile.mkdtemp())
    cfg = load_config(ws)
    cfg.data["context_policy"] = {"enabled": True}
    s = Session(cfg, cwd=ws, persist=False)
    s.profile.context_window = 131072          # 128K 模型
    assert s.prefix is None, "前提：新会话还没冻结前缀"
    s.stats.last_prompt = 100_000              # 已用 76%，超过修剪阈值 60%
    s.stats.last_completion = 1_000
    d = P.observe(s)
    assert d is not None
    assert d.used_ratio > 0.6, f"应按真实占用算，实际 {d.used_ratio}"
    assert d.suggest != P.SUGGEST_HOLD, (
        f"前缀未冻结是正常初始态，不该永远 hold。实际 {d.suggest}：{d.reason}"
    )


def test_prefix_switch_is_detected_as_unstable(tmp_path):
    """真的换前缀（sha 变了）才算不稳定：此时改写历史代价高，应保守。"""
    import tempfile
    from pathlib import Path

    from ee.config import load_config
    from ee.core.session import Session
    from ee.core import context_policy as P

    ws = Path(tempfile.mkdtemp())
    cfg = load_config(ws)
    cfg.data["context_policy"] = {"enabled": True}
    s = Session(cfg, cwd=ws, persist=False)
    s.profile.context_window = 131072
    s.stats.last_prompt = 100_000
    s.stats.last_completion = 1_000
    # 先有真实前缀 A（首次冻结是正常态，不算切换）
    s.prefix = type("PF", (), {"sha256": "shaA", "system": {}, "tools": []})()
    assert P.observe(s).prefix_stable is True
    # 换成不同前缀 B —— 这才是「前缀不稳定」
    s.prefix = type("PF", (), {"sha256": "shaB", "system": {}, "tools": []})()
    d = P.observe(s)
    assert d.suggest == P.SUGGEST_HOLD
    assert not d.prefix_stable
