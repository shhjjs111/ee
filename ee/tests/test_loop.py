import asyncio
import time

from conftest import collect, finish, make_session, reasoning_chunk, run, text_chunks, tool_chunks

from ee.core.events import (ApprovalDecision, ApprovalRequest, Notice, PlanUpdate, TextDelta, ToolEnd, ToolStart,
                            TurnDone, Usage)


def done(events):
    return [e for e in events if isinstance(e, TurnDone)][-1]


def tool_msgs(s):
    return [m for m in s.history if m["role"] == "tool"]


def test_text_only_ends_turn(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("你好，有什么可以帮你？") + finish("stop")])
    ev = run(collect(s, "hi"))
    assert done(ev).reason == "completed"
    assert "".join(e.text for e in ev if isinstance(e, TextDelta)) == "你好，有什么可以帮你？"
    assert len(fake.calls) == 1
    assert [m["role"] for m in s.history] == ["user", "assistant"]


def test_tool_then_answer(ee_home, workspace):
    (workspace / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("文件有两行。") + finish("stop"),
    ])
    ev = run(collect(s, "读 a.txt"))
    assert done(ev).reason == "completed"
    assert len(fake.calls) == 2
    t = tool_msgs(s)
    assert len(t) == 1 and t[0]["tool_call_id"] == "c1" and "hello" in t[0]["content"]
    # 第二次请求里带上了 assistant(tool_calls) + tool 输出
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


def test_finish_reason_stop_but_tool_calls_present_still_runs(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("stop"),
        text_chunks("ok") + finish("stop"),
    ])
    ev = run(collect(s, "go"))
    assert len(fake.calls) == 2 and done(ev).reason == "completed"


def test_max_steps(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    script = [tool_chunks(0, f"c{i}", "read", {"path": "a.txt", "offset": 1, "limit": i + 1}) + finish("tool_calls")
              for i in range(3)]
    s, fake = make_session(workspace, script, max_steps=3)
    ev = run(collect(s, "loop"))
    assert done(ev).reason == "max_steps"
    assert len(fake.calls) == 3
    # 最后一步前追加了“只总结”的提示；最后一步的调用没执行但也有输出
    assert any("最大步数" in (m.get("content") or "") for m in fake.calls[2]["messages"] if m["role"] == "user")
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(m["tool_call_id"] for m in tool_msgs(s))


def test_errors_fed_back(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "nope_tool", {}) + tool_chunks(1, "c2", "read", "{bad json") +
        tool_chunks(2, "c3", "read", {"path": "missing.txt"}) + finish("tool_calls"),
        text_chunks("我改一下") + finish("stop"),
    ])
    ev = run(collect(s, "x"))
    out = {m["tool_call_id"]: m["content"] for m in tool_msgs(s)}
    assert "未知工具" in out["c1"]
    assert "JSON" in out["c2"]
    assert "不存在" in out["c3"]
    ends = [e for e in ev if isinstance(e, ToolEnd)]
    assert all(not e.ok for e in ends)
    assert done(ev).reason == "completed"


def test_parallel_readonly_tools(ee_home, workspace, monkeypatch):
    from ee.tools.read import ReadTool
    active = {"now": 0, "max": 0}
    orig = ReadTool.run

    async def slow_run(self, args, ctx):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.2)
        active["now"] -= 1
        return await orig(self, args, ctx)

    monkeypatch.setattr(ReadTool, "run", slow_run)
    for n in "abc":
        (workspace / f"{n}.txt").write_text(n, encoding="utf-8")
    calls = []
    for i, n in enumerate("abc"):
        calls += tool_chunks(i, f"c{i}", "read", {"path": f"{n}.txt"})
    s, fake = make_session(workspace, [calls + finish("tool_calls"), text_chunks("ok") + finish("stop")])
    t0 = time.monotonic()
    run(collect(s, "读三个"))
    assert active["max"] == 3
    assert time.monotonic() - t0 < 0.55
    # 输出顺序与调用顺序一致
    assert [m["tool_call_id"] for m in tool_msgs(s)] == ["c0", "c1", "c2"]


def test_write_tools_sequential(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "w1", "write", {"path": "n.txt", "content": "1"}) +
        tool_chunks(1, "r1", "read", {"path": "n.txt"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
    ])
    run(collect(s, "x"))
    out = {m["tool_call_id"]: m["content"] for m in tool_msgs(s)}
    assert "新建" in out["w1"] and "1" in out["r1"]


def test_reasoning_passback_deepseek(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        [reasoning_chunk("先读文件")] + tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        [reasoning_chunk("完成")] + text_chunks("好了") + finish("stop"),
        text_chunks("再见") + finish("stop"),
    ])
    run(collect(s, "a"))
    run(collect(s, "b"))
    msgs = fake.calls[2]["messages"]
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst[0]["reasoning_content"] == "先读文件"     # 带工具轮：回传
    assert "reasoning_content" not in asst[1]            # 纯文本轮：不回传
    assert "reasoning" not in asst[0]                    # 内部键不上线


def test_interrupt_fills_outputs(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "shell", {"command": "sleep 5"}) + finish("tool_calls"),
        text_chunks("继续") + finish("stop"),
    ], mode="auto")

    async def go():
        events = []
        async def consume():
            async for ev in s.run_turn("跑"):
                events.append(ev)
                if isinstance(ev, ToolStart):
                    await asyncio.sleep(0.3)
                    s.interrupt()
        await asyncio.wait_for(consume(), 10)
        return events

    ev = run(go())
    assert done(ev).reason == "interrupted"
    t = tool_msgs(s)
    assert len(t) == 1 and "中断" in t[0]["content"]
    # 中断后还能继续对话
    ev2 = run(collect(s, "继续"))
    assert done(ev2).reason == "completed"


def test_queued_input_is_sent_after_step(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("收到补充") + finish("stop"),
    ])

    async def go():
        async for ev in s.run_turn("第一句"):
            if isinstance(ev, ToolStart):
                s.queue_input("补充一句")
    run(go())
    last_req = fake.calls[1]["messages"]
    assert last_req[-1]["role"] == "user" and last_req[-1]["content"].startswith("补充一句")


def test_approval_deny_with_reason(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "shell", {"command": "git commit -m x"}) + finish("tool_calls"),
        text_chunks("好的") + finish("stop"),
    ])
    asked = []

    def approve(ev: ApprovalRequest):
        asked.append(ev)
        return ApprovalDecision("deny_with_reason", "先别提交")

    run(collect(s, "提交", approve))
    assert asked and asked[0].prefix == "git commit"
    assert "先别提交" in tool_msgs(s)[0]["content"]


def test_approval_always_remembers_prefix(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "shell", {"command": "npm run build"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "shell", {"command": "npm run build --prod"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
    ])
    asked = []
    run(collect(s, "x", lambda ev: (asked.append(ev), ApprovalDecision("always"))[1]))
    assert len(asked) == 1


def test_plan_update_and_usage(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "todo", {"todos": [{"content": "a", "status": "in_progress"},
                                               {"content": "b", "status": "pending"}]})
        + finish("tool_calls", prompt=1000, hit=0),
        text_chunks("ok") + finish("stop", prompt=1100, hit=960),
    ])
    ev = run(collect(s, "x"))
    assert any(isinstance(e, PlanUpdate) and len(e.items) == 2 for e in ev)
    us = [e for e in ev if isinstance(e, Usage)]
    assert us[-1].totals["hit"] == 960
    assert abs(s.stats.hit_rate - 960 / 2100) < 1e-9
    assert s.stats.cost > 0


def test_recovered_content_tool_call(ee_home, workspace):
    (workspace / "a.txt").write_text("abc\n", encoding="utf-8")
    dsml = ('<｜DSML｜tool_calls><｜DSML｜invoke name="read"><｜DSML｜parameter name="path" string="true">a.txt'
            '</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>')
    s, fake = make_session(workspace, [text_chunks(dsml, 7) + finish("stop"), text_chunks("读到了") + finish("stop")])
    ev = run(collect(s, "x"))
    assert any(isinstance(e, Notice) and "正文" in e.text for e in ev)
    assert "abc" in tool_msgs(s)[0]["content"]


def test_retry_on_5xx_then_success(ee_home, workspace, monkeypatch):
    from conftest import FakeError
    import ee.core.providers.openai_chat as oc

    async def nosleep(_):
        return None
    monkeypatch.setattr(oc.asyncio, "sleep", nosleep)
    s, fake = make_session(workspace, [FakeError(503, "busy"), text_chunks("ok") + finish("stop")])
    ev = run(collect(s, "x"))
    assert done(ev).reason == "completed" and len(fake.calls) == 2


def test_degrade_unsupported_param(ee_home, workspace):
    from conftest import FakeError
    s, fake = make_session(workspace, [FakeError(400, "Unsupported parameter: stream_options"),
                                       text_chunks("ok") + finish("stop"), text_chunks("ok2") + finish("stop")])
    ev = run(collect(s, "x"))
    assert done(ev).reason == "completed"
    assert "stream_options" in fake.calls[0] and "stream_options" not in fake.calls[1]
    run(collect(s, "y"))
    assert "stream_options" not in fake.calls[2]   # 记住了
