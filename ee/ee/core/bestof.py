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
    # 完成了几组完整的位置对调（一对 A/B 两轮都拿到有效选票才记 1）。
    # None = 老调用方没传这个字段，排序沿用"净胜票绝对值"的旧行为；
    # 0 与 None 不同：0 是"确实没比过"，中性，不能因为没被比就吃亏。
    comparisons: int | None = None
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
                "comparisons": self.comparisons,
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


def fair_pairs(ids: list[str], seed: str = "") -> list[tuple[str, str]]:
    """A/B 配对的公平顺序（轮转法）：每轮一人只比一次，先排"还没被比过"的人。

    **为什么不用 combinations**：`combinations(ids, 2)` 的顺序前缀全落在前几个 id 上，
    而 A/B 有全局时间预算（90 秒）——预算一用完，后面的候选一票没有，在排序里恒输。
    这是确定性偏差、可复现，不是"弱的候选真的弱"。

    做法：排序后的候选上跑轮转赛程（奇数补一个轮空位），得到 n-1 轮、每轮⌊n/2⌋ 组；
    每对只出现一次，每人每轮最多出现一次。`seed` 只影响**两处旋转**：座位顺序（谁和谁
    同轮）与轮次顺序（预算被砍时先比哪几轮）。同一 `(ids, seed)` 永远得到同一个序列。

    - `ids` 传入顺序不影响结果（内部排序 + 去重）；
    - 每对内部先后按排序后的位置固定，调用方负责做位置对调；
    - 2 路 → 1 对；3 路 → 3 对；6 路 → 15 对（顺序不再是 id 前缀）。
    """
    import hashlib

    order = sorted(dict.fromkeys(str(x) for x in ids))
    n = len(order)
    if n < 2:
        return []
    key = int(hashlib.sha256((seed or "").encode("utf-8")).hexdigest()[:8], 16)
    seats: list[str | None] = list(order[key % n:] + order[:key % n])
    if n % 2:
        seats.append(None)                      # 轮空位：奇数路时每轮有一人休息
    m = len(seats)
    rounds: list[list[tuple[str, str]]] = []
    for _ in range(m - 1):
        pairs: list[tuple[str, str]] = []
        for i in range(m // 2):
            a, b = seats[i], seats[m - 1 - i]
            if a is None or b is None:
                continue
            lo, hi = (a, b) if order.index(a) < order.index(b) else (b, a)
            pairs.append((lo, hi))
        rounds.append(pairs)
        seats = [seats[0], seats[-1], *seats[1:-1]]   # 固定第一个位，其余轮转
    if rounds:
        shift = (key // max(1, n)) % len(rounds)
        rounds = rounds[shift:] + rounds[:shift]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pairs in rounds:
        for pair in pairs:
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def _vote_score(c: Candidate) -> float:
    """净胜票的**机会归一化**分：比过的按场均净胜，没比过的中性 0。

    为什么需要它：A/B 有全局时间预算，6 路时 15 组对照不可能都做完。以前直接比
    净胜票绝对值，于是"只被比了 1 次且赢了"的候选会压过"被比了 5 次净胜 2"的
    候选，而排在前面的 id 天然先被比——这是确定性偏差，不是质量差异。

    `comparisons is None` 保留旧调用方的语义（raw votes）。
    """
    votes = int(c.votes or 0)
    if c.comparisons is None:
        return float(votes)
    if c.comparisons <= 0:
        return 0.0
    return votes / float(c.comparisons)


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
        -_vote_score(c),                 # 场均净胜（没比过 = 中性 0，不吃亏）
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


def _vote_text(c: Candidate) -> str:
    """模型对照得分的人话。与 `_rank_key` 同一口径：场均净胜，不是票数绝对值。"""
    if c.comparisons is None:
        return f"净胜 {int(c.votes or 0)} 票（旧数据无比较次数，按原始票数）"
    if c.comparisons <= 0:
        return "未做模型对照（中性，既不加分也不扣分）"
    return (f"场均净胜 {_vote_score(c):+.2f}（净胜 {int(c.votes or 0)} 票 / "
            f"{int(c.comparisons)} 组完整对照）")


def pick_reason(winner: Candidate | None, candidates: list[Candidate]) -> str:
    """一句话说明「凭什么选它」，用于 report 与界面。

    归因必须与 `_rank_key` 一致：排序用的是 `_vote_score`（场均净胜），所以
    "票数相同但比较次数不同"也属于模型对照决定的胜负——旧实现按 raw votes 相等
    就跳到测试数/改动面，会把真实原因说错。
    """
    if winner is None:
        return "没有候选通过判据，不选（模型自述不算证据）"
    if any(_vote_score(c) != _vote_score(winner) for c in candidates):
        return f"{winner.id} 通过了判据，按「A/B 模型对照」选中：{_vote_text(winner)}"
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
        # 比较覆盖要跟票数一起报："净胜 2"和"2 组对照里净胜 2"是两个说服力。
        vs = ""
        if c.comparisons is not None:
            vs = f" A/B {int(c.votes):+d}/{int(c.comparisons)} 组"
        elif int(c.votes or 0):
            vs = f" A/B 净胜 {int(c.votes)}"
        lines.append(f"- {c.id} [{mark}] {c.title}{cost}{vs} → {c.reason}")
    win = next((c for c in candidates if c.id == winner_id), None)
    lines.append(f"选中理由：{pick_reason(win, candidates)}")
    return "\n".join(lines)
