"""best-of-n 的判据编排（T10-d 界面侧）：bestof/verify。

交接文档里唯一的实质卡点：界面把 worktree 列表当候选提交，但不带 verdict，
后端一律判未验证、永远「不选」。这一层补的就是那一步。

守的是四条不变量：

1. **只观测**：单轮 judge，不跑续跑循环——候选代码已写完，重跑不该改代码。
2. **路径限定**：只能跑在 ee 登记过的 worktree 里，不新开任意目录执行的口子。
3. **不假装**：判据红就是红（exit≠0 / 零测试 / 超时都算不通过）。
4. **胜者只由判据定**：自报「通过」不进已验证列。
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from ee.server.app import Backend, RpcError


@pytest.fixture
def backend(tmp_path):
    return Backend(home=tmp_path, persist=False)


def _call(backend, method, **params):
    return asyncio.run(backend.methods[method](**params))


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo_with_two_worktrees(tmp_path, *, good_tests: str, bad_tests: str):
    """建一个仓库 + 两个 ee worktree。绿的写通过的测试，红的写失败的测试。

    用真实 pytest 判据（不是 mock）：这一层要证明的就是「真实命令的退出码
    与测试计数如何变成 Verdict」。
    """
    from ee.core import worktree as W

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "conftest.py").write_text("# 让 pytest 有根\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "init"], repo)

    home = tmp_path / "home"
    home.mkdir()
    wt_a = W.create(repo, "a", None, home)
    wt_b = W.create(repo, "b", None, home)

    # 两个 worktree 里各放一份测试：一个真的过，一个真的不过
    for wt, body in ((wt_a, good_tests), (wt_b, bad_tests)):
        p = wt["path"]
        with open(f"{p}/test_case.py", "w", encoding="utf-8") as f:
            f.write(body)
    return home, wt_a, wt_b


def test_verify_picks_the_green_worktree(tmp_path, backend):
    """一个 worktree 的判据红、一个绿 → 选绿。这是交接文档要求的那条验收。"""
    home, wt_a, wt_b = _repo_with_two_worktrees(
        tmp_path,
        good_tests="def test_ok():\n    assert 1 == 1\n",
        bad_tests="def test_bad():\n    assert 1 == 2\n",
    )
    backend.home = home

    res = _call(backend, "bestof/verify",
                goal={"command": "python -m pytest -q test_case.py", "timeout": 120},
                candidates=[
                    {"id": "a", "title": "候选 a", "worktree": wt_a["path"]},
                    {"id": "b", "title": "候选 b", "worktree": wt_b["path"]},
                ])

    by_id = {c["id"]: c for c in res["candidates"]}
    assert by_id["a"]["verified"] is True, by_id["a"]["reason"]
    assert by_id["b"]["verified"] is False, by_id["b"]["reason"]
    assert res["winner"] == "a"


def test_verify_none_when_all_red(tmp_path, backend):
    """全红时不硬挑一个——返回 winner=None。"""
    home, wt_a, wt_b = _repo_with_two_worktrees(
        tmp_path,
        good_tests="def test_bad():\n    assert 1 == 2\n",
        bad_tests="def test_bad2():\n    assert 2 == 3\n",
    )
    backend.home = home

    res = _call(backend, "bestof/verify",
                goal={"command": "python -m pytest -q test_case.py", "timeout": 120},
                candidates=[
                    {"id": "a", "worktree": wt_a["path"]},
                    {"id": "b", "worktree": wt_b["path"]},
                ])
    assert res["winner"] is None
    assert all(c["verified"] is False for c in res["candidates"])


def test_verify_refuses_unregistered_path(tmp_path, backend):
    """不在 ee 登记的目录里跑判据 → 明确拒绝，不执行。"""
    home, wt_a, _ = _repo_with_two_worktrees(
        tmp_path,
        good_tests="def test_ok():\n    assert 1 == 1\n",
        bad_tests="def test_bad():\n    assert 1 == 2\n",
    )
    backend.home = home
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(RpcError) as ei:
        _call(backend, "bestof/verify",
              goal={"command": "python -m pytest -q", "timeout": 30},
              candidates=[{"id": "x", "worktree": str(outside)}])
    assert "不是 ee 登记的 worktree" in str(ei.value)


def test_verify_rejects_empty_command(tmp_path, backend):
    """判据命令不能为空——没有判据的验证没有意义（复用 _norm_goal）。"""
    home, wt_a, _ = _repo_with_two_worktrees(
        tmp_path,
        good_tests="def test_ok():\n    assert 1 == 1\n",
        bad_tests="def test_bad():\n    assert 1 == 2\n",
    )
    backend.home = home
    with pytest.raises(RpcError) as ei:
        _call(backend, "bestof/verify", goal={"command": "   "},
              candidates=[{"id": "a", "worktree": wt_a["path"]}])
    assert "goal.command 不能为空" in str(ei.value)


def test_verify_ignores_self_reported_pass(tmp_path, backend):
    """自报通过不算证据：判据红时，即使 self_reported_ok=True 也不选它。"""
    home, wt_a, wt_b = _repo_with_two_worktrees(
        tmp_path,
        good_tests="def test_bad():\n    assert 1 == 2\n",
        bad_tests="def test_bad2():\n    assert 3 == 4\n",
    )
    backend.home = home
    res = _call(backend, "bestof/verify",
                goal={"command": "python -m pytest -q test_case.py", "timeout": 120},
                candidates=[
                    {"id": "a", "worktree": wt_a["path"], "self_reported_ok": True},
                    {"id": "b", "worktree": wt_b["path"]},
                ])
    assert res["winner"] is None
    a = next(c for c in res["candidates"] if c["id"] == "a")
    assert a["verified"] is False
    assert a["self_reported_ok"] is True
