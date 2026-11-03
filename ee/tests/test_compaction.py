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


def test_needs_compaction_absolute_cap_for_huge_window(ee_home, workspace):
    """1M 窗口按 80% 要等 80 万；max_tokens 让它 14 万就压。"""
    s, _ = make(workspace, [], window=1_000_000, compact={"max_tokens": 140_000})
    s._fresh_usage = True
    s.stats.last_prompt, s.stats.last_completion = 139_000, 500
    assert not s.needs_compaction()
    s.stats.last_prompt = 140_000
    assert s.needs_compaction()


def test_needs_compaction_percent_without_cap(ee_home, workspace):
    """没配 max_tokens 时仍按窗口百分比（小窗口模型的原有行为）。"""
    s, _ = make(workspace, [], window=100_000, compact={"max_tokens": 0})
    s._fresh_usage = True
    s.stats.last_prompt, s.stats.last_completion = 70_000, 0
    assert not s.needs_compaction()
    s.stats.last_prompt = 80_000
    assert s.needs_compaction()


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
    # 新历史 = AGENTS + 摘要（带全量环境附录） + 保留的最近往来（keep_tools 默认 16）
    roles = [m["role"] for m in s.history]
    kinds = [m.get("_kind") for m in s.history]
    si = kinds.index("summary")
    assert "<summary>" in s.history[si]["content"] and "修 bug" in s.history[si]["content"]
    assert "<context-update" in s.history[si]["content"] and 'mode="delta"' not in s.history[si]["content"]
    # 保留段排在摘要之后，且刚才读到的文件内容还在（这是「不变傻」的关键）
    assert si < len(s.history) - 1
    assert "hello" in "".join(str(m.get("content")) for m in s.history[si:])
    assert s.history[si + 1]["content"].startswith("第一件事")    # 本回合的用户原话
    assert roles[si + 2:] == ["assistant", "tool", "assistant"]   # 工具往来的原始顺序
    # 下一回合：system 不变，历史接在新历史后面，只追加
    run(collect(s, "第二件事"))
    nxt = fake.calls[3]["messages"]
    assert nxt[0] == fake.calls[1]["messages"][0]
    # 只追加：压缩后的历史原样打头，末尾多一条本回合的用户消息
    assert [m["role"] for m in nxt] == ["system"] + roles + ["user"]
    assert nxt[-1]["content"].startswith("第二件事")
    assert "_kind" not in json.dumps(nxt)          # 内部标记不发给模型
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
    msgs = fake.calls[2]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["content"].startswith("问二")
    summary = [m for m in msgs if str(m.get("content", "")).startswith("[上下文已压缩]")]
    assert len(summary) == 1
    assert [m["role"] for m in msgs[3:-1]] == ["assistant"]   # 保留段：上一回合的回答
    assert done(ev).reason == "completed"


def test_no_compact_mid_turn(ee_home, workspace):
    """回合中途达到水位也不压：压缩会把本回合已发出的历史整段作废，代价远大于推迟。

    实测：回合内压缩后下一次请求整段未命中 3k–7k token，是正常追加（~500）的 6–12 倍。
    所以推迟到下一回合开始——那时本来就要发一条带新消息的请求。
    """
    (workspace / "a.txt").write_text("x" + chr(10), encoding="utf-8")
    s, fake = make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls", prompt=900),
        text_chunks("完成") + finish("stop", prompt=200),
    ], window=1000)
    ev = run(collect(s, "做事"))
    assert done(ev).reason == "completed"
    assert not any(isinstance(e, Compacted) for e in ev)
    assert len(fake.calls) == 2


def test_compact_when_urgent_mid_turn(ee_home, workspace):
    """逼近窗口上限（>0.95×window）时回合内也必须压，否则下一次请求直接 400。"""
    (workspace / "a.txt").write_text("x" + chr(10), encoding="utf-8")
    s, fake = make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls", prompt=980),
        summary_chunks(),
        text_chunks("完成") + finish("stop", prompt=200),
    ], window=1000)
    ev = run(collect(s, "做事"))
    assert done(ev).reason == "completed"
    assert any(isinstance(e, Compacted) for e in ev)


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
    assert any("兜底摘要" in str(m.get("content")) for m in s.history)
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

def test_retain_budget_follows_watermark(ee_home, workspace):
    """压缩后历史必须明显低于水位，否则压完还超阈 -> 每回合都压。

    实测（2026-11-03，隔离 EE_HOME + 真 API，水位 40k）：保留段固定 64k 时，压缩后
    34,456 token（摘要 + AGENTS + 用户原话 16,257 + 保留段 16,497）仍超 40k 水位，
    24 回合压 9 次、命中率 53%。预算挂到水位上之后压缩次数降到 4、命中率 79%。
    """
    s, _ = make(workspace, [], window=200_000, compact={"max_tokens": 40_000})
    assert s._compact_limit() == 40_000
    # 0.4 × 40k = 16k，远低于 64k 常量
    assert s._retain_budget() == 16_000
    # 水位抬到默认 140k：0.4 × 140k = 56k 略低于 64k 常量，收窄幅度很小
    s.config.data["compact"]["max_tokens"] = 140_000
    assert s._compact_limit() == 140_000
    assert s._retain_budget() == 56_000
    # 水位再大，64k 常量成为上界
    s.config.data["compact"]["max_tokens"] = 400_000
    assert s._retain_budget() == C.RETENTION_TOKENS


def test_user_quote_budget_follows_watermark(ee_home, workspace):
    """较早用户原话也要挂到水位上：水位 40k 时 20k 原话就占了一半。"""
    s, _ = make(workspace, [], window=200_000, compact={"max_tokens": 40_000})
    assert s._user_quote_budget(20_000) == 6_000
    s.config.data["compact"]["max_tokens"] = 140_000
    assert s._user_quote_budget(20_000) == 20_000


def test_retain_budget_zero_when_auto_compact_off(ee_home, workspace):
    """关掉自动压缩时水位为 0，预算退回常量，不能算成 0（否则压缩后一条往来都不留）。"""
    s, _ = make(workspace, [], window=200_000, compact={"auto": False, "max_tokens": 40_000})
    assert s._compact_limit() == 0
    assert s._retain_budget() == C.RETENTION_TOKENS
    assert s._user_quote_budget(20_000) == 20_000


def test_compact_result_stays_below_watermark(ee_home, workspace):
    """压缩后重建的历史必须真的低于水位，否则下一回合立刻又压。"""
    from ee.core import compaction as C
    hist = []
    for i in range(40):
        hist.append({"role": "user", "content": f"消息 {i} " + "内容" * 400})
        hist.append({"role": "assistant", "content": "好的" + "回复" * 400})
    before = C.estimate_messages(hist)
    summary = "## 用户目标" + "摘要" * 200
    new = C.build_history(hist, summary, "", 3, 6_000, keep_tools=0, keep_tokens=16_000)
    after = C.estimate_messages(new)
    assert after < 40_000, f"压缩后 {after:,} 仍超 40k 水位（压缩前 {before:,}）"
