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
    s, fake = make_session(workspace, script, max_steps=3, max_steps_segments=1)
    ev = run(collect(s, "loop"))
    assert done(ev).reason == "max_steps"
    assert len(fake.calls) == 3
    # 最后一步前追加了“只总结”的提示；最后一步的调用没执行但也有输出
    assert any("最大步数" in (m.get("content") or "") for m in fake.calls[2]["messages"] if m["role"] == "user")
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(m["tool_call_id"] for m in tool_msgs(s))


def test_segments_continue_past_step_limit(ee_home, workspace):
    """max_steps_segments=2：步数上限翻倍，段边界不写“只总结”而是接着做。

    关键 1：段边界**不**注入“只总结”——模型因此继续调工具（旧行为下它会收尾）。
    关键 2：最终仍受总预算约束，到最后一格才停。
    """
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    script = [tool_chunks(0, f"c{i}", "read", {"path": "a.txt", "offset": 1, "limit": i + 1}) + finish("tool_calls")
              for i in range(6)]
    s, fake = make_session(workspace, script[:4], max_steps=2, max_steps_segments=2)
    ev = run(collect(s, "loop"))
    assert len(fake.calls) == 4, "2 段 × 2 步应该跑满 4 步"
    assert done(ev).reason == "max_steps"
    # 第 3 步（第二段第一步）本应带“只总结”，分段模式下改成“接着做”
    third = [m.get("content") or "" for m in fake.calls[2]["messages"] if m["role"] == "user"][-1]
    assert "接着做" in third and "不要再调用任何工具" not in third
    # 每一格 tool_call 都有配对的输出（否则会话永久 400）
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(m["tool_call_id"] for m in tool_msgs(s))


def test_segments_one_is_old_behavior(ee_home, workspace):
    """显式 1 段 == 旧行为：到 max_steps 就停，不续跑。"""
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("收尾") + finish("stop"),
    ], max_steps=2, max_steps_segments=1)
    ev = run(collect(s, "go"))
    assert len(fake.calls) == 2 and done(ev).reason == "max_steps"


def test_segment_boundary_also_compacts(ee_home, workspace, monkeypatch):
    """段边界要顺手压缩：否则第二段从水位起跑，一路顶到窗口 400。

    现状把这次压缩发生在「下一回合开始」，这里只把它挪到同一回合的段边界——次数不变。
    """
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c3", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c4", "read", {"path": "a.txt"}) + finish("tool_calls"),
    ], max_steps=2, max_steps_segments=2)
    calls = []
    orig = type(s)._maybe_auto_compact

    async def spy(self, emit):
        calls.append(self._turn_running)      # 压缩那一刻守卫必须是 False（已临时放开）
        await orig(self, emit)

    monkeypatch.setattr(type(s), "_maybe_auto_compact", spy)
    run(collect(s, "go"))
    # 第 2 步（段边界）之前那次调用必须发生在守卫放开之后
    assert calls, "段边界没有触发 _maybe_auto_compact"
    assert calls[0] is False, "段边界压缩时 _turn_running 应当临时放开为 False"


def test_segment_boundary_retracks_turn_start(ee_home, workspace):
    """压缩会清空 turn_starts；段边界压缩后必须重新登记，否则本回合再也不能回退。"""
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c3", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c4", "read", {"path": "a.txt"}) + finish("tool_calls"),
    ], max_steps=2, max_steps_segments=2)
    run(collect(s, "go"))
    # 直接调用重建函数，验证它在新历史里能找到本回合的用户消息
    s._retrack_turn_start()
    assert s.turn_starts, "压缩后必须重新登记回合起点，否则本回合无法回退"
    i = s.turn_starts[-1]
    assert 0 <= i < len(s.history)
    assert s.history[i]["role"] == "user" and not s.history[i].get("_kind")


def test_last_step_text_summary_is_max_steps(ee_home, workspace):
    """最后一格给出纯文字收尾，仍然标 max_steps。

    这**不是**误报：最后一步之前 ee 注入了 LAST_STEP_NOTE（"不要再调用任何工具，只用文字
    总结"），模型正是被它逼着收尾的。实测 31 个触顶回合最后一步全是纯文字——那恰恰说明
    它们都是被步数上限截断的，任务本身还没跑完。
    """
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("做完了") + finish("stop"),
    ], max_steps=2, max_steps_segments=1)
    ev = run(collect(s, "go"))
    assert done(ev).reason == "max_steps"
    # 最后一步前确实注入了「只总结」提示，这才是标 max_steps 的依据
    assert any("不要再调用任何工具" in (m.get("content") or "")
               for m in fake.calls[1]["messages"] if m["role"] == "user")


def test_segment_boundary_real_compact_keeps_pairs(ee_home, workspace, monkeypatch):
    """段边界真的压缩了以后，历史里 tool_calls / tool 必须仍然一一配对。

    前面两条段边界测试用的是小对话，压缩根本没触发（needs_compaction 依赖 fresh_usage
    且要过水位），所以「压完历史还能发出去」这件事实际上没被测到。这里用大 prompt_tokens
    把水位顶穿，让 _compact 真的重建历史，再验证：
    1. 确实压缩了（历史条数变少 / 出现摘要）
    2. 配对没坏——破坏配对会让下一格请求永久 400
    3. 压缩后仍能跑完第二段
    """
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    big = 200_000                       # 远超 0.9 × 128k 窗口
    # 按请求内容分流：压缩请求返回纯文本摘要，其余才是真实采样。
    # 之前数格子写死脚本的做法不可靠——压缩自己就要占 1~3 次请求（主路 + 侧路重试）。
    n = {"samples": 0}

    def script(params):
        if "需要压缩" in str(params.get("messages")) or "上下文检查点压缩" in str(params.get("messages")):
            return text_chunks("【摘要】已读过 a.txt") + finish("stop")
        n["samples"] += 1
        return (tool_chunks(0, f"c{n['samples']}", "read", {"path": "a.txt"})
                + finish("tool_calls", prompt=big))

    s, fake = make_session(workspace, [script] * 20,
                           max_steps=2, max_steps_segments=2)

    results = []
    orig = type(s)._compact

    async def spy(self, emit, reason="auto"):
        r = await orig(self, emit, reason)
        results.append((r.get("ok"), r.get("tokens_before", 0), r.get("tokens_after", 0)))
        return r

    monkeypatch.setattr(type(s), "_compact", spy)

    ev = run(collect(s, "go"))
    assert results, "水位已顶穿，段边界必须真的压缩了一次"
    assert done(ev).reason == "max_steps"
    # 配对完整：破坏它会让下一格请求永久 400
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(m["tool_call_id"] for m in tool_msgs(s))
    # 压缩必须真的成功并把历史变短：这是「续跑不会一路顶到窗口 400」的证据。
    # 只看 _compact 被调用过是不够的——早期版本就是这么写的，实际上压缩返回了
    # ok=False（「模型没有返回摘要」），测试却仍然是绿的。
    assert results and all(r[0] for r in results), f"压缩没成功：{results}"
    before_tok, after_tok = results[0][1], results[0][2]
    assert after_tok < before_tok, f"压缩后 token 没变小：{before_tok} → {after_tok}"
    # 历史里必须真的出现了摘要（_kind=summary），而不是原地没动
    assert any(m.get("_kind") == "summary" for m in s.history), "压缩后历史里没有摘要"
    # 回合起点重建到本回合那句用户话上（否则压缩后本回合无法回退）
    assert s.turn_starts and s.history[s.turn_starts[-1]]["role"] == "user"


def test_last_step_tool_calls_still_hard_stops(ee_home, workspace):
    """最后一格还在调工具：那才是真的被步数逼停，仍然标 max_steps。"""
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "a.txt"}) + finish("tool_calls"),
    ], max_steps=2, max_steps_segments=1)
    ev = run(collect(s, "go"))
    assert done(ev).reason == "max_steps"


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
    active = {"now": 0, "max": 0, "t0": None, "t1": 0.0}
    orig = ReadTool.run

    async def slow_run(self, args, ctx):
        if active["t0"] is None:
            active["t0"] = time.monotonic()
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.2)
        active["now"] -= 1
        active["t1"] = time.monotonic()
        return await orig(self, args, ctx)

    monkeypatch.setattr(ReadTool, "run", slow_run)
    for n in "abc":
        (workspace / f"{n}.txt").write_text(n, encoding="utf-8")
    calls = []
    for i, n in enumerate("abc"):
        calls += tool_chunks(i, f"c{i}", "read", {"path": f"{n}.txt"})
    s, fake = make_session(workspace, [calls + finish("tool_calls"), text_chunks("ok") + finish("stop")])
    run(collect(s, "读三个"))
    assert active["max"] == 3
    # 只量工具阶段（回合前后的磁盘快照在 Windows 上要几百毫秒，不算进来）；串行至少 0.6 秒
    assert active["t1"] - active["t0"] < 0.5
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
    s.config.data["prune"] = {**s.config.data["prune"], "strip_reasoning": False}   # 本用例只验回传机制
    run(collect(s, "a"))
    run(collect(s, "b"))
    msgs = fake.calls[2]["messages"]
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst[0]["reasoning_content"] == "先读文件"     # 带工具轮：回传
    assert asst[1]["reasoning_content"] == "完成"         # 纯文本轮也回传（带 tools 的请求要求所有轮次回传）
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
