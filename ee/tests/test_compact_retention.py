"""压缩后保留最近一段原始往来（retained_suffix / build_history）。

守卫两件事（都是实测过的回归）：
1. 顺序：摘要必须排在保留段之前，保留段内部保持原始时间顺序；
2. 协议合法：截断点落在工具往来中间时不能留下孤儿 tool / tool_calls，否则下一次请求 400。
"""
from ee.core import compaction as C


def _u(text):
    return {"role": "user", "content": text}


def _a(text, calls=None):
    m = {"role": "assistant", "content": text}
    if calls:
        m["tool_calls"] = [{"id": c, "type": "function", "function": {"name": "read", "arguments": "{}"}}
                           for c in calls]
    return m


def _t(cid, text):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def test_retained_suffix_keeps_raw_order():
    hist = [_u("q1"), _a("", ["c1"]), _t("c1", "x" * 400), _a("done1"),
            _u("q2"), _a("", ["c2"]), _t("c2", "y" * 400), _a("done2")]
    _, seg = C.retained_suffix(hist, max_tokens=10_000, max_count=20)
    assert [m["role"] for m in seg] == [r for r in ("user", "assistant", "tool", "assistant",
                                                    "user", "assistant", "tool", "assistant")]
    assert seg[0]["content"] == "q1" and seg[-1]["content"] == "done2"


def test_retained_suffix_budget_stops_before_orphan():
    """预算只够尾部几条：留下的必须是完整后缀，开头的孤儿 tool 要去掉。"""
    hist = [_u("q1"), _a("", ["c1"]), _t("c1", "x" * 4000), _a("done1"), _u("q2")]
    start, seg = C.retained_suffix(hist, max_tokens=5, max_count=20)
    assert seg and seg[0]["role"] != "tool"
    assert hist[start:] == [] or [m["role"] for m in seg] == [m["role"] for m in hist[start:]][len(hist[start:]) - len(seg):]


def test_retained_suffix_drops_tool_calls_whose_result_was_cut():
    hist = [_u("q"), _a("", ["c1", "c2"]), _t("c1", "a" * 100), _t("c2", "b" * 100), _a("fin")]
    _, seg = C.retained_suffix(hist, max_tokens=100_000, max_count=2)
    assert [m["role"] for m in seg] == ["tool", "assistant"] or [m["role"] for m in seg] == ["assistant"]


def test_build_history_order_summary_before_tail():
    """历史足够长时：AGENTS -> 较早用户原话 -> 摘要 -> 保留段（原始顺序）。"""
    hist = [{"role": "user", "content": "AGENTS", "_kind": "agents"},
            _u("老问题"), _a("老回答")]
    for n in range(30):
        hist += [_u(f"q{n}"), _a("", [f"c{n}"]), _t(f"c{n}", "z" * 50), _a(f"done{n}")]
    hist += [_u("新问题"), _a("", ["c9"]), _t("c9", "读到内容"), _a("收尾")]
    new = C.build_history(hist, "摘要正文", "附录", max_user=3, user_tokens=1000, keep_tools=8)
    assert new[0].get("_kind") == "agents"
    kinds = [m.get("_kind") for m in new]
    si = kinds.index("summary")
    # 摘要在保留段之前，保留段排在最后
    assert si < len(new) - 1
    assert new[-1]["content"] == "收尾"
    assert "读到内容" in [m.get("content") for m in new]
    # 保留段里的角色顺序必须和时间顺序一致（不能把 assistant 排到 user 后面）
    tail_roles = [m["role"] for m in new[si + 1:]]
    assert tail_roles == [m["role"] for m in hist[-len(tail_roles):]]


def test_build_history_keep_tools_zero_is_old_behavior():
    hist = [_u("q1"), _a("", ["c1"]), _t("c1", "x"), _a("done"), _u("q2")]
    new = C.build_history(hist, "摘要", "", max_user=3, user_tokens=1000)
    assert [m["role"] for m in new] == ["user", "user", "user"]
    assert new[-1]["_kind"] == "summary"
    assert not any(m["role"] == "tool" for m in new)


def test_build_history_never_duplicates_recent_user_message():
    """保留段里已有的用户原话不应再被 recent_user_messages 抄一遍。"""
    hist = [_u("很久以前"), _a("答"), _u("最近的"), _a("答2")]
    new = C.build_history(hist, "摘要", "", max_user=3, user_tokens=1000, keep_tools=16)
    texts = [m.get("content") for m in new if m.get("role") == "user" and m.get("_kind") != "summary"]
    assert texts.count("最近的") == 1


# ---------------------------------------------------------------- 增量边界（压缩不能白压一遍）
#
# 压缩后的历史是 [AGENTS, 用户原话, 摘要, 保留段]。保留段排在摘要后面、且是原文，
# 于是「摘要之后有没有新内容」这个问题如果按字面回答，永远都是「有」——刚压完的会话
# 会被立刻再压一次，每次压缩都作废整段前缀缓存。下面这几个用例守的就是这条边界。


def test_has_new_since_ignores_retained_tail():
    from ee.core import compaction as C

    hist = [
        {"role": "user", "content": "早先的原话"},
        {"role": "user", "_kind": "summary", "content": "摘要", "_retained_to": 4},
        {"role": "user", "content": "保留段里的原话"},
        {"role": "assistant", "content": "保留段里的回答"},
    ]
    assert C.after_summary(hist, 1) == 4
    assert not C.has_new_since(hist, 1), "保留段不是新内容，不能据此再压一次"
    hist.append({"role": "assistant", "content": "压完之后的新回答"})
    assert C.has_new_since(hist, 1)


def test_has_new_since_falls_back_to_next_message_without_marker():
    """老会话（摘要消息没有 _retained_to）仍按摘要下一条判断，不能一上来就判成「没新内容」。"""
    from ee.core import compaction as C

    hist = [{"role": "user", "_kind": "summary", "content": "摘要"},
            {"role": "assistant", "content": "之后的内容"}]
    assert C.after_summary(hist, 0) == 1
    assert C.has_new_since(hist, 0)


def test_retained_suffix_respects_floor():
    """增量压缩时保留段不能越过上次的保留边界：越过去等于把已概括的原文捞回来。"""
    from ee.core import compaction as C

    old = [
        {"role": "user", "content": "很早"},
        {"role": "assistant", "content": "很早的回答"},
        {"role": "user", "content": "较新"},
        {"role": "assistant", "content": "较新的回答"},
    ]
    start, seg = C.retained_suffix(old, max_tokens=10_000, max_count=10, floor=2)
    assert start == 2
    assert [m["content"] for m in seg] == ["较新", "较新的回答"]
