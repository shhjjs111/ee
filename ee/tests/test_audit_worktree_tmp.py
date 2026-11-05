"""ee 自己的原子写残留不该挡住 worktree 合并（2026-11 审计修复）。

worktree/merge 的前置门是 `_dirty(root)`，而它用 `git status --porcelain
--untracked-files=normal` —— 未跟踪文件也算「未提交的改动」。原子写留下的
`.<name>.<pid>.<8hex>.tmp` 正是未跟踪文件：进程被强杀/断电时来不及删，就一直躺在
用户仓库里，于是合并被永久拒绝，而报错只说「主目录有未提交的改动，先提交或暂存后再合并」，
完全不提这是我们自己留下的隐藏临时文件。

这是 ee 自己制造的垃圾挡住了用户的操作，方向反了：把它排除在外。
"""
import ee.core.worktree as W


def test_tmp_residue_alone_is_not_dirty(monkeypatch):
    monkeypatch.setattr(W, "_git", lambda *a, **k: "?? .notes.txt.12345.a1b2c3d4.tmp\0")
    assert W._dirty("/tmp/x") is False, "ee 自己的原子写残留被当成「未提交的改动」"


def test_nested_tmp_residue_is_also_ignored(monkeypatch):
    monkeypatch.setattr(W, "_git", lambda *a, **k: "?? src/.main.py.999.0f1e2d3c.tmp\0")
    assert W._dirty("/tmp/x") is False


def test_real_untracked_file_still_blocks(monkeypatch):
    """护栏不能削弱：用户真留下的未跟踪文件仍然要拦。"""
    monkeypatch.setattr(W, "_git", lambda *a, **k: "?? 我的笔记.txt\0")
    assert W._dirty("/tmp/x") is True


def test_tmp_residue_alongside_real_change_still_blocks(monkeypatch):
    out = "?? .a.txt.1.a1b2c3d4.tmp\0 M src/app.py\0"
    monkeypatch.setattr(W, "_git", lambda *a, **k: out)
    assert W._dirty("/tmp/x") is True, "有真实改动时不能因为混着 tmp 就放过"


def test_clean_repo_is_not_dirty(monkeypatch):
    monkeypatch.setattr(W, "_git", lambda *a, **k: "")
    assert W._dirty("/tmp/x") is False
