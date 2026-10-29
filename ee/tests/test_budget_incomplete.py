"""预算在「花费不可知」时的保守行为（Wave 1 / T06，对应 ACC-06）。

问题：花了钱但金额不可知（服务端没给 usage、或模型没配单价）时，
`session_spent()` 返回的是**已知部分**，缺的那部分等于按 0 计。
于是预算检查看到"还有余额"，继续放行——用户设的上限形同虚设。

正确语义：
- 有人值守：仍按已知金额判断，但要**提示**这份金额不完整，不能让人以为很准；
- 无人值守（ee exec / 自动化）：金额不可知就不能继续追加付费请求，直接停并说明原因；
- 金额完整时行为完全不变（不引入额外打扰）。
"""
from __future__ import annotations

import asyncio

import pytest

from ee.core.budget import BudgetGuard


def _guard(tmp_path, **cfg) -> BudgetGuard:
    data = {"budget": {"session": 1.0, "currency": "CNY", **cfg}}
    return BudgetGuard(data, tmp_path)


def _run(guard, spent: float, *, complete: bool = True):
    """跑一次 check：spent 是当前已花，complete 表示这份金额是否可信。"""
    notes: list[str] = []

    async def ask(scope, text):
        return "deny"

    def notify(t):
        notes.append(t)

    async def go():
        return await guard._check(lambda cur: spent, notify, ask)

    reason = asyncio.run(go())
    return reason, notes


class TestIncompleteSpend:
    def test_unattended_stops_when_amount_unknown(self, tmp_path):
        """无人值守 + 金额不可知：不能当 0 继续放行。"""
        g = _guard(tmp_path, unattended=True)
        g.accounting_complete = False
        reason, _ = _run(g, spent=0.1, complete=False)
        assert reason, "金额不可知却继续放行，无人值守的花费上限形同虚设"
        assert "费用" in reason or "不可知" in reason or "不完整" in reason

    def test_attended_warns_but_does_not_block(self, tmp_path):
        """有人值守：提示金额不完整，但仍按已知金额判断，不擅自停机。"""
        g = _guard(tmp_path, unattended=False, action="warn")
        g.accounting_complete = False
        reason, notes = _run(g, spent=0.1, complete=False)
        assert reason is None, "有人值守时不应因金额不可知直接停机"
        assert any("不完整" in n or "不可知" in n for n in notes), f"没有提示金额不完整：{notes}"

    def test_complete_amount_behaves_exactly_as_before(self, tmp_path):
        """金额完整且未超限：不提示、不停止（不引入额外打扰）。"""
        g = _guard(tmp_path, action="warn")
        g.accounting_complete = True
        reason, notes = _run(g, spent=0.1, complete=True)
        assert reason is None
        assert notes == []

    def test_complete_amount_over_limit_still_stops(self, tmp_path):
        """金额完整且超限：照旧按 action 处理（warn 提醒）。"""
        g = _guard(tmp_path, action="warn")
        g.accounting_complete = True
        reason, notes = _run(g, spent=1.5, complete=True)
        assert reason is None            # warn 模式只提醒
        assert any("上限" in n for n in notes)
