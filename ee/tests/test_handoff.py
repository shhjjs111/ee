"""文件交接的迁移清单（T10-e）。

方案原文（`stabilization-and-d6.md:128`）：
> **本地↔worktree 交接**：先生成迁移清单，包含 tracked/untracked、二进制、
> staged/unstaged、忽略文件策略；用独立备份和内容哈希证明可恢复。
> 先复制并验证目标，再切换会话 cwd 与派生资源，最后经确认清理源；
> 失败保留原件，恢复失败显式列出。现有影子快照不能充当无条件兜底。

这一层**只做清单与验证**，不做实际复制/切换/删除——那些是不可逆动作，
必须由用户在看过清单后确认。守的是：
- 清单必须覆盖 tracked/untracked/staged/unstaged/ignored 与二进制；
- 每一条有内容哈希，能证明「复制后确实一致」；
- 忽略文件有明确策略，不能悄悄漏掉；
- 清单里没有的路径绝不出现（防止把源目录整个卷进去）。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo(tmp_path):
    """一个真实 git 仓库：tracked + staged + unstaged + untracked + ignored + 二进制。

    顺序有讲究：staged 的改动必须在**最后一次 commit 之后**再做，否则
    下一次 commit 会把它一起提交掉，就没有 staged 了。
    """
    import subprocess

    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / ".gitignore").write_text("*.log\nnode_modules/\n", encoding="utf-8")
    (r / "a.py").write_text("print(1)\n", encoding="utf-8")
    (r / "b.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)

    # 最后一次 commit 之后再制造三种工作区状态：
    (r / "a.py").write_text("print(2)\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=r, check=True)      # staged
    (r / "b.py").write_text("x = 2\n", encoding="utf-8")            # unstaged
    (r / "new.txt").write_text("hello\n", encoding="utf-8")         # untracked
    (r / "debug.log").write_text("log\n", encoding="utf-8")         # ignored
    return r


def test_manifest_covers_all_categories(repo):
    """清单要区分 tracked-modified / staged / untracked / ignored 四类。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    cats = {e.category for e in m.entries}
    assert "staged" in cats, cats
    assert "unstaged" in cats, cats
    assert "untracked" in cats, cats
    assert "ignored" in cats, cats


def test_manifest_entries_have_content_hash(repo):
    """每条都要有 sha256，才能证明复制后一致。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    for e in m.entries:
        if e.kind == "file":
            assert e.sha256 and len(e.sha256) == 64, f"{e.path} 缺哈希"
            assert e.size >= 0


def test_binary_files_are_flagged(repo):
    """二进制要标出来：文本工具处理二进制会坏。"""
    from ee.core.handoff import build_manifest

    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    m = build_manifest(repo)
    binentry = next(e for e in m.entries if e.path.endswith("blob.bin"))
    assert binentry.binary is True


def test_manifest_does_not_escape_repo(repo):
    """清单里不能出现仓库外的路径。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    root = str(repo.resolve())
    for e in m.entries:
        full = str((repo / e.path).resolve())
        assert full.startswith(root), f"{full} 跑出仓库了"


def test_git_dir_is_never_listed(repo):
    """.git 内部不能进清单——复制它等于复制一半的仓库状态。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    assert not any(".git/" in e.path.replace("\\", "/") for e in m.entries)


def test_ignored_policy_is_explicit(repo):
    """忽略文件要有明确策略，默认不迁移但必须列出来让人看见。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    ign = [e for e in m.entries if e.category == "ignored"]
    assert ign, "忽略文件必须出现在清单里，不能不声不响漏掉"
    assert m.ignored_policy in ("skip", "include"), m.ignored_policy
    assert all(e.selected is False for e in ign) or m.ignored_policy == "include"


def test_manifest_summary_counts(repo):
    """摘要要给出各类计数与总字节，用户看清单前先有量级。"""
    from ee.core.handoff import build_manifest

    m = build_manifest(repo)
    s = m.summary()
    assert s["total"] == len(m.entries)
    assert s["selected"] <= s["total"]
    assert "bytes" in s


# ---------------------------------------------------------------- 验证

def test_verify_detects_missing_file(repo, tmp_path):
    """目标缺文件时必须报出来，不能静默当成功。"""
    from ee.core.handoff import build_manifest, verify_target

    m = build_manifest(repo)
    dest = tmp_path / "dest"
    dest.mkdir()
    problems = verify_target(m, dest)
    assert problems, "目标全空却报验证通过"
    assert any("缺失" in p or "missing" in p.lower() for p in problems)


def test_verify_detects_content_mismatch(repo, tmp_path):
    """内容不一致要报出来（哈希不符）。"""
    from ee.core.handoff import build_manifest, verify_target

    m = build_manifest(repo)
    dest = tmp_path / "dest"
    dest.mkdir()
    for e in m.entries:
        if e.kind != "file" or not e.selected:
            continue
        p = dest / e.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"wrong content")
    problems = verify_target(m, dest)
    assert problems
    assert any("哈希" in p or "hash" in p.lower() or "不一致" in p for p in problems)


def test_verify_passes_for_exact_copy(repo, tmp_path):
    """逐字节正确的目标应验证通过。"""
    import shutil

    from ee.core.handoff import build_manifest, verify_target

    m = build_manifest(repo)
    dest = tmp_path / "dest"
    dest.mkdir()
    for e in m.entries:
        if e.kind != "file" or not e.selected:
            continue
        src = repo / e.path
        dst = dest / e.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    assert verify_target(m, dest) == []


# ---------------------------------------------------------------- 安全

def test_manifest_is_readonly(repo):
    """生成清单不能改仓库任何东西（含 .git/index）。"""
    import subprocess

    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    from ee.core.handoff import build_manifest

    build_manifest(repo)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert before == after, "生成清单改变了工作区状态"


def test_manifest_is_deterministic(repo):
    """同状态两次生成结果一致（条目顺序稳定）。"""
    from ee.core.handoff import build_manifest

    a = [(e.path, e.sha256, e.category) for e in build_manifest(repo).entries]
    b = [(e.path, e.sha256, e.category) for e in build_manifest(repo).entries]
    assert a == b
