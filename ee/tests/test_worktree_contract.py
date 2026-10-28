"""worktree 查询与删除的安全契约。

既有测试覆盖了 finish_agent 的 diff 失败（test_worktree_finish_safety.py）与删除前自动保存，
但没有覆盖下面四条，而它们都通向"用户成果被删"或"报告与事实不符"：

1. `W.diff()` 为了把未跟踪文件放进 diff，会执行 `git add -A --intent-to-add`。这是**查询**，
   却修改了真实索引；编排 status / 卡片刷新都会反复调用它。
2. diff 用 `check=False`：git 失败时返回空字符串，和"确实没有改动"无法区分。
   自动化收尾据此删除 worktree（与 finish_agent 修复前的缺陷同族）。
3. 被 git 忽略的文件超过备份上限时只记进 skipped，删除照常继续 —— 那些文件不在分支里，
   删完就没有第二份了。
4. 保留 worktree 时，给模型的说明仍然写"已删除"，用户据此以为没有东西要救。

修复约定：
- `W.diff()` 用临时索引计算，不动真实索引；返回值增加 `ok` / `error`。
- `ok=False` 时消费方一律保留，不做破坏性清理。
- 备份不完整默认拒绝删除，除非调用方显式要求丢弃。
- 报告只说事实：`removed=True` 才说已删除。
"""
import subprocess
from pathlib import Path

import pytest

from ee.core import worktree as W

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="需要 git")


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    (r / ".gitignore").write_text(".env\nnode_modules/\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def index_entries(p: Path) -> list[str]:
    """真实索引里的条目（路径 + 暂存的内容哈希 + 标记）。查询前后必须一致。"""
    out = git(p, "ls-files", "-s")
    return sorted(line for line in out.splitlines() if line.strip())


# ---------------------------------------------------------------- 1) 查询不改索引


def test_diff_does_not_touch_real_index(repo, ee_home):
    info = W.create(repo, "peek", home=ee_home)
    p = Path(info["path"])
    (p / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (p / "brand-new.txt").write_text("new\n", encoding="utf-8")

    before = index_entries(p)
    d = W.diff(p, ee_home)
    after = index_entries(p)

    assert after == before, "查看差异改动了真实索引（intent-to-add 不应留下痕迹）"
    assert d["ok"] is True and d["error"] == ""
    assert "brand-new.txt" in {f["path"] for f in d["files"]}
    assert "a.txt" in {f["path"] for f in d["files"]}


def test_diff_is_repeatable(repo, ee_home):
    """连续两次查询结果一致，且第二次也不再改索引。"""
    info = W.create(repo, "peek2", home=ee_home)
    p = Path(info["path"])
    (p / "n.txt").write_text("n\n", encoding="utf-8")
    before = index_entries(p)
    first = W.diff(p, ee_home)
    second = W.diff(p, ee_home)
    assert index_entries(p) == before
    assert {f["path"] for f in first["files"]} == {f["path"] for f in second["files"]}
    assert first["ok"] and second["ok"]


# ---------------------------------------------------------------- 2) 读取失败 ≠ 没有改动


def test_diff_reports_git_failure(repo, ee_home, monkeypatch):
    info = W.create(repo, "boom", home=ee_home)
    p = Path(info["path"])
    (p / "x.txt").write_text("x\n", encoding="utf-8")
    real = W.run_capture

    def fake(argv, **kw):
        if "diff" in argv:
            return 1, b"", b"fatal: boom"
        return real(argv, **kw)

    monkeypatch.setattr(W, "run_capture", fake)
    d = W.diff(p, ee_home)
    assert d["ok"] is False
    assert d["error"], "失败时 error 不能为空"


def test_diff_does_not_leak_stale_files_on_failure(repo, ee_home, monkeypatch):
    """失败时不返回半份文件列表，免得消费方把残缺结果当成"就这些改动"。"""
    info = W.create(repo, "boom2", home=ee_home)
    p = Path(info["path"])
    (p / "y.txt").write_text("y\n", encoding="utf-8")
    real = W.run_capture

    def fake(argv, **kw):
        if "diff" in argv:
            return 1, b"", b"fatal: boom"
        return real(argv, **kw)

    monkeypatch.setattr(W, "run_capture", fake)
    d = W.diff(p, ee_home)
    assert d["files"] == []


# ---------------------------------------------------------------- 3) 备份不完整不删除


def test_remove_refuses_when_ignored_backup_incomplete(repo, ee_home, monkeypatch):
    monkeypatch.setattr(W, "MAX_INCLUDE_BYTES", 8)      # 让 .env 放不进备份
    info = W.create(repo, "bk", home=ee_home)
    p = Path(info["path"])
    (p / ".env").write_text("KEY=important\n" * 20, encoding="utf-8")

    with pytest.raises(W.WorktreeError) as ei:
        W.remove(p, home=ee_home)
    assert ".env" in str(ei.value), "报错要说清哪个文件没备份"
    assert (p / ".env").is_file(), "拒绝删除后文件必须还在"
    assert p.is_dir()


def test_remove_can_discard_incomplete_backup_explicitly(repo, ee_home, monkeypatch):
    """显式要求丢弃时才删；这条路径不静默，必须由调用方传参。"""
    monkeypatch.setattr(W, "MAX_INCLUDE_BYTES", 8)
    info = W.create(repo, "bk2", home=ee_home)
    p = Path(info["path"])
    (p / ".env").write_text("KEY=important\n" * 20, encoding="utf-8")

    r = W.remove(p, home=ee_home, discard_unbacked=True)
    assert r["removed"] is True
    assert not p.exists()


def test_remove_still_works_when_backup_complete(repo, ee_home):
    info = W.create(repo, "bk3", home=ee_home)
    p = Path(info["path"])
    (p / ".env").write_text("K=1\n", encoding="utf-8")
    r = W.remove(p, home=ee_home)
    assert r["removed"] is True and not p.exists()
    assert r["backed_up"] == [".env"] and r["backup_skipped"] == []


# ---------------------------------------------------------------- 4) 自动化收尾


class _StubBackend:
    def __init__(self, home):
        self.home = home

    def notify(self, *_a, **_kw):
        pass


def test_automation_keeps_worktree_when_diff_unknown(repo, ee_home, monkeypatch):
    """自动化收尾拿到 ok=False：保留成果，标 pending，等用户处理。"""
    from ee.server.automations import Automations

    info = W.create(repo, "auto-unknown", home=ee_home)
    p = Path(info["path"])
    (p / "art.txt").write_text("工作成果\n", encoding="utf-8")     # 未提交成果
    run = {"worktree": {"path": str(p), "branch": info["branch"], "repo": str(repo), "name": info["name"]}}

    monkeypatch.setattr(W, "diff", lambda *a, **kw: {
        "ok": False, "error": "git diff 失败", "files": [], "additions": 0, "deletions": 0,
        "worktree": {"commits": 0}})
    au = Automations(_StubBackend(ee_home))
    au._wt_summary(run, "completed")

    assert p.is_dir(), "diff 结果未知时删掉了 worktree（成果丢失）"
    assert (p / "art.txt").is_file()
    assert run["worktree"]["state"] == "pending"
    assert "diff" in run["worktree"].get("error", "")


def test_automation_removes_only_when_known_empty(repo, ee_home, monkeypatch):
    """确实没有改动（ok=True 且空）时照旧清理，别把正常路径修坏。"""
    from ee.server.automations import Automations

    info = W.create(repo, "auto-empty", home=ee_home)
    p = Path(info["path"])
    run = {"worktree": {"path": str(p), "branch": info["branch"], "repo": str(repo), "name": info["name"]}}

    monkeypatch.setattr(W, "diff", lambda *a, **kw: {
        "ok": True, "error": "", "files": [], "additions": 0, "deletions": 0, "worktree": {"commits": 0}})
    au = Automations(_StubBackend(ee_home))
    au._wt_summary(run, "completed")

    assert not p.exists()
    assert run["worktree"]["state"] == "clean"


# ---------------------------------------------------------------- 5) 报告说事实


def test_worktree_note_does_not_claim_deleted_when_kept():
    from ee.core.subagent import worktree_note

    note = worktree_note({"removed": False, "kept": True, "kept_reason": "diff 失败，不确定是否有改动",
                          "files": [], "file_count": 0, "additions": 0, "deletions": 0,
                          "branch": "ee/x", "path": "C:/tmp/wt", "base": "0" * 40, "commits": []})
    assert "已删除" not in note
    assert "保留" in note and "不确定" in note


def test_worktree_note_reports_deleted_when_actually_removed():
    from ee.core.subagent import worktree_note

    note = worktree_note({"removed": True, "kept": False, "files": [], "file_count": 0, "additions": 0,
                          "deletions": 0, "branch": "ee/x", "path": "C:/tmp/wt", "base": "0" * 40,
                          "commits": []})
    assert "已删除" in note
