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

from dataclasses import dataclass


@dataclass
class Candidate:
    """一个候选。`verdict` 是唯一能证明它「已验证」的东西。

    但 Verdict 只对**跑判据那一刻的代码**成立。`verified_state` 记下当时的
    代码状态指纹，`current_state` 是调用方随后读到的状态；两者不一致即 `stale`
    ——证据过期，不能再声称已验证（ACC-16）。
    """
    id: str
    title: str = ""
    worktree: str = ""                 # 各自独立的 worktree 路径
    run_id: str = ""                   # 对应 meta.runs 里的 run
    cost: float = 0.0
    verdict: object | None = None      # core.goal.Verdict
    self_reported_ok: bool = False     # 模型自己说通过 —— 不参与判定，只记录
    verified_state: str | None = None  # 跑判据时的代码状态指纹
    current_state: str | None = None   # 现在的代码状态指纹（调用方给；None = 未比对）
    # ---- allout 的质量信号（全部中性默认：老调用方不传时行为与 T10-d 完全一致）
    regressed: bool = False            # 回归门：弄坏了改动前已通过的测试
    votes: int = 0                     # 同模型 A/B 位置对调后的净胜票数
    touched_tests: bool = False        # 改动碰过测试文件（可疑，排在后面）
    files: int = 0                     # 改动文件数（改动面）
    additions: int = 0                 # 新增行数
    deletions: int = 0                 # 删除行数

    @property
    def stale(self) -> bool:
        """验证之后代码是否变过。未绑定状态（老数据）不算 stale，保持旧行为。"""
        if self.verified_state is None or self.current_state is None:
            return False
        return self.verified_state != self.current_state

    @property
    def met(self) -> bool:
        return bool(getattr(self.verdict, "met", False))

    @property
    def verified(self) -> bool:
        """只有判据通过、且代码没变过才算已验证。自述通过不构成证据。"""
        if self.verdict is None:
            return False
        if self.stale:                 # 证据过期：证明的是旧代码
            return False
        return bool(getattr(self.verdict, "met", False))

    @property
    def reason(self) -> str:
        if self.verdict is None:
            return "没有判据结果（模型自述不算证据）" if self.self_reported_ok else "没有判据结果"
        base = str(getattr(self.verdict, "reason", "") or "")
        if self.stale:
            return f"{base}（验证后代码已变，证据过期）" if base else "验证后代码已变，证据过期"
        return base

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "worktree": self.worktree,
                "run_id": self.run_id, "cost": self.cost, "verified": self.verified,
                "met": self.met, "self_reported_ok": self.self_reported_ok,
                "stale": self.stale, "reason": self.reason,
                # allout 的质量信号（老调用方不传时全是中性默认）
                "regressed": self.regressed, "votes": self.votes,
                "touched_tests": self.touched_tests, "files": self.files,
                "additions": self.additions, "deletions": self.deletions,
                # 跨请求比对要用：调用方拿 verified_state 存起来，下次连同 current_state 传回来
                "verified_state": self.verified_state, "current_state": self.current_state}


def recheck(candidates: list[Candidate], states: dict[str, str]) -> list[Candidate]:
    """用当前状态重新核对各候选：改过的标 stale，读不到的不下结论。

    `states` 是 {候选 id: 当前状态指纹}。空串表示读不到（目录没了/git 挂了）——
    这种情况**不判 stale**：读不到不等于变了，不能凭空降级一个可能仍然有效的结论。
    本函数只改 `current_state`，不动 verdict；`verified`/`stale` 是派生属性。
    """
    for c in candidates:
        st = (states or {}).get(c.id) or ""
        c.current_state = st or None
    return candidates


def state_fingerprint(head: str | None, dirty: str | None) -> str:
    """候选代码状态的指纹：HEAD 提交 + 工作区改动摘要。

    判据是对**这份代码**跑的；两者任一变化，证据就过期了。
    调用方负责取 head/dirty（本模块不碰磁盘，保持纯判断）。
    """
    return f"{head or '-'}:{dirty or '-'}"


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


def _rank_key(c: Candidate) -> tuple:
    """胜者的排序键（allout v2 §8）。每一条都来自可观测事实，没有模型自述。

    顺序即优先级：回归门 → A/B 净胜票 → 测试数多 → 改动面小 → 未碰测试
    → 成本低 → id（稳定排序，不因输入顺序碰运气）。
    没给新字段的老数据全是中性默认，于是退化成原来的「成本 → id」。
    """
    tests = int(getattr(c.verdict, "tests", 0) or 0)
    size = int(c.files or 0) + int(c.additions or 0) + int(c.deletions or 0)
    return (
        0 if not c.regressed else 1,     # 回归过的一律排后面
        -int(c.votes or 0),              # 净胜票多者优先
        -tests,                          # 覆盖更完整的优先
        size,                            # 改动面小者优先
        0 if not c.touched_tests else 1, # 没碰测试的优先
        float(c.cost or 0.0),            # 同等条件下成本低者优先
        c.id,
    )


def pick(candidates: list[Candidate]) -> Candidate | None:
    """选出胜者。没有任何候选已验证时返回 None —— 不凭自述挑。

    「已验证」仍是硬门槛；在此之上按 `_rank_key` 排序。全部回归的候选
    不算赢（宁可不写回，也不把弄坏旧测试的改动写进主目录）。
    """
    verified = [c for c in candidates if c.verified]
    if not verified:
        return None
    clean = [c for c in verified if not c.regressed]
    if not clean:
        return None
    clean.sort(key=_rank_key)
    return clean[0]


def pick_reason(winner: Candidate | None, candidates: list[Candidate]) -> str:
    """一句话说明「凭什么选它」，用于 report 与界面。"""
    if winner is None:
        return "没有候选通过判据，不选（模型自述不算证据）"
    if int(winner.votes or 0) and any(int(c.votes or 0) != int(winner.votes or 0) for c in candidates):
        return f"{winner.id} 通过了判据，且同模型 A/B 净胜 {int(winner.votes)} 票"
    tests = int(getattr(winner.verdict, "tests", 0) or 0)
    if tests and any(int(getattr(c.verdict, "tests", 0) or 0) != tests for c in candidates):
        return f"{winner.id} 通过了判据，按「测试覆盖更完整」选中（{tests} 个测试）"
    size = int(winner.files or 0) + int(winner.additions or 0) + int(winner.deletions or 0)
    if size and any((int(c.files or 0) + int(c.additions or 0) + int(c.deletions or 0)) != size
                    for c in candidates):
        return f"{winner.id} 通过了判据，按「改动面更小」选中（{size} 处改动）"
    if any(bool(c.touched_tests) != bool(winner.touched_tests) for c in candidates):
        return f"{winner.id} 通过了判据，且没有改动测试文件"
    return f"{winner.id} 通过了判据（{winner.reason}）"


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
    lines.append(f"选中理由：{pick_reason(win, candidates)}")
    return "\n".join(lines)
