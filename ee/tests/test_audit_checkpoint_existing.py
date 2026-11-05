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


# ---------------------------------------------------------------- 时间线视图
#
# `checkpoint/list` 的 _list 原来是 register() 内的闭包，逐条算 expired/after_expired，
# 判据 `(not unknown) and tree not in alive` 埋在闭包里取不到直接调用点，也没有测试
# 覆盖 unknown 分支。这里把整段摊平逻辑提成模块级 timeline_entries。

ENT = {"id": "u1", "ts": 1.0, "text": "第一问", "tree": TREE, "tree_after": "b" * 40, "undone": False}
RP = {"id": "rp-1", "ts": 2.0, "label": "恢复点", "tree": "c" * 40}
ENTRY_KEYS = {"id", "kind", "turn", "ts", "text", "before", "after",
              "undone", "running", "stat", "expired", "after_expired"}


def _numstat_recorder():
    calls = []

    def numstat(before, after):
        calls.append((before, after))
        return {"files": 1, "additions": 1, "deletions": 0}

    return numstat, calls


def test_timeline_does_not_mark_expired_when_git_fails():
    """git 故障（alive=None）：一条都不能标 expired——「不知道」不是「已清理」。"""
    numstat, calls = _numstat_recorder()

    out = R.timeline_entries([(1, ENT)], [RP], None, numstat, None)

    assert [e["id"] for e in out] == ["u1", "rp-1"]
    assert [e["expired"] for e in out] == [False, False], out
    assert [e["after_expired"] for e in out] == [False, False], out
    assert [e["stat"] for e in out] == [None, None], out
    assert calls == [], "存活无法判断时不该去算 diff"


def test_timeline_marks_expired_only_when_tree_really_gone():
    """git 正常且 tree 确实不在了：仍然如实标 expired。"""
    numstat, calls = _numstat_recorder()

    out = R.timeline_entries([(1, ENT)], [RP], {"b" * 40}, numstat, None)

    turn, rp = out
    assert turn["expired"] is True
    assert turn["after_expired"] is False
    assert turn["stat"] is None
    assert rp["expired"] is True
    assert calls == []


def test_timeline_computes_stat_when_both_trees_alive():
    numstat, calls = _numstat_recorder()

    out = R.timeline_entries([(1, ENT)], [], {ENT["tree"], ENT["tree_after"]}, numstat, None)

    assert out[0]["expired"] is False and out[0]["after_expired"] is False
    assert out[0]["stat"] == {"files": 1, "additions": 1, "deletions": 0}
    assert calls == [(ENT["tree"], ENT["tree_after"])]


def test_timeline_marks_running_turn():
    """抽取时不能丢掉 running 判据：正在跑且还没结束快照的最后一回合要标出来。"""
    numstat, _ = _numstat_recorder()
    running_ent = {**ENT, "tree_after": None}

    out = R.timeline_entries([(1, running_ent)], [], {running_ent["tree"]}, numstat, running_ent["id"])

    assert out[0]["running"] is True


def test_timeline_entry_fields_unchanged():
    """字段集合不能在设计外变动——前端 Checkpoint 类型按这些 key 渲染（turn 只属于回合）。"""
    numstat, _ = _numstat_recorder()

    out = R.timeline_entries([(1, ENT)], [RP], None, numstat, None)

    assert set(out[0]) == ENTRY_KEYS
    assert set(out[1]) == ENTRY_KEYS - {"turn"}
    assert out[1]["kind"] == "restore" and out[1].get("turn") is None

def test_worktree_pointer_written_atomically():
    """ee-worktree.txt 必须原子写（审计 P2#1）。

    影子仓库靠它记录工作区路径。用裸 `write_text` 时，断电/强杀正好卡在写这一瞬间会留下
    空文件或半截路径；而 checkpoints.gc 读它失败时只是 `wt = ""`，于是这个仓库被判成
    「工作区没了」——如果恰好又没有会话引用它，整个影子仓库会被 rmtree 掉。
    """
    import inspect

    from ee.core.snapshot import DiskSnapshot

    src = inspect.getsource(DiskSnapshot._ensure)
    assert '(self.gitdir / "ee-worktree.txt").write_text(' not in src, "仍然是裸 write_text"
    assert "atomic" in src.lower()
