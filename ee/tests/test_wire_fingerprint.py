"""wire 指纹：断点定位的纯函数 + 账本接线。

为什么需要这一层：命中率只回答「漏了多少」，回答不了「从哪一条开始不一样」。
逐条消息指纹和上一次已记账的请求比对，才能把 unknown 断点分成
「正常追加 / 客户端改写历史 / 换了模型或端点」——否则账本上永远只有一个大问号。
"""
from __future__ import annotations

from ee.core.context import UsageStats, compare_wire, message_signature, wire_fingerprint
from ee.core.providers.profiles import Price

SYS = {"role": "system", "content": "sys"}
TOOLS = [{"type": "function", "function": {"name": "a"}}]


def _price() -> Price:
    return Price(input=2.0, output=8.0, cache_read=0.04)


def _fp(history, *, model="m", effort="high", provider="p", base="u"):
    return wire_fingerprint(SYS, TOOLS, history, options_key=f"{model}|{effort}",
                            endpoint_key=f"{provider}|{base}")


def _usage(prompt=1000, hit=0):
    return {"prompt": prompt, "completion": 10, "hit": hit}


# ---------------------------------------------------------------- 纯函数

def test_signature_ignores_internal_keys():
    """下划线键是 ee 内部字段，不会发出去，不该让指纹变。"""
    a = {"role": "user", "content": "x", "_images": ["C:/a.png"]}
    b = {"role": "user", "content": "x", "_images": []}
    assert message_signature(a) == message_signature(b)
    assert message_signature({"role": "user", "content": "y"}) != message_signature(b)


def test_first_request_has_no_baseline():
    d = compare_wire(None, _fp([{"role": "user", "content": "a"}]))
    assert d["wire_cmp"] == "baseline_missing"
    assert "wire_diverged" not in d


def test_append_is_not_a_rewrite():
    prev = _fp([{"role": "user", "content": "a"}])
    cur = _fp([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    d = compare_wire(prev, cur)
    assert d["wire_cmp"] == "appended"
    assert "wire_diverged" not in d
    assert "msgs_changed" not in d


def test_identical_history_is_equal():
    prev = _fp([{"role": "user", "content": "a"}])
    assert compare_wire(prev, prev)["wire_cmp"] == "equal"


def test_middle_rewrite_reports_the_first_diverged_index():
    prev = _fp([{"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "tool", "content": "c"}])
    cur = _fp([{"role": "user", "content": "a"},
               {"role": "assistant", "content": "CHANGED"},
               {"role": "tool", "content": "c"}])
    d = compare_wire(prev, cur)
    assert d["wire_cmp"] == "rewritten"
    assert d["wire_diverged"] == 1, "第 0 条相同，第一条不同"
    assert d["msgs_changed"] is True


def test_truncation_is_a_rewrite_not_a_shorter_append():
    prev = _fp([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    cur = _fp([{"role": "user", "content": "a"}])
    assert compare_wire(prev, cur)["wire_cmp"] == "rewritten"


def test_system_or_tools_change_is_a_new_cache_generation():
    hist = [{"role": "user", "content": "a"}]
    prev = _fp(hist)
    changed_sys = wire_fingerprint({"role": "system", "content": "SYS2"}, TOOLS, hist,
                                   options_key="m|high", endpoint_key="p|u")
    assert compare_wire(prev, changed_sys)["wire_cmp"] == "generation_changed"
    assert compare_wire(prev, changed_sys)["sys_changed"] is True
    changed_tools = wire_fingerprint(SYS, TOOLS + [{"type": "function", "function": {"name": "b"}}], hist,
                                     options_key="m|high", endpoint_key="p|u")
    assert compare_wire(prev, changed_tools)["tools_changed"] is True


def test_model_or_endpoint_change_is_a_generation_even_if_history_is_identical():
    hist = [{"role": "user", "content": "a"}]
    prev = _fp(hist)
    assert compare_wire(prev, _fp(hist, model="other"))["wire_cmp"] == "generation_changed"
    assert compare_wire(prev, _fp(hist, base="other"))["wire_cmp"] == "generation_changed"


# ---------------------------------------------------------------- 账本接线

def test_fingerprint_lands_on_the_request_row():
    s = UsageStats()
    s.note_wire(1000, _fp([{"role": "user", "content": "a"}]))
    s.add(_usage(), _price(), model="m")
    row = s.records[-1]
    assert row["wire_cmp"] == "baseline_missing"
    assert row["wire_msgs"] == 1
    assert len(row["sys"]) == 12 and len(row["tools"]) == 12


def test_retry_does_not_move_the_baseline():
    """400 回退 / 重试会用同一条请求再调一次 note_wire：基线不能跟着走。"""
    s = UsageStats()
    a = [{"role": "user", "content": "a"}]
    b = a + [{"role": "assistant", "content": "b"}]
    s.note_wire(1000, _fp(a))
    s.add(_usage(), _price(), model="m")
    s.note_wire(1100, _fp(b))
    s.note_wire(1100, _fp(b))          # 同一条请求重试
    s.add(_usage(1100, 400), _price(), model="m")
    assert s.records[-1]["wire_cmp"] == "appended"


def test_side_request_does_not_consume_the_pending_fingerprint():
    s = UsageStats()
    a = [{"role": "user", "content": "a"}]
    b = a + [{"role": "assistant", "content": "b"}]
    s.note_wire(1000, _fp(a))
    s.add(_usage(), _price(), model="m")
    s.note_wire(1100, _fp(b))
    s.add({"prompt": 20, "completion": 1, "hit": 0}, _price(), model="m", role="title")
    assert "wire_cmp" not in s.records[-1]
    s.add(_usage(1100, 400), _price(), model="m")
    assert s.records[-1]["wire_cmp"] == "appended", "侧路请求把主请求的指纹偷走了"


def test_rewrite_sets_a_cause_but_not_a_break_when_gap_is_small():
    s = UsageStats()
    a = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    s.note_wire(1000, _fp(a))
    s.add(_usage(), _price(), model="m")
    s.note_wire(1100, _fp([{"role": "user", "content": "a"}, {"role": "assistant", "content": "X"}]))
    s.add(_usage(1100, 1000), _price(), model="m")
    row = s.records[-1]
    assert row["cause"] == "client_history_rewrite"
    assert "break" not in row, "gap 只有 100，没有达到断点阈值"


def test_rewrite_is_attributed_to_the_client_not_upstream():
    """本地指纹确认改写了历史时，断点原因不该再落进 unknown（那是上游失配的位置）。"""
    s = UsageStats()
    a = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    s.note_wire(100_000, _fp(a))
    s.add(_usage(100_000, 99_000), _price(), model="m")
    s.note_wire(100_100, _fp([{"role": "user", "content": "a"}, {"role": "assistant", "content": "X"}]))
    s.add(_usage(100_100, 100), _price(), model="m")
    row = s.records[-1]
    assert row["break"] == "client_history_rewrite"
    assert row["cause"] == "client_history_rewrite"
    assert row["wire_diverged"] == 1


def test_known_cause_wins_over_the_fingerprint():
    s = UsageStats()
    a = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    s.note_wire(100_000, _fp(a))
    s.add(_usage(100_000, 99_000), _price(), model="m")
    s.note_wire(100_100, _fp([{"role": "user", "content": "a"}, {"role": "assistant", "content": "X"}]))
    s.add(_usage(100_100, 100), _price(), model="m", break_reason="compact")
    row = s.records[-1]
    assert row["cause"] == "compact"
    assert row["break"] == "compact"


def test_tail_and_idle_are_recorded_for_main_only():
    s = UsageStats()
    s.note_wire(1000, _fp([{"role": "user", "content": "a"}]))
    s.add(_usage(1000, 900), _price(), model="m", cache_spec={"block": 64})
    row = s.records[-1]
    assert row["tail"] == 1000 % 64
    s.add({"prompt": 1000, "completion": 10, "hit": 0}, _price(), model="m", role="title",
          cache_spec={"block": 64})
    assert "tail" not in s.records[-1], "侧路请求不参与主对话的尾块口径"


def test_no_tail_when_the_provider_has_no_declared_block_size():
    s = UsageStats()
    s.note_wire(1000, _fp([{"role": "user", "content": "a"}]))
    s.add(_usage(1000, 900), _price(), model="m", cache_spec={"block": 0})
    assert "tail" not in s.records[-1], "不知道块大小就不猜"


def test_prompt_below_the_minimum_is_uncacheable_not_a_tail():
    """低于服务商最短长度时整个 prompt 进不了缓存；不能记成尾块（会把尾块占比说大）。"""
    s = UsageStats()
    s.note_wire(800, _fp([{"role": "user", "content": "a"}]))
    s.add(_usage(800, 0), _price(), model="m", cache_spec={"block": 128, "min": 1024})
    row = s.records[-1]
    assert row.get("uncacheable") is True
    assert "tail" not in row and "break" not in row, "结构上不可能命中，不是断点"


def test_no_tail_for_providers_without_cache():
    s = UsageStats()
    s.note_wire(1000, _fp([{"role": "user", "content": "a"}]))
    s.add(_usage(1000, 0), _price(), model="m", cache_spec={"type": "none", "block": 64})
    assert "tail" not in s.records[-1]


def test_idle_beyond_the_provider_ttl_is_marked_expired():
    """空闲超过 TTL：整段未命中是服务端过期，不是前缀不稳，也不该记断点。"""
    s = UsageStats()
    a = [{"role": "user", "content": "a"}]
    s.note_wire(100_000, _fp(a))
    s.add(_usage(100_000, 99_000), _price(), model="m", cache_spec={"ttl": 300})
    s._last_main_at -= 400                        # 模拟空闲 400 秒（超过 300s TTL）
    s.note_wire(100_100, _fp(a + [{"role": "assistant", "content": "b"}]))
    s.add(_usage(100_100, 100), _price(), model="m", cache_spec={"ttl": 300})
    row = s.records[-1]
    assert row.get("cold_ttl") is True
    assert row["idle_s"] >= 400
    assert "break" not in row and "cause" not in row
