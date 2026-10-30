"""best-of-n 的 RPC（T10-d 界面侧）。

界面要做的是**展示候选与证据**，让人看见「为什么选它」。守的纪律：

- 胜者只由 verdict 决定（模型自述不算），沿用 core/bestof 的不变量；
- 没有候选通过验证时 winner=None，不硬挑一个；
- 候选必须各有独立 worktree——共用就不是比较了。
"""
from __future__ import annotations

import asyncio

from ee.server.app import Backend


def _call(backend, method, **params):
    return asyncio.run(backend.methods[method](**params))


def test_pick_returns_winner_and_report(tmp_path):
    b = Backend(home=tmp_path, persist=False)
    v_ok = {"exit": 0, "tests": 3, "timed_out": False, "output": "3 passed", "min_tests": 1}
    v_bad = {"exit": 1, "tests": 1, "timed_out": False, "output": "1 failed", "min_tests": 1}
    res = _call(b, "bestof/pick", candidates=[
        {"id": "a", "worktree": "/wt/a", "verdict": v_bad},
        {"id": "b", "worktree": "/wt/b", "verdict": v_ok},
    ])
    assert res["winner"] == "b"
    assert "b" in res["report"] and "a" in res["report"]
    assert res["cleanup"]["keep"] == ["a", "b"], "默认必须全保留"
    assert res["cleanup"]["remove"] == []


def test_pick_none_when_nothing_verified(tmp_path):
    """全没通过验证时不挑——不能凭自述硬选一个。"""
    b = Backend(home=tmp_path, persist=False)
    res = _call(b, "bestof/pick", candidates=[
        {"id": "a", "worktree": "/wt/a", "self_reported_ok": True},
        {"id": "b", "worktree": "/wt/b"},
    ])
    assert res["winner"] is None
    assert res["candidates"][0]["verified"] is False


def test_pick_rejects_shared_worktree(tmp_path):
    """共用 worktree 等于改同一份文件，比不出高下。"""
    b = Backend(home=tmp_path, persist=False)
    try:
        _call(b, "bestof/pick", candidates=[
            {"id": "a", "worktree": "/same"},
            {"id": "b", "worktree": "/same"},
        ])
    except Exception as e:  # noqa: BLE001
        assert "worktree" in str(e).lower()
    else:
        raise AssertionError("共用 worktree 应报错")


def test_pick_single_candidate(tmp_path):
    b = Backend(home=tmp_path, persist=False)
    v = {"exit": 0, "tests": 2, "timed_out": False, "output": "", "min_tests": 1}
    res = _call(b, "bestof/pick", candidates=[{"id": "x", "worktree": "/wt/x", "verdict": v}])
    assert res["winner"] == "x"


# ---------------------------------------------------------------- bestof/recheck（ACC-16 跨请求）

def test_recheck_marks_stale_when_worktree_changed(tmp_path):
    """验证后代码变了：recheck 返回 stale 列表，候选不再算已验证。"""
    import subprocess

    wt = tmp_path / "wt"
    wt.mkdir()

    def _git(*a):
        return subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True)

    _git("init")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (wt / "a.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "init")

    from ee.core import worktree as W
    from ee.core import bestof as B
    head = W._git(["rev-parse", "HEAD"], str(wt), timeout=15, check=False).strip()
    verified = B.state_fingerprint(head, "")

    b = Backend(home=tmp_path, persist=False)
    v_ok = {"exit": 0, "tests": 3, "timed_out": False, "output": "3 passed", "min_tests": 1}
    base = [{"id": "a", "worktree": str(wt), "verdict": v_ok, "verified_state": verified}]

    # 状态没变 → 不算 stale
    res = _call(b, "bestof/recheck", candidates=base)
    assert res["stale"] == [], res
    assert res["candidates"][0]["verified"] is True

    # 改了工作区 → stale
    (wt / "a.txt").write_text("y\n", encoding="utf-8")
    res2 = _call(b, "bestof/recheck", candidates=base)
    assert res2["stale"] == ["a"], f"代码变了应报 stale：{res2}"
    assert res2["candidates"][0]["verified"] is False
    assert res2["winner"] is None, "stale 的候选不能被选中"
