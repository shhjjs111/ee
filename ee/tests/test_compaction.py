import json

from conftest import FakeClient, FakeError, collect, finish, run, text_chunks, tool_chunks

from ee.core import compaction as C
from ee.core.events import Compacted, Notice, TurnDone
from ee.core.store import SessionStore


def make(workspace, script, window=None, persist=False, compact=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    if window:
        cfg.data["models"]["deepseek-flash"]["context_window"] = window
    if compact:
        cfg.data["compact"].update(compact)
    fake = FakeClient(script)
    return Session(cfg, cwd=workspace, client=fake, persist=persist), fake


def summary_chunks(s="## 用户目标\n修 bug\n## 下一步\n跑测试"):
    return text_chunks(s, 20) + finish("stop", prompt=500, completion=50)


def done(ev):
    return [e for e in ev if isinstance(e, TurnDone)][-1]


def test_manual_compact_rebuilds_history(ee_home, workspace):
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    s, fake = make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("读完了") + finish("stop"),
        summary_chunks(),
        text_chunks("继续") + finish("stop"),
    ])
    run(collect(s, "第一件事"))
    events = []
    res = run(s.compact(events.append))
    assert res["ok"] and "已压缩上下文" in res["message"]
    comp = [e for e in events if isinstance(e, Compacted)]
    assert len(comp) == 1 and comp[0].reason == "manual" and isinstance(comp[0], Notice)
    # 压缩请求 = 同一前缀 + 原历史 + 压缩指令（复用缓存）；工具 schema 也不变
    req = fake.calls[2]
    assert req["messages"][0] == fake.calls[1]["messages"][0]
    prev = fake.calls[1]["messages"]
    assert req["messages"][:len(prev)] == prev            # 原请求是压缩请求的字节前缀
    assert req["messages"][len(prev)]["role"] == "assistant"
    assert "上下文检查点压缩" in req["messages"][-1]["content"]
    assert req["tools"] == fake.calls[1]["tools"]
    # 新历史：最近用户原话（去掉附录） + 摘要（带全量环境附录）
    assert [m["role"] for m in s.history] == ["user", "user"]
    assert s.history[0]["content"] == "第一件事"
    assert s.history[1]["_kind"] == "summary"
    assert "<summary>" in s.history[1]["content"] and "修 bug" in s.history[1]["content"]
    assert "<context-update" in s.history[1]["content"] and 'mode="delta"' not in s.history[1]["content"]
    assert not any(m["role"] == "tool" for m in s.history)
    # 下一回合：system 不变，历史接在新历史后面，只追加
    run(collect(s, "第二件事"))
    nxt = fake.calls[3]["messages"]
    assert nxt[0] == fake.calls[1]["messages"][0]
    assert [m["role"] for m in nxt] == ["system", "user", "user", "user"]
    assert "_kind" not in json.dumps(nxt)
    assert nxt[-1]["content"].startswith("第二件事")


def test_compact_clears_read_dedup(ee_home, workspace):
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    s, fake = make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
        summary_chunks(),
        tool_chunks(0, "c2", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
    ])
    run(collect(s, "读"))
    run(s.compact())
    run(collect(s, "再读"))
    out = [m for m in s.history if m["role"] == "tool"][-1]["content"]
    assert "hello" in out and "与上次读取相同" not in out


def test_auto_compact_at_turn_start(ee_home, workspace):
    s, fake = make(workspace, [
        text_chunks("答一") + finish("stop", prompt=850, completion=10),
        summary_chunks(),
        text_chunks("答二") + finish("stop", prompt=300, completion=10),
    ], window=1000)
    run(collect(s, "问一"))
    ev = run(collect(s, "问二"))
    comp = [e for e in ev if isinstance(e, Compacted)]
    assert len(comp) == 1 and comp[0].reason == "auto" and comp[0].tokens_before == 860
    assert len(fake.calls) == 3
    roles = [m["role"] for m in fake.calls[2]["messages"]]
    assert roles == ["system", "user", "user", "user"]
    assert fake.calls[2]["messages"][2]["content"].startswith("[上下文已压缩]")
    assert fake.calls[2]["messages"][3]["content"].startswith("问二")
    assert done(ev).reason == "completed"


def test_auto_compact_mid_turn(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls", prompt=900),
        summary_chunks(),
        text_chunks("完成") + finish("stop", prompt=200),
    ], window=1000)
    ev = run(collect(s, "做事"))
    assert done(ev).reason == "completed"
    assert any(isinstance(e, Compacted) for e in ev)
    # 压缩后历史仍然配对完整：没有孤立的 tool 消息
    assert [m["role"] for m in s.history] == ["user", "user", "assistant"]
    last_req = fake.calls[2]["messages"]
    assert [m["role"] for m in last_req] == ["system", "user", "user"]
    assert last_req[1]["content"] == "做事"


def test_no_repeated_compaction_without_new_usage(ee_home, workspace):
    s, fake = make(workspace, [
        text_chunks("答一") + finish("stop", prompt=900),
        summary_chunks(),
        text_chunks("答二") + finish("stop", prompt=100),
        text_chunks("答三") + finish("stop", prompt=100),
    ], window=1000)
    run(collect(s, "一"))
    run(collect(s, "二"))
    ev = run(collect(s, "三"))
    assert not any(isinstance(e, Compacted) for e in ev)
    assert len(fake.calls) == 4


def test_compact_fallback_to_side_request(ee_home, workspace):
    s, fake = make(workspace, [
        text_chunks("答一") + finish("stop"),
        FakeError(400, "context length exceeded"),
        summary_chunks("兜底摘要"),
    ])
    run(collect(s, "一"))
    events = []
    res = run(s.compact(events.append))
    assert res["ok"]
    side = fake.calls[2]
    assert [m["role"] for m in side["messages"]] == ["system", "user"]
    assert "【用户】一" in side["messages"][1]["content"]
    assert "兜底摘要" in s.history[-1]["content"]
    assert any(isinstance(e, Notice) and "失败" in e.text for e in events)


def test_compact_failure_keeps_history(ee_home, workspace):
    s, fake = make(workspace, [
        text_chunks("答一") + finish("stop"),
        FakeError(400, "bad"),
        FakeError(400, "bad"),
    ])
    run(collect(s, "一"))
    before = [dict(m) for m in s.history]
    res = run(s.compact())
    assert not res["ok"] and s.history == before


def test_compact_empty_and_persisted(ee_home, workspace):
    s, fake = make(workspace, [text_chunks("答一") + finish("stop"), summary_chunks()], persist=True)
    assert not run(s.compact())["ok"]
    run(collect(s, "一"))
    run(s.compact())
    assert SessionStore.load_messages(s.store.path) == s.history


def test_recent_user_messages_budget():
    hist = [{"role": "user", "content": "旧" * 50}, {"role": "assistant", "content": "a"},
            {"role": "user", "content": "[系统提示] x", "_kind": "note"},
            {"role": "user", "content": "新消息\n\n<context-update seq=\"3\"/>"}]
    assert C.recent_user_messages(hist, 3, 10000) == ["旧" * 50, "新消息"]
    assert C.recent_user_messages(hist, 1, 10000) == ["新消息"]
    clipped = C.recent_user_messages([{"role": "user", "content": "长" * 5000}], 3, 300)
    assert len(clipped) == 1 and "省略" in clipped[0] and len(clipped[0]) < 5000
