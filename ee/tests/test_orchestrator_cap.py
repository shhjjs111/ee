"""D6-1 U4：子会话能力上限（测试）。

每条对应一条不变量：子不能比父宽、deny 必须继承、沙箱/网络跟随父、
共享 cwd 只能只读。这些是"模式名一样但权限不一样"的典型漏点。
"""
import pytest

from ee.core.orchestrator_cap import (
    CapabilityError,
    child_capability,
    child_mode,
    merge_child_rules,
)
from ee.core.permissions import PermissionPolicy


@pytest.fixture
def policy(tmp_path):
    return PermissionPolicy(mode="default", workspace=tmp_path, allow=["shell(ls *)"],
                            ask=["write"], deny=["shell(rm *)"])


# ---------------------------------------------------------------- 模式


def test_child_mode_never_wider():
    assert child_mode("read-only", "auto") == "read-only"
    assert child_mode("plan", "default") == "plan"
    assert child_mode("default", "auto") == "default"
    assert child_mode("auto", "auto") == "auto"


def test_child_mode_can_be_narrower():
    assert child_mode("default", "read-only") == "read-only"
    assert child_mode("auto", "default") == "default"


def test_unknown_mode_raises():
    with pytest.raises(CapabilityError, match="未知权限模式"):
        child_mode("default", "yolo")


def test_missing_parent_mode_defaults_to_default():
    assert child_mode(None, None) == "default"


# ---------------------------------------------------------------- 规则继承


def test_deny_rules_are_inherited(policy):
    cap = child_capability(parent_mode="default", parent_policy=policy, data={}, worktree=True)
    assert "shell(rm *)" in cap.deny
    assert cap.ask == ["write"] and cap.allow == ["shell(ls *)"]


def test_merge_child_rules_keeps_parent_and_dedupes(policy):
    cap = child_capability(parent_mode="default", parent_policy=policy, data={}, worktree=True)
    merged = merge_child_rules(cap, policy)
    assert merged.count("shell(rm *)") == 1
    assert "shell(rm *)" in merged


def test_readonly_child_stays_readonly(policy):
    cap = child_capability(parent_mode="read-only", parent_policy=policy, data={}, worktree=True)
    assert cap.mode == "read-only" and cap.readonly is True


# ---------------------------------------------------------------- 沙箱 / 网络


def test_sandbox_follows_parent_config(policy):
    data = {"sandbox": {"enabled": True, "network": False}, "network": {"proxy": "direct"}}
    cap = child_capability(parent_mode="default", parent_policy=policy, data=data, worktree=True)
    assert cap.sandbox == {"enabled": True, "network": False}
    assert cap.network == {"proxy": "direct"}


def test_capability_does_not_alias_parent_config(policy):
    """复制而不是引用：改子会话的字段不能影响父配置。"""
    data = {"sandbox": {"enabled": True}}
    cap = child_capability(parent_mode="default", parent_policy=policy, data=data, worktree=True)
    cap.sandbox["enabled"] = False
    assert data["sandbox"]["enabled"] is True


# ---------------------------------------------------------------- 共享 cwd


def test_shared_cwd_requires_readonly(policy):
    with pytest.raises(CapabilityError, match="worktree"):
        child_capability(parent_mode="default", parent_policy=policy, data={}, worktree=False)


def test_shared_cwd_allowed_for_readonly(policy):
    cap = child_capability(parent_mode="read-only", parent_policy=policy, data={}, worktree=False)
    assert cap.mode == "read-only"
    assert cap.requires_worktree is False


def test_widening_request_is_clamped_not_rejected(policy):
    """请求比父宽时按父的算（而不是报错）——模型经常多要一点，不该整次失败。"""
    cap = child_capability(parent_mode="default", parent_policy=policy, data={},
                           requested_mode="auto", worktree=True)
    assert cap.mode == "default"
