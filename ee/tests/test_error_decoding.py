"""子进程报错信息的中文解码：中文 Windows 上 git 钩子、系统错误、MCP 服务器的 stderr 常是 GBK，不能按 UTF-8 硬解成乱码。"""
import pytest

from ee.core import gitinfo, snapshot, worktree

GBK = "拒绝访问：钩子检查未通过".encode("gbk")


def test_worktree_git_error_gbk(monkeypatch, tmp_path):
    monkeypatch.setattr(worktree.gitinfo, "git_exe", lambda: "git")
    monkeypatch.setattr(worktree, "run_capture", lambda *a, **k: (128, b"", b"fatal: " + GBK))
    with pytest.raises(worktree.WorktreeError) as ei:
        worktree._git(["status"], tmp_path)
    assert "拒绝访问：钩子检查未通过" in str(ei.value)


def test_commit_output_gbk_from_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(gitinfo, "git_exe", lambda: "git")
    monkeypatch.setattr(gitinfo, "run_capture", lambda *a, **k: (1, b"", GBK))
    ok, msg = gitinfo.commit(tmp_path, "msg", stage_all=False)
    assert not ok and "拒绝访问：钩子检查未通过" in msg


def test_snapshot_error_gbk(monkeypatch, tmp_path):
    s = snapshot.DiskSnapshot.__new__(snapshot.DiskSnapshot)
    s.exe, s.worktree = "git", tmp_path
    monkeypatch.setattr(s, "_env", lambda: None, raising=False)
    monkeypatch.setattr(snapshot, "run_capture", lambda *a, **k: (1, b"", GBK))
    with pytest.raises(snapshot.SnapshotError) as ei:
        s._run("add")
    assert "拒绝访问：钩子检查未通过" in str(ei.value)


def test_mcp_stderr_gbk():
    from ee.mcp import client
    assert client._decode_line(GBK) == "拒绝访问：钩子检查未通过"
    assert client._decode_line("正常 UTF-8".encode("utf-8")) == "正常 UTF-8"
