"""D5-10 修剪旧工具输出：触发、保护范围、缓存稳定、恢复会话。"""
import json

from conftest import FakeClient, collect, finish, run, text_chunks, tool_chunks

from ee.core import prune as P
from ee.core.store import SessionStore

BIG = "".join(f"line {i:04d} " + "x" * 60 + "\n" for i in range(150))     # 约 1 万字符 ≈ 2500 tokens


def make(workspace, script, persist=False, window=10000, prune=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    cfg.data["models"]["deepseek-flash"]["context_window"] = window
    cfg.data["prune"] = {"threshold": 0.5, "keep_turns": 1, "keep_tokens": 0, "min_tokens": 500,
                         "min_output_tokens": 100, **(prune or {})}
    fake = FakeClient(script)
    return Session(cfg, cwd=workspace, client=fake, persist=persist), fake


def tool_msg(s, cid):
    return next(m for m in s.history if m.get("role") == "tool" and m.get("tool_call_id") == cid)


def files(ws):
    (ws / "big.txt").write_text(BIG, encoding="utf-8")
    (ws / "big2.txt").write_text(BIG.replace("x", "y"), encoding="utf-8")
    (ws / "small.txt").write_text("hi\n", encoding="utf-8")


def three_turns(extra_t2_text="第二回合读完了", t3=None, p=6000):
    return [
        tool_chunks(0, "c1", "read", {"path": "big.txt"}) + tool_chunks(1, "c1s", "read", {"path": "small.txt"})
        + finish("tool_calls", prompt=1000),
        text_chunks("第一回合读完了") + finish("stop", prompt=4000),
        tool_chunks(0, "c2", "read", {"path": "big2.txt"}) + finish("tool_calls", prompt=4500),
        text_chunks(extra_t2_text) + finish("stop", prompt=p),
    ] + (t3 or [text_chunks("好") + finish("stop", prompt=3000)])


def test_prune_triggers_and_protects(ee_home, workspace):
    files(workspace)
    s, fake = make(workspace, three_turns(t3=[
        tool_chunks(0, "c3", "read", {"path": "small.txt"}) + finish("tool_calls", prompt=3000),
        text_chunks("好") + finish("stop", prompt=3100)]))
    run(collect(s, "读 big"))
    run(collect(s, "读 big2"))
    assert not any(m.get("_pruned") for m in s.history)          # 还没到第三回合，不修剪
    ev = run(collect(s, "第三件事"))
    pruned = [e for e in ev if isinstance(e, P.Pruned)]
    assert len(pruned) == 1 and "已修剪 1 个" in pruned[0].text
    m1 = tool_msg(s, "c1")
    assert m1["content"].startswith(P.MARK) and "read(path=big.txt)" in m1["content"]
    assert "重新调用" in m1["content"] and m1["_pruned"]["chars"] > 9000
    assert tool_msg(s, "c1s")["content"].endswith("hi")            # 太小的不修剪
    assert "line 0001" in tool_msg(s, "c2")["content"]             # 最近回合保护
    # 发给模型的请求里是占位，没有 _ 字段
    req = fake.calls[4]["messages"]
    assert not any("line 0001 xxx" in (m.get("content") or "") for m in req)
    assert "_pruned" not in json.dumps(req)
    # 用量统计里有修剪节省量
    assert s.stats.totals().get("pruned", 0) > 1500


def test_prefix_stable_after_prune(ee_home, workspace):
    files(workspace)
    s, fake = make(workspace, three_turns(t3=[
        tool_chunks(0, "c3", "read", {"path": "small.txt"}) + finish("tool_calls", prompt=7000),
        tool_chunks(0, "c4", "read", {"path": "small.txt", "offset": 1}) + finish("tool_calls", prompt=7100),
        text_chunks("好") + finish("stop", prompt=7200)]))
    run(collect(s, "读 big"))
    run(collect(s, "读 big2"))
    before = fake.calls[3]["messages"]
    run(collect(s, "第三件事"))
    a, b, c = (fake.calls[i]["messages"] for i in (4, 5, 6))
    # 修剪点之前的消息与修剪前的请求逐字节相同（缓存仍命中那一段）
    first = next(i for i, m in enumerate(a) if (m.get("content") or "").startswith(P.MARK))
    assert a[:first] == before[:first]
    # 修剪之后只追加：同一修剪点后每次请求都是下一次请求的字节前缀；占用仍高于阈值也不会第二次修剪
    assert b[:len(a)] == a and c[:len(b)] == b
    assert sum(1 for m in s.history if m.get("_pruned")) == 1


def test_referenced_read_is_kept(ee_home, workspace):
    files(workspace)
    s, fake = make(workspace, three_turns(extra_t2_text="接下来要改 big.txt 里的第 3 行"))
    run(collect(s, "读 big"))
    run(collect(s, "读 big2"))
    run(collect(s, "第三件事"))
    assert not tool_msg(s, "c1").get("_pruned")       # 最近回合提到了 big.txt：它最后一次 read 仍被引用


def test_below_threshold_or_disabled_does_nothing(ee_home, workspace):
    files(workspace)
    s, _ = make(workspace, three_turns(p=4000))          # 4000 < 0.5 × 10000
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "第三件事"))
    assert not any(m.get("_pruned") for m in s.history)
    s, _ = make(workspace, three_turns(), prune={"enabled": False})
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "第三件事"))
    assert not any(m.get("_pruned") for m in s.history)
    s, _ = make(workspace, three_turns(), prune={"min_tokens": 100000})   # 一批太少不值得让缓存失效
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "第三件事"))
    assert not any(m.get("_pruned") for m in s.history)


def test_threshold_always_below_compaction():
    cfg = P.settings({"prune": {"threshold": 0.95}, "compact": {"threshold": 0.8}})
    assert cfg["threshold"] < 0.8


def test_reread_after_prune_returns_full_text(ee_home, workspace):
    files(workspace)
    s, _ = make(workspace, three_turns(t3=[
        tool_chunks(0, "c3", "read", {"path": "big.txt"}) + finish("tool_calls", prompt=3000),
        text_chunks("好") + finish("stop", prompt=3100)]))
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "再读 big"))
    out = tool_msg(s, "c3")["content"]
    assert "line 0001" in out and "与上次读取相同" not in out


def test_resume_replays_prune_and_keeps_original(ee_home, workspace):
    files(workspace)
    s, _ = make(workspace, three_turns(), persist=True)
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "第三件事"))
    path = s.store.path
    msgs = SessionStore.load_messages(path)
    assert msgs == s.history                              # 恢复后与内存里逐条相同（含占位）
    assert SessionStore.load(path)["messages"] == s.history
    raw = path.read_text(encoding="utf-8")
    assert "line 0001 xxx" in raw                         # 原文仍在 JSONL 里（界面可查看）
    # 恢复的会话继续聊：历史前缀与修剪后一致，不会再修剪同一条
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    fake2 = FakeClient([text_chunks("继续") + finish("stop", prompt=100)])
    s2 = Session(cfg, cwd=workspace, client=fake2, persist=False)
    s2.history = list(msgs)
    run(collect(s2, "接着"))
    sent = fake2.calls[0]["messages"]
    assert any((m.get("content") or "").startswith(P.MARK) for m in sent)


def test_prune_then_rewind_and_compact(ee_home, workspace):
    files(workspace)
    s, _ = make(workspace, three_turns() + [
        text_chunks("## 目标\n修") + finish("stop", prompt=500)], persist=True)
    run(collect(s, "读 big")); run(collect(s, "读 big2")); run(collect(s, "第三件事"))
    # 回退最后一个回合：修剪记录在被删消息之前，重放仍正确
    s.rewind_last_turn()
    assert SessionStore.load_messages(s.store.path) == s.history
    assert tool_msg(s, "c1").get("_pruned")
    # 压缩后历史里不再有工具消息，修剪不越过摘要
    res = run(s.compact())
    assert res["ok"]
    assert SessionStore.load_messages(s.store.path) == s.history
    assert P.plan(s.history, P.settings({"prune": {"keep_turns": 0, "keep_tokens": 0, "min_tokens": 1}})) == []


def test_skip_non_prunable_tools(ee_home, workspace):
    hist = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "task", "arguments": "{}"}},
            {"id": "t2", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls -R"}'}},
            {"id": "t3", "type": "function", "function": {"name": "mcp__x__y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "报告" * 3000},
        {"role": "tool", "tool_call_id": "t2", "content": "f" * 20000},
        {"role": "tool", "tool_call_id": "t3", "content": "m" * 20000},
        {"role": "user", "content": "b"},
    ]
    items = P.plan(hist, P.settings({"prune": {"keep_turns": 1, "keep_tokens": 0, "min_tokens": 100}}))
    assert [x["id"] for x in items] == ["t2", "t3"]
    assert "shell(command=ls -R)" in items[0]["content"]
    # keep_tokens 保护最近的工具输出（从新到旧数）
    items = P.plan(hist, P.settings({"prune": {"keep_turns": 1, "keep_tokens": 5000, "min_tokens": 100}}))
    assert [x["id"] for x in items] == ["t2"]


def test_due_hysteresis():
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=10000), stats=st, compactions=0)
    cfg = P.settings({"prune": {"threshold": 0.5, "min_tokens": 1000}})
    st.requests, st.last_prompt = 3, 6000
    assert P.due(s, cfg)
    s._prune_mark, s._prune_floor, s._prune_comp = 3, None, 0       # 刚修剪过
    assert not P.due(s, cfg)                     # 还没有新用量
    st.requests, st.last_prompt = 4, 5500
    assert not P.due(s, cfg) and s._prune_floor == 5500   # 记下修剪后的实际大小
    st.requests, st.last_prompt = 5, 6000
    assert not P.due(s, cfg)                     # 只涨了 500 < min_tokens
    st.requests, st.last_prompt = 6, 6600
    assert P.due(s, cfg)                         # 再涨够一批：可以再修剪
    s.compactions = 1
    st.last_prompt = 5100
    assert P.due(s, cfg) and s._prune_mark is None   # 压缩后重新开始
