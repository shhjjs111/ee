"""best-of-n 的质量信号（allout 阶段 4）。

现状 `pick` 在同等通过时按 `(cost, id)` 排——id 是自动生成的名字，等于随机。
allout 要把胜者自动写回主目录，随机选不可接受。这里守新的排序键：

1. 判据通过（现状不变，硬门槛）
2. 回归门通过（allout 新增：不得弄坏改动前已通过的测试）
3. 同模型 A/B 净胜票数
4. 判据读出的测试数（多 = 覆盖更完整）
5. 改动面小
6. 未触碰测试文件
7. 成本
8. id（稳定排序）

前四条全部来自可观测事实，没有一条来自模型自述（`self_reported_ok` 只记录）。
"""
from __future__ import annotations

from ee.core.bestof import Candidate


def _v(met: bool = True, tests: int = 3):
    from ee.core.goal import Verdict

    return Verdict(exit=0 if met else 1, tests=tests, timed_out=False, output="", min_tests=1)


def _c(cid: str, **kw) -> Candidate:
    base = {"title": cid, "worktree": f"/tmp/wt-{cid}", "verdict": _v()}
    base.update(kw)
    return Candidate(id=cid, **base)


# ---------------------------------------------------------------- 回归门


def test_regressed_candidate_loses_to_clean_one():
    """判据都过、但一个弄坏了旧测试：先淘汰它，哪怕它更便宜。"""
    clean = _c("clean", cost=1.0)
    broke = _c("broke", cost=0.1, regressed=True)
    from ee.core.bestof import pick

    assert pick([clean, broke]).id == "clean"


def test_only_regressed_candidates_yields_none():
    """全部回归 = 全部不算赢：宁可不写回。"""
    from ee.core.bestof import pick

    assert pick([_c("a", regressed=True), _c("b", regressed=True)]) is None


# ---------------------------------------------------------------- A/B 票数


def test_votes_beat_cheaper_candidate():
    """同模型 A/B 净胜的候选优先于更便宜的。"""
    voted = _c("voted", cost=5.0, votes=2)
    cheap = _c("cheap", cost=0.1, votes=0)
    from ee.core.bestof import pick

    assert pick([voted, cheap]).id == "voted"


def test_votes_can_be_negative_and_rank_below_zero():
    from ee.core.bestof import pick

    assert pick([_c("neg", votes=-1, cost=0.1), _c("zero", votes=0, cost=5.0)]).id == "zero"


# ---------------------------------------------------------------- 测试数


def test_more_tests_beat_cheaper():
    """通过的测试更多 = 覆盖更完整，优先于成本。"""
    wide = _c("wide", verdict=_v(tests=142), cost=3.0)
    narrow = _c("narrow", verdict=_v(tests=88), cost=0.1)
    from ee.core.bestof import pick

    assert pick([wide, narrow]).id == "wide"


# ---------------------------------------------------------------- 改动面


def test_smaller_diff_wins_at_equal_signals():
    small = _c("small", cost=1.0, files=2, additions=10, deletions=2)
    big = _c("big", cost=1.0, files=9, additions=400, deletions=120)
    from ee.core.bestof import pick

    assert pick([big, small]).id == "small"


# ---------------------------------------------------------------- 测试文件


def test_untouched_tests_win_at_equal_signals():
    """碰过测试文件的候选排在后面（可疑）；同分时用它决出胜者。"""
    pure = _c("pure", cost=1.0, files=2, additions=10, touched_tests=False)
    edited = _c("edited", cost=1.0, files=2, additions=10, touched_tests=True)
    from ee.core.bestof import pick

    assert pick([edited, pure]).id == "pure"


# ---------------------------------------------------------------- 报告


def test_report_explains_which_rule_picked():
    from ee.core.bestof import pick, report

    winner = pick([_c("a", votes=2, cost=1.0), _c("b", votes=0, cost=0.1)])
    text = report([_c("a", votes=2, cost=1.0), _c("b", votes=0, cost=0.1)], winner.id)
    assert "选中理由" in text
    assert "A/B" in text or "票" in text, f"报告要写明用了哪条规则：{text}"


def test_new_fields_default_keep_old_behavior():
    """没给新字段的老数据（T10-d 的 RPC 调用方）行为不变：通过即候选，按成本排。"""
    from ee.core.bestof import pick

    a = Candidate(id="a", worktree="/tmp/wt-a", verdict=_v(), cost=2.0)
    b = Candidate(id="b", worktree="/tmp/wt-b", verdict=_v(), cost=1.0)
    assert pick([a, b]).id == "b"
    assert a.regressed is False and a.votes == 0 and a.touched_tests is False
