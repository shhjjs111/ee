"""「上次上下文大小」只能由主对话更新（2026-11 审计修复）。

UsageStats.add_record 对任何 role 都无条件改写 last_prompt / last_completion。
而侧路请求（AI 标题、/commit 生成提交信息、压缩、以及子代理合并进来的记录）
的 prompt 是另一段内容：一次标题请求就能把 13 万改成 2600。

后果不是显示问题：session.context_tokens() 优先读 last_prompt，
needs_compaction()、prune.due() 和回合内的 urgent 判断都据此比较水位，
于是「本该压缩」的回合被判定成「上下文还很小」而跳过，直到撞上服务端超长 400
才靠 overflow 分支补救。子代理路径早就手工保存-恢复这两个字段（subagent.py），
说明作者知道这个坑，只是侧路没同样处理。
"""
from ee.core.context import UsageStats


def test_side_requests_do_not_move_the_watermark():
    st = UsageStats()
    st.add({"prompt": 100_000, "completion": 1_200}, None, role="main")

    for role, prompt in (("title", 2_600), ("commit", 60_000),
                         ("compact", 180_000), ("subagent", 5_000)):
        st.add({"prompt": prompt, "completion": 100}, None, role=role)
        assert st.last_prompt == 100_000, f"{role} 请求改写了水位"
        assert st.last_completion == 1_200

    # 累计量照常计（只是不改「上次」）
    assert st.prompt == 100_000 + 2_600 + 60_000 + 180_000 + 5_000


def test_main_requests_still_move_the_watermark():
    st = UsageStats()
    st.add({"prompt": 1_000, "completion": 10}, None, role="main")
    st.add({"prompt": 2_000, "completion": 20}, None, role="main")
    assert (st.last_prompt, st.last_completion) == (2_000, 20)


def test_missing_usage_does_not_touch_the_watermark():
    """缺 usage 的请求只是记一条 usage_missing，不该把水位清零。"""
    st = UsageStats()
    st.add({"prompt": 100_000, "completion": 1_200}, None, role="main")
    st.add(None, None, role="title")
    assert st.last_prompt == 100_000
    assert st.usage_missing == 1


def test_record_without_role_is_treated_as_main():
    """老记录 / 直接构造的记录可能没有 role 字段——按主对话处理，保持旧行为。"""
    st = UsageStats()
    st.add_record({"prompt": 5_000, "completion": 50, "hit": None,
                   "reasoning": None, "cost": None})
    assert st.last_prompt == 5_000
