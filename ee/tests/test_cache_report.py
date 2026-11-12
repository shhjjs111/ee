"""命中率诊断（`ee usage`）的纯计算层：把逐请求 usage 行算成可验收的数字。

守三件容易悄悄算错的事：
  1. 主对话命中率只算 role=main，侧路 / 压缩 / 子代理不能进分母；
  2. 覆盖率与命中率分开给——分母只含报过 hit 的请求，样本不足时不能假装 99%；
  3. 断点数与压缩次数分开给，前者多于后者就是「有别的东西在改写前缀」的信号。
"""
from __future__ import annotations

import pytest

from ee.core.cache_report import summarize


def _r(prompt, hit, role="main", **kw):
    return {"prompt": prompt, "completion": 10, "hit": hit, "role": role, "model": "m", **kw}


def _brk(**kw):
    """带断点字段的记录。`break` 是 Python 关键字，没法当关键字参数传。"""
    return _r(kw.pop("prompt"), kw.pop("hit"), **kw)


def test_empty_rows_is_all_none_not_zero():
    """没有账本时算出来的是「未知」而不是 0%——0% 会被读成「命中率烂透了」。"""
    d = summarize([])
    assert d["requests"] == 0
    assert d["main"]["hit_rate"] is None
    assert d["main"]["hit_coverage"] is None
    assert d["breaks"]["count"] == 0
    assert d["normal_gap"]["median"] is None


def test_side_requests_do_not_enter_main_hit_rate():
    rows = [_r(10_000, 9_900) for _ in range(4)] + [_r(3000, 0, role="compact")]
    d = summarize(rows)
    assert d["main"]["requests"] == 4
    assert d["main"]["hit_rate"] == 0.99
    # 总口径会被压缩请求拉低，这正是要单算的原因
    assert d["by_role"]["main"]["hit_rate"] == 0.99
    assert d["by_role"]["compact"]["hit_rate"] == 0.0


def test_coverage_tells_how_much_of_input_is_known():
    """有的请求没报 hit：命中率的分母只含报过的那部分，覆盖率说明漏了多少。"""
    rows = [_r(10_000, 9_900), _r(10_000, None), _r(20_000, 19_800)]
    d = summarize(rows)
    assert d["main"]["prompt"] == 40_000
    assert d["main"]["hit_coverage"] == 0.75
    assert d["main"]["hit_coverage_requests"] == pytest.approx(2 / 3)
    assert d["main"]["hit_rate"] == (9_900 + 19_800) / 30_000


def test_usage_missing_is_invisible_to_token_coverage_but_not_to_request_coverage():
    """缺用量的请求 prompt=0，永远进不了 token 分母——「覆盖 100% 输入」不能当成样本完整。

    真实账本里 10,949 条请求中有 27 条 usage_missing，而 hit_coverage 一直是 1.0000；
    研究稿 §8 的 `assert main_hit_coverage == 1.0` 因此形同虚设。
    """
    rows = [_r(10_000, 9_900),
            {"prompt": 0, "completion": 0, "hit": None, "usage_missing": True, "role": "main"}]
    d = summarize(rows)
    assert d["main"]["hit_coverage"] == 1.0
    assert d["main"]["hit_coverage_requests"] == 0.5
    assert d["main"]["usage_missing"] == 1
    assert d["usage_missing"] == 1


def test_breaks_and_compactions_are_counted_separately():
    rows = [_r(80_000, 79_000, turn=1),
            _brk(prompt=50_000, hit=40_000, turn=2, gap=10_000, new=500, **{"break": "compact"}),
            _r(60_000, 59_500, turn=3, gap=500, new=600)]
    d = summarize(rows)
    assert d["breaks"]["count"] == 1
    assert d["breaks"]["items"][0]["reason"] == "compact"
    assert d["compactions"] == 0, "压缩请求本身不在这一批里（它 role=compact，不算 main）"
    # 断点回合不能混进「正常回合未命中」的分布；没报 gap 的行（老记录）也不该被当成 0
    assert d["normal_gap"]["count"] == 1
    assert d["normal_gap"]["median"] == 500
    assert d["normal_gap"]["max"] == 500


def test_more_breaks_than_compactions_is_visible():
    """断点比压缩多：说明除了压缩还有别的东西在改写前缀（换模型 / 修剪 / 老版本白压）。"""
    rows = [_brk(prompt=50_000, hit=40_000, turn=i, gap=10_000, new=300, **{"break": "unknown"}) for i in range(3)]
    d = summarize(rows)
    assert d["breaks"]["count"] == 3
    assert d["compactions"] == 0
    assert d["breaks"]["count"] > d["compactions"]


def test_break_reasons_are_summarized():
    """断点要能看出是哪种：upstream_full_miss 和 compact 的处理方式完全不同。"""
    rows = [_brk(prompt=50_000, hit=0, gap=50_000, new=10, **{"break": "upstream_full_miss"}),
            _brk(prompt=50_000, hit=40_000, gap=10_000, new=10, **{"break": "compact"}),
            _brk(prompt=50_000, hit=45_000, gap=5_000, new=10, **{"break": "upstream_full_miss"})]
    d = summarize(rows)
    assert d["breaks"]["by_reason"] == {"upstream_full_miss": 2, "compact": 1}
    assert summarize([])["breaks"]["by_reason"] == {}


def test_buckets_split_by_prompt_size():
    rows = [_r(20_000, 19_000), _r(60_000, 59_700), _r(100_000, 99_800), _r(200_000, 199_900)]
    buckets = {b["label"]: b for b in summarize(rows)["buckets"]}
    assert buckets["<40k"]["requests"] == 1
    assert buckets["40–80k"]["requests"] == 1
    assert buckets["80–150k"]["requests"] == 1
    assert buckets[">150k"]["requests"] == 1
    assert buckets["<40k"]["hit_rate"] == 0.95
    # 边界：150k 归入 >150k，80k 归入 80–150k
    edge = {b["label"]: b for b in summarize([_r(80_000, 80_000), _r(150_000, 150_000)])["buckets"]}
    assert edge["80–150k"]["requests"] == 1
    assert edge[">150k"]["requests"] == 1


def test_gap_median_uses_nearest_rank_not_interpolation():
    """分位数用最近秩：线性插值会编出账本里不存在的 token 数，看着精确其实是假的。"""
    rows = [_r(1000, 1000, gap=g, new=1) for g in (10, 20, 30, 40)]
    d = summarize(rows)
    assert d["normal_gap"]["median"] in (10, 20, 30, 40)
    assert d["normal_gap"]["max"] == 40


def test_old_records_without_role_count_as_main():
    """账本上线前的老记录没有 role 字段，它们只可能是主对话——算 main 而不是丢弃。"""
    rows = [{"prompt": 1000, "completion": 10, "hit": 990}]
    d = summarize(rows)
    assert d["main"]["requests"] == 1
    assert d["main"]["hit_rate"] == 0.99


def test_cold_start_is_not_a_normal_gap():
    """没有 wire 基线的首发不进 normal_gap，但仍在用户看到的总命中率里。"""
    rows = [_r(100_000, 0, cold=True, baseline=0, gap=100_000, new=0),
            _r(101_000, 100_000, gap=1_000, new=1_000)]
    d = summarize(rows)
    assert d["normal_gap"]["count"] == 1
    assert d["normal_gap"]["max"] == 1_000
    assert d["cold"]["count"] == 1 and d["cold"]["gap"] == 100_000
    assert d["legacy"]["count"] == 0
    assert d["main"]["requests"] == 2
    assert d["main"]["hit_rate"] == 100_000 / 201_000, "总口径不能把首发的整段未命中藏掉"


def test_legacy_rows_are_neither_cold_nor_normal():
    """老账本（没有 baseline）里分不清冷启动与整段未命中的行：单独一类，两边都不进。

    算成冷启动会让次数虚高、把一次重算隐藏在「重启」背后；算正常回合会把一次整段
    重算当成「前缀不稳」，进而把压缩水位往下推（和断点污染同一类错误）。
    """
    rows = [_r(100_000, 0, new=0, gap=100_000),          # 老账本：冷启动或整段未命中
            _r(101_000, 100_500, new=0, gap=495)]         # wire 变小，但服务端几乎全命中
    d = summarize(rows)
    assert d["cold"]["count"] == 0, "没有 cold 标记的不能冒充冷启动"
    assert d["legacy"]["count"] == 1 and d["legacy"]["gap"] == 100_000
    assert d["normal_gap"]["count"] == 1
    assert d["normal_gap"]["max"] == 495


def test_known_causes_are_summarized_even_without_break():
    """cause 可能没有达到断点阈值：单独计数，不能只在 breaks 里找。"""
    rows = [_r(10_000, 9_900, cause="model_switch"),
            _r(10_000, 9_900, cause="compact"),
            _r(10_000, 9_900)]
    d = summarize(rows)
    assert d["causes"]["count"] == 2
    assert d["causes"]["by_reason"] == {"model_switch": 1, "compact": 1}
    assert d["breaks"]["count"] == 0
