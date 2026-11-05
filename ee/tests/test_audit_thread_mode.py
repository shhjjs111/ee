"""权限模式的切换入口（2026-11 审计修复）。

以前没有「切换会话模式」这个 RPC：模式只随 turn/start 的 mode 参数带过去。
前端又把全局草稿值（localStorage 的 ee.mode）当 mode 发给每一条消息，于是
「打开一个只读会话、随手发一句话」就把它提成了 draftMode——界面全程显示那个模式，
用户没有任何线索发现权限被放宽；子会话也同理，绕过了 orchestrator 的能力上限。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ee.core.permissions import PermissionPolicy
from ee.server.threads import Thread


def _thread(meta: dict, mode: str = "default"):
    t = object.__new__(Thread)
    t.meta = meta
    s = SimpleNamespace(policy=PermissionPolicy(mode=mode, workspace=Path.cwd()))
    t.ensure_session = lambda: s
    t.save_meta = lambda: None
    t.notify = lambda *a, **k: None
    t.summary = lambda: {"id": "t1", "mode": s.policy.mode}
    return t, s


def test_set_mode_rejects_child_threads():
    """子会话的模式由父会话在 create_child 时按能力上限算出来，不能再单独改。"""
    t, base = _thread({"orchestrator_parent_id": "p1"})
    with pytest.raises(ValueError) as ei:
        t.set_mode("auto")
    assert "子会话" in str(ei.value)
    assert base.policy.mode == "default", "拒绝之后不该改动任何状态"


def test_set_mode_updates_policy_and_meta():
    t, base = _thread({})
    out = t.set_mode("auto")
    assert out["mode"] == "auto"
    assert base.policy.mode == "auto", "会话自己的策略要真的改掉"
    assert t.meta["mode"] == "auto", "meta 要跟着改，否则重启后又回到旧模式"


def test_set_mode_rejects_unknown_mode():
    t, base = _thread({})
    with pytest.raises(ValueError):
        t.set_mode("sudo")
    assert base.policy.mode == "default"
