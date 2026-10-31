"""子代理（task 工具）：假模型脚本按调用顺序消费——父请求、子请求交替。"""
import asyncio
import copy

from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.events import (ApprovalDecision, ApprovalRequest, SubagentEnd, SubagentStart, SubagentUpdate, ToolEnd,
                            TurnDone)
from ee.core.subagent import apply_event


def done(ev):
    return [e for e in ev if isinstance(e, TurnDone)][-1]


def test_subagent_runs_in_own_context(ee_home, workspace):
    (workspace / "a.txt").write_text("secret-line\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        # 1 父：派生子代理
        tool_chunks(0, "p1", "task", {"description": "读文件", "prompt": "读 a.txt 并报告内容", "agent": "explore"})
        + finish("tool_calls"),
        # 2 子：读文件
        tool_chunks(0, "k1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        # 3 子：报告
        text_chunks("a.txt 里是 secret-line") + finish("stop"),
        # 4 父：收尾
        text_chunks("子代理说了 secret-line") + finish("stop"),
    ])
    ev = run(collect(s, "帮我看看 a.txt"))
    assert done(ev).reason == "completed"
    # 子代理请求：独立上下文（没有父的用户消息），工具表没有 task（不嵌套），explore 只读工具集
    child_req = fake.calls[1]
    child_users = [m["content"] for m in child_req["messages"] if m["role"] == "user"]
    assert any("[子代理任务]" in c and "读 a.txt" in c for c in child_users)
    assert not any("帮我看看" in c for c in child_users)
    child_tools = {t["function"]["name"] for t in child_req["tools"]}
    assert "task" not in child_tools and "write" not in child_tools and "read" in child_tools
    # 父拿到 <task_result>
    out = [m for m in s.history if m["role"] == "tool" and m["tool_call_id"] == "p1"][0]["content"]
    assert out.startswith('<task_result agent="explore" status="completed"') and "secret-line" in out
    # 事件
    st = [e for e in ev if isinstance(e, SubagentStart)][0]
    assert st.call_id == "p1" and st.mode == "read-only" and st.agent == "explore"
    ups = [e for e in ev if isinstance(e, SubagentUpdate)]
    assert any(u.kind == "tool_start" and u.tool == "read" for u in ups)
    end = [e for e in ev if isinstance(e, SubagentEnd)][0]
    assert end.status == "completed" and "secret-line" in end.summary
    te = [e for e in ev if isinstance(e, ToolEnd) and e.call_id == "p1"][0]
    rec = te.meta["subagent"]
    assert te.meta["kind"] == "subagent" and rec["status"] == "completed" and rec["tools"][0]["status"] == "ok"
    # 花费计入主会话：4 次请求
    assert s.stats.requests == 4


def test_subagent_approval_bubbles_and_inherits(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "跑命令", "prompt": "运行 curl"}) + finish("tool_calls"),
        tool_chunks(0, "k1", "shell", {"command": "curl https://example.com"}) + finish("tool_calls"),
        text_chunks("命令被拒绝了") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    seen = []

    def approve(e: ApprovalRequest):
        seen.append(e)
        return ApprovalDecision("deny", "")
    ev = run(collect(s, "go", approve))
    assert len(seen) == 1 and seen[0].call_id == "p1/k1" and seen[0].title.startswith("[子代理 general]")
    st = [e for e in ev if isinstance(e, SubagentStart)][0]
    assert st.mode == "default"
    assert done(ev).reason == "completed"


def test_parent_read_only_forces_child_read_only(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "改文件", "prompt": "写 b.txt"}) + finish("tool_calls"),
        tool_chunks(0, "k1", "write", {"path": "b.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("写不了") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ], mode="read-only")
    ev = run(collect(s, "go"))
    assert [e for e in ev if isinstance(e, SubagentStart)][0].mode == "read-only"
    assert not (workspace / "b.txt").exists()


def test_max_steps_and_undo(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "写", "prompt": "写 c.txt", "max_steps": 2}) + finish("tool_calls"),
        tool_chunks(0, "k1", "write", {"path": "c.txt", "content": "hi\n"}) + finish("tool_calls"),
        tool_chunks(0, "k2", "write", {"path": "d.txt", "content": "no\n"}) + finish("tool_calls"),   # 最后一步：不执行
        text_chunks("完成") + finish("stop"),
    ])
    ev = run(collect(s, "go"))
    end = [e for e in ev if isinstance(e, SubagentEnd)][0]
    assert end.status == "max_steps"
    assert (workspace / "c.txt").exists() and not (workspace / "d.txt").exists()
    # 子代理写的文件能被主会话 /undo 撤销
    restored = s.undo()
    assert any(r.endswith("c.txt") for r in restored) and not (workspace / "c.txt").exists()


def test_interrupt_propagates(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "慢", "prompt": "运行很久"}) + finish("tool_calls"),
        tool_chunks(0, "k1", "shell", {"command": "sleep 30"}) + finish("tool_calls"),
    ], mode="auto")

    async def go():
        evs = []
        async for e in s.run_turn("go"):
            evs.append(e)
            if isinstance(e, SubagentUpdate) and e.kind == "tool_start":
                await asyncio.sleep(0.3)
                s.interrupt()
        return evs
    ev = asyncio.run(asyncio.wait_for(go(), 20))
    assert done(ev).reason == "interrupted"
    end = [e for e in ev if isinstance(e, SubagentEnd)][0]
    assert end.status == "interrupted"
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(m["tool_call_id"] for m in s.history if m["role"] == "tool")


def test_unknown_agent_and_reducer(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "x", "prompt": "y", "agent": "nope"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    run(collect(s, "go"))
    out = [m for m in s.history if m["role"] == "tool"][0]["content"]
    assert "没有名为 nope" in out
    rec = apply_event(None, SubagentStart("c", "general", "d", "m", "default", 5))
    rec2 = apply_event(rec, SubagentUpdate("c", "tool_start", tool="read", tool_call_id="t1", summary="Read a"))
    rec3 = apply_event(rec2, SubagentEnd("c", "interrupted", "s", 2))
    assert rec["tools"] == [] and rec3["tools"][0]["status"] == "interrupted" and rec3["status"] == "interrupted"


def test_server_thread_attaches_subagent_meta(ee_home, workspace):
    from conftest import FakeClient
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.threads import Thread
    fake = FakeClient([
        tool_chunks(0, "p1", "task", {"description": "探索", "prompt": "看看", "agent": "explore"}) + finish("tool_calls"),
        text_chunks("没什么") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    notes = []
    t = Thread("t1", ee_home, lambda m, p: notes.append((m, copy.deepcopy(p))), {"cwd": str(workspace)},
               client_factory=lambda spec: ChatClient(spec, client=fake), persist=False)

    async def go():
        t.start_turn("go")
        await t.task
    asyncio.run(go())
    tool = [it for it in t.items if it.get("type") == "tool" and it["name"] == "task"][0]
    assert tool["details"]["kind"] == "subagent" and tool["details"]["subagent"]["status"] == "completed"
    assert any(m == "item/updated" and p["item"].get("details", {}).get("subagent", {}).get("status") == "running"
               for m, p in notes)


def test_explore_has_no_shell(ee_home, workspace):
    """explore 是只读探索，不该带 shell。

    只读模式下 shell 会进 Windows 受限令牌沙箱，而 Git Bash（本机默认 shell）
    在受限令牌下起不来（CreateFileMapping Win32 error 5）；只读里能用的是
    read/grep/glob/repo_map，它们都不走 shell。带上 shell 只会让子代理多一条
    注定失败的路径，还白白把「沙箱能不能跑 bash」这个未解问题引进来。
    """
    (workspace / "a.txt").write_text("secret-line\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "读文件", "prompt": "读 a.txt", "agent": "explore"})
        + finish("tool_calls"),
        tool_chunks(0, "k1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("done") + finish("stop"),
        text_chunks("收尾") + finish("stop"),
    ])
    ev = run(collect(s, "帮我看看"))
    assert done(ev).reason == "completed"
    child_tools = {t["function"]["name"] for t in fake.calls[1]["tools"]}
    assert "shell" not in child_tools, f"explore 不该有 shell：{sorted(child_tools)}"
    assert {"read", "grep", "glob"} <= child_tools


def test_general_subagent_gets_more_steps_than_explore(ee_home, workspace):
    """写型 general 的步伐预算要大于只读 explore。

    改代码是「读 → 改 → 跑测试 → 看失败 → 再改」的循环，30 步常常卡在收尾前；
    步数用完只能交不完整报告，主代理得重派一次——比给足步数更贵。
    explore 只读，不需要那么多预算。
    """
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "改", "prompt": "做点事"}) + finish("tool_calls"),
        tool_chunks(0, "k1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("done") + finish("stop"),
        text_chunks("收尾") + finish("stop"),
    ])
    ev = run(collect(s, "派个活"))
    assert done(ev).reason == "completed"
    card = [e for e in ev if isinstance(e, SubagentStart)][0]
    assert card.max_steps >= 60, f"general 的步数预算太小：{card.max_steps}"
