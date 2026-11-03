"""逐请求用量落盘（缓存命中率诊断的地基）。

turn_end 的 usage 是累计快照，回答不了「哪一次请求漏了多少」——而命中率优化只能从
逐请求的 wire/new/gap/break 里看出问题。这里守三件事：
  1. 每笔请求真的写进会话 JSONL，读得回来，字段完整；
  2. 老会话（没有 usage 行）读取不报错、不误判；
  3. 子代理那些请求也进账本（它 persist=False，得由父会话代写）。
"""
from __future__ import annotations

from conftest import FakeClient, collect, finish, run, text_chunks

from ee.core.store import SessionStore


def _session(workspace, ee_home, script):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    return Session(cfg, cwd=workspace, client=FakeClient(script), home=ee_home)


def _usage(path):
    return SessionStore.usage_records(path)


def test_each_request_is_written_with_attribution(ee_home, workspace):
    """两回合 → 两次主对话请求 → 两条 usage 行，带 wire/new/gap。"""
    s = _session(workspace, ee_home, [
        text_chunks("答一") + finish("stop", prompt=2000, completion=50, hit=0),
        text_chunks("答二") + finish("stop", prompt=2600, completion=50, hit=1700),
    ])
    run(collect(s, "问一" + "长" * 200))
    run(collect(s, "问二" + "长" * 200))
    rows = _usage(s.store.path)
    assert len(rows) == 2
    for r in rows:
        assert r["role"] == "main"
        assert r["prompt"] > 0 and r["completion"] == 50
        assert r["wire"] > 0 and r["new"] >= 0
        assert r["model"] and r["turn"] is not None
    assert rows[0]["hit"] == 0
    assert rows[1]["hit"] == 1700
    assert rows[1]["gap"] == 900
    assert "break" not in rows[1]


def test_persisted_rows_match_in_memory_records(ee_home, workspace):
    """落盘的必须和内存里的 records 一模一样，否则算出来的命中率和界面对不上。"""
    s = _session(workspace, ee_home, [
        text_chunks("答一") + finish("stop", prompt=1000, completion=10, hit=0),
        text_chunks("答二") + finish("stop", prompt=1200, completion=10, hit=900),
    ])
    run(collect(s, "问一"))
    run(collect(s, "问二"))
    assert _usage(s.store.path) == s.stats.records


def test_side_and_compact_requests_keep_their_role(ee_home, workspace):
    """压缩请求命中恒 0，落盘时必须还带着 role=compact，事后算命中率才分得清。"""
    s = _session(workspace, ee_home, [
        text_chunks("答一") + finish("stop", prompt=10_000, completion=50, hit=9_900),
        text_chunks("## 用户目标\n继续") + finish("stop", prompt=10_200, completion=50, hit=0),
    ])
    run(collect(s, "问一"))
    assert run(s.compact())["ok"]
    roles = [r["role"] for r in _usage(s.store.path)]
    assert roles.count("main") == 1
    assert roles.count("compact") == 1


def test_missing_usage_still_written(ee_home, workspace):
    """流中途失败没有 usage，也要落一行（usage_missing），否则账本少一笔。"""
    s = _session(workspace, ee_home, [text_chunks("说到一半断了")])
    run(collect(s, "问一"))
    rows = _usage(s.store.path)
    assert len(rows) == 1
    assert rows[0].get("usage_missing") is True
    assert rows[0]["cost"] is None


def test_usage_does_not_affect_history_rebuild(ee_home, workspace):
    """usage 行只进账本，不能混进 messages（否则恢复会话时历史里多出怪消息）。"""
    s = _session(workspace, ee_home, [text_chunks("答一") + finish("stop", prompt=1000, completion=10)])
    run(collect(s, "问一"))
    d = SessionStore.load(s.store.path)
    assert _usage(s.store.path)
    # 账本是独立的一条读取路径：历史重建既看不到它，也不会被它改变
    assert not [m for m in d["messages"] if m.get("_kind") == "usage" or "wire" in m]
    assert d["messages"] == SessionStore.load_messages(s.store.path)


def test_old_session_without_usage_lines_reads_fine(ee_home, workspace):
    """老会话（这个功能上线前写的）没有 usage 行：load() 照常工作，usage 是空列表。"""
    store = SessionStore(ee_home, session_id="old")
    store.message({"role": "user", "content": "很久以前的一条消息"})
    store.item({"id": "m0", "type": "user", "text": "很久以前的一条消息", "seq": 1})
    assert SessionStore.usage_records(store.path) == []
    assert SessionStore.load(store.path)["messages"] == [{"role": "user", "content": "很久以前的一条消息"}]


def test_subagent_requests_are_booked_by_parent(ee_home, workspace):
    """子代理 persist=False，它自己写不了盘；它的请求必须由父会话按 role=subagent 补进账本。"""
    from ee.core.context import UsageStats
    from ee.core.providers.profiles import Price
    from ee.core.subagent import _merge_usage

    parent = _session(workspace, ee_home, [text_chunks("答") + finish("stop")])
    child = _session(workspace, ee_home, [text_chunks("答") + finish("stop")])
    child.stats.add({"prompt": 500, "completion": 20, "hit": 100}, Price(input=2.0, output=8.0, cache_read=0.04),
                    model="m", role="main")
    assert child.store.enabled is False or not child.store.path.is_file(), "子代理不该自己开文件"
    _merge_usage(parent, child)
    rows = _usage(parent.store.path)
    assert [r["role"] for r in rows] == ["subagent"]
    assert parent.stats.records[-1]["role"] == "subagent"
    assert isinstance(parent.stats, UsageStats)
