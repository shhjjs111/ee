"""best-of-n 的评选与验证记录（T10-d）。

方案原文（`stabilization-and-d6.md:134`）：
> 候选有独立 run_id/worktree/费用/验证记录；「模型说测试通过」不能进入已验证列。
> 只合并选中候选，其余默认保留。

**本模块唯一的核心是「谁算已验证」**：判据来自 Goal 的真实 `Verdict`
（退出码 + 真实测试计数），不是模型的自我评价。没有 Verdict 的候选一律
unverified——这与本项目在 CI 上踩过的「0 passed 也是 exit 0」是同一条教训。

不在这里做的事（有意为之）：
- 不创建 worktree（`core/worktree.py` 已有，调用方负责）
- 不跑判据（`core/goal.py` 已有）
- 不合并（合并是用户确认后的动作，见 worktree 的 merge 流程）

本模块只做纯判断：给定一组候选，谁该被选中、谁保留、报告长什么样。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """一个候选。`verdict` 是唯一能证明它「已验证」的东西。"""
    id: str
    title: str = ""
    worktree: str = ""                 # 各自独立的 worktree 路径
    run_id: str = ""                   # 对应 meta.runs 里的 run
    cost: float = 0.0
    verdict: object | None = None      # core.goal.Verdict
    self_reported_ok: bool = False     # 模型自己说通过 —— 不参与判定，只记录

    @property
    def met(self) -> bool:
        return bool(getattr(self.verdict, "met", False))

    @property
    def verified(self) -> bool:
        """只有判据通过才算已验证。自述通过（self_reported_ok）不构成证据。"""
        if self.verdict is None:
            return False
        return bool(getattr(self.verdict, "met", False))

    @property
    def reason(self) -> str:
        if self.verdict is None:
            return "没有判据结果（模型自述不算证据）" if self.self_reported_ok else "没有判据结果"
        return str(getattr(self.verdict, "reason", "") or "")

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "worktree": self.worktree,
                "run_id": self.run_id, "cost": self.cost, "verified": self.verified,
                "met": self.met, "self_reported_ok": self.self_reported_ok,
                "reason": self.reason}


def validate(candidates: list[Candidate]) -> list[str]:
    """集合层面的校验。返回问题列表（空 = 合法）。"""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for c in candidates:
        wt = (c.worktree or "").rstrip("/\\").lower()
        if not wt:
            problems.append(f"候选 {c.id} 没有 worktree")
            continue
        if wt in seen:
            # 共用 worktree 等于改同一份文件，比不出高下
            problems.append(f"候选 {c.id} 与 {seen[wt]} 共用 worktree：{c.worktree}")
        else:
            seen[wt] = c.id
    ids = [c.id for c in candidates]
    if len(ids) != len(set(ids)):
        problems.append("候选 id 有重复")
    return problems


def pick(candidates: list[Candidate]) -> Candidate | None:
    """选出胜者。没有任何候选已验证时返回 None —— 不凭自述挑。"""
    verified = [c for c in candidates if c.verified]
    if not verified:
        return None
    # 先按花费低，再按 id：同花费时结果稳定，不因输入顺序让候选「碰运气」
    verified.sort(key=lambda c: (float(c.cost or 0.0), c.id))
    return verified[0]


def plan_cleanup(candidates: list[Candidate], winner_id: str | None,
                 remove_losers: bool = False) -> dict:
    """默认全保留（数据保全）。只有显式 remove_losers 才清理落选者。"""
    keep = [c.id for c in candidates]
    remove: list[str] = []
    if remove_losers and winner_id:
        keep = [c.id for c in candidates if c.id == winner_id]
        # 胜者永远不进 remove
        remove = [c.id for c in candidates if c.id != winner_id]
    return {"keep": keep, "remove": remove}


def report(candidates: list[Candidate], winner_id: str | None) -> str:
    """人可复核的报告：每个候选一行证据 + 结论。"""
    lines = [f"best-of-n：{len(candidates)} 个候选，胜者 {winner_id or '无（没有候选通过验证）'}"]
    for c in candidates:
        mark = "已验证" if c.verified else "未验证"
        cost = f" ¥{float(c.cost or 0):.4f}" if c.cost else ""
        lines.append(f"- {c.id} [{mark}] {c.title}{cost} → {c.reason}")
    win = next((c for c in candidates if c.id == winner_id), None)
    if win is not None:
        lines.append(f"选中理由：{win.id} 通过了判据（{win.reason}）")
    else:
        lines.append("选中理由：没有候选通过判据，不选（模型自述不算证据）")
    return "\n".join(lines)
