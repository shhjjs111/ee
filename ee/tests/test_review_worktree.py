"""审查（worktree / 磁盘快照）发现问题的复现测试。"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from ee.core import gitinfo, snapshot
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
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


# ------------------------------------------------------------------ worktree
def test_remove_keeps_files_when_status_fails(repo, ee_home, monkeypatch):
    """git status 超时/失败被当成“没有改动”时，删除不能直接 --force 把未提交的文件删掉。"""
    info = W.create(repo, "st", home=ee_home)
    p = Path(info["path"])
    (p / "work.txt").write_text("important\n", encoding="utf-8")
    real = gitinfo._run

    def flaky(args, cwd, timeout=10):
        if args[:1] == ["status"]:
            return 1, ""                     # gitinfo._run 超时时就是这样返回
        return real(args, cwd, timeout)
    monkeypatch.setattr(gitinfo, "_run", flaky)
    try:
        W.remove(p, home=ee_home)
    except W.WorktreeError:
        pass
    monkeypatch.setattr(gitinfo, "_run", real)
    saved = subprocess.run(["git", "show", f"{info['branch']}:work.txt"], cwd=repo, capture_output=True, text=True,
                           check=False)
    assert (p / "work.txt").is_file() or saved.stdout == "important\n"


def test_remove_broken_worktree_does_not_rmtree_user_files(repo, ee_home):
    """worktree 的 .git 指向失效（主仓库搬走/元数据被清）时，不能静默 rmtree 掉里面的文件。"""
    info = W.create(repo, "broken", home=ee_home)
    p = Path(info["path"])
    (p / "work.txt").write_text("important\n", encoding="utf-8")
    (p / ".git").unlink()                      # Windows 上 .git 文件带隐藏属性，直接覆盖写会被拒
    (p / ".git").write_text("gitdir: " + str(p.parent / "nowhere") + "\n", encoding="utf-8")
    with pytest.raises(W.WorktreeError):
        W.remove(p, home=ee_home)
    assert (p / "work.txt").read_text(encoding="utf-8") == "important\n"
    assert len(W.list_for(repo, ee_home, with_status=False)) == 1


def test_remove_reports_branch_delete_failure(repo, ee_home):
    """分支没删掉（例如 worktree 被 git lock、仍登记为已检出）时不能报告 branch_deleted=True。"""
    info = W.create(repo, "locked", home=ee_home)
    p = Path(info["path"])
    git(repo, "worktree", "lock", str(p))
    try:
        r = W.remove(p, delete_branch=True, force=True, home=ee_home)
    except W.WorktreeError:
        r = None
    exists = bool(git(repo, "branch", "--list", info["branch"]).strip())
    if r is not None:
        assert r["branch_deleted"] == (not exists)


def test_remove_keeps_entry_when_dir_cannot_be_deleted(repo, ee_home, monkeypatch):
    """目录删不掉（Windows 文件被占用）时不能把登记删了、留下 UI 里看不到的孤儿目录。"""
    info = W.create(repo, "busy", home=ee_home)
    p = Path(info["path"])
    git(repo, "worktree", "lock", str(p))             # 让 git worktree remove 失败
    monkeypatch.setattr(W.shutil, "rmtree", lambda *a, **k: None)   # 模拟文件被占用删不掉
    with pytest.raises(W.WorktreeError):
        W.remove(p, home=ee_home)
    assert p.exists() and len(W.list_for(repo, ee_home, with_status=False)) == 1


@pytest.mark.parametrize("name", ["CON", "nul", "Aux", "com1", "LPT9", "con.txt", "prn.log"])
def test_slug_avoids_windows_reserved_names(name):
    s = W._slug(name)
    stem = s.split(".")[0].upper()
    assert s and stem not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                              *(f"LPT{i}" for i in range(1, 10))}


def test_git_calls_enable_longpaths(repo, ee_home, monkeypatch):
    """worktree 目录（~/.ee/worktrees/…/名字）比原仓库深，Windows 下检出深层文件需要 core.longpaths。"""
    seen = []
    real = W.run_capture

    def spy(argv, **kw):
        seen.append(argv)
        return real(argv, **kw)
    monkeypatch.setattr(W, "run_capture", spy)
    W.create(repo, "lp", home=ee_home)
    add = [a for a in seen if "worktree" in a and "add" in a]
    assert add and "core.longpaths=true" in add[0]


def test_merge_and_remove_refuse_running_thread_in_subdir(repo, ee_home):
    """worktree 里还有对话在跑（含 cwd 是 worktree 子目录的对话）时，合并/删除都要拒绝。"""
    from ee.server.app import Backend, RpcError
    b = Backend(home=ee_home)
    info = W.create(repo, "run", home=ee_home)
    sub = Path(info["path"]) / "sub"
    sub.mkdir()

    class T:
        running = True
        session = None
    t = T()
    t.meta = {"cwd": str(sub), "worktree": {"path": info["path"]}}
    b.threads["x"] = t
    (Path(info["path"]) / "half.txt").write_text("half\n", encoding="utf-8")

    async def go():
        with pytest.raises(RpcError, match="运行"):
            await b.call("worktree/merge", {"path": info["path"]})
        with pytest.raises(RpcError, match="运行"):
            await b.call("worktree/remove", {"path": info["path"]})
    asyncio.run(go())
    assert Path(info["path"]).is_dir() and not (repo / "half.txt").exists()


# ------------------------------------------------------------------ snapshot
def test_restore_paths_are_literal_not_globs(workspace, ee_home):
    """文件名里有 [ ] 时，恢复 a[b].txt 不能顺带把 ab.txt 也改回去（覆盖用户的改动）。"""
    (workspace / "a[b].txt").write_text("glob-old", encoding="utf-8")
    (workspace / "ab.txt").write_text("plain-old", encoding="utf-8")
    ds = snapshot.DiskSnapshot(workspace, ee_home)
    t1 = ds.capture()
    (workspace / "a[b].txt").write_text("glob-new", encoding="utf-8")
    (workspace / "ab.txt").write_text("plain-new", encoding="utf-8")
    ds.restore(t1, ["a[b].txt"])
    assert (workspace / "a[b].txt").read_text(encoding="utf-8") == "glob-old"
    assert (workspace / "ab.txt").read_text(encoding="utf-8") == "plain-new"


def test_restore_ignores_project_gitattributes_eol(workspace, ee_home):
    """项目 .gitattributes 写了 eol=crlf / text 时，恢复出来的文件必须和快照时逐字节一致。"""
    (workspace / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
    (workspace / "lf.txt").write_bytes(b"a\nb\n")
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    ds = snapshot.DiskSnapshot(workspace, ee_home)
    t1 = ds.capture()
    (workspace / "lf.txt").write_bytes(b"changed\n")
    (workspace / "crlf.txt").write_bytes(b"changed\n")
    t2 = ds.capture()
    ds.restore(t1, ds.changed(t1, t2))
    assert (workspace / "lf.txt").read_bytes() == b"a\nb\n"
    assert (workspace / "crlf.txt").read_bytes() == b"a\r\nb\r\n"


def test_big_file_exclude_is_literal(workspace, ee_home, monkeypatch):
    """大文件按名字写进排除列表时要转义 glob 字符：大文件 x[12].bin 不能把小文件 x1.txt…挡在快照外，自己也不能漏排除。"""
    monkeypatch.setattr(snapshot, "MAX_UNTRACKED_BYTES", 10)
    ds = snapshot.DiskSnapshot(workspace, ee_home)
    ds.capture()
    (workspace / "x[12]").write_bytes(b"0" * 100)          # 大文件
    (workspace / "x1").write_text("small", encoding="utf-8")  # 小文件，名字恰好匹配 glob
    t = ds.capture()
    names = ds._run("ls-tree", "-r", "--name-only", t).decode().split("\n")
    assert "x1" in names and "x[12]" not in names


def test_merge_refuses_dirty_main_when_status_fails(repo, ee_home, monkeypatch):
    """主目录 git status 超时被当成“干净”时，合并不能继续（冲突回滚可能动到主目录未提交的改动）。"""
    info = W.create(repo, "m", home=ee_home)
    (Path(info["path"]) / "b.txt").write_text("b\n", encoding="utf-8")
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    real = gitinfo._run
    monkeypatch.setattr(gitinfo, "_run", lambda args, cwd, timeout=10:
                        (1, "") if args[:1] == ["status"] else real(args, cwd, timeout))
    with pytest.raises(W.WorktreeError):
        W.merge(Path(info["path"]), home=ee_home)
    monkeypatch.setattr(gitinfo, "_run", real)
    assert not (repo / "b.txt").exists() and (repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"


@pytest.mark.parametrize("cmd", ["git worktree remove --force ../wt", "git worktree remove -f wt",
                                 "git -C repo worktree remove wt --force"])
def test_force_ask_worktree_remove_force(tmp_path, cmd):
    """git worktree remove --force 会连同未提交/未跟踪文件删掉整个目录，全自动模式也要确认。"""
    from ee.core.permissions import PermissionPolicy
    d = PermissionPolicy(mode="auto", workspace=tmp_path).check("shell", {"command": cmd})
    assert d.action == "ask" and d.force
