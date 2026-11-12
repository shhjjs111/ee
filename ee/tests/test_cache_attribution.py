"""缓存未命中归因第一步：记录每条请求发生在第几回合（turn）。

目的：前缀缓存未命中集中在哪里，需要「回合」这个维度才能回答——
首回合必然冷启动（整段 prompt 都新），后续回合的未命中才是可优化的。
"""
from __future__ import annotations

import pytest

from ee.core.context import UsageStats
from ee.core.providers.profiles import Price


def _price() -> Price:
    return Price(input=2.0, output=8.0, cache_read=0.04, currency="¥")


def test_turn_recorded_in_request_record():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 10, "hit": 0}, _price(), model="m", turn=3)
    assert s.records[-1]["turn"] == 3


def test_turn_defaults_to_none_when_not_given():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 10, "hit": 0}, _price(), model="m")
    assert s.records[-1].get("turn") is None


# ---------------------------------------------------------------- 主对话口径与断点归因（P0-1）

def test_side_requests_do_not_lower_main_hit_rate():
    """侧路请求（标题 / commit / 压缩）命中恒 0，不能把用户看到的主对话命中率拉下来。"""
    from ee.core import subagent as SA  # noqa: F401  确保模块可导入（同进程口径一致）

    s = UsageStats()
    for _ in range(4):
        s.add({"prompt": 10_000, "completion": 100, "hit": 9_900}, _price(), model="m", role="main")
    for _ in range(4):
        s.add({"prompt": 3_000, "completion": 100, "hit": 0}, _price(), model="m", role="title")
    totals = s.totals()
    assert totals["hit_rate"] < 0.9, "总口径本来就会被侧路拉低（这正是要区分的原因）"
    assert totals["main_hit_rate"] == 0.99
    assert totals["main_requests"] == 4
    assert totals["by_role"]["title"]["requests"] == 4


def test_title_request_records_its_role():
    s = UsageStats()
    s.add({"prompt": 100, "completion": 5, "hit": 0}, _price(), model="m", role="title")
    assert s.records[-1]["role"] == "title"


def test_recent_main_hit_rate_ignores_side_requests():
    """压缩水位自适应用的是「最近主对话请求」，侧路请求混进来会把水位推向错误方向。"""
    s = UsageStats()
    for _ in range(6):
        s.add({"prompt": 1000, "completion": 10, "hit": 995}, _price(), model="m", role="main")
        s.add({"prompt": 300, "completion": 10, "hit": 0}, _price(), model="m", role="title")
    assert s.recent_main_hit_rate() == pytest.approx(0.995)


def test_subagent_records_do_not_pollute_main_hit_rate():
    """子代理并入主会话后必须落成 role=subagent：它的前缀独立、命中率天然低。"""
    from ee.core import subagent as SA

    parent = type("S", (), {})()
    parent.stats = UsageStats()
    for _ in range(10):
        parent.stats.add({"prompt": 10_000, "completion": 100, "hit": 9_900}, _price(), model="m", role="main")
    child = type("S", (), {})()
    child.stats = UsageStats()
    for _ in range(10):
        child.stats.add({"prompt": 10_000, "completion": 100, "hit": 0}, _price(), model="m", role="main")

    SA._merge_usage(parent, child)
    totals = parent.stats.totals()
    assert totals["main_hit_rate"] == pytest.approx(0.99), "子代理不能把主对话命中率拉到 0.495"
    assert totals["by_role"]["subagent"]["requests"] == 10
    assert parent.stats.recent_main_hit_rate() == pytest.approx(0.99)


def test_recent_main_hit_rate_ignores_break_requests():
    """断点请求不能进水位自适应的样本，否则会变成正反馈。

    真实 DeepSeek 会话实测（temp/ee-live 复现）：同一段历史含断点时 93.01%、排除断点
    99.48%，对应压缩水位 126k 与 168k，差一整次压缩。断点的未命中是压缩本身造成的既定
    成本，不是「前缀稳不稳」的证据；拿它调水位就成了「压缩造成断点 → 命中率看起来低 →
    水位下调 → 压缩更频繁 → 更多断点」。
    """
    s = UsageStats()
    for _ in range(10):
        s.note_wire(100_000)
        s.add({"prompt": 100_000, "completion": 50, "hit": 99_500}, _price(), model="m", role="main")
    # 一次压缩后的断点：历史从 100k 缩到 30k，整段未命中
    s.note_wire(30_000)
    s.add({"prompt": 30_000, "completion": 50, "hit": 3_000}, _price(), model="m", role="main",
          break_reason="compact")
    assert s.records[-1]["break"] == "compact"
    # 命中率只看正常回合；断点不进样本
    assert s.recent_main_hit_rate() == pytest.approx(0.995)
    # 但总口径（用户看到的数）仍然老实包含那次断点
    assert s.totals()["main_hit_rate"] < 0.98


def test_many_breaks_do_not_starve_the_watermark_sample():
    """断点多到把最近窗口占满时，样本不足应返回 None（不动水位），而不是给出 0%。"""
    s = UsageStats()
    s.note_wire(200_000)
    s.add({"prompt": 200_000, "completion": 50, "hit": 199_000}, _price(), model="m", role="main")
    for _ in range(8):
        s.note_wire(30_000)
        s.add({"prompt": 30_000, "completion": 50, "hit": 1_000}, _price(), model="m", role="main",
              break_reason="compact")
    assert all(r.get("break") for r in s.records[1:]), "构造前提：这 8 条必须真的被标成断点"
    assert s.recent_main_hit_rate() is None


def test_head_only_miss_is_attributed_to_upstream():
    """断点归因不到具体原因时，按「命中到多深」分类，别再一律记 unknown。

    真实会话里踩过：prompt 只比上一条多 224，hit 却从 128,384 掉到 2,560——命中的那
    一小块大致就是 system+tools。本地改写历史（压缩 / 修剪 / 清推理）都在某个位置之后
    改字节，那个位置之前的前缀照样命中；只有服务端整段失配才会把 95%+ 的前缀打掉。
    同时必须留下 `prefix_hit`，否则事后只剩一个原因字符串，没法核对。
    """
    s = UsageStats()
    s.note_wire(129_172)
    s.add({"prompt": 129_172, "completion": 50, "hit": 128_384}, _price(), model="m", role="main")
    s.note_wire(129_396)                      # 只多了 224
    s.add({"prompt": 129_396, "completion": 50, "hit": 2_560}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["break"] == "upstream_full_miss"
    assert r["prefix_hit"] == 2_560


def test_zero_hit_is_upstream_full_miss():
    """hit 恰好是 0：一个字节都没命中，本地改写做不到（system+tools 那段总是先命中）。"""
    s = UsageStats()
    s.note_wire(150_000)
    s.add({"prompt": 150_000, "completion": 50, "hit": 149_500}, _price(), model="m", role="main")
    s.note_wire(150_600)
    s.add({"prompt": 150_600, "completion": 50, "hit": 0}, _price(), model="m", role="main")
    assert s.records[-1]["break"] == "upstream_full_miss"
    assert s.records[-1]["prefix_hit"] == 0


def test_tail_break_stays_unknown():
    """命中了一大半、只丢了尾部（例如历史中段被改）：不能算成上游整段失配。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 50, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(100_200)
    s.add({"prompt": 100_200, "completion": 50, "hit": 20_000}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["break"] == "unknown"
    assert r["prefix_hit"] == 20_000


def test_known_break_reason_does_not_add_prefix_hit():
    """原因已知（compact / model_switch）时不必重复记：那几个原因本身就够解释。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 50, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(30_000)
    s.add({"prompt": 30_000, "completion": 50, "hit": 3_000}, _price(), model="m", role="main",
          break_reason="compact")
    assert s.records[-1]["break"] == "compact"
    assert "prefix_hit" not in s.records[-1]


def test_switching_model_marks_the_next_request_as_a_break(workspace, ee_home):
    """换模型会 reset 环境附录 → 前缀整体变化 → 必然断点，原因必须写进账本。

    修这条之前，set_model 不设 `_break_reason`，换模型造成的断点在账上只能显示
    "unknown"，跟真正说不清的断点混在一起（真实会话里就是这么误判的）。
    """
    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(workspace)
    names = list(cfg.data.get("models", {}))
    if len(names) < 2:
        pytest.skip("只有一个模型，无法验证切换")
    s = Session(cfg, model=names[0], cwd=workspace, persist=False, user_home=workspace)
    s.set_model(names[1])
    assert s._take_break() == "model_switch"
    assert s._take_break() is None, "断点原因只能被取走一次"


def test_reloading_the_same_model_is_not_a_switch(workspace, ee_home):
    """原地重设同一个模型不该被记成断点（否则每次打开设置页都会污染账本）。"""
    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(workspace)
    name = list(cfg.data.get("models", {}))[0]
    s = Session(cfg, model=name, cwd=workspace, persist=False, user_home=workspace)
    s.set_model(name)
    assert s._take_break() is None


def test_switching_effort_marks_the_next_request_as_a_break(workspace, ee_home):
    """换推理强度会改请求参数（reasoning_effort / thinking），服务端按参数区分缓存 → 必然断点。

    真实会话里改档位造成的断点一直显示 "unknown"，被当成「有东西在偷偷改写前缀」查了半天。
    """
    from ee.config import load_config
    from ee.core import reasoning as RZ
    from ee.core.session import Session

    cfg = load_config(workspace)
    name = list(cfg.data.get("models", {}))[0]
    s = Session(cfg, model=name, cwd=workspace, persist=False, user_home=workspace)
    lvls = RZ.levels(s.profile)
    if not lvls:
        pytest.skip("这个模型没有推理档位，无法验证")
    target = lvls[0]
    if RZ.effective(s.profile, target) == RZ.effective(s.profile, s.effort):
        pytest.skip("起始档位已经是目标档位，切换不会改变请求参数")
    s.set_effort(target)
    assert s._take_break() == "effort_switch"


def test_switching_effort_warns_about_the_cache(workspace, ee_home):
    """换档位同样会让整个前缀失效：提示文案要和 set_model 对齐，不能静默烧钱。"""
    from ee.config import load_config
    from ee.core import reasoning as RZ
    from ee.core.session import Session

    cfg = load_config(workspace)
    name = list(cfg.data.get("models", {}))[0]
    s = Session(cfg, model=name, cwd=workspace, persist=False, user_home=workspace)
    lvls = RZ.levels(s.profile)
    if not lvls:
        pytest.skip("这个模型没有推理档位，无法验证")
    target = lvls[0]
    if RZ.effective(s.profile, target) == RZ.effective(s.profile, s.effort):
        pytest.skip("起始档位已经是目标档位，切换不会改变请求参数")
    s.stats.last_prompt = 92_000            # 模拟一个已经跑了一半的长会话
    msg = s.set_effort(target)
    assert "缓存会从零开始" in msg
    assert "92,000 token" in msg


def test_effort_switch_without_param_change_does_not_warn(workspace, ee_home):
    """档位不影响请求字节时不提缓存：否则每次点同一个档位都弹一句无意义的警告。"""
    from ee.config import load_config
    from ee.core import reasoning as RZ
    from ee.core.session import Session

    cfg = load_config(workspace)
    name = list(cfg.data.get("models", {}))[0]
    s = Session(cfg, model=name, cwd=workspace, persist=False, user_home=workspace)
    lvls = RZ.levels(s.profile)
    if not lvls:
        pytest.skip("这个模型没有推理档位，无法验证")
    unsupported = next((x for x in ("low", "medium", "high", "max")
                        if RZ.normalize(x) is not None and RZ.canonical(s.profile, x) not in lvls), None)
    if unsupported is None:
        pytest.skip("这个模型所有档位都支持，无法验证降级路径")
    if RZ.effective(s.profile, s.effort) == "":
        pytest.skip("起始档位已经是「不发参数」，切换不会改变请求字节")
    assert "缓存会从零开始" not in s.set_effort(unsupported)


def test_effort_switch_that_changes_no_param_is_not_a_break(workspace, ee_home):
    """当前模型不支持这个档位时 effective 退回 ""，请求字节没变 → 不该记断点（否则是误报）。"""
    from ee.config import load_config
    from ee.core import reasoning as RZ
    from ee.core.session import Session

    cfg = load_config(workspace)
    name = list(cfg.data.get("models", {}))[0]
    s = Session(cfg, model=name, cwd=workspace, persist=False, user_home=workspace)
    # 找一个「服务端实际不认」的档位：canonical 认它，但 effective 退回 ""
    unsupported = next((x for x in ("low", "medium", "high", "max")
                        if RZ.normalize(x) is not None and RZ.canonical(s.profile, x) not in RZ.levels(s.profile)), None)
    if unsupported is None:
        pytest.skip("这个模型所有档位都支持，无法验证降级路径")
    if RZ.effective(s.profile, s.effort) == "":
        pytest.skip("起始档位已经是「不发参数」，切换不会改变请求字节")
    s.set_effort(unsupported)
    assert s._take_break() is None


# ---------------------------------------------------------------- 断点判定（wire / gap）

def test_gap_within_normal_growth_is_not_a_break():
    """纯追加回合：未命中约等于这次新增的量，不该标成断点。"""
    s = UsageStats()
    s.note_wire(10_000)
    s.add({"prompt": 10_000, "completion": 50, "hit": 0}, _price(), model="m", role="main")
    s.note_wire(12_000)                       # 追加 2000
    s.add({"prompt": 12_000, "completion": 50, "hit": 10_400}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["new"] == 2_000
    assert r["gap"] == 1_600
    assert "break" not in r


def test_gap_far_beyond_growth_is_a_break():
    """历史被整段改写（压缩）：未命中远超新增，必须标成断点并带上原因。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 50, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(30_000)                       # 压缩后历史变短了
    s.add({"prompt": 30_000, "completion": 50, "hit": 3_000}, _price(), model="m", role="main",
          break_reason="compact")
    r = s.records[-1]
    assert r["gap"] == 27_000
    assert r["break"] == "compact"


def test_side_request_never_reports_a_break():
    """侧路请求的消息和主对话没有公共前缀，拿它算断点会把主对话的归因全带偏。"""
    s = UsageStats()
    s.note_wire(50_000)                       # 主对话刚发过
    s.add({"prompt": 3_000, "completion": 50, "hit": 0}, _price(), model="m", role="title")
    r = s.records[-1]
    assert "gap" not in r and "break" not in r
    assert s.last_wire_tokens == 0, "侧路不动主对话的 wire 游标"


def test_side_request_does_not_steal_the_pending_wire():
    """标题生成是后台 task，可能落在主请求 note_wire() 与 _charge() 之间。

    它不能把主请求刚记下的 wire 取走：否则那条主记录的 wire/new/gap/break 全部消失，
    断点不会报错，只会静默漏记——账本上看起来一切正常。
    """
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 10, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(120_000)                      # 主请求 2 已经准备好
    s.add({"prompt": 3_000, "completion": 50, "hit": 0}, _price(), model="m", role="title")
    assert s._wire_pending == 120_000, "侧路不能动主对话的待用 wire"
    s.add({"prompt": 120_000, "completion": 10, "hit": 0}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["wire"] == 120_000 and r["new"] == 20_000, "主请求的归因不能被侧路偷走"
    assert r["gap"] == 120_000
    assert r["break"] == "upstream_full_miss"


def test_no_baseline_is_marked_cold_and_keeps_the_known_cause():
    """首次请求/重启首发要留下明确的 cold 标记；已知原因不能因为没基线就丢掉。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 10, "hit": 0}, _price(), model="m", role="main",
          break_reason="model_switch")
    r = s.records[-1]
    assert r["baseline"] == 0 and r["cold"] is True
    assert r["cause"] == "model_switch"
    assert "break" not in r, "没有基线时不能凭 gap 断言断点，但原因必须留下"


def test_cold_requests_do_not_enter_the_watermark_sample():
    """冷启动的整段未命中不能当成「前缀不稳」；否则一次重启就把水位因子压到 0.9。"""
    s = UsageStats()
    s.note_wire(200_000)
    s.add({"prompt": 200_000, "completion": 10, "hit": 0}, _price(), model="m", role="main")
    for _ in range(6):
        s.note_wire(201_000)
        s.add({"prompt": 201_000, "completion": 10, "hit": 200_500}, _price(), model="m", role="main")
    # 排除 cold 后是 200_500/201_000；把 cold 算进来会掉到约 0.83。
    assert s.recent_main_hit_rate() == pytest.approx(200_500 / 201_000)


def test_legacy_unattributed_rows_do_not_enter_the_watermark_sample():
    """老账本里分不清冷启动/整段未命中的行也不能进水位样本（同样会把水位往下推）。"""
    s = UsageStats()
    for _ in range(6):
        s.note_wire(10_000)
        s.add({"prompt": 10_000, "completion": 10, "hit": 9_950}, _price(), model="m", role="main")
    # 老记录：没有 baseline、没有 cold/break，new==0 且 hit==0
    s.records.append({"prompt": 50_000, "completion": 10, "hit": 0, "wire": 50_000,
                      "new": 0, "gap": 50_000, "role": "main"})
    assert s.recent_main_hit_rate() == pytest.approx(0.995)


def test_known_cause_with_small_gap_is_kept_but_not_called_a_break():
    """原因已知但服务端仍命中时：记 cause，不硬造 break。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 10, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(101_000)
    s.add({"prompt": 101_000, "completion": 10, "hit": 100_500}, _price(), model="m", role="main",
          break_reason="model_switch")
    r = s.records[-1]
    assert r["cause"] == "model_switch"
    assert "break" not in r


def test_server_prompt_delta_beats_the_wire_estimate():
    """wire 只涨 1000、服务端 prompt 涨 10000 时，不能拿 1000 当新增量误判断点。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 10, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(101_000)                       # 估算只涨 1000
    s.add({"prompt": 110_000, "completion": 10, "hit": 100_000}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["new"] == 1_000
    assert r["server_new"] == 10_000
    assert r["gap"] == 10_000
    assert "break" not in r, "gap 约等于服务端新增量，是正常追加"


def test_wire_shrink_is_marked_but_not_cold():
    """wire 估算变小会产生 new==0，但它不是冷启动，单独记 shrink。"""
    s = UsageStats()
    s.note_wire(100_000)
    s.add({"prompt": 100_000, "completion": 10, "hit": 99_000}, _price(), model="m", role="main")
    s.note_wire(50_000)
    s.add({"prompt": 50_000, "completion": 10, "hit": 49_000}, _price(), model="m", role="main")
    r = s.records[-1]
    assert r["new"] == 0 and r["shrink"] is True
    assert not r.get("cold")


def test_server_new_does_not_get_reset_by_a_missing_usage_record():
    """usage_missing 的 prompt=0 不能抹掉上一次服务端基线，否则下一条又退回 wire 估算。"""
    s = UsageStats()
    s.note_wire(10_000)
    s.add({"prompt": 10_000, "completion": 10, "hit": 9_000}, _price(), model="m", role="main")
    s.note_wire(11_000)
    s.add(None, _price(), model="m", role="main")
    assert s.last_server_prompt == 10_000
    s.note_wire(12_000)
    s.add({"prompt": 12_000, "completion": 10, "hit": 10_000}, _price(), model="m", role="main")
    assert s.records[-1]["server_new"] == 2_000
