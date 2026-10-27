"""只读/计划模式默认开沙箱：白名单不是安全边界，写侧必须由 ACL 兜住。

背景（本轮查证）：只读模式的命令白名单是"防误操作"用的，不是安全边界。
PowerShell 管道操作符的成员调用已经修了三轮（裸词 / 冒号 / 动态表达式），
但静态分析证明不了"没有第四种语法"。真正的强制隔离是 [sandbox]（受限令牌 +
能力 SID ACE），它限制的是**写**——而只读模式的威胁正是写（删文件、改配置）。

所以：只读/计划模式下，即使 [sandbox] enabled=false，也默认启用沙箱。
用户显式写了 sandbox.enabled=true 本来就开；显式写 "readonly" 之外的策略仍可覆盖
（见 readonly_default 配置，默认 True）。

这条不变量要在"计划生成"这一层钉住：sandbox.plan() 收到的 data 决定一切。
"""
from pathlib import Path

import pytest

from ee.core import sandbox as SB


def _data(enabled=None, readonly_default=None, network=None):
    sb = {}
    if enabled is not None:
        sb["enabled"] = enabled
    if readonly_default is not None:
        sb["readonly_default"] = readonly_default
    if network is not None:
        sb["network"] = network
    return {"sandbox": sb}


# ---------------------------------------------------------------- settings 层


def test_settings_exposes_readonly_default():
    """配置项必须有默认值（默认 True），否则调用方读不到。"""
    s = SB.settings({})
    assert "readonly_default" in s
    assert s["readonly_default"] is True


def test_settings_can_turn_it_off():
    s = SB.settings(_data(readonly_default=False))
    assert s["readonly_default"] is False


# ---------------------------------------------------------------- plan 层


def test_readonly_mode_gets_sandbox_even_when_disabled(tmp_path, monkeypatch):
    """只读模式 + enabled=false → 仍然给沙箱计划（active 取决于平台，但不能再返回 None）。"""
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())
    p = SB.plan(_data(enabled=False), tmp_path, readonly=True)
    assert p is not None, "只读模式必须拿到沙箱计划"
    assert p.active is True


def test_plan_mode_gets_sandbox_even_when_disabled(tmp_path, monkeypatch):
    """计划模式同理（两者都在 READONLY_MODES 里）。"""
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())
    assert SB.plan(_data(enabled=False), tmp_path, readonly=True) is not None


def test_default_mode_still_honors_disabled(tmp_path):
    """默认模式（非只读）不受影响：enabled=false 仍然不开——不能悄悄改变普通模式的行为。"""
    assert SB.plan(_data(enabled=False), tmp_path, readonly=False) is None


def test_readonly_default_can_be_disabled(tmp_path):
    """用户显式关掉 readonly_default 时，只读模式也回到"不开"。"""
    assert SB.plan(_data(enabled=False, readonly_default=False), tmp_path, readonly=True) is None


def test_explicit_enabled_wins(tmp_path, monkeypatch):
    """显式 enabled=true 在两种模式下都开。"""
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())
    assert SB.plan(_data(enabled=True), tmp_path, readonly=False) is not None
    assert SB.plan(_data(enabled=True), tmp_path, readonly=True) is not None


# ---------------------------------------------------------------- 工具层接线


def test_shell_tool_passes_readonly_flag(tmp_path, monkeypatch):
    """shell 工具要把它所在会话的模式传给 plan()，否则上面那层永远走不到。"""
    from ee.tools import shell as SH

    seen = {}

    def fake_plan(data, workspace, readonly=False):
        seen["readonly"] = readonly
        return None

    monkeypatch.setattr(SH.SB, "plan", fake_plan)

    class FakePolicy:
        mode = "read-only"

    class FakeSession:
        policy = FakePolicy()

    cfg = type("Cfg", (), {"data": {}})()
    from ee.tools.base import ToolContext, ToolState
    ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path,
                      extra={"config": cfg, "session": FakeSession()})
    SH.sandbox_plan(ctx)
    assert seen["readonly"] is True

    FakePolicy.mode = "default"
    SH.sandbox_plan(ctx)
    assert seen["readonly"] is False
