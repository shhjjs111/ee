"""配置文件的作用域：用户级 ~/.ee 绝不能被当成“项目级配置”再叠一遍。

背景：load_config 从 cwd 往上找 .ee/config.toml 当项目级配置。原来的排除条件只比较
ee_home()（可用 EE_HOME 改），所以只要 EE_HOME 指向别处（测试、或用户按文档改了位置），
真实主目录的 ~/.ee/config.toml 就会被当成项目配置叠进来，盖掉用户级/测试注入的设置。

注意：这里**不**去 patch Path.home()——真实主目录照旧存在于 cwd 的祖先链里，
这正是要复现的场景；patch 掉它反而会让测试脱离真实条件。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ee.config import load_config, user_ee_dirs


def test_user_ee_dirs_includes_both_ee_home_and_default(ee_home):
    """排除集必须同时含 EE_HOME 与“未设置 EE_HOME 时的默认 ~/.ee”。"""
    dirs = user_ee_dirs()
    assert ee_home.resolve() in dirs
    assert (Path.home() / ".ee").resolve() in dirs


def test_real_user_config_not_treated_as_project_config(ee_home, workspace):
    """EE_HOME 指向别处时，真实主目录的 ~/.ee/config.toml 不能当项目配置叠进来。"""
    default_ee = Path.home() / ".ee"
    if not (default_ee / "config.toml").is_file():
        pytest.skip("本机没有 ~/.ee/config.toml，构造不出这个场景（CI 上正常）")
    (ee_home / "config.toml").write_text(
        'config_version = 3\n[providers.deepseek]\nbase_url = "https://api.deepseek.com"\n'
        'api_key_env = "EE_TEST_KEY_ENV"\n', encoding="utf-8")

    cfg = load_config(workspace)

    assert (default_ee / "config.toml").resolve() not in {p.resolve() for p in cfg.sources}
    assert cfg.resolve_model().api_key_env == "EE_TEST_KEY_ENV"


def test_ee_home_itself_is_never_project_config(tmp_path, monkeypatch):
    """EE_HOME 就在 cwd 祖先链上时，它仍然只是用户级，不是项目级。"""
    owner = tmp_path / "owner"
    ee_home = owner / ".ee"
    ee_home.mkdir(parents=True)
    monkeypatch.setenv("EE_HOME", str(ee_home))
    (ee_home / "config.toml").write_text('default_model = "deepseek-pro"\n', encoding="utf-8")
    proj = owner / "proj"
    (proj / ".ee").mkdir(parents=True)
    (proj / ".ee" / "config.toml").write_text('shell = "git-bash"\n', encoding="utf-8")

    cfg = load_config(proj)

    assert cfg.default_model == "deepseek-pro"      # 用户级生效
    assert cfg.data["shell"] == "git-bash"          # 项目级生效
    configs = [p.resolve() for p in cfg.sources if p.name == "config.toml"]
    assert configs.count((ee_home / "config.toml").resolve()) == 1      # 用户级只叠一次
    assert configs.count((proj / ".ee" / "config.toml").resolve()) == 1  # 项目级只叠一次
    assert configs.index((proj / ".ee" / "config.toml").resolve()) > configs.index(
        (ee_home / "config.toml").resolve())                             # 项目级在后，覆盖用户级
    assert cfg.sources[-1] == proj / ".ee" / "config.toml"


def test_project_config_loaded_once_from_subdir(tmp_path):
    """项目子目录里开对话时，上层的 .ee/config.toml 只叠一次。"""
    proj = tmp_path / "proj2"
    (proj / ".ee").mkdir(parents=True)
    (proj / ".ee" / "config.toml").write_text('shell = "git-bash"\n', encoding="utf-8")
    deeper = proj / "a" / "b"
    deeper.mkdir(parents=True)

    cfg = load_config(deeper)

    target = (proj / ".ee" / "config.toml").resolve()
    assert [p.resolve() for p in cfg.sources if p.name == "config.toml"].count(target) == 1
    assert cfg.data["shell"] == "git-bash"
