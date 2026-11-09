"""生产路径的 A/B 排序（allout 阶段 5）。

真模型演练已证明两件事：A/B 提示词能区分「诚实修复」与「改测试作弊」
（两轮位置对调都投前者），以及分叉的隔离/写回。缺的最后一块是
`Thread._allout_rank` 这段**生产接线**本身：两个候选都通过时，它是否
真的进行位置对调两轮、把净胜票写回候选、并交给 bestof.pick。

这里用真实 git 仓库里的两个候选 + 桩 `side_complete`（返回脚本化的
FINAL 标记）覆盖它——判定输入是真实补丁，评定模型是桩，这正是
「接线测试」该有的边界。
"""
from __future__ import annotations

import asyncio
import subprocess


class _V:
    """Verdict 的最小替身：Candidate 只读 met / tests / reason。"""

    def __init__(self, met: bool = True, tests: int = 3, reason: str = "ok"):
        self.met = met
        self.tests = tests
        self.reason = reason
        self.exit = 0 if met else 1
        self.timed_out = False
        self.min_tests = 1
        self.output = f"{tests} passed"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo_with_two_candidates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "init"], repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()

    cands = []
    for cid, body in (("cand-1", "def add(a, b):\n    return a + b\n"),
                      ("cand-2", "def add(a, b):\n    return b + a\n")):
        d = tmp_path / cid
        _git(["worktree", "add", "-q", "-b", cid, str(d).replace("\\", "/"), base], repo)
        (d / "calc.py").write_text(body, encoding="utf-8")
        _git(["add", "."], d)
        _git(["commit", "-qm", cid], d)
        cands.append({"id": cid, "cwd": str(d), "worktree": str(d), "base": base,
                      "verdict": _V(True, 3), "votes": 0, "touched_tests": False,
                      "files": 1, "additions": 1, "deletions": 1, "cost": 0.0})
    return repo, cands


def _thread(home, repo):
    from ee.server.threads import Thread

    th = Thread("t1", home, lambda m, p: None, {"cwd": str(repo)}, persist=False)
    th.ensure_session()
    return th


def test_rank_runs_position_swapped_two_rounds(ee_home, tmp_path):
    """桩按**候选**投票：两轮都选 cand-1（不管它在 A 槽还是 B 槽）。

    位置对调的意义就在这里：第一轮 cand-1 在 A 槽投 A，第二轮它换到 B 槽
    就投 B。净胜票才能是 +2/−2。若桩按槽位投（每轮都投 A），两轮各投一边，
    净胜 0——那是位置偏差被抵消的正确表现，不是评定结果。
    """
    repo, cands = _repo_with_two_candidates(tmp_path)
    th = _thread(ee_home, repo)
    calls: list[str] = []

    async def fake_side(system: str, user: str, role: str | None = None) -> str:
        calls.append(user)
        # 第一轮 A=cand-1 → 投 A；第二轮 A=cand-2 → 投 B
        return "分析……\nFINAL: A" if len(calls) == 1 else "分析……\nFINAL: B"

    th.session.side_complete = fake_side
    winner = asyncio.run(th._allout_rank(cands, "修好 add", "3 passed"))

    assert len(calls) == 2, "必须位置对调评两轮"
    assert winner is not None and winner["id"] == "cand-1"
    assert {c["id"]: c["votes"] for c in cands} == {"cand-1": 2, "cand-2": -2}


def test_slot_biased_votes_cancel_out(ee_home, tmp_path):
    """按槽位投（两轮都投 A）→ 各得 1 票、净胜 0：位置偏差被抵消。"""
    repo, cands = _repo_with_two_candidates(tmp_path)
    th = _thread(ee_home, repo)

    calls: list[int] = []

    async def side(system: str, user: str, role: str | None = None) -> str:
        calls.append(1)
        return "FINAL: A"

    th.session.side_complete = side
    winner = asyncio.run(th._allout_rank(cands, "修好 add", "3 passed"))
    assert {c["id"]: c["votes"] for c in cands} == {"cand-1": 0, "cand-2": 0}
    assert winner is not None and winner["id"] == "cand-1", "平票时按 id 稳定排序"


def test_rank_judge_failure_counts_as_tie(ee_home, tmp_path):
    """评定请求失败时按平票处理，不能阻塞写回、也不能崩。"""
    repo, cands = _repo_with_two_candidates(tmp_path)
    th = _thread(ee_home, repo)

    async def boom(system: str, user: str, role: str | None = None) -> str:
        raise RuntimeError("网关 500")

    th.session.side_complete = boom
    winner = asyncio.run(th._allout_rank(cands, "修好 add", "3 passed"))
    assert winner is not None, "评定失败不应导致没有胜者"
    assert {c["id"]: c["votes"] for c in cands} == {"cand-1": 0, "cand-2": 0}


def test_rank_reports_ab_reason(ee_home, tmp_path):
    """选中理由要写明 A/B 净胜票，而不是笼统的「通过了判据」。"""
    from ee.core import bestof as B

    repo, cands = _repo_with_two_candidates(tmp_path)
    th = _thread(ee_home, repo)
    calls: list[int] = []

    async def fake_side(system: str, user: str, role: str | None = None) -> str:
        calls.append(1)
        return "FINAL: A" if len(calls) == 1 else "FINAL: B"   # 两轮都投 cand-1

    th.session.side_complete = fake_side
    winner = asyncio.run(th._allout_rank(cands, "修好 add", "3 passed"))
    objs = [B.Candidate(id=c["id"], worktree=c["worktree"], verdict=c["verdict"],
                        votes=int(c["votes"]), files=int(c["files"]))
            for c in cands]
    reason = B.pick_reason(next(o for o in objs if o.id == winner["id"]), objs)
    assert "A/B" in reason and "2" in reason, reason
