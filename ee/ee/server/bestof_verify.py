"""best-of-n 的判据编排（T10-d 界面侧）：bestof/verify。

界面原来把 worktree 列表当候选提交给 `bestof/pick`，但**不带 verdict**，
所以后端一律判未验证、永远「不选」——这是交接文档里唯一的实质卡点。

这一层补的就是那一步：为每个候选 worktree **各跑一次判据**，把 `Verdict`
回填到候选，再交给 `bestof/pick` 选。纪律：

- **只观测，不修复**：候选代码已经写完了，这里只跑判据（`goal.judge`），
  不跑 `run_goal` 的续跑循环——否则「重跑一次测试」会变成「修改候选」，
  证据就不再属于同一个候选。这也让预算可控：N 个候选 = N 次判据。
- **路径必须是我们登记过的 worktree**：不新开「任意目录跑任意命令」的口子。
  判据命令是用户给的，但执行目录只能是自己建的 worktree。
- **判据校验复用 `threads._norm_goal`**：不复制一份规则，避免两处漂移。
- **不假装**：拿不到退出码、超时、零测试都不算通过，沿用 `core/goal` 的判据。
"""
from __future__ import annotations

import asyncio

from ee.core import bestof as B
from ee.core import worktree as W
from ee.core.goal import GoalSpec, Verdict, judge


def register(methods: dict, backend) -> None:
    from ee.server.app import RpcError
    from ee.server.threads import _norm_goal

    def _registered_paths(home) -> set[str]:
        """我们登记过的 worktree 路径（归一化）。候选只能在这些目录里跑。"""
        return {W._norm(x["path"]) for x in W.list_for(None, home, with_status=False)}

    def _resolve(raw: str, allowed: set[str]) -> str:
        if not raw:
            raise RpcError("候选没有 worktree", -32602)
        key = W._norm(raw)
        if key not in allowed:
            raise RpcError(f"候选 {raw} 不是 ee 登记的 worktree，不在里面跑判据", -32602)
        return raw

    async def verify(goal: dict, candidates: list) -> dict:
        """对每个候选各跑一次判据，回填 Verdict，再交 bestof/pick 选。

        返回形状与 `bestof/pick` 一致，界面共用一套渲染。
        """
        try:
            g = _norm_goal(goal)
        except ValueError as e:
            raise RpcError(str(e), -32602) from None

        if not isinstance(candidates, list) or not candidates:
            raise RpcError("候选不能为空", -32602)

        allowed = await asyncio.to_thread(_registered_paths, backend.home)
        cands: list[B.Candidate] = []
        for i, c in enumerate(candidates):
            if not isinstance(c, dict):
                raise RpcError(f"候选 {i} 不是对象", -32602)
            wt = _resolve(str(c.get("worktree") or ""), allowed)
            cands.append(B.Candidate(
                id=str(c.get("id") or f"c{i}"),
                title=str(c.get("title") or ""),
                worktree=wt,
                run_id=str(c.get("run_id") or ""),
                cost=float(c.get("cost") or 0.0),
                # 界面自报的「通过」只记录，不参与判定
                self_reported_ok=bool(c.get("self_reported_ok")),
            ))

        problems = await asyncio.to_thread(B.validate, cands)
        if problems:
            raise RpcError("；".join(problems), -32602)

        # 各候选并行跑判据：它们本来就是独立目录，串行只会让界面白等。
        # 单轮 judge（不续跑），超时用 goal.timeout，默认 300s。
        spec_kwargs = {"command": g["command"], "min_tests": int(g.get("min_tests") or 1),
                       "timeout": float(g.get("timeout") or 300.0)}

        async def _one(c: B.Candidate) -> Verdict:
            return await judge(GoalSpec(cwd=c.worktree, **spec_kwargs))

        verdicts = await asyncio.gather(*[_one(c) for c in cands])
        for c, v in zip(cands, verdicts):
            c.verdict = v

        winner = await asyncio.to_thread(B.pick, cands)
        wid = winner.id if winner is not None else None
        return {
            "winner": wid,
            "candidates": [c.as_dict() for c in cands],
            "report": B.report(cands, wid),
            "cleanup": B.plan_cleanup(cands, wid),
        }

    methods["bestof/verify"] = verify
