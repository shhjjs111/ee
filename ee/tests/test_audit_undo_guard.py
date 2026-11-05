"""/undo 的目标选择（2026-11 审计修复）。

回合中断 / 崩溃 / 收尾抓取失败时，最近回合没有「回合后快照」。原实现遇到这种条目
直接 continue，继续往更早的回合找——而更早那条有 after，于是它成了撤销对象：
最近回合的改动没撤，更早回合的成果被回滚，界面上写的却是「已恢复到你上一回合修改之前」，
被误动的那一条还被永久标成 undone。
"""
import pytest

from ee.core.session import Session
from ee.core.snapshot import DiskSnapshot


class _ProbeSession(Session):
    """只跑 _disk_undo 需要的那几个方法，不建完整的 Session。"""

    _after_restore = lambda self, *a, **k: None       # 只验文件状态与标记，不验提示文案

    def __init__(self, ds):
        self._ds = ds
        self.turn_trees = []
        self.last_undo_tree = None

    def disk(self):
        return self._ds


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    f = ws / "a.txt"
    f.write_text("1\n", encoding="utf-8")
    ds = DiskSnapshot(ws, home)
    if ds.disabled:
        pytest.skip(f"磁盘快照不可用：{ds.disabled}")
    return ds, f


def test_undo_refuses_when_recent_turn_has_no_after_snapshot(env):
    ds, f = env
    t1 = ds.capture()
    f.write_text("2\n", encoding="utf-8")
    t2 = ds.capture()
    t3 = ds.capture()
    f.write_text("3\n", encoding="utf-8")     # 回合中断：这条回合没有 after

    s = _ProbeSession(ds)
    s.turn_trees = [{"before": t1, "after": t2}, {"before": t3}]
    ds.capture = lambda: None                 # undo 时抓不到当前快照

    assert s._disk_undo() is None, "拿不到当前快照时应该拒绝撤销，而不是退回内存层之外"
    assert f.read_text(encoding="utf-8") == "3\n", "把更早回合的成果回滚了"
    assert not any(e.get("undone") for e in s.turn_trees), "更早回合被误标成已撤销"


def test_undo_still_works_when_after_snapshot_exists(env):
    """守住正常路径：最近回合有 after 时照常撤销它。"""
    ds, f = env
    t1 = ds.capture()
    f.write_text("2\n", encoding="utf-8")
    t2 = ds.capture()

    s = _ProbeSession(ds)
    s.turn_trees = [{"before": t1, "after": t2}]

    restored = s._disk_undo()

    assert restored, "正常回合的撤销被拦住了"
    assert f.read_text(encoding="utf-8") == "1\n"
    assert s.turn_trees[0]["undone"] is True
    assert s.last_undo_tree == t1
