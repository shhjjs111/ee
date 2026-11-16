"""A/B 配对的公平顺序与"比较机会"归一化（2026-11 第二批）。

问题：A/B 有全局时间预算（`_allout_rank` 的 90 秒），旧实现用
`combinations(passed, 2)`——配对顺序的前缀全落在排序靠前的 id 上。预算一用完，
后面的候选一票没有；而 `_rank_key` 直接比净胜票**绝对值**，没被比过的候选恒输。
这是确定性偏差、可复现，与候选质量无关。

这里守两件事：
1. `fair_pairs` 的赛程公平：每轮一人只比一次、每对只出现一次、种子可复现、
   输入顺序不影响结果，且时间预算截断时前缀不集中在少数候选身上。
2. `Candidate.comparisons` 的归一化：比过的按场均净胜，没比过是中性 0，
   `None` 保持旧调用方的 raw votes 语义。公平不等于保证弱候选获胜。
"""
from __future__ import annotations

from itertools import combinations


def _v(met: bool = True):
    from ee.core.goal import Verdict

    if met:
        return Verdict(exit=0, tests=3, output="ok：3 个测试通过", min_tests=1)
    return Verdict(exit=1, tests=0, output="exit=1", min_tests=1)


def _cand(cid: str, *, votes: int = 0, comparisons=None, regressed: bool = False, met: bool = True):
    from ee.core.bestof import Candidate

    return Candidate(id=cid, title=cid, worktree=f"/tmp/wt-{cid}", votes=votes,
                     comparisons=comparisons, regressed=regressed, verdict=_v(met))


# ---------------------------------------------------------------- fair_pairs 赛程

def test_two_candidates_have_exactly_one_pair():
    from ee.core.bestof import fair_pairs

    assert fair_pairs(["cand-2", "cand-1"]) == [("cand-1", "cand-2")]


def test_three_and_six_candidates_cover_every_pair_once():
    from ee.core.bestof import fair_pairs

    for n in (3, 6):
        ids = [f"cand-{i}" for i in range(1, n + 1)]
        pairs = fair_pairs(ids)
        assert len(pairs) == len(list(combinations(ids, 2))), f"{n} 路的对照组数不对"
        assert len(set(pairs)) == len(pairs), f"{n} 路有重复配对"
        assert {frozenset(p) for p in pairs} == {frozenset(c) for c in combinations(ids, 2)}


def test_each_round_gives_every_candidate_at_most_one_comparison():
    """轮转赛程的关键性质：时间预算只够一轮时，不该出现某个候选被反复比较。"""
    from ee.core.bestof import fair_pairs

    for n in (3, 4, 6):
        ids = [f"cand-{i}" for i in range(1, n + 1)]
        pairs = fair_pairs(ids)
        first_round = pairs[: n // 2]
        seen: list[str] = []
        for a, b in first_round:
            seen.extend([a, b])
        assert len(seen) == len(set(seen)), f"{n} 路第一轮有人被比了两次：{first_round}"


def test_time_budget_prefix_does_not_favor_first_ids():
    """预算只够前两组时，覆盖的候选不能全是排序最前的两三个。"""
    from ee.core.bestof import fair_pairs

    ids = [f"cand-{i}" for i in range(1, 7)]
    prefix = fair_pairs(ids, seed="rank-1")[:2]
    covered = {x for pair in prefix for x in pair}
    assert len(covered) == 4, f"两组对照只覆盖了 {sorted(covered)}"


def test_input_order_does_not_change_the_schedule():
    from ee.core.bestof import fair_pairs

    ids = ["cand-3", "cand-1", "cand-2", "cand-4"]
    assert fair_pairs(ids) == fair_pairs(sorted(ids))
    assert fair_pairs(list(reversed(ids))) == fair_pairs(ids)


def test_seed_is_reproducible_and_actually_rotates():
    from ee.core.bestof import fair_pairs

    ids = [f"cand-{i}" for i in range(1, 7)]
    assert fair_pairs(ids, seed="s1") == fair_pairs(ids, seed="s1")
    orders = {tuple(fair_pairs(ids, seed=f"s{i}")) for i in range(6)}
    assert len(orders) > 1, "种子应当能改变配对/轮次顺序，否则预算被砍时仍是同一批先比"


def test_degenerate_inputs():
    from ee.core.bestof import fair_pairs

    assert fair_pairs([]) == []
    assert fair_pairs(["only"]) == []
    assert fair_pairs(["a", "a"]) == []          # 去重后只剩一个候选，没有可比的


# ---------------------------------------------------------------- 归一化排序

def test_zero_comparisons_is_neutral_not_a_penalty():
    """没被比过的候选既不该直接夺冠，也不该被"净胜 0"判死。"""
    from ee.core.bestof import _rank_key

    unmeasured = _cand("cand-1", votes=0, comparisons=0)
    neutral = _cand("cand-2", votes=0, comparisons=3)
    assert _rank_key(unmeasured)[1] == _rank_key(neutral)[1] == 0.0


def test_average_net_votes_beat_raw_vote_count():
    """1 组对照净胜 +1（场均 1.0）应当压过 4 组对照净胜 +1（场均 0.25）。"""
    from ee.core.bestof import _rank_key

    sharp = _cand("cand-9", votes=1, comparisons=1)
    grinder = _cand("cand-1", votes=1, comparisons=4)
    assert _rank_key(sharp)[1] < _rank_key(grinder)[1]


def test_none_comparisons_keeps_raw_vote_order_for_old_callers():
    from ee.core.bestof import _rank_key

    more = _cand("cand-2", votes=3, comparisons=None)
    fewer = _cand("cand-1", votes=1, comparisons=None)
    assert _rank_key(more)[1] < _rank_key(fewer)[1] == -1.0


def test_pick_uses_normalized_votes_not_raw_counts():
    from ee.core.bestof import pick

    sharp = _cand("cand-9", votes=1, comparisons=1)
    grinder = _cand("cand-1", votes=1, comparisons=4)
    assert pick([grinder, sharp]).id == "cand-9"


def test_unverified_and_regressed_still_cannot_win():
    from ee.core.bestof import pick

    winner = pick([_cand("cand-unverified", votes=9, comparisons=1, met=False)])
    assert winner is None, "没有判据结果不得胜出"
    winner = pick([_cand("cand-regressed", votes=9, comparisons=1, regressed=True)])
    assert winner is None, "回归过的候选不得胜出"


def test_as_dict_and_report_carry_comparisons():
    from ee.core.bestof import report

    c = _cand("cand-1", votes=2, comparisons=3)
    assert c.as_dict()["comparisons"] == 3
    assert "+2/3" in report([c], "cand-1")


def test_pick_reason_states_comparison_coverage():
    from ee.core.bestof import pick_reason

    a = _cand("cand-1", votes=2, comparisons=3)
    b = _cand("cand-2", votes=-2, comparisons=3)
    why = pick_reason(a, [a, b])
    assert "3 组完整对照" in why and "场均净胜" in why


def test_pick_reason_explains_average_when_votes_are_equal():
    """票数相同、比较次数不同：实际是场均净胜分出胜负，不能说成"测试覆盖"。"""
    from ee.core.bestof import pick_reason

    sharp = _cand("cand-1", votes=1, comparisons=1)     # 场均 +1.00
    grinder = _cand("cand-2", votes=1, comparisons=4)   # 场均 +0.25
    why = pick_reason(sharp, [sharp, grinder])
    assert "模型对照" in why and "场均净胜 +1.00" in why
    assert "3 组" not in why and "测试覆盖" not in why and "改动面" not in why


def test_pick_reason_keeps_old_raw_vote_wording_for_none():
    from ee.core.bestof import pick_reason

    a = _cand("cand-1", votes=3, comparisons=None)
    b = _cand("cand-2", votes=1, comparisons=None)
    why = pick_reason(a, [a, b])
    assert "旧数据无比较次数" in why


def test_pick_reason_does_not_blame_unmeasured_candidates():
    """全都未比较（中性 0）时，不能说成模型对照选出来的。"""
    from ee.core.bestof import pick_reason

    a = _cand("cand-1", votes=0, comparisons=0)
    b = _cand("cand-2", votes=0, comparisons=0)
    why = pick_reason(a, [a, b])
    assert "模型对照" not in why
    assert "通过了判据" in why
