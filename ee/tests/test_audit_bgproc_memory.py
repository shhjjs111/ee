"""后台命令表不回收 = 长会话内存无界增长（2026-11 审计 P2#57）。

`BgManager.procs` 里的条目永不移除：进程结束后 `_cleanup_files` 只关日志、删临时脚本，
既不清 `self.procs`，也不释放 `BgProc._buf`（BUFFER_CHARS=400_000 字符，最坏约 1.6MB）。
`MAX_PROCS` 只统计 running，所以「起-停」循环可以无限往里堆。

长会话或自动化反复跑后台命令时内存单调增长，`shell/list` 与 bgshell 的「现有后台命令」
提示也随之越来越长。完整输出本来就在日志文件里，内存缓冲不需要整段留着。
"""
import asyncio
import os
import sys

from ee.core import bgproc
from ee.core.bgproc import BgManager
from ee.tools.shellenv import resolve_shell


def _cmd(sh, body: str) -> str:
    """用 sys.executable 跑一行 -c；body 里不放引号，避免 Windows/POSIX 的引号规则打架。"""
    exe = sys.executable
    if sh.syntax == "powershell":
        return f'& "{exe}" -u -c "{body}"'
    return f'"{exe}" -u -c "{body}"'


def test_finished_entries_are_capped(tmp_path, monkeypatch):
    """起停多次后，已结束的条目不能无限堆积。"""
    monkeypatch.setattr(bgproc, "MAX_FINISHED", 3)

    async def go():
        sh = resolve_shell()
        mgr = BgManager(tmp_path / "logs", owner="t")
        for _ in range(6):
            p = await mgr.start(_cmd(sh, "print(1)"), sh, tmp_path, dict(os.environ))
            assert await mgr.wait_exit(p, timeout=120), "后台命令没在预算内结束"
        await mgr.close()
        return mgr

    mgr = asyncio.run(go())
    assert len(mgr.procs) <= 3, f"已结束条目没有被回收：{len(mgr.procs)}"


def test_buffer_is_trimmed_after_exit(tmp_path, monkeypatch):
    """结束后释放内存缓冲，但累计计数与「丢了多少」必须仍然诚实。"""
    monkeypatch.setattr(bgproc, "KEEP_AFTER_EXIT", 1000)

    async def go():
        sh = resolve_shell()
        mgr = BgManager(tmp_path / "logs", owner="t")
        p = await mgr.start(_cmd(sh, "print(chr(120)*20000)"), sh, tmp_path, dict(os.environ))
        assert await mgr.wait_exit(p, timeout=120)
        await mgr.close()
        return p

    p = asyncio.run(go())
    assert p.total > 20000, p.total
    assert p._buf_len <= 1000, f"结束后仍留着 {p._buf_len} 字符"
    text, lost = p.text_from(0)
    assert len(text) <= 1000
    assert lost > 0, "缓冲被裁掉后必须如实报告丢了多少"


def test_latest_finished_entry_is_still_queryable(tmp_path, monkeypatch):
    """回收只能淘汰最旧的：刚结束的那条还要能被 shell_output 取到。"""
    monkeypatch.setattr(bgproc, "MAX_FINISHED", 1)

    async def go():
        sh = resolve_shell()
        mgr = BgManager(tmp_path / "logs", owner="t")
        ids = []
        for _ in range(3):
            p = await mgr.start(_cmd(sh, "print(2)"), sh, tmp_path, dict(os.environ))
            assert await mgr.wait_exit(p, timeout=120)
            ids.append(p.id)
        await mgr.close()
        return mgr, ids

    mgr, ids = asyncio.run(go())
    assert list(mgr.procs) == [ids[-1]], f"留下的应该只有最后一条，实际 {list(mgr.procs)}"
    assert mgr.get(ids[-1]).status != "running"
