"""best-of-n 的评选与验证记录（T10-d）。

方案原文（`stabilization-and-d6.md:134`）：
> 候选有独立 run_id/worktree/费用/验证记录；「模型说测试通过」不能进入已验证列。
> 只合并选中候选，其余默认保留。

所以这一层的核心只有一件事：**谁能被判为"已验证"**。
判据是 Goal 的 Verdict（真实退出码 + 真实测试计数），不是模型的自我评价。
没有判据结果的候选一律 unverified——这条和本项目在 CI 上踩过的「0 passed 也算绿」
是同一个教训。
"""
from __future__ import annotations

import pytest


def _cand(cid: str, *, verified=False, cost=0.0, worktree=None):
    """构造一个候选。verified=True 时给它一个真实通过的 Verdict——
    `verified` 是只读属性，由 verdict 推出来，不能直接赋值（这正是要守的不变量）。
    """
    from ee.core.bestof import Candidate

    v = _verdict(True) if verified else None
    return Candidate(id=cid, title=f"候选 {cid}", worktree=worktree or f"/tmp/wt-{cid}",
                     cost=cost, verdict=v)


def _verdict(met: bool, tests: int = 3, exit_code: int = 0):
    from ee.core.goal import Verdict

    return Verdict(exit=exit_code, tests=tests, timed_out=False, output="", min_tests=1)


# ---------------------------------------------------------------- 验证记录

def test_candidate_without_verdict_is_not_verified():
    """没有判据结果的候选不能进已验证列（模型自述不算）。"""
    from ee.core.bestof import Candidate

    c = Candidate(id="a", title="a", worktree="/w", cost=0.0, self_reported_ok=True)
    assert c.verified is False, "只有模型自述时不许标记为已验证"


def test_candidate_with_failed_verdict_is_not_verified():
    """判据没通过的候选不是已验证。"""
    from ee.core.bestof import Candidate

    c = Candidate(id="a", title="a", worktree="/w", verdict=_verdict(False, tests=2, exit_code=1))
    assert c.verified is False
    assert c.met is False


def test_candidate_with_passing_verdict_is_verified():
    """判据真通过（exit 0 且有测试）才是已验证。"""
    from ee.core.bestof import Candidate

    c = Candidate(id="a", title="a", worktree="/w", verdict=_verdict(True, tests=3))
    assert c.verified is True
    assert c.met is True


def test_zero_tests_verdict_is_not_verified():
    """exit 0 但零测试：不许算已验证（沿用 Goal 的不变量）。"""
    from ee.core.bestof import Candidate
    from ee.core.goal import Verdict

    c = Candidate(id="a", title="a", worktree="/w",
                  verdict=Verdict(exit=0, tests=0, timed_out=False, output="", min_tests=1))
    assert c.verified is False


# ---------------------------------------------------------------- 评选

def test_pick_prefers_verified_over_unverified():
    """已验证的优先于未验证的，哪怕后者自述通过。"""
    from ee.core.bestof import pick

    a = _cand("a", verified=False)
    b = _cand("b", verified=True)
    win = pick([a, b])
    assert win.id == "b"


def test_pick_none_when_nothing_verified():
    """全都没验证时不选任何候选——不能凭自述挑一个\"最好的\"。"""
    from ee.core.bestof import pick

    assert pick([_cand("a"), _cand("b")]) is None


def test_pick_breaks_tie_by_cost_then_id():
    """都验证通过时：先看花费低的，再按 id 稳定排序（结果可复现）。"""
    from ee.core.bestof import pick

    a = _cand("a", verified=True, cost=0.5)
    b = _cand("b", verified=True, cost=0.1)
    assert pick([a, b]).id == "b"
    # 同花费时按 id，保证确定性
    c = _cand("c", verified=True, cost=0.1)
    assert pick([b, c]).id == "b"
    assert pick([c, b]).id == "b"


def test_pick_is_deterministic_regardless_of_input_order():
    """输入顺序不影响结果——不能因为列表顺序让候选\"碰运气\"。"""
    from ee.core.bestof import pick

    a = _cand("a", verified=True, cost=0.1)
    b = _cand("b", verified=True, cost=0.1)
    assert pick([a, b]).id == pick([b, a]).id


# ---------------------------------------------------------------- 保留策略

def test_losers_are_kept_by_default():
    """没选中的候选默认保留（worktree 不删），由用户决定。"""
    from ee.core.bestof import plan_cleanup

    cands = [_cand("a", verified=True), _cand("b"), _cand("c")]
    plan = plan_cleanup(cands, winner_id="a")
    assert plan["keep"] == ["a", "b", "c"]
    assert plan["remove"] == []


def test_cleanup_can_explicitly_remove_losers():
    """只有显式要求才清理落选者（数据保全：默认不删）。"""
    from ee.core.bestof import plan_cleanup

    cands = [_cand("a", verified=True), _cand("b")]
    plan = plan_cleanup(cands, winner_id="a", remove_losers=True)
    assert plan["keep"] == ["a"]
    assert plan["remove"] == ["b"]


def test_cleanup_never_removes_winner():
    """清理时绝不能把winner 算进 remove。"""
    from ee.core.bestof import plan_cleanup

    cands = [_cand("a", verified=True), _cand("b")]
    plan = plan_cleanup(cands, winner_id="a", remove_losers=True)
    assert "a" not in plan["remove"]


# ---------------------------------------------------------------- 集合校验

def test_candidates_must_have_distinct_worktrees():
    """候选必须各有独立 worktree，否则改的是同一份文件、比不出高下。"""
    from ee.core.bestof import validate

    a = _cand("a", worktree="/same")
    b = _cand("b", worktree="/same")
    problems = validate([a, b])
    assert problems, "共用 worktree 必须报错"
    assert "worktree" in " ".join(problems).lower()


def test_single_candidate_is_valid():
    """只有一个候选时合法（虽无比较意义）。"""
    from ee.core.bestof import validate

    assert validate([_cand("a")]) == []


def test_report_lists_every_candidate_with_evidence():
    """报告要列出每个候选与它的证据，人能自己复核为什么选它。"""
    from ee.core.bestof import report

    cands = [_cand("a", verified=True), _cand("b")]
    text = report(cands, winner_id="a")
    assert "a" in text and "b" in text
    assert "verified" in text.lower() or "已验证" in text
