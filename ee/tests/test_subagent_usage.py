"""ACC-04：子代理费用只入账一次（会话视图含它，日/月账只加一次）。

既有代码看起来对（`_merge_usage` 用 merged 游标去重、`_charge` 走共用 BudgetGuard），
但**没有任何测试守护**——这就是矩阵里它一直标「未开始」的原因。

两条必须同时成立：
1. **会话视图**（`parent.stats`）包含子代理的费用——用户能在父会话看到总花费；
2. **日/月账**（BudgetGuard 账本）只加一次——子代理自己的 `_charge` 已经写过，
   `_merge_usage` 复制记录时不能再写一次。

第 2 条是真正的风险点：`_merge_usage` 用 `st.add_record(r)` 复制记录，
如果哪天有人让 add_record 也写账本，就会重复计费——这个测试会立刻变红。
"""
from __future__ import annotations

import pytest

from ee.core import subagent as SA
from ee.core.context import UsageStats


class _Stub(UsageStats):
    """带可观察记录的 UsageStats（就绪时直接用真实实现）。"""


def _price(inp: float, currency: str = "CNY"):
    class P:
        def cost(self, prompt: int, completion: int, hit=None, discount: float = 1.0):
            return (prompt * inp + completion * inp * 2) / 1_000_000

        def __repr__(self):
            return f"price({inp},{currency})"
    P.currency = currency
    return P()


def _parent_child():
    """构造父会话与子代理的最小骨架：只要 stats 与合并需要的属性。"""
    parent = type("S", (), {})()
    parent.stats = UsageStats()
    child = type("S", (), {})()
    child.stats = UsageStats()
    return parent, child


def test_child_cost_lands_in_parent_stats_once():
    """子代理花一次钱 → 父会话统计算一次（不是零次，也不是两次）。"""
    parent, child = _parent_child()
    child.stats.add({"prompt": 1000, "completion": 100, "hit": 0}, _price(1.0), "child-model")

    before = parent.stats.requests
    SA._merge_usage(parent, child)
    assert parent.stats.requests == before + 1, "子代理的请求要计入父会话"
    # 金额：1000*1 + 100*2 = 1200 / 1e6 = 0.0012
    assert parent.stats.cost == pytest.approx(0.0012, rel=1e-6)


def test_merge_is_idempotent_with_cursor():
    """同一个子代理合并两次，不会重复计入（merged 游标去重）。"""
    parent, child = _parent_child()
    child.stats.add({"prompt": 1000, "completion": 100, "hit": 0}, _price(1.0), "child-model")

    merged = [0, 0]
    SA._merge_usage(parent, child, merged)
    first_cost = parent.stats.cost
    SA._merge_usage(parent, child, merged)          # 第二次：没有新记录
    assert parent.stats.cost == first_cost, "重复合并不能重复计费"
    assert parent.stats.requests == 1


def test_incremental_merge_adds_only_new_records():
    """子代理后续再花钱：第二次合并只加新的那一条。"""
    parent, child = _parent_child()
    child.stats.add({"prompt": 1000, "completion": 100, "hit": 0}, _price(1.0), "m")
    merged = [0, 0]
    SA._merge_usage(parent, child, merged)

    child.stats.add({"prompt": 2000, "completion": 200, "hit": 0}, _price(1.0), "m")
    SA._merge_usage(parent, child, merged)
    assert parent.stats.requests == 2, "新记录要并入"
    assert parent.stats.cost == pytest.approx(0.0012 + 0.0024, rel=1e-6)


def test_merge_does_not_touch_last_context_size():
    """合并不能改父会话的「上次上下文大小」——那是压缩判断用的，混进子代理会误触发压缩。"""
    parent, child = _parent_child()
    parent.stats.last_prompt, parent.stats.last_completion = 111, 22
    child.stats.add({"prompt": 1000, "completion": 100, "hit": 0}, _price(1.0), "m")
    SA._merge_usage(parent, child)
    assert (parent.stats.last_prompt, parent.stats.last_completion) == (111, 22)


def test_missing_usage_in_child_is_counted_as_missing_not_free():
    """子代理缺 usage 时，父会话要记「未知」，不能当免费。"""
    parent, child = _parent_child()
    child.stats.add(None, _price(1.0), "m")        # 缺 usage
    SA._merge_usage(parent, child)
    assert parent.stats.usage_missing == 1, "子代理的缺失要如实计入父会话"
    assert parent.stats.cost == 0, "缺 usage 时花费仍为已知部分"


# ---------------------------------------------------------------- 端到端：日/月账只加一次

def test_budget_ledger_charged_once_for_child(tmp_path):
    """真实 BudgetGuard：子代理的 _charge 写一次账，_merge_usage 不再写。

    这是 ACC-04 的核心验收面——「日/月账只加一次」必须用真实账本验证，
    不能只测内存统计（stats 对但账本重复，用户的每日上限照样会被虚耗）。
    """
    import asyncio

    from ee.core.budget import BudgetGuard

    parent, child = _parent_child()
    guard = BudgetGuard({"budget": {"daily": 100}}, tmp_path)
    parent.budget = guard
    child.budget_root = parent          # 子代理共用父会话的账本

    async def go():
        # 子代理自己记一次（真实路径：Session._charge → budget.record）
        child.budget = guard
        child._budget_guards = lambda: [guard]
        # 直接模拟 _charge 的效果：stats.add + budget.record
        cost = child.stats.add({"prompt": 1000, "completion": 100, "hit": 0}, _price(1.0), "m")
        guard.record(cost, "¥")   # 与 BudgetGuard 默认币种一致
        # 然后合并到父会话
        SA._merge_usage(parent, child)

    asyncio.run(go())
    # 账本：只应加一次 0.0012（真实口径：BudgetGuard.status 的 daily.spent）
    spend = guard.status(0.0)["scopes"]["daily"]["spent"]   # 默认币种 ¥
    assert spend == pytest.approx(0.0012, rel=1e-6), (
        f"日账应只加一次 0.0012，实际 {spend}——重复计费会让用户的上限虚耗"
    )
    assert parent.stats.cost == pytest.approx(0.0012, rel=1e-6)
