"""全力以赴的配置边界（allout 阶段 2）。

守的是一条安全边界（与 orchestrator 同款）：**陌生仓库不能让 ee 自己打开并行
改代码**。项目级 `.ee/config.toml` 只能关、不能开；`candidates` 只能调小。
"""
from __future__ import annotations

from ee.config import DEFAULTS, sanitize_project_layer


def _user(**kw) -> dict:
    import copy
    u = copy.deepcopy(DEFAULTS)
    u["allout"] = {"enabled": False, "candidates": 3, **kw}
    return u


def test_default_off():
    assert DEFAULTS["allout"]["enabled"] is False
    assert DEFAULTS["allout"]["candidates"] == 3


def test_project_layer_cannot_enable_allout():
    layer = {"allout": {"enabled": True}}
    clean, dropped = sanitize_project_layer(layer, _user())
    assert "enabled" not in (clean.get("allout") or {}), "项目层不能打开 allout"
    assert "allout.enabled" in dropped


def test_project_layer_can_enable_when_user_already_enabled():
    layer = {"allout": {"enabled": True}}
    clean, dropped = sanitize_project_layer(layer, _user(enabled=True))
    assert clean.get("allout", {}).get("enabled") is True
    assert "allout.enabled" not in dropped


def test_project_layer_cannot_raise_candidates():
    layer = {"allout": {"candidates": 6}}
    clean, dropped = sanitize_project_layer(layer, _user())
    assert "candidates" not in (clean.get("allout") or {})
    assert "allout.candidates" in dropped


def test_project_layer_can_lower_candidates():
    layer = {"allout": {"candidates": 2}}
    clean, _ = sanitize_project_layer(layer, _user())
    assert clean.get("allout", {}).get("candidates") == 2


def test_project_layer_non_int_candidates_dropped():
    layer = {"allout": {"candidates": "many"}}
    clean, dropped = sanitize_project_layer(layer, _user())
    assert "candidates" not in (clean.get("allout") or {})
    assert "allout.candidates" in dropped


def test_project_layer_unknown_keys_dropped():
    layer = {"allout": {"enabled": False, "candidates": 1, "model": "gpt-5"}}
    clean, dropped = sanitize_project_layer(layer, _user())
    assert "model" not in (clean.get("allout") or {}), "allout 不接受换模型"
    assert "allout.model" in dropped


def test_project_layer_non_dict_dropped():
    layer = {"allout": True}
    clean, dropped = sanitize_project_layer(layer, _user())
    assert "allout" not in clean
    assert "allout" in dropped


def test_project_layer_can_disable():
    layer = {"allout": {"enabled": False}}
    clean, _ = sanitize_project_layer(layer, _user(enabled=True))
    assert clean.get("allout", {}).get("enabled") is False
