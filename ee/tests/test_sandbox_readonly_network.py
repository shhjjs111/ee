"""只读/计划模式默认禁网（沙箱层面）。

用户决定（2026-10-28）：只读/计划模式下默认关联网——需要联网时用户会明确提要求。

范围要说清楚：
- 这里禁的是**沙箱内 shell 子进程**的网络（环境变量尽力而为：死代理 + 各包管理器离线
  + GIT_SSH_COMMAND=false）；
- `web_search` / `web_fetch` 工具**不经沙箱**，不受影响——那是用户主动发起的联网搜索，
  属于明确要求，不算被这条默认挡住的场景。

配置：`[sandbox] readonly_network_default`（默认 False = 只读模式禁网）。
显式 `[sandbox] network = false` 本来就禁；显式 `network = true` 时只读模式是否放行，
由 readonly_network_default 决定（默认仍禁，与"默认关"的意图一致）。
"""
from pathlib import Path

import pytest

from ee.core import sandbox as SB


def _data(**sb):
    return {"sandbox": dict(sb)}


# ---------------------------------------------------------------- settings


def test_settings_exposes_readonly_network_default():
    s = SB.settings({})
    assert "readonly_network_default" in s
    assert s["readonly_network_default"] is False      # 默认关（= 只读模式禁网）


def test_settings_can_enable_it():
    s = SB.settings(_data(readonly_network_default=True))
    assert s["readonly_network_default"] is True


# ---------------------------------------------------------------- plan 层


def _fake_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())


def test_readonly_mode_blocks_network_by_default(tmp_path, monkeypatch):
    """只读模式：即使 [sandbox] network=true，也默认禁网。"""
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=True), tmp_path, readonly=True)
    assert p is not None
    assert p.network is False, "只读模式默认应禁网"


def test_plan_mode_blocks_network_by_default(tmp_path, monkeypatch):
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=True), tmp_path, readonly=True)
    assert p is not None and p.network is False


def test_default_mode_keeps_network_setting(tmp_path, monkeypatch):
    """非只读模式不受影响：network=true 就是允许联网。"""
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=True), tmp_path, readonly=False)
    assert p is not None and p.network is True


def test_readonly_can_opt_in_to_network(tmp_path, monkeypatch):
    """用户显式放行时，只读模式也能联网。"""
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=True, readonly_network_default=True), tmp_path, readonly=True)
    assert p is not None and p.network is True


def test_explicit_network_false_still_wins_in_default_mode(tmp_path, monkeypatch):
    """显式 network=false 在普通模式也照样禁。"""
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=False), tmp_path, readonly=False)
    assert p is not None and p.network is False


# ---------------------------------------------------------------- 环境变量真的生效


def test_readonly_plan_env_has_dead_proxy(tmp_path, monkeypatch):
    """禁网不能只写在字段上——真正跑命令时环境变量里要有拦截。"""
    _fake_windows(monkeypatch, tmp_path)
    p = SB.plan(_data(enabled=True, network=True), tmp_path, readonly=True)
    env = p.env({"PATH": "/usr/bin"})
    assert env.get("HTTP_PROXY") == SB.DEAD_PROXY
    assert env.get("EE_SANDBOX_NO_NETWORK") == "1"


# ---------------------------------------------------------------- web 工具不受影响


def test_web_tools_do_not_go_through_sandbox():
    """web_search / web_fetch 不经沙箱——只读模式下的联网搜索仍然可用。"""
    import ee.tools.web as WEB
    src = __import__("inspect").getsource(WEB)
    assert "sandbox" not in src.lower(), "web 工具不该引用沙箱；否则只读模式会连搜索都用不了"
