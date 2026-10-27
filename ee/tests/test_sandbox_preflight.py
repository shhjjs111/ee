"""沙箱的可用性预检：起不来的时候要说清楚，不能让用户只看到"拒绝访问。"。

背景（真实探针发现）：只读/计划模式现在默认开沙箱。如果工作区落在受限令牌访问不到的
位置（典型：`%TEMP%` 下的临时目录），命令会失败，而用户看到的只有：

    拒绝访问。
    [exit 1 · 7.7s]

沙箱当时 `notice` 是空的——没有任何线索说明"这是沙箱导致的、换个目录就好"。
以前沙箱默认关闭，碰不到；现在只读模式默认开，碰到的概率变高了。

这里钉两件事：
1. `plan()` 能识别"这个工作区在临时目录下"并给出明确 notice（不是静默失败）；
2. notice 必须真的出现在工具结果里（`_sb_note` 已接在结果尾部）。
"""
from pathlib import Path

import pytest

from ee.core import sandbox as SB


# ---------------------------------------------------------------- 环境判定


def test_detects_temp_dir_workspace(tmp_path, monkeypatch):
    """工作区在临时目录下 → 能被识别出来。"""
    import tempfile
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())
    temp = Path(tempfile.gettempdir()).resolve()
    p = SB.plan({}, temp, readonly=True)
    assert p is not None
    assert p.notice, "临时目录下的工作区必须给出提示，不能静默"
    assert "临时" in p.notice or "TEMP" in p.notice.upper()
    assert "readonly_default" in p.notice or "沙箱" in p.notice


def test_normal_workspace_has_no_such_notice(tmp_path, monkeypatch):
    """项目目录下不该出现这条提示（别误伤正常场景）。

    注意：pytest 的 tmp_path 本身就在 %TEMP% 下，拿它测“正常目录”会误判。
    这里用一个明确不在临时目录下的路径（只需要 resolve 结果，不必真存在）。
    """
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    monkeypatch.setattr(SB, "capability_sid", lambda ws: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(SB, "tmp_dir", lambda ws: tmp_path / "sbtmp")
    monkeypatch.setattr(SB, "_win", lambda: type("W", (), {"grant": staticmethod(lambda p, s: None)})())
    # 不能用 tmp_path / Path.home()：conftest 把 home 也隔离到临时目录下了。
    # 用一个与临时目录无关的绝对路径（只比较 resolve 结果，不必真存在）。
    import os
    drive = os.environ.get("SystemDrive", "C:")
    proj = Path(f"{drive}/ee-projects/demo")
    p = SB.plan({}, proj, readonly=True)
    assert p is not None
    assert "临时" not in (p.notice or "")


def test_notice_absent_when_sandbox_not_enabled(tmp_path, monkeypatch):
    """普通模式没开沙箱时，不该有沙箱提示。"""
    monkeypatch.setattr(SB, "IS_WINDOWS", True)
    import tempfile
    temp = Path(tempfile.gettempdir()).resolve()
    assert SB.plan({}, temp, readonly=False) is None


# ---------------------------------------------------------------- 提示真的能到用户眼前


def test_notice_reaches_tool_output(tmp_path, monkeypatch):
    """沙箱 notice 必须出现在 shell 工具结果里。"""
    from ee.tools import shell as SH

    class FakePolicy:
        mode = "read-only"

    class FakeSession:
        policy = FakePolicy()

    cfg = type("Cfg", (), {"data": {}})()
    from ee.tools.base import ToolContext, ToolState
    ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path,
                      extra={"config": cfg, "session": FakeSession()})

    class FakePlan:
        notice = "工作区在临时目录下，沙箱可能无法写入"
        active = False
        network = False

        def wrap(self, argv, env):
            return list(argv), env

        def env(self, env):
            return env

    monkeypatch.setattr(SH.SB, "plan", lambda data, ws, readonly=False: FakePlan())
    n = SH._sb_note(SH.sandbox_plan(ctx))
    assert "临时目录" in n
