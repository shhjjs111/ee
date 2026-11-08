"""全力以赴的火力推导（allout 阶段 2）。

守三件事：
- 没有判据时强制单路、零修复、标注未验证（不许假装火力全开）；
- 每个取值都由模型档案与机器钳制出来，`reasons` 必须非空、能向用户解释；
- 模型不换：这里只读 profile 字段，没有任何换模型的入口。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ee.core.allout import derive, max_effort


def _profile(**kw):
    base = {"effort_param": "", "effort_levels": (), "context_window": 128000, "temperature": None}
    base.update(kw)
    return SimpleNamespace(**base)


DEEPSEEK = _profile(effort_param="reasoning_effort", effort_levels=("low", "max"), context_window=1_000_000)
GEMINI = _profile(effort_param="reasoning_effort", effort_levels=("low", "medium", "high"), context_window=1_000_000)
GPT = _profile(effort_param="reasoning_effort", effort_levels=("low", "medium", "high", "max"), context_window=272_000)
CLAUDE = _profile(context_window=200_000)                      # 不支持调强度
KIMI = _profile(effort_param="reasoning_effort", effort_levels=("low", "high", "max"), context_window=262_144)
SMALL_CTX = _profile(effort_param="reasoning_effort", effort_levels=("low", "max"), context_window=32_000)


# ---------------------------------------------------------------- effort


@pytest.mark.parametrize("profile,want", [
    (DEEPSEEK, "max"), (GEMINI, "high"), (GPT, "max"), (CLAUDE, ""), (KIMI, "max"), (SMALL_CTX, "max"),
])
def test_max_effort_per_profile(profile, want):
    assert max_effort(profile) == want


def test_unsupported_model_reason_says_default():
    plan = derive(config={}, profile=CLAUDE, has_goal=True, cpu_count=16)
    assert plan.effort == ""
    assert any("不支持调节" in r for r in plan.reasons)


# ---------------------------------------------------------------- 降级


def test_no_goal_forces_single_lane_unverified():
    plan = derive(config={}, profile=DEEPSEEK, has_goal=False, cpu_count=16)
    assert plan.candidates == 1
    assert plan.repair_rounds == 0
    assert plan.max_rounds == 1
    assert plan.verified is False
    assert plan.degraded is True
    assert any("未验证" in d for d in plan.degradations)
    assert any("目标" in d for d in plan.degradations), "降级要告诉用户去哪里手填"


# ---------------------------------------------------------------- 路数钳制


@pytest.mark.parametrize("cpu,want", [(16, 3), (8, 2), (4, 1), (2, 1), (64, 3)])
def test_candidates_clamped_by_cpu(cpu, want):
    plan = derive(config={}, profile=DEEPSEEK, has_goal=True, cpu_count=cpu, max_parallel=8)
    assert plan.candidates == want


def test_candidates_clamped_by_context():
    plan = derive(config={}, profile=SMALL_CTX, has_goal=True, cpu_count=16)
    assert plan.candidates == 1
    assert any("上下文" in r for r in plan.reasons)


def test_candidates_clamped_by_gate():
    plan = derive(config={}, profile=DEEPSEEK, has_goal=True, cpu_count=16, max_parallel=1)
    assert plan.candidates == 1
    assert any("闸门" in r for r in plan.reasons)


def test_gate_defaults_from_config():
    plan = derive(config={"subagent": {"max_parallel": 2}}, profile=DEEPSEEK, has_goal=True, cpu_count=16)
    assert plan.candidates == 2


# ---------------------------------------------------------------- 温度阶梯


def test_temperature_ladder_follows_candidates():
    plan = derive(config={}, profile=DEEPSEEK, has_goal=True, cpu_count=16)
    assert plan.candidates == 3
    assert plan.temperature_ladder == (0.0, 0.3, 0.6)


def test_profile_temperature_goes_first():
    prof = _profile(effort_param="reasoning_effort", effort_levels=("low", "max"),
                    context_window=1_000_000, temperature=0.7)
    plan = derive(config={}, profile=prof, has_goal=True, cpu_count=16)
    assert plan.temperature_ladder[0] == 0.7
    assert 0.7 not in plan.temperature_ladder[1:]
    assert len(plan.temperature_ladder) == 3


# ---------------------------------------------------------------- 解释与序列化


def test_reasons_always_explain_every_value():
    for prof in (DEEPSEEK, GEMINI, GPT, CLAUDE, KIMI, SMALL_CTX):
        plan = derive(config={}, profile=prof, has_goal=True, cpu_count=16)
        assert plan.reasons, "每个 plan 都必须有解释"
        assert any("强度" in r for r in plan.reasons)
        assert any("并行" in r for r in plan.reasons)
        assert any("修复" in r for r in plan.reasons)


def test_max_steps_comes_from_config():
    assert derive(config={"max_steps": 80}, profile=DEEPSEEK, has_goal=True, cpu_count=16).max_steps == 80
    assert derive(config={"max_steps": "bad"}, profile=DEEPSEEK, has_goal=True, cpu_count=16).max_steps == 50


def test_as_dict_round_trips():
    d = derive(config={}, profile=DEEPSEEK, has_goal=True, cpu_count=16).as_dict()
    assert d["candidates"] == 3
    assert d["temperature_ladder"] == [0.0, 0.3, 0.6]
    assert isinstance(d["reasons"], list) and d["reasons"]
    assert d["verified"] is True


def test_model_is_never_changed():
    """守「模型不换」：derive 不接受任何模型名参数，输出里也没有模型字段。"""
    plan = derive(config={}, profile=DEEPSEEK, has_goal=True, cpu_count=16)
    assert not hasattr(plan, "model")
    assert "model" not in plan.as_dict()
