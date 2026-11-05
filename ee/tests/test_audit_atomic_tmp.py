"""原子写临时文件留在用户工作区里的残渣要有人收（2026-11 审计 P2#82）。

`atomic.write_bytes` 的临时文件是 `.{目标名}.{pid}.{8hex}.tmp`，直接落在**目标文件
所在目录**，也就是用户的仓库里。只有 Python 层抛异常时才会 unlink；进程被
`taskkill /F`、`lifeline.hard_exit_after` 的 `os._exit`、或断电杀掉时，解释器不执行
任何 finally，文件永久留下。

这些残留不是无害的：glob/grep 都带 `--hidden`、@文件 索引用 `rg --files --hidden`、
文件面板 scandir 列出所有点文件、磁盘快照的 `add -A` 会把它写进 tree 并在检查点 diff
里显示成「改动过的文件」。全仓原本没有任何代码扫描或回收这个命名模式。

清扫的安全边界（宁可漏删，不可错删）：
- 写者进程还活着 → 不动（那是别人正在写的文件，删了就毁了他这次写入）
- 文件刚创建 → 先不动（给正在启动的写者留窗口）
- 名字不匹配本模式 → 一律不碰（`.env.local` 这类用户的点文件必须原样保留）
"""
import os
import time
from pathlib import Path

from ee.core import atomic


def _age(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_sweeps_residue_left_by_dead_writer(tmp_path):
    """写者已死 + 早于窗口期：残留被清掉，正常写入不受影响。"""
    leftover = tmp_path / ".a.txt.999999.0badc0de.tmp"
    leftover.write_text("半截内容", encoding="utf-8")
    _age(leftover, 7200)

    atomic.write_text(tmp_path / "a.txt", "新内容\n")

    assert (tmp_path / "a.txt").read_text(encoding="utf-8").strip() == "新内容"
    assert not leftover.exists(), "写者已死的残留应当被清掉"


def test_keeps_residue_of_live_writer(tmp_path):
    """写者进程还活着：绝不能删。"""
    live = tmp_path / f".b.txt.{os.getpid()}.0badc0de.tmp"
    live.write_text("正在写", encoding="utf-8")
    _age(live, 7200)

    removed = atomic.sweep_leftovers(tmp_path)

    assert live.exists(), "写者还活着时删掉 = 毁掉别人这次写入"
    assert removed == []


def test_keeps_fresh_residue(tmp_path):
    """刚创建的文件不动：可能正有一个进程在写它。"""
    fresh = tmp_path / ".c.txt.999999.0badc0de.tmp"
    fresh.write_text("刚建的", encoding="utf-8")

    atomic.sweep_leftovers(tmp_path)

    assert fresh.exists()


def test_does_not_touch_unrelated_dotfiles(tmp_path):
    """名字不匹配本模式的点文件一律不碰。"""
    env = tmp_path / ".env.local"
    env.write_text("SECRET=1", encoding="utf-8")
    _age(env, 7200)
    plain = tmp_path / ".a.txt.tmp"          # 少了 pid 段，不合模式
    plain.write_text("x", encoding="utf-8")
    _age(plain, 7200)

    removed = atomic.sweep_leftovers(tmp_path)

    assert env.exists() and plain.exists()
    assert removed == []
