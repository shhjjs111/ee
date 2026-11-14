"""交付验证：预期交付状态的构造与内容比对（2026-11 第一批）。

这里的每一条都在**真实 git 小仓库**里跑：临时目录里用同一份补丁应用出来的预期状态，
必须与真实工作区逐字节一致。换行、新增、删除、重命名、二进制、以及"同一个文件的补丁外
位置被外部改过"都在覆盖范围内——这些正是"路径集合相等"证明不了的东西。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from ee.core import allout as A
from ee.core import delivery as D
from ee.core import snapshot as S

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="需要 git")


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "t@example.com")
    git(ws, "config", "user.name", "t")
    # 关掉自动换行转换：本用例要验证 CRLF 文件在「快照 → 临时目录 → 写回」之间字节一致，
    # 让 git 在 add/checkout 时把 CRLF 归一成 LF 就测不出真实差异了。
    git(ws, "config", "core.autocrlf", "false")
    (ws / "keep.txt").write_text("keep\n", encoding="utf-8")
    (ws / "edit.txt").write_text("v1\n", encoding="utf-8")
    (ws / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (ws / "gone.txt").write_text("bye\n", encoding="utf-8")
    (ws / "old.txt").write_text("old\n", encoding="utf-8")
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02BIN")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "base")
    return ws


def _candidate(repo, *, extra=None) -> tuple[str, dict]:
    """在仓库里造一份候选改动并提交，返回 (基线 commit, 固定补丁)。"""
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "edit.txt").write_text("v2\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "old.txt").rename(repo / "renamed.txt")
    (repo / "bin.dat").write_bytes(b"\x00\x09CHANGED")
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    if extra:
        extra(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    patch = A.candidate_patch(repo, base)
    assert patch["ok"], patch.get("error")
    git(repo, "reset", "-q", "--hard", base)      # 回到写回前状态
    return base, patch


def test_expected_state_matches_real_apply(repo, ee_home):
    """换行 / 新增 / 删除 / 重命名 / 二进制：预期状态必须与真实写回逐字节一致。"""
    ds = S.DiskSnapshot(repo, ee_home)
    pre = ds.capture()
    assert pre, "首次抓取就失败了，用例前提不成立"
    _, patch = _candidate(repo)

    expected = D.build_expected(ds, pre, patch["patch"], patch["paths"], repo)
    assert expected.ok, expected.reason
    assert not expected.uncovered, expected.uncovered

    res = A.apply_patch(repo, patch["patch"])
    assert res["applied"], res.get("error")

    mismatches = D.compare(expected, repo, patch["paths"])
    assert mismatches == [], f"预期状态与真实交付不一致：{mismatches}"
    assert (repo / "renamed.txt").exists() and not (repo / "old.txt").exists()


def test_external_edit_outside_patch_hunks_is_detected(repo, ee_home):
    """同一文件的**补丁外位置**被外部改过：路径集合相等，但内容比对必须发现。

    这正是"反向检查 + 路径子集"证明不了的情况（GPT 指出）：补丁改第 1 行、
    外部改第 3 行，两者仍然满足路径子集与可反向应用。
    """
    ds = S.DiskSnapshot(repo, ee_home)
    pre = ds.capture()
    assert pre
    _, patch = _candidate(repo)
    expected = D.build_expected(ds, pre, patch["patch"], patch["paths"], repo)
    assert expected.ok, expected.reason
    assert A.apply_patch(repo, patch["patch"])["applied"]

    # 外部编辑器在写回之后改了 edit.txt 的别处（补丁只动第 1 行）
    (repo / "edit.txt").write_text("v2\nexternal edit\n", encoding="utf-8")
    mismatches = D.compare(expected, repo, patch["paths"])
    assert [m["path"] for m in mismatches] == ["edit.txt"], mismatches
    assert mismatches[0]["kind"] == "content"


def test_stale_snapshot_is_reported_not_assumed(repo, ee_home):
    """写回前状态与快照不一致（外部改动）→ 不能构造预期状态，必须如实报错。"""
    ds = S.DiskSnapshot(repo, ee_home)
    pre = ds.capture()
    assert pre
    _, patch = _candidate(repo)
    (repo / "edit.txt").write_text("v1 CHANGED BY EDITOR\n", encoding="utf-8")
    expected = D.build_expected(ds, pre, patch["patch"], patch["paths"], repo)
    assert not expected.ok
    assert "不一致" in expected.reason


def test_uncovered_path_is_reported(repo, ee_home, monkeypatch):
    """快照里没有、但工作区里存在的补丁路径 → 记 uncovered，不能当成"新文件"。

    真实环境里这类路径很窄（被 .gitignore 排除/超过体积上限的文件不会进补丁），
    所以这里直接构造分支条件：只让快照"少看见"一个路径，验证上层能据此拒绝宣称已验证。
    """
    ds = S.DiskSnapshot(repo, ee_home)
    pre = ds.capture()
    assert pre
    _, patch = _candidate(repo)
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    paths = [*patch["paths"], "untracked.txt"]

    real = D._tree_entries
    monkeypatch.setattr(D, "_tree_entries", lambda disk, tree, ps: real(disk, tree, [p for p in ps if p != "untracked.txt"]))
    expected = D.build_expected(ds, pre, patch["patch"], paths, repo)
    assert expected.uncovered == ["untracked.txt"], expected.uncovered
