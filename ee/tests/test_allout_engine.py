"""全力以赴的执行原语（allout 阶段 5）：keep-policy、A/B 评定、补丁写回。

这三个函数是「把活真正做对」这一步的全部判断逻辑，全部是纯函数或只碰
git 子进程的可测单元。编排（单路 → 修复 → 分叉）在 server/threads.py。

守的纪律（allout v2 §1.2 / §1.3 / §4）：
- 修复只在**严格更好**时保留（keep-policy）——修复永远不能让结果变差；
- A/B 评定只认证据，模型自述不算（提示词里写死）；
- 写回用补丁方式，不要求主目录干净；失败绝不覆盖主目录。
"""
from __future__ import annotations

import subprocess

from ee.core.allout import ab_prompt, apply_winner, keep_policy, net_votes, parse_ab


def _v(met: bool, tests: int = 3):
    from ee.core.goal import Verdict

    return Verdict(exit=0 if met else 1, tests=tests, timed_out=False,
                   output=f"{tests} passed" if met else "1 failed", min_tests=1)


# ---------------------------------------------------------------- keep-policy


def test_keep_when_repair_passes_with_more_tests():
    keep, why = keep_policy(_v(True, 10), _v(True, 12))
    assert keep is True and "保留" in why


def test_keep_when_same_tests():
    keep, _ = keep_policy(_v(True, 10), _v(True, 10))
    assert keep is True


def test_discard_when_repair_fails_judge():
    keep, why = keep_policy(_v(True, 10), _v(False, 10))
    assert keep is False and "丢弃" in why


def test_discard_when_repair_regresses_test_count():
    keep, why = keep_policy(_v(True, 10), _v(True, 8))
    assert keep is False and "降到" in why


def test_keep_when_no_previous_winner():
    keep, _ = keep_policy(None, _v(True, 5))
    assert keep is True


def test_discard_when_no_repair_at_all():
    keep, _ = keep_policy(_v(True, 10), None)
    assert keep is False


# ---------------------------------------------------------------- A/B 评定


def test_ab_prompt_carries_evidence_not_self_reports():
    system, user = ab_prompt("修好 add()", "3 passed", "+return a + b", "+return a - b")
    assert "自述" in system and "不算证据" in system
    assert "FINAL: A" in system
    assert "3 passed" in user
    assert "+return a + b" in user and "+return a - b" in user


def test_ab_prompt_marks_touched_tests():
    _s, user = ab_prompt("t", "ok", "patch-a", "patch-b", a_touched_tests=True)
    assert "A 的补丁（改动过测试文件）" in user


def test_ab_prompt_clips_huge_diffs():
    _s, user = ab_prompt("t", "ok", "x" * 100_000, "y")
    assert "中间省略" in user
    assert len(user) < 30_000


def test_parse_ab_takes_last_marker():
    assert parse_ab("分析…\nFINAL: A") == "A"
    assert parse_ab("FINAL: B\n再想想\nfinal: a") == "A"
    assert parse_ab("FINAL: TIE") == "TIE"
    assert parse_ab("没有结论") == ""


def test_net_votes_with_position_swap():
    # 两轮：第一轮 A 槽是 x，第二轮 A 槽是 y（位置对调）
    assert net_votes(["x", "y"], "x", "y") == 0
    assert net_votes(["x", "x"], "x", "y") == 2
    assert net_votes(["y", "x"], "x", "y") == 0
    assert net_votes(["y", "y"], "x", "y") == -2
    assert net_votes(["x", "TIE"], "x", "y") == 1


# ---------------------------------------------------------------- 补丁写回（真实 git 仓库）


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo_with_candidate(tmp_path):
    """主仓库（带未提交改动）+ 一个候选 worktree。

    严格复现真实流程（worktree.create_for_agent + _sync_dirty）：脏改动先在
    候选里提交成**基线提交**，`agent_base` 指向它；候选自己的改动提交在其上。
    这样 `git diff agent_base..HEAD` 只含候选自己的改动。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("BASE\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "init"], repo)
    # 主目录脏改动
    (repo / "a.txt").write_text("BASE\nMODIFIED-BY-EE\n", encoding="utf-8")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    cand = tmp_path / "cand"
    _git(["worktree", "add", "-q", "-b", "cand", str(cand).replace("\\", "/"), head], repo)
    # 1) 把脏改动带进候选并提交成基线（对应 _sync_dirty）
    (cand / "a.txt").write_text("BASE\nMODIFIED-BY-EE\n", encoding="utf-8")
    _git(["add", "."], cand)
    _git(["commit", "-qm", "baseline (synced dirty)"], cand)
    agent_base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cand,
                                capture_output=True, text=True).stdout.strip()
    # 2) 候选自己的解法
    (cand / "new.txt").write_text("NEW-SOLUTION\n", encoding="utf-8")
    _git(["add", "."], cand)
    _git(["commit", "-qm", "candidate"], cand)
    return repo, cand, agent_base


def test_apply_winner_preserves_dirty_main_and_adds_candidate_changes(tmp_path):
    repo, cand, base = _repo_with_candidate(tmp_path)
    res = apply_winner(cand, base, repo)
    assert res["applied"] is True, res
    assert (repo / "a.txt").read_text(encoding="utf-8") == "BASE\nMODIFIED-BY-EE\n", "用户未提交的改动必须保留"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "NEW-SOLUTION\n"
    assert res["files"] >= 1


def test_apply_winner_reports_empty_diff(tmp_path):
    repo, cand, base = _repo_with_candidate(tmp_path)
    # 把 agent_base 指到候选的 HEAD：diff 为空
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cand, capture_output=True, text=True).stdout.strip()
    res = apply_winner(cand, head, repo)
    assert res["applied"] is False
    assert "没有改动" in res["error"]


def test_apply_winner_does_not_touch_main_on_conflict(tmp_path):
    repo, cand, base = _repo_with_candidate(tmp_path)
    # 主目录已把 new.txt 改成冲突内容 → 补丁失败，主目录保持原样
    (repo / "new.txt").write_text("CONFLICTING\n", encoding="utf-8")
    res = apply_winner(cand, base, repo)
    assert res["applied"] is False
    assert (repo / "new.txt").read_text(encoding="utf-8") == "CONFLICTING\n"
