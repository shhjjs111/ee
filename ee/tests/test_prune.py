"""D5-10 修剪旧工具输出 + D5-11 零断点清推理：触发、保护范围、缓存稳定、恢复会话。"""
import json

from conftest import (FakeClient, collect, finish, make_session, reasoning_chunk, run, text_chunks,
                      tool_chunks)

from ee.core import prune as P
from ee.core.store import SessionStore

BIG = "".join(f"line {i:04d} " + "x" * 60 + "\n" for i in range(150))     # 约 1 万字符 ≈ 2500 tokens


def make(workspace, script, persist=False, window=10000, prune=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    cfg.data["models"]["deepseek-flash"]["context_window"] = window
    # 消息级修剪默认已关（max_tokens=0，理由见 prune.DEFAULTS）：这组测试要验证修剪本身，
    # 所以把绝对水位抬得很高（1e9），让它退化成 threshold × 窗口 的百分比判定。
    cfg.data["prune"] = {"enabled": True, "threshold": 0.5, "keep_turns": 1, "keep_tokens": 0,
                         "min_tokens": 500, "max_tokens": 10**9,
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


def test_reasoning_strip_cursor_survives_reload(ee_home, workspace):
    """游标存盘：重开会话后仍然只发空推理，不会退回全带（那会把首包再翻一倍）。"""
    path = ee_home / "sessions" / "t1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = [
        {"kind": "meta", "meta": {"cwd": str(workspace)}},
        {"kind": "message", "message": {"role": "user", "content": "一"}},
        {"kind": "message", "message": {"role": "assistant", "content": "", "reasoning": "r1"}},
        {"kind": "message", "message": {"role": "user", "content": "二"}},
        {"kind": "message", "message": {"role": "assistant", "content": "", "reasoning": "r2"}},
        {"kind": "reasoning_strip", "to": 2},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    d = SessionStore.load(path)
    assert d["meta"]["reasoning_strip_to"] == 2
    # 历史重建（压缩 / 回退 / 截断）后旧游标必须作废，否则会指向不存在的消息
    for kind in ({"kind": "reset"}, {"kind": "rewind", "to": 1}, {"kind": "truncate", "from_seq": 1}):
        with_extra = recs + [kind]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in with_extra) + "\n", encoding="utf-8")
        assert "reasoning_strip_to" not in SessionStore.load(path)["meta"], kind["kind"]


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


def test_due_absolute_ceiling_for_huge_window():
    """1M 窗口的模型：threshold × 窗口 = 60 万永远到不了，max_tokens 按绝对值封顶。"""
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=1_000_000), stats=st, compactions=0)
    st.requests, st.last_prompt = 3, 190_000
    assert not P.due(s, P.settings({"prune": {"enabled": True, "threshold": 0.6, "max_tokens": 0}}))
    cfg = P.settings({"prune": {"enabled": True, "threshold": 0.6, "max_tokens": 150_000}})
    assert P.due(s, cfg)
    st.last_prompt = 140_000                     # 低于绝对上限就不动
    s._prune_mark = None
    assert not P.due(s, cfg)


def test_due_absolute_ceiling_never_raises_percent_threshold():
    """小窗口时 max_tokens 比百分比高就不起作用：仍按百分比。"""
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=10_000), stats=st, compactions=0)
    st.requests, st.last_prompt = 3, 6000
    cfg = P.settings({"prune": {"enabled": True, "threshold": 0.5, "max_tokens": 500_000}})
    assert P.due(s, cfg)                          # 60% ≥ 50% × 1 万
    st.last_prompt = 4000
    s._prune_mark = None
    assert not P.due(s, cfg)                      # 40% < 50%：绝对上限没有把它放低


def test_default_config_does_not_prune_messages():
    """默认配置：消息级修剪已关（max_tokens=0）。

    全量重放证据（5 个真实会话，成本按官方价、首包按 1317 + 0.0072×prompt）：
    现状（prune@100k + compact@140k）¥13.37 / 2,919s，只压缩不修剪 ¥11.96 / 2,856s。
    机制：修剪改历史中段 → 其后全部内容重算，一次未命中≈整个 prompt，而只省下修剪量本身。
    """
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=1_000_000), stats=st, compactions=0)
    cfg = P.settings({})
    assert cfg["max_tokens"] == 0
    st.requests, st.last_prompt = 3, 10_000_000     # 就算上下文大到离谱也不修剪
    assert not P.due(s, cfg)


def test_due_honours_explicit_waterline():
    """显式配了绝对水位时仍按水位修剪（功能本体没被删）。"""
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=1_000_000), stats=st, compactions=0)
    cfg = P.settings({"prune": {"max_tokens": 100_000}})
    st.requests, st.last_prompt = 3, 99_000
    assert not P.due(s, cfg)
    st.last_prompt = 100_000
    assert P.due(s, cfg)


def test_prune_waterline_stays_below_compaction():
    """两边都配了绝对水位时，修剪线不得高于压缩线（否则压缩永远轮不到）。"""
    cfg = P.settings({"prune": {"max_tokens": 200_000}, "compact": {"max_tokens": 140_000}})
    assert cfg["max_tokens"] == 140_000


def test_wire_messages_strip_old_reasoning():
    """清旧推理在发送时做：历史不动，游标只影响发出去的字节。"""
    from ee.core.providers import transforms as T
    from ee.core.providers.profiles import profile_for
    prof = profile_for("deepseek-flash")
    hist = [{"role": "assistant", "content": f"c{i}", "reasoning": f"r{i}"} for i in range(3)]
    assert [w["reasoning_content"] for w in T.wire_messages(hist, prof)] == ["r0", "r1", "r2"]
    # 游标 1：前两条置空，最后一条保留；历史里的 reasoning 一字不动
    assert [w["reasoning_content"] for w in T.wire_messages(hist, prof, 1)] == ["", "", "r2"]
    assert [m["reasoning"] for m in hist] == ["r0", "r1", "r2"]


def test_maybe_prune_strips_old_reasoning(ee_home, workspace):
    """sync_reasoning 与水位无关：每次请求前都把游标推到最新，第一条 assistant 就清空。"""
    from types import SimpleNamespace
    from ee.config import load_config
    from ee.core.context import UsageStats

    history = []
    for i in range(5):
        history.append({"role": "user", "content": f"第{i}件事"})
        # 每个用户回合里两次工具调用：同一个用户问题内的思维链必须完整回传（否则 400）
        for j in range(2):
            history.append({"role": "assistant", "content": "", "reasoning": "推" * 400,
                            "tool_calls": [{"id": f"c{i}_{j}", "type": "function",
                                            "function": {"name": "shell", "arguments": "{}"}}]})
            history.append({"role": "tool", "tool_call_id": f"c{i}_{j}", "content": "x" * 200})
    cfg = load_config(workspace)
    st = UsageStats()
    st.requests, st.last_prompt = 10, 120_000
    store_appends = []
    s = SimpleNamespace(config=SimpleNamespace(data=cfg.data), history=history, stats=st,
                        profile=SimpleNamespace(context_window=1_000_000),
                        spec=SimpleNamespace(profile=SimpleNamespace(reasoning_passback="all",
                                                                     empty_assistant_content="null")),
                        compactions=0, store=SimpleNamespace(append=lambda k, p: store_appends.append((k, p))),
                        state=SimpleNamespace(files={}),
                        _prune_mark=None, _prune_floor=None, _prune_comp=0, _reasoning_strip_to=-1)
    saved = P.sync_reasoning(s, P.settings(cfg.data))
    assert saved > 0
    assert s._reasoning_strip_to == 23
    # 默认 keep_reasoning_turns=0：最后一条 assistant（下标 23）也在游标之内
    assert P.settings(cfg.data)["keep_reasoning_turns"] == 0
    # 换个配置保留最近 2 个用户回合：游标只到下标 13，更早的才清
    s2 = SimpleNamespace(**{**vars(s), "_reasoning_strip_to": -1})
    s2.store = SimpleNamespace(append=lambda k, p: None)
    keep2 = P.settings({**cfg.data, "prune": {**cfg.data["prune"], "keep_reasoning_turns": 2}})
    P.sync_reasoning(s2, keep2)
    assert s2._reasoning_strip_to == 13 and history[15]["content"] == "第3件事"
    assert any(k == "reasoning_strip" and p["to"] == 23 for k, p in store_appends)
    # 历史一律原样，裁剪只发生在发送时
    assert all(m.get("reasoning") == "推" * 400 for m in history if m.get("role") == "assistant")
    from ee.core.providers import transforms as T
    wired = T.wire_messages(history, s.spec.profile, s._reasoning_strip_to)
    assert all(w["reasoning_content"] == "" for w, m in zip(wired, history) if m.get("role") == "assistant")
    # 幂等：再调一次不再推进、也不再写记录
    assert P.sync_reasoning(s, P.settings(cfg.data)) == 0
    assert len(store_appends) == 1


def test_every_request_is_prefix_of_next(ee_home, workspace):
    """零断点的硬保证：每次真实请求的消息列表，都是下一次请求的逐字节前缀。

    前缀缓存只在「已发过的字节不再变」时命中。这个用例跑三回合、每回合多步带推理，
    把每次实际发给模型的消息序列化为 JSON 逐条比对：一旦某条消息发生过变化（清推理
    晚一步、占位不安全、tool_call_id 随机化…），就会在两次请求中间出现第一个不等的位置。
    """
    import json as _json
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    (workspace / "b.txt").write_text("y\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        [reasoning_chunk("想一")] + tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        [reasoning_chunk("想二")] + tool_chunks(0, "c2", "read", {"path": "b.txt"}) + finish("tool_calls"),
        [reasoning_chunk("想三")] + text_chunks("好了") + finish("stop"),
        [reasoning_chunk("想四")] + tool_chunks(0, "c3", "read", {"path": "a.txt"}) + finish("tool_calls"),
        [reasoning_chunk("想五")] + text_chunks("行") + finish("stop"),
    ])
    run(collect(s, "第一个问题"))
    run(collect(s, "第二个问题"))
    assert any(m.get("role") == "assistant" for c in fake.calls for m in c["messages"])
    # 推理必须真的被清了（否则「从不清理」也满足「字节不变」，测试就失去意义）
    for c in fake.calls:
        for m in c["messages"]:
            if m.get("role") == "assistant":
                assert m.get("reasoning_content") == "", "旧推理又被带上去了：prompt 白多一截"
    reqs = [[_json.dumps(m, ensure_ascii=False, sort_keys=True) for m in c["messages"]] for c in fake.calls]
    assert len(reqs) >= 5
    for prev, cur in zip(reqs, reqs[1:]):
        assert cur[:len(prev)] == prev, "上一次请求的消息被改写了：前缀缓存从改动处起全部失效"


def test_reasoning_zero_break_after_each_step(ee_home, workspace):
    """零断点：同一条 assistant 从它第一次被发送起，字节就再不变化（断点后不再重算）。"""
    from ee.core.providers import transforms as T
    from ee.core.providers.profiles import profile_for
    prof = profile_for("deepseek-flash")
    hist = []
    seen = {}
    for i in range(6):
        hist.append({"role": "user", "content": f"问{i}"})
        hist.append({"role": "assistant", "content": f"答{i}", "reasoning": "想" * 100})
        # 模拟每一步发送前调用 sync_reasoning（游标推到最新）
        cur = P.reasoning_target(hist, 0)
        for j, w in enumerate(T.wire_messages(hist, prof, cur)):
            if w.get("role") != "assistant":
                continue
            key = (i, j)
            if key in seen:
                assert seen[key] == w["reasoning_content"], "同一条消息的字节变了（缓存断点）"
            seen[key] = w["reasoning_content"]
    assert set(seen.values()) == {""}          # 全都是空串：首次发送即固定


def test_due_hysteresis():
    from types import SimpleNamespace
    from ee.core.context import UsageStats
    st = UsageStats()
    s = SimpleNamespace(profile=SimpleNamespace(context_window=10000), stats=st, compactions=0)
    cfg = P.settings({"prune": {"enabled": True, "threshold": 0.5, "max_tokens": 10**9, "min_tokens": 1000}})
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
