"""D3-C：config.toml 版本迁移、search_fallback 设置、模型价格 variants。"""
import tomllib

import pytest
from conftest import run

from ee.config import load_config, load_user_file
from ee.core.migrate import CURRENT_VERSION, migrate_user_file
from ee.core.providers.profiles import profile_for

OLD = """# 我的配置（注释要保留）
default_model = "deepseek-flash"

[web]
search_backend = "deepseek"  # 旧默认
fetch_backend = "auto"
timeout = 30

[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
"""


def test_migrate_v2_rewrites_and_keeps_comments(ee_home):
    cfg = ee_home / "config.toml"
    cfg.write_text(OLD, encoding="utf-8")
    assert migrate_user_file(cfg) is True
    text = cfg.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["config_version"] == CURRENT_VERSION
    assert data["web"]["search_backend"] == "exa" and data["web"]["fetch_backend"] == "direct"
    assert data["web"]["timeout"] == 30
    assert "# 我的配置（注释要保留）" in text
    bak = ee_home / f"config.toml.pre-v{CURRENT_VERSION}.bak"
    assert bak.read_text(encoding="utf-8") == OLD
    # 再跑一次：不改动
    assert migrate_user_file(cfg) is False


def test_migrated_file_respects_user_choice_later(ee_home):
    """迁移过（有 config_version）的文件里用户主动选 deepseek，不能再被改回去。"""
    cfg = ee_home / "config.toml"
    cfg.write_text(f'config_version = {CURRENT_VERSION}\n\n[web]\nsearch_backend = "deepseek"\nfetch_backend = "auto"\n',
                   encoding="utf-8")
    assert migrate_user_file(cfg) is False
    w = load_config(ee_home).data["web"]
    assert w["search_backend"] == "deepseek" and w["fetch_backend"] == "auto"


def test_load_config_runs_migration(ee_home, workspace):
    (ee_home / "config.toml").write_text(OLD, encoding="utf-8")
    w = load_config(workspace).data["web"]
    assert w["search_backend"] == "exa" and w["fetch_backend"] == "direct"
    assert load_user_file()["config_version"] == CURRENT_VERSION


def test_no_file_no_migration(ee_home):
    assert migrate_user_file(ee_home / "config.toml") is False
    assert not (ee_home / "config.toml").exists()


def test_broken_file_does_not_crash(ee_home):
    cfg = ee_home / "config.toml"
    cfg.write_text("this is = = not toml", encoding="utf-8")
    assert migrate_user_file(cfg) is False


def test_settings_set_writes_version_and_fallback(ee_home):
    from ee.server.app import Backend
    b = Backend(home=ee_home, persist=False)
    st = run(b.call("settings/get", {}))["settings"]
    assert st["web"]["search_fallback"] == ["duckduckgo"] and st["web"]["fetch_backend"] == "direct"
    st = run(b.call("settings/set", {"settings": {"web": {"search_backend": "deepseek", "search_fallback": []}}}))["settings"]
    assert st["web"]["search_backend"] == "deepseek" and st["web"]["search_fallback"] == []
    data = tomllib.loads((ee_home / "config.toml").read_text(encoding="utf-8"))
    assert data["config_version"] == CURRENT_VERSION
    # 重新加载不会被迁移改回 exa
    assert load_config(ee_home).data["web"]["search_backend"] == "deepseek"
    st = run(b.call("settings/set", {"settings": {"web": {"search_fallback": ["exa", "duckduckgo", "exa"]}}}))["settings"]
    assert st["web"]["search_fallback"] == ["exa", "duckduckgo"]
    with pytest.raises(Exception, match="."):
        run(b.call("settings/set", {"settings": {"web": {"search_fallback": ["google"]}}}))
    with pytest.raises(Exception, match="."):
        run(b.call("settings/set", {"settings": {"web": {"search_fallback": ["off"]}}}))


def test_price_variants():
    flash = profile_for("deepseek-flash").price
    pro = profile_for("deepseek-v4-pro").price
    assert (flash.input, flash.output, flash.cache_read) == (2.0, 8.0, 0.04)
    assert (pro.input, pro.output, pro.cache_read) == (9.0, 27.0, 0.30)
    # 用户在设置里填的单价仍然优先，且只覆盖填了的键
    p = profile_for("deepseek-v4-pro", overrides={"price": {"output": 20}}).price
    assert (p.input, p.output) == (9.0, 20)
    # 中转站带前缀的名字也能匹配
    assert profile_for("openrouter/deepseek-v4-pro").price.output == 27.0


def test_settings_default_price_shows_variant(ee_home):
    from ee.server.app import Backend
    b = Backend(home=ee_home, persist=False)
    models = run(b.call("settings/get", {}))["settings"]["models"]
    assert models["deepseek-pro"]["default_price"]["output"] == 27.0
    assert models["deepseek-flash"]["default_price"]["output"] == 8.0
