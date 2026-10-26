"""D5-1：git worktree 管理（建、.worktreeinclude、列表/状态、diff、合并、删除前自动保存）与在 worktree 里开对话。"""
import asyncio
import shutil
import subprocess

import pytest
from conftest import FakeClient, finish, text_chunks

from ee.core import worktree as W

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@t")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    (r / ".gitignore").write_text(".env\nnode_modules/\n", encoding="utf-8")
    (r / ".env").write_text("KEY=1\n", encoding="utf-8")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    (r / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def test_create_list_diff_merge_remove(repo, ee_home):
    info = W.create(repo, "修 bug", home=ee_home)
    p = __import__("pathlib").Path(info["path"])
    assert info["branch"] == "ee/修-bug" and p.is_dir() and (p / "a.txt").read_text() == "one\n"
    assert (p / ".env").read_text() == "KEY=1\n" and not (p / "node_modules").exists()   # 只复制 include 里的
    assert info["included"] == [".env"]
    with pytest.raises(W.WorktreeError, match="已存在"):
        W.create(repo, "修 bug", home=ee_home)
    # 在 worktree 里改
    (p / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (p / "new.txt").write_text("hi\n", encoding="utf-8")
    lst = W.list_for(repo, ee_home)
    assert len(lst) == 1 and lst[0]["changed"] == 1 and lst[0]["untracked"] == 1
    assert W.list_for(p, ee_home)[0]["path"] == info["path"]            # 在 worktree 里查也能找到主仓库
    assert W.info_for_dir(p / "sub", ee_home)["branch"] == info["branch"]
    d = W.diff(p, ee_home)
    assert sorted(f["path"] for f in d["files"]) == ["a.txt", "new.txt"] and d["additions"] == 2
    # 主目录脏时拒绝合并
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(W.WorktreeError, match="未提交"):
        W.merge(p, home=ee_home)
    git(repo, "checkout", "--", "a.txt")
    r = W.merge(p, home=ee_home)
    assert r["merged"] == "merge" and r["autosaved"]
    assert (repo / "new.txt").read_text() == "hi\n" and "two" in (repo / "a.txt").read_text()
    # 已合并的分支可以删
    r = W.remove(p, delete_branch=True, home=ee_home)
    assert r["branch_deleted"] and not p.exists() and W.list_for(repo, ee_home) == []
    assert "ee/修-bug" not in git(repo, "branch")


def test_remove_autosaves_and_protects_unmerged(repo, ee_home):
    info = W.create(repo, None, home=ee_home)
    p = __import__("pathlib").Path(info["path"])
    (p / "work.txt").write_text("important\n", encoding="utf-8")
    with pytest.raises(W.WorktreeError, match="未合并"):
        W.remove(p, delete_branch=True, home=ee_home)
    # 拒绝时改动已自动提交到分支，目录和登记都还在
    assert "important" in git(repo, "show", f"{info['branch']}:work.txt")
    assert p.exists() and len(W.list_for(repo, ee_home)) == 1
    r = W.remove(p, home=ee_home)                    # 只删目录、保留分支
    assert r["removed"] and not r["branch_deleted"] and not p.exists()
    assert info["branch"] in git(repo, "branch")
    W.create(repo, "again", home=ee_home)
    assert W.remove(W.list_for(repo, ee_home)[0]["path"], delete_branch=True, force=True, home=ee_home)["branch_deleted"]


def test_merge_conflict_aborts(repo, ee_home):
    info = W.create(repo, "c", home=ee_home)
    p = __import__("pathlib").Path(info["path"])
    (p / "a.txt").write_text("from worktree\n", encoding="utf-8")
    (repo / "a.txt").write_text("from main\n", encoding="utf-8")
    git(repo, "commit", "-qam", "main change")
    with pytest.raises(W.WorktreeError, match="冲突"):
        W.merge(p, home=ee_home)
    assert (repo / "a.txt").read_text() == "from main\n"
    assert git(repo, "status", "--porcelain").strip() == ""


def test_squash_and_errors(repo, ee_home, tmp_path):
    info = W.create(repo, "s", home=ee_home)
    p = __import__("pathlib").Path(info["path"])
    (p / "b.txt").write_text("b\n", encoding="utf-8")
    r = W.merge(p, squash=True, home=ee_home)
    assert r["merged"] == "squash" and "A  b.txt" in git(repo, "status", "--porcelain")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(W.WorktreeError, match="不是 git 仓库"):
        W.create(plain, home=ee_home)


def test_thread_in_worktree(repo, ee_home):
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.app import Backend, RpcError
    fake = FakeClient([text_chunks("好") + finish("stop")])
    b = Backend(home=ee_home, client_factory=lambda spec: ChatClient(spec, client=fake))

    async def go():
        th = (await b.call("thread/start", {"cwd": str(repo), "worktree": {"name": "feat-x"}}))["thread"]
        assert th["worktree"]["branch"] == "ee/feat-x" and th["cwd"] == th["worktree"]["path"]
        assert th["workspace"] == str(repo.resolve())
        lst = (await b.call("worktree/list", {"cwd": str(repo)}))
        assert lst["repo"] == str(repo.resolve()) and lst["worktrees"][0]["name"] == "feat-x"
        lst2 = (await b.call("thread/list", {}))["threads"]
        assert lst2[0]["worktree"]["name"] == "feat-x"
        with pytest.raises(RpcError):
            await b.call("worktree/merge", {"path": str(repo)})
        r = await b.call("worktree/remove", {"path": th["cwd"]})
        assert r["removed"]

    asyncio.run(go())
