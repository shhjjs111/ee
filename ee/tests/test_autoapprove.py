"""自动审批的建议态（T10-c）：只提建议，不放行。

方案原文两条硬约束（`stabilization-and-d6.md:118`、`:313`）：
> 自动审批必须服从确定性 deny、强制询问、显式 ask、只读/计划限制；
> 不能仅「排除危险词后信任模型 allow」。建议先做建议展示，不默认自动放行。
> 先建议态、默认关闭；deny/force/显式 ask/只读限制不可由模型裁决覆盖。

所以这一层**不改变任何决策**：它只回答「如果自动放行，会放行什么、为什么」。
任何一处把 `ask`/`deny`/`force` 变成 `allow` 的路径都是缺陷——那等于模型裁决权限。

这些用例守的正是那些「看起来省事、实际越权」的改法。
"""
from __future__ import annotations

import pytest


def _dec(action: str, reason: str = "", prefix: str = "", force: bool = False):
    from ee.core.permissions import Decision

    return Decision(action, reason, prefix, force)


# ---------------------------------------------------------------- 建议态不改决策

def test_suggestion_never_changes_action():
    """给任何决策生成建议后，action 必须原样保持。"""
    from ee.core.autoapprove import suggest

    for action in ("allow", "ask", "deny"):
        d = _dec(action, prefix="shell(git status *)")
        s = suggest(d, tool="shell", args={"command": "git status"}, mode="default")
        assert s.decision.action == action, f"{action} 被改成了 {s.decision.action}"


def test_force_ask_is_never_auto_approved():
    """强制询问（force）永远不给「可自动放行」的建议。"""
    from ee.core.autoapprove import suggest

    d = _dec("ask", reason="危险操作", prefix="shell(git push --force *)", force=True)
    s = suggest(d, tool="shell", args={"command": "git push --force"}, mode="default")
    assert s.eligible is False
    assert "force" in s.blocked_by.lower() or "强制" in s.blocked_by


def test_deny_is_never_eligible():
    """确定性 deny 不能被建议成可放行。"""
    from ee.core.autoapprove import suggest

    d = _dec("deny", reason="当前是只读模式")
    s = suggest(d, tool="edit", args={"path": "a.py"}, mode="read-only")
    assert s.eligible is False


def test_readonly_mode_blocks_everything_mutable():
    """只读/计划模式下，写类工具的建议一律不合格。"""
    from ee.core.autoapprove import suggest

    for tool in ("edit", "write", "apply_patch"):
        d = _dec("ask", prefix=tool)
        s = suggest(d, tool=tool, args={"path": "a.py"}, mode="read-only")
        assert s.eligible is False, f"{tool} 在只读模式下不该有可放行建议"
        d2 = _dec("ask", prefix=tool)
        s2 = suggest(d2, tool=tool, args={"path": "a.py"}, mode="plan")
        assert s2.eligible is False, f"{tool} 在计划模式下不该有可放行建议"


def test_explicit_ask_without_prefix_is_not_eligible():
    """没有 prefix 的显式询问不能永久允许，也就没有「以后都不问」的建议。"""
    from ee.core.autoapprove import suggest

    d = _dec("ask", reason="强制询问", prefix="")
    s = suggest(d, tool="shell", args={"command": "rm -rf /"}, mode="default")
    assert s.eligible is False


def test_allow_decision_produces_nothing_to_suggest():
    """已经是 allow 的没有建议可提（不需要问用户）。"""
    from ee.core.autoapprove import suggest

    s = suggest(_dec("allow"), tool="read", args={"path": "a.txt"}, mode="default")
    assert s.eligible is False
    assert s.reason == "" or "无需" in s.reason or "不需要" in s.reason


# ---------------------------------------------------------------- 建议内容

def test_suggestion_carries_rule_and_counts():
    """合格建议要带可写入的规则和「影响面」信息，用户才知道同意什么。

    这里显式开启开关：本用例验的是「够格时建议长什么样」，
    默认关闭的行为由 test_default_is_off 单独守。
    """
    from ee.core.autoapprove import suggest

    d = _dec("ask", reason="要跑命令", prefix="shell(git commit *)")
    s = suggest(d, tool="shell", args={"command": "git commit -m x"}, mode="default",
                hits=3, config={"autoapprove": {"enabled": True}})
    assert s.eligible is True
    assert s.rule == "shell(git commit *)"
    assert s.hits == 3
    assert s.summary, "要有给用户看的一句话摘要"


def test_suggestion_is_deterministic():
    """同输入两次建议结果一致——纯函数，不带隐藏状态。"""
    from ee.core.autoapprove import suggest

    d = _dec("ask", prefix="shell(npm install *)")
    a = suggest(d, tool="shell", args={"command": "npm install x"}, mode="default")
    b = suggest(d, tool="shell", args={"command": "npm install x"}, mode="default")
    assert (a.eligible, a.rule, a.summary) == (b.eligible, b.rule, b.summary)


def test_default_is_off():
    """默认关闭：没有显式开启时不产生任何建议。"""
    from ee.core.autoapprove import enabled, suggest

    s = suggest(_dec("ask", prefix="shell(git status *)"), tool="shell",
                args={"command": "git status"}, mode="default", config={})
    assert s.eligible is False
    assert enabled({}) is False
    assert enabled({"autoapprove": {"enabled": True}}) is True


def test_config_can_only_tighten_not_loosen():
    """项目配置只能收紧：不能把已禁用的能力打开，也不能放宽白名单。"""
    from ee.core.autoapprove import enabled, merge_config

    user = {"autoapprove": {"enabled": True, "tools": ["shell"]}}
    project = {"autoapprove": {"enabled": True, "tools": ["shell", "edit", "write"]}}
    m = merge_config(user, project)
    # 项目不能新增工具（那是放宽）
    assert m["tools"] == ["shell"], m
    # 项目可以关掉
    assert enabled(merge_config(user, {"autoapprove": {"enabled": False}})) is False
