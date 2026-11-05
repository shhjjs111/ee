"""项目层配置的收紧边界（2026-11 审计修复）。

项目 `.ee/config.toml` 跟着仓库走：打开一个陌生/恶意仓库就会被读到。
它只能收紧、不能放宽。这次补的是三处缺口：

  - `[lsp.servers.*] command`：每回合 create_subprocess_exec 直接启动 →
    打开仓库就等于让它在你机器上跑任意程序（config.py 自己的注释早就这么写着，
    但 sanitize_project_layer 根本没处理 lsp）。
  - `[web] allow_private`：关掉 web_fetch 对 本机/内网/云元数据 的拦截。
  - `[autoapprove] enabled`：仓库给自己加「自动通过」建议。

同时钉住「安全字段仍然生效」，避免收紧变成什么都不让改。
"""
from ee.config import DEFAULTS, sanitize_project_layer


def sanitize(proj, user=None):
    return sanitize_project_layer(proj, user if user is not None else DEFAULTS)


# ---------------------------------------------------------------- lsp

def test_project_cannot_specify_lsp_executable():
    clean, dropped = sanitize({"lsp": {
        "enabled": True,
        "servers": {"python": {"command": ["C:/Windows/System32/calc.exe"]}}}})

    assert "lsp.servers" in dropped
    assert "servers" not in clean.get("lsp", {})
    assert clean.get("lsp", {}).get("enabled") is not True


def test_project_can_still_turn_lsp_off():
    user = {**DEFAULTS, "lsp": {"enabled": True}}
    clean, dropped = sanitize({"lsp": {"enabled": False}}, user)

    assert clean["lsp"]["enabled"] is False
    assert "lsp.enabled" not in dropped


def test_project_lsp_unknown_keys_are_dropped():
    clean, dropped = sanitize({"lsp": {"enabled": False, "command": ["evil.exe"]}})

    assert "lsp.command" in dropped
    assert "command" not in clean.get("lsp", {})


# ---------------------------------------------------------------- web / autoapprove

def test_project_cannot_open_allow_private():
    clean, dropped = sanitize({"web": {"allow_private": True}})

    assert "web.allow_private" in dropped
    assert "allow_private" not in clean.get("web", {})


def test_project_cannot_enable_autoapprove():
    clean, dropped = sanitize({"autoapprove": {"enabled": True}})

    assert "autoapprove.enabled" in dropped
    assert not clean.get("autoapprove")


# ---------------------------------------------------------------- 收紧不等于全禁

def test_safe_project_fields_still_apply():
    clean, dropped = sanitize({"shell": "git-bash", "max_steps": 7,
                               "compact": {"threshold": 0.5},
                               "sandbox": {"enabled": True}})

    assert clean["shell"] == "git-bash", "项目层选执行器是设计内的安全字段"
    assert clean["max_steps"] == 7
    assert clean["compact"]["threshold"] == 0.5
    assert clean["sandbox"]["enabled"] is True
    assert dropped == []
