"""best-of-n 的 RPC（T10-d 界面侧）：bestof/pick。

界面要做的是**展示候选与证据**，让用户看见"为什么选它"。
这一层的纪律：

- 胜者只由 verdict 决定（模型自述不算），沿用 core/bestof 的不变量；
- 没有候选通过验证时返回 winner=None，不硬挑一个；
- 校验候选必须各有独立 worktree——共用就不是比较了。
"""
from __future__ import annotations

import asyncio

from ee.core import bestof as B
from ee.core.goal import Verdict


def _verdict(raw) -> Verdict | None:
    """把界面传来的 verdict 字典还原成 Verdict。

    Candidate.verified 读的是 `getattr(verdict, "met")`，传 dict 会恒为 False——
    那会让所有候选都判成未验证，静默变成"没选出胜者"。
    """
    if raw is None:
        return None
    if isinstance(raw, Verdict):
        return raw
    if not isinstance(raw, dict):
        return None
    return Verdict(exit=raw.get("exit"), tests=raw.get("tests"),
                   timed_out=bool(raw.get("timed_out")),
                   output=str(raw.get("output") or ""),
                   min_tests=int(raw.get("min_tests") or 1))


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError

    def _build(raw: list) -> list:
        out = []
        for i, c in enumerate(raw or []):
            if not isinstance(c, dict):
                raise RpcError(f"候选 {i} 不是对象", -32602)
            out.append(B.Candidate(
                id=str(c.get("id") or f"c{i}"),
                title=str(c.get("title") or ""),
                worktree=str(c.get("worktree") or ""),
                run_id=str(c.get("run_id") or ""),
                cost=float(c.get("cost") or 0.0),
                verdict=_verdict(c.get("verdict")),
                self_reported_ok=bool(c.get("self_reported_ok")),
            ))
        return out

    async def pick(candidates: list) -> dict:
        cands = await asyncio.to_thread(_build, candidates)
        problems = await asyncio.to_thread(B.validate, cands)
        if problems:
            raise RpcError("；".join(problems), -32602)
        winner = await asyncio.to_thread(B.pick, cands)
        wid = winner.id if winner is not None else None
        return {
            "winner": wid,
            "candidates": [c.as_dict() for c in cands],
            "report": B.report(cands, wid),
            "cleanup": B.plan_cleanup(cands, wid),   # 默认全保留
        }

    methods["bestof/pick"] = pick
