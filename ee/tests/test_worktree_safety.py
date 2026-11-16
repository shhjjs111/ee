"""自动回收的安全性：归属、分支、git 失败、未备份的 ignored 数据、未知临时文件、创建回滚。

bot1 的独立审计（channel-result-bd3ab3d8）证明旧的 strict 删除会误删数据：
- 只信 index.json + HEAD，不证明路径仍是**登记仓库的 linked worktree**（同 HEAD 的外来
  clone/替换目录也照删）；
- 分支一致性闸门是空操作（`W.status` 的 branch 是登记值，不是实际值）；
- ignored 的**目录/嵌套 git 仓库/符号链接**被静默跳过，备份为空也照删；
- `git worktree remove` 是 check=False，失败后还 fallback 到 `shutil.rmtree`，先删掉能删的；
- `git status` 失败被当成"干净"；
- 创建后 `_save` 失败会留下没有登记的孤儿 worktree。

这些用例用真实临时 git 仓库或失败注入，锁住"宁可保留也不误删"。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=120)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr[:300]}")
    return out.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n",
                                             encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\nvendor/\n.env\n", encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _made(repo: Path, ee_home, label: str = "cand") -> dict:
    from ee.core import worktree as W

    info = W.create_for_agent(repo, label, ee_home)
    # 一个干净的提交，模拟"收尾时已提交"
    p = Path(info["path"])
    (p / "note.txt").write_text("done\n", encoding="utf-8", newline="\n")
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "candidate work")
    return info


def _strict_remove(info: dict, ee_home, **kw):
    from ee.core import worktree as W

    head = _git(Path(info["path"]), "rev-parse", "HEAD")
    return W.remove(info["path"], delete_branch=False, force=True, home=ee_home, strict=True,
                    expected_head=kw.pop("expected_head", head),
                    expected_branch=kw.pop("expected_branch", info["branch"]), **kw)


# ---------------------------------------------------------------- A. 归属

def test_strict_remove_refuses_foreign_repo_with_same_head(tmp_path, ee_home):
    """同 HEAD 的外来 clone 不能因为 index.json 登记过就被删。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    head = _git(Path(info["path"]), "rev-parse", "HEAD")
    clone = tmp_path / "foreign"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(clone, "checkout", "-q", head)
    assert _git(clone, "rev-parse", "HEAD") == head, "夹具要求同 HEAD"

    # 登记被篡改成指向外来目录（现实里可能来自过期索引/目录被替换）
    items = W._load(ee_home)
    for x in items:
        if W._norm(x["path"]) == W._norm(info["path"]):
            x["path"] = str(clone)
    W._save(items, ee_home)

    with pytest.raises(W.WorktreeError) as e:
        W.remove(clone, delete_branch=False, force=True, home=ee_home, strict=True,
                 expected_head=head, expected_branch=info["branch"])
    assert "归属" in str(e.value) or "worktree" in str(e.value)
    assert clone.is_dir() and (clone / "calc.py").is_file(), "外来仓库必须原样保留"


def test_strict_remove_refuses_when_branch_switched(tmp_path, ee_home):
    """worktree 里切到别的分支（同 HEAD）也必须拒绝。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    _git(p, "checkout", "-q", "-b", "other-branch")

    with pytest.raises(W.WorktreeError) as e:
        _strict_remove(info, ee_home)
    assert "分支" in str(e.value)
    assert p.is_dir(), "拒绝后目录必须保留"


# ---------------------------------------------------------------- C. ignored 数据

def test_strict_remove_refuses_ignored_nested_git_repo(tmp_path, ee_home):
    """被忽略的嵌套 git 仓库（vendor/）不能被静默跳过然后删掉。"""
    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    nested = p / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "precious.txt").write_text("keep me\n", encoding="utf-8", newline="\n")

    with pytest.raises(Exception) as e:
        _strict_remove(info, ee_home)
    assert "备份" in str(e.value) or "忽略" in str(e.value)
    assert (nested / "precious.txt").is_file(), "嵌套仓库里的数据必须保留"


def test_strict_remove_refuses_ignored_symlink(tmp_path, ee_home):
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    # 目标放在 worktree 外，避免未跟踪目标文件先触发另一道拒删门控。
    target = tmp_path / "outside-target.txt"
    target.write_text("x\n", encoding="utf-8", newline="\n")
    link = p / ".env"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接（Windows 需要开发者模式/管理员）")

    assert _git(p, "check-ignore", "--", ".env") == ".env"
    assert _git(p, "status", "--porcelain", "--untracked-files=all") == "", "夹具不能被未跟踪文件门控挡住"
    assert W.backup_ignored(info, ee_home, strict=True)["skipped"] == [".env"]
    with pytest.raises(W.WorktreeError) as e:
        _strict_remove(info, ee_home)
    assert "备份" in str(e.value) or "忽略" in str(e.value)
    assert p.is_dir() and link.is_symlink(), "符号链接与目录都必须保留"
    assert target.read_text(encoding="utf-8") == "x\n", "外部目标内容必须保留"


def test_strict_remove_allows_only_safe_judge_caches(tmp_path, ee_home):
    """本轮判据自己产生的 __pycache__ 属于可安全舍弃的缓存，不因此挡住正常回收。"""
    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    cache = p / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-312.pyc").write_bytes(b"bytecode")

    out = _strict_remove(info, ee_home)
    assert out["removed"] is True
    assert not p.exists()
    assert info["branch"] in _git(repo, "branch", "--list", "--format=%(refname:short)"), "分支必须保留"


def test_ignored_plain_files_are_backed_up_before_deletion(tmp_path, ee_home):
    """ignored 的普通文件不能静默消失：先备份到 worktree-backups，再删目录。"""
    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    (p / "vendor" / "lib").mkdir(parents=True)
    (p / "vendor" / "lib" / "a.js").write_text("x\n", encoding="utf-8", newline="\n")

    out = _strict_remove(info, ee_home)
    assert out["removed"] is True
    assert "vendor/lib/a.js" in out["backed_up"], out
    backup_dir = Path(out["ignored_backup"])
    assert (backup_dir / "vendor" / "lib" / "a.js").read_text(encoding="utf-8") == "x\n"


# ---------------------------------------------------------------- D. git 状态

def test_status_reports_failure_instead_of_clean(tmp_path, ee_home):
    """git status 失败时必须 status_ok=False，不能报“干净”。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    index = Path(_git(p, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = p / index
    index.write_bytes(b"definitely not a git index")

    st = W.status(info)
    assert st["status_ok"] is False
    assert st["error"], "必须给出失败原因"
    with pytest.raises(W.WorktreeError):
        _strict_remove(info, ee_home)
    assert p.is_dir(), "读不到状态时必须保留"


# ---------------------------------------------------------------- F. 未跟踪的 tmp 样文件

def test_strict_remove_refuses_tmp_looking_untracked_file(tmp_path, ee_home):
    """ee 原子写残留的按名豁免只用于手动路径；自动回收不得放过任何未跟踪文件。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    sneaky = p / ".important.123.deadbeef.tmp"
    sneaky.write_text("user data\n", encoding="utf-8", newline="\n")

    with pytest.raises(W.WorktreeError) as e:
        _strict_remove(info, ee_home)
    assert "未跟踪" in str(e.value) or "未提交" in str(e.value)
    assert sneaky.is_file(), "按名字豁免在这里会误删用户数据"


# ---------------------------------------------------------------- E. git remove 失败

def test_git_remove_failure_keeps_directory_intact(tmp_path, ee_home, monkeypatch):
    """`git worktree remove` 失败时绝不 fallback 到 shutil.rmtree。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    p = Path(info["path"])
    keep = p / "keep-me.txt"
    keep.write_text("data\n", encoding="utf-8", newline="\n")
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "keep")

    real_git = W._git

    def failing_git(args, cwd, timeout=60, check=True, env=None):
        if args[:2] == ["worktree", "remove"]:
            raise W.WorktreeError("模拟：文件被占用")
        return real_git(args, cwd, timeout=timeout, check=check, env=env)

    monkeypatch.setattr(W, "_git", failing_git)
    with pytest.raises(W.WorktreeError) as e:
        _strict_remove(info, ee_home)
    assert "未做任何强制文件删除" in str(e.value)
    assert p.is_dir() and keep.is_file(), "删除失败时目录必须完整"
    assert any(f["kind"] == "worktree_remove" for f in W.cleanup_failures())


# ---------------------------------------------------------------- E. 创建/回滚

def test_create_rolls_back_when_registry_save_fails(tmp_path, ee_home, monkeypatch):
    """`git worktree add` 成功但 `_save` 失败：回收刚建的 worktree 与新分支。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    real_save = W._save
    calls = {"n": 0}

    def flaky_save(items, home=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("磁盘满（模拟）")
        return real_save(items, home)

    monkeypatch.setattr(W, "_save", flaky_save)
    with pytest.raises(W.WorktreeError) as e:
        W.create(repo, name="rollback-me", home=ee_home)
    assert "登记失败" in str(e.value)
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert "rollback-me" not in listed, "失败的创建不能留下孤儿 worktree"
    branches = _git(repo, "branch", "--list", "--format=%(refname:short)")
    assert "ee/rollback-me" not in branches, "新分支也要回收"


def test_create_for_agent_surfaces_rollback_failure(tmp_path, ee_home, monkeypatch):
    """同步脏改动失败且回滚也失败时，错误必须带上路径/分支，不能吞掉。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8", newline="\n")   # 未提交改动

    monkeypatch.setattr(W, "_sync_dirty", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("同步失败（模拟）")))
    monkeypatch.setattr(W, "remove", lambda *a, **k: (_ for _ in ()).throw(W.WorktreeError("回滚失败（模拟）")))
    with pytest.raises(W.WorktreeError) as e:
        W.create_for_agent(repo, "sync-fail", ee_home)
    msg = str(e.value)
    assert "回滚也失败" in msg and "分支" in msg
    assert any(f["kind"] == "sync_dirty_rollback" for f in W.cleanup_failures())


def test_prepare_dirty_base_reports_cleanup_failure(tmp_path, ee_home, monkeypatch):
    """baseline 临时 worktree 删除失败要被记录（调用方可能已被取消）。"""
    from ee.core import worktree as W

    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(W, "remove", lambda *a, **k: (_ for _ in ()).throw(W.WorktreeError("删不掉（模拟）")))

    out = W.prepare_dirty_base(repo, ee_home)
    assert out["cleanup"]["removed"] is False
    assert "删不掉" in out["cleanup"]["error"]
    assert any(f["kind"] == "prepare_dirty_base_cleanup" for f in W.cleanup_failures())


def test_strict_remove_happy_path_keeps_branch(tmp_path, ee_home):
    """正常落选：只删目录，保留可恢复分支（新 strict 语义下的基线行为）。"""
    repo = _repo(tmp_path)
    info = _made(repo, ee_home)
    out = _strict_remove(info, ee_home)
    assert out["removed"] is True and out["branch_deleted"] is False
    assert not Path(info["path"]).exists()
    branches = _git(repo, "branch", "--list", "--format=%(refname:short)")
    assert info["branch"] in branches
