"""排除项锚点：只抽结论性的否定，不恢复推理，不重写旧消息。"""
from __future__ import annotations

from conftest import collect, finish, make_session, reasoning_chunk, run, text_chunks

from ee.core import anchors as A


# ---------------------------------------------------------------- 纯函数

def test_extract_keeps_negative_conclusions_only():
    text = ("先看 A。A 不可行，因为缺少依赖。B 是可行方向。"
            "The retry approach is not feasible. Use C instead.")
    got = " ".join(A.extract(text))
    assert "A 不可行" in got
    assert "not feasible" in got
    assert "可行方向" not in got, "肯定句不该被抽成锚点"
    assert "Use C instead" not in got


def test_extract_ignores_too_short_fragments():
    assert A.extract("不是") == []
    assert A.extract("这个不是") == []          # 太短，没有信息量
    assert A.extract("这个方案不是") == []       # “不是”是口语否定，不当作结论锚点
    assert A.extract("这个方案不可行") != []


def test_from_history_ignores_content_and_users():
    history = [
        {"role": "user", "content": "A 不可行"},
        {"role": "assistant", "content": "A 不可行"},
        {"role": "assistant", "reasoning": "A 不可行因为缺少依赖。"},
    ]
    assert A.from_history(history) == ["A 不可行因为缺少依赖"]


def test_repeated_claim_moves_to_the_latest_position():
    history = [
        {"role": "assistant", "reasoning": "A 不可行。"},
        {"role": "assistant", "reasoning": "B 不可行。"},
        {"role": "assistant", "reasoning": "A 不可行。"},
    ]
    assert A.from_history(history) == ["B 不可行", "A 不可行"]


def test_anchor_count_and_total_size_are_capped():
    history = [{"role": "assistant", "reasoning": f"方案 {i} 不可行。"} for i in range(10)]
    got = A.from_history(history)
    assert len(got) == A.MAX_ANCHORS
    assert got[-1] == "方案 9 不可行", "保留最近几条"
    assert sum(len(x) + 3 for x in got) <= A.MAX_TOTAL


def test_render_is_a_stable_block():
    assert A.render([]) == ""
    text = A.render(["A 不可行"])
    assert text.startswith("<excluded>") and text.endswith("</excluded>")
    assert "- A 不可行" in text


def test_enabled_is_opt_in():
    assert A.enabled({}) is False, "默认关：试点没有看到收益"
    assert A.enabled({"prune": {"exclusion_anchors": True}}) is True
    assert A.enabled({"prune": {"exclusion_anchors": "true"}}) is False, "只认真正的布尔 true"
    assert A.enabled({"prune": {"exclusion_anchors": True, "strip_reasoning": False}}) is False, \
        "推理没清就不重复加锚点"


# ---------------------------------------------------------------- 接线：附录与缓存

def test_excluded_claims_land_in_the_next_user_turn(ee_home, workspace):
    s, _fake = make_session(workspace, [
        [reasoning_chunk("先试 A。A 不可行，因为缺依赖。")] + text_chunks("好") + finish("stop", prompt=1000),
        text_chunks("继续") + finish("stop", prompt=1100, hit=900),
    ])
    run(collect(s, "问一"))
    s.config.data.setdefault("prune", {})["exclusion_anchors"] = True
    first_user = [m for m in s.history if m["role"] == "user"][0]["content"]
    run(collect(s, "问二"))
    users = [m for m in s.history if m["role"] == "user"]
    assert users[0]["content"] == first_user, "锚点变化不能改写已发出的旧消息（缓存）"
    assert "<excluded>" in users[-1]["content"]
    assert "A 不可行" in users[-1]["content"]


def test_anchors_are_off_by_default(ee_home, workspace):
    s, _fake = make_session(workspace, [
        [reasoning_chunk("A 不可行，因为缺依赖。")] + text_chunks("好") + finish("stop", prompt=1000),
        text_chunks("继续") + finish("stop", prompt=1100, hit=900),
    ])
    run(collect(s, "问一"))
    run(collect(s, "问二"))
    assert "<excluded>" not in [m for m in s.history if m["role"] == "user"][-1]["content"]


def test_meta_discussion_and_hypotheticals_are_not_anchors():
    """推理里“讨论候选/假设”不算已排除：留着会变成噪声，甚至误导模型。"""
    assert A.extract("不可行的：") == []
    assert A.extract("另一个看似可行其实不可行的：只改判断") == []
    assert A.extract("如果 A 不可行，就走 B") == []
    assert A.extract("候选修法 A 不可行") == []
    assert A.extract("Let me also confirm this is unlikely") == []
    assert A.extract('"不要动手改代码" — writing a temp file isn’t changing repo code') == []
    # 真正断言过的结论仍然保留
    assert A.extract("改测试断言不可行：会破坏其他测试") != []
    assert A.extract("总之改测试不可行") != []


def test_bare_english_fragments_are_not_anchors():
    """“Not feasible” 没有主语，当锚点没有信息量；完整句子仍然保留。"""
    assert A.extract("Not feasible") == []
    assert A.extract("The retry approach is not feasible") != []
