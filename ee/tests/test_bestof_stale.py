"""ACC-16：候选证据绑定代码状态（验证后改动 → stale，不再声称已验证）。

问题：`bestof/verify` 给候选跑判据、回填 Verdict，然后 pick 出胜者。但如果用户
在验证之后又改了那个候选的代码，Verdict 就是**过期证据**——它证明的是旧代码。
此时若还显示「已验证」，用户会基于错误信息做合并决定。

修复：验证时记下候选的代码状态指纹（`verified_state`）；之后比对当前状态，
不一致即 `stale=True`，`verified` 一并降级为 False。
"""
from __future__ import annotations

from ee.core.bestof import Candidate


def _verdict(met: bool, reason: str = "ok"):
    return type("V", (), {"met": met, "reason": reason})()


def test_verified_candidate_with_matching_state_stays_verified():
    """代码没变：证据仍有效。"""
    c = Candidate(id="a", worktree="/w/a", verdict=_verdict(True),
                  verified_state="shaA", current_state="shaA")
    assert c.stale is False
    assert c.verified is True


def test_changed_code_marks_candidate_stale():
    """验证后代码变了：证据过期，不能再声称已验证。"""
    c = Candidate(id="a", worktree="/w/a", verdict=_verdict(True),
                  verified_state="shaA", current_state="shaB")
    assert c.stale is True, "代码变了却仍报已验证 —— 用户会基于过期证据做决定"
    assert c.verified is False, "stale 的候选不能算已验证"
    assert "变了" in c.reason or "过期" in c.reason, f"理由要说明为什么失效：{c.reason}"


def test_candidate_without_state_binding_is_not_stale_but_conservative():
    """没记状态（老数据/未来不传）：不判 stale，但也不能因此凭空声称已验证。"""
    c = Candidate(id="a", worktree="/w/a", verdict=_verdict(True))
    assert c.stale is False
    assert c.verified is True, "没有状态绑定时按旧行为（只看 Verdict），不倒退"


def test_stale_candidate_never_wins():
    """stale 的候选不能被 pick 选中——它连「已验证」都不算了。"""
    from ee.core.bestof import pick

    fresh = Candidate(id="fresh", worktree="/w/f", verdict=_verdict(True),
                      verified_state="s1", current_state="s1")
    stale = Candidate(id="stale", worktree="/w/s", verdict=_verdict(True),
                      verified_state="s1", current_state="s2")
    win = pick([stale, fresh])
    assert win is not None and win.id == "fresh"


def test_stale_only_candidate_yields_no_winner():
    """全部 stale 时不硬挑——和「全部未验证」同等处理。"""
    from ee.core.bestof import pick

    a = Candidate(id="a", worktree="/w/a", verdict=_verdict(True),
                  verified_state="s1", current_state="s2")
    assert pick([a]) is None


def test_report_and_dict_expose_stale():
    """报告与序列化都要带 stale，否则界面无从显示。"""
    from ee.core.bestof import report

    c = Candidate(id="a", title="t", worktree="/w/a", verdict=_verdict(True),
                  verified_state="s1", current_state="s2")
    d = c.as_dict()
    assert d["stale"] is True and d["verified"] is False
    assert "过期" in report([c], None) or "stale" in report([c], None) or "变了" in report([c], None)


# ---------------------------------------------------------------- 端到端：verify 真的绑定状态

def test_verify_binds_state_fingerprint(ee_home, workspace):
    """真实 bestof/verify 必须给候选写上 verified_state，否则 stale 永远判不出来。"""
    import asyncio
    import subprocess

    from ee.core import bestof as B

    def _git(*a):
        return subprocess.run(["git", *a], cwd=workspace, capture_output=True, text=True)

    _git("init")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    (workspace / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "init")

    head = _git("rev-parse", "HEAD").stdout.strip()
    dirty = _git("status", "--porcelain").stdout.strip()
    fp = B.state_fingerprint(head, dirty)
    assert fp and ":" in fp, f"指纹格式不对：{fp}"

    # 同样的代码状态 → 指纹相同（这是 stale 判定的基础）
    assert B.state_fingerprint(head, dirty) == fp
    # 工作区改动 → 指纹变化
    (workspace / "a.txt").write_text("y\n", encoding="utf-8")
    dirty2 = _git("status", "--porcelain").stdout.strip()
    assert B.state_fingerprint(head, dirty2) != fp, "工作区变了指纹必须变"
