"""子代理收尾：diff 失败不能当成"没改动"把 worktree 连分支一起删掉。

缺陷（探针可复现）：`finish_agent()` 里

    out = _git(["diff", "--numstat", ...], p, timeout=60, check=False)

`check=False` 失败时返回空字符串，和"真的没有改动"无法区分。接着：

    if not files:              # files 来自 out，空
        remove(p, delete_branch=True, force=True)   # 连 worktree 带分支强删

于是超时 / git 报错 / 目录暂时不可用，都会导致**子代理已经提交的代码被删掉**。
`commits` 变量就算出来了，却从来没参与删除判断。

修法：只有"确实没改动"才删——diff 成功且为空，并且没有任何提交。
diff 失败一律保留，交给用户处理（worktree 列表里能看到并手动删）。
"""
import subprocess
from pathlib import Path

import pytest

from ee.core import worktree as W


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def init_repo(path: Path) -> Path:
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    return init_repo(r)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def make_agent_worktree(repo, home):
    """建一个子代理用的 worktree（复用生产代码）。"""
    return W.create_for_agent(repo, "测试", home=home)


def commit_in(path: Path, name="b.txt", text="hi\n", msg="子代理的成果"):
    (path / name).write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- 缺陷复现


def test_diff_failure_keeps_worktree(repo, home, monkeypatch):
    """diff 命令失败 → 不能删 worktree / 分支。"""
    info = make_agent_worktree(repo, home)
    wt = Path(info["path"])
    commit_in(wt)                      # 子代理确实提交了东西

    real = W._git

    def flaky(args, cwd, timeout=60, check=True):
        if args and args[0] == "diff":
            return ""                  # check=False 失败时的真实返回值：空字符串
        return real(args, cwd, timeout=timeout, check=check)

    monkeypatch.setattr(W, "_git", flaky)
    res = W.finish_agent(info, "收尾", home=home)

    assert wt.is_dir(), "diff 失败时 worktree 被删了（子代理代码丢失）"
    assert res["removed"] is False
    # 分支也要还在
    out = git(repo, "branch", "--list", info["branch"]).stdout
    assert info["branch"] in out


def test_diff_failure_with_commits_reports_them(repo, home, monkeypatch):
    """diff 失败时，至少要把 commits 报出来（用户能知道有东西要救）。"""
    info = make_agent_worktree(repo, home)
    wt = Path(info["path"])
    commit_in(wt, msg="重要提交")

    real = W._git

    def flaky(args, cwd, timeout=60, check=True):
        if args and args[0] == "diff":
            return ""
        return real(args, cwd, timeout=timeout, check=check)

    monkeypatch.setattr(W, "_git", flaky)
    res = W.finish_agent(info, "收尾", home=home)
    assert res["removed"] is False
    assert any("重要提交" in c for c in res["commits"]), res["commits"]


# ---------------------------------------------------------------- 正常路径不能坏


def test_no_changes_still_removed(repo, home):
    """确实一点改动都没有（diff 成功且为空、无提交）→ 照旧清理。"""
    info = make_agent_worktree(repo, home)
    wt = Path(info["path"])
    res = W.finish_agent(info, "收尾", home=home)
    assert res["removed"] is True
    assert not wt.exists()


def test_committed_changes_kept(repo, home):
    """有提交 → 保留 worktree，并报告文件与提交。"""
    info = make_agent_worktree(repo, home)
    wt = Path(info["path"])
    commit_in(wt)
    res = W.finish_agent(info, "收尾", home=home)
    assert res["removed"] is False
    assert wt.is_dir()
    assert res["file_count"] >= 1
    assert len(res["commits"]) >= 1


def test_uncommitted_changes_are_autosaved_then_kept(repo, home):
    """没提交但有改动 → _autosave 先提交，再按"有提交"保留。"""
    info = make_agent_worktree(repo, home)
    wt = Path(info["path"])
    (wt / "c.txt").write_text("uncommitted\n", encoding="utf-8")
    res = W.finish_agent(info, "收尾", home=home)
    assert res["removed"] is False
    assert wt.is_dir()
    assert (wt / "c.txt").exists()
