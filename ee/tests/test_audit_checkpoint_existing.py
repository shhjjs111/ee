"""检查点存储故障不能被翻译成「数据不存在」（2026-11 审计修复）。

`DiskSnapshot.existing()` 捕获任何 SnapshotError（git 超时、对象被占用、另一进程正在写 refs）
后返回空集合，而空集合的语义是「这些 tree 都已过期被清理」。调用方据此给时间线里每个
检查点标 expired=True、并拒绝恢复/对比，文案是「这个检查点已被清理（超过保留期限）」。

一次瞬时基础设施故障于是变成业务结论「你的历史没了」，用户会放弃恢复；刷新之后又恢复
正常，行为前后矛盾。这里让 existing() 用 None 表达「无法判断」，并把「故障 vs 真没了」
的判断收进一个共用函数（原来两处各写一遍 `!= {…}`）。
"""
import threading
from types import SimpleNamespace

import pytest

from ee.core.snapshot import DiskSnapshot, SnapshotError
from ee.server import checkpoint_rpc as R
from ee.server.app import RpcError

TREE = "a" * 40


def test_existing_returns_none_when_git_fails(tmp_path, monkeypatch):
    """git 出故障 → None（无法判断），不是空集合（全都不存在）。"""
    ds = object.__new__(DiskSnapshot)
    ds.gitdir = tmp_path
    (tmp_path / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    ds._lock = threading.Lock()

    def boom(*a, **k):
        raise SnapshotError("git cat-file 超时")

    monkeypatch.setattr(ds, "_run", boom)

    assert ds.existing([TREE]) is None


def test_require_alive_says_unavailable_on_fault():
    """存储故障：提示「暂时不可用」，不是「已被清理」。"""
    fake = SimpleNamespace(existing=lambda ts: None)

    with pytest.raises(RpcError) as ei:
        R._require_alive(fake, [TREE], "这个检查点")

    msg = str(ei.value)
    assert "暂时" in msg or "稍后" in msg, msg
    assert "已被清理" not in msg, msg


def test_require_alive_says_expired_when_really_gone():
    """确实被清理了：仍然如实说「已被清理」。"""
    fake = SimpleNamespace(existing=lambda ts: set())

    with pytest.raises(RpcError) as ei:
        R._require_alive(fake, [TREE], "这个检查点")

    assert "已被清理" in str(ei.value)


def test_require_alive_passes_when_all_present():
    fake = SimpleNamespace(existing=lambda ts: set(ts))
    R._require_alive(fake, [TREE], "这个检查点")      # 不抛
