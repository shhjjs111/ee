"""残留 index.lock 的自愈（2026-11 审计修复）。

git 在 `add -A` 期间持有 $GIT_DIR/index.lock。进程被 taskkill /F、断电，或 capture
超时后 run_capture 杀进程树时，锁来不及删。此后每次 add 都 fatal，而错误文本不含
「超时」，所以不会置 disabled —— 快照静默退化成内存快照，界面却仍显示可用。
"""
import os
import time

import pytest

from ee.core.snapshot import CAPTURE_TIMEOUT, DiskSnapshot


@pytest.fixture
def snap(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    ds = DiskSnapshot(ws, home)
    if ds.disabled:
        pytest.skip(f"磁盘快照不可用：{ds.disabled}")   # 没装 git 的环境
    return ds


def test_stale_index_lock_is_cleared_and_capture_recovers(snap):
    assert snap.capture(), "首次抓取就失败了，用例前提不成立"

    lock = snap.gitdir / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - CAPTURE_TIMEOUT * 3
    os.utime(lock, (old, old))
    (snap.worktree / "b.txt").write_text("second\n", encoding="utf-8")

    tree = snap.capture()

    assert tree, "残留锁之后 capture 仍然失败（工作区静默退化成内存快照）"
    assert not lock.exists(), "残留锁没有被清理"
    assert not snap.disabled, "自愈之后不该把快照置为不可用"


def test_fresh_lock_is_not_stolen(snap):
    """另一个 ee 进程正在 add（锁很新）时不能把它的锁删掉。"""
    assert snap.capture()
    lock = snap.gitdir / "index.lock"
    lock.write_text("", encoding="utf-8")

    assert snap.capture() is None, "锁被别的进程持有时本次抓取应该失败"
    assert lock.exists(), "把别的进程正在持有的锁删了"
