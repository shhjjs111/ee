"""D5-10 fork 式子代理：继承主会话上下文（前缀字节相同）、只回传结论、权限与并行规则不变。"""
from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.events import ApprovalDecision, ApprovalRequest, SubagentStart, ToolEnd, TurnDone
from ee.core.subagent import FORK_PREAMBLE, apply_event


def done(ev):
    return [e for e in ev if isinstance(e, TurnDone)][-1]


def tool_out(s, cid):
    return next(m for m in s.history if m.get("role") == "tool" and m.get("tool_call_id") == cid)["content"]


def test_fork_inherits_prefix_and_returns_only_result(ee_home, workspace):
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        # 回合 1：主会话读文件
        tool_chunks(0, "p0", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("a.txt 是 alpha") + finish("stop"),
        # 回合 2：派分支子代理
        tool_chunks(0, "p1", "task", {"description": "旁支调研", "prompt": "根据上文判断 a.txt 该叫什么名字",
                                      "agent": "explore", "fork": True}) + finish("tool_calls"),
        tool_chunks(0, "k1", "grep", {"pattern": "alpha"}) + finish("tool_calls", hit=900),   # 子
        text_chunks("建议改名 alpha.txt") + finish("stop"),                                    # 子
        text_chunks("子代理建议 alpha.txt") + finish("stop"),                                  # 主
    ])
    run(collect(s, "读 a.txt，记住暗号是 swordfish"))
    ev = run(collect(s, "派个分支想想名字"))
    assert done(ev).reason == "completed"
    parent_req, child_req, parent_next = fake.calls[2], fake.calls[3], fake.calls[5]
    # 1) 同一份 system + tools（字节相同）
    assert child_req["messages"][0] == parent_req["messages"][0]
    assert child_req["tools"] == parent_req["tools"]
    # 2) 子代理的历史 = 主会话请求 + 含 task 调用的 assistant 消息（与主会话下一次请求的前缀逐条相同）
    n = len(parent_req["messages"]) + 1
    assert child_req["messages"][:n] == parent_next["messages"][:n]
    assert any("swordfish" in (m.get("content") or "") for m in child_req["messages"])      # 继承了上下文
    assert child_req["messages"][n]["role"] == "tool" and child_req["messages"][n]["tool_call_id"] == "p1"
    last = child_req["messages"][-1]
    assert last["role"] == "user" and last["content"].startswith(FORK_PREAMBLE) and "该叫什么名字" in last["content"]
    # 3) 主会话历史不被子代理过程污染：没有 k1 的调用和输出，只有结论
    assert not any(m.get("tool_call_id") == "k1" for m in s.history)
    assert not any(FORK_PREAMBLE in (m.get("content") or "") for m in s.history)
    out = tool_out(s, "p1")
    assert out.startswith('<task_result agent="explore" fork="true" status="completed"') and "alpha.txt" in out
    # 4) 事件 / 界面记录带 fork 标识；子代理请求的缓存命中计入主会话
    st = [e for e in ev if isinstance(e, SubagentStart)][0]
    assert st.fork and st.mode == "read-only"
    rec = [e for e in ev if isinstance(e, ToolEnd) and e.call_id == "p1"][0].meta["subagent"]
    assert rec["fork"] is True and apply_event(None, st)["fork"] is True
    assert s.stats.hit >= 900


def test_fork_blocks_nesting_and_questions(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "分支", "prompt": "做点事", "fork": True}) + finish("tool_calls"),
        tool_chunks(0, "k1", "task", {"description": "再派", "prompt": "x"})
        + tool_chunks(1, "k2", "ask_user", {"question": "要吗？", "options": ["要", "不要"]}) + finish("tool_calls"),
        text_chunks("自己做完了") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    ev = run(collect(s, "开始"))
    assert done(ev).reason == "completed"
    child_final = fake.calls[2]["messages"]
    outs = {m["tool_call_id"]: m["content"] for m in child_final if m.get("role") == "tool"}
    assert "不能使用 task" in outs["k1"] and "不能使用 ask_user" in outs["k2"]
    # 工具 schema 仍与主会话相同（task / ask_user 在表里，只是调用报错）
    assert fake.calls[1]["tools"] == fake.calls[0]["tools"]
    assert "自己做完了" in tool_out(s, "p1")


def test_fork_permissions(ee_home, workspace):
    # explore 分支：只读，写被拒
    s, _ = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "e", "prompt": "写个文件", "agent": "explore", "fork": True})
        + finish("tool_calls"),
        tool_chunks(0, "k1", "write", {"path": "x.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("写不了") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    run(collect(s, "go"))
    assert not (workspace / "x.txt").exists()
    # general 分支：继承默认模式，需要审批的照常冒泡到主会话；拒绝后不执行
    s, _ = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "g", "prompt": "跑 curl", "fork": True}) + finish("tool_calls"),
        tool_chunks(0, "k1", "shell", {"command": "curl https://example.com"}) + finish("tool_calls"),
        text_chunks("被拒绝") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    asked = []

    def approve(e: ApprovalRequest):
        asked.append(e)
        return ApprovalDecision("deny")
    run(collect(s, "go", approve=approve))
    assert asked and asked[0].call_id == "p1/k1" and asked[0].source["call_id"] == "p1"
    # 主会话只读：分支也只读
    s, _ = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "g", "prompt": "写", "fork": True}) + finish("tool_calls"),
        tool_chunks(0, "k1", "write", {"path": "y.txt", "content": "y"}) + finish("tool_calls"),
        text_chunks("不行") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ], mode="read-only")
    ev = run(collect(s, "go"))
    assert not (workspace / "y.txt").exists()
    assert [e for e in ev if isinstance(e, SubagentStart)][0].mode == "read-only"


def test_fork_rejects_custom_agent_and_other_model(ee_home, workspace):
    agents = workspace / ".ee" / "agents"
    agents.mkdir(parents=True)
    (agents / "rev.md").write_text("---\nname: rev\ndescription: 审查\n---\n你是审查员", encoding="utf-8")
    s, _ = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "a", "prompt": "x", "agent": "rev", "fork": True})
        + tool_chunks(1, "p2", "task", {"description": "b", "prompt": "x", "model": "deepseek-pro", "fork": True})
        + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    run(collect(s, "go"))
    assert "fork 模式只支持" in tool_out(s, "p1")
    assert "沿用主会话当前的模型" in tool_out(s, "p2")


def test_parallel_forks(ee_home, workspace):
    started = []

    def child(tag):
        def item(params):
            started.append(tag)
            return text_chunks(f"结论{tag}") + finish("stop")
        return item
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "A", "prompt": "查 A", "agent": "explore", "fork": True})
        + tool_chunks(1, "p2", "task", {"description": "B", "prompt": "查 B", "agent": "explore", "fork": True})
        + finish("tool_calls"),
        child("1"), child("2"),
        text_chunks("汇总") + finish("stop"),
    ])
    ev = run(collect(s, "并行查"))
    assert done(ev).reason == "completed"
    assert "结论" in tool_out(s, "p1") and "结论" in tool_out(s, "p2")
    for req in fake.calls[1:3]:
        tools = [m for m in req["messages"] if m.get("role") == "tool"]
        assert {m["tool_call_id"] for m in tools} == {"p1", "p2"}          # 两个调用都补了占位，消息格式合法
        assert req["messages"][:len(fake.calls[0]["messages"])] == fake.calls[0]["messages"]
    assert sorted(started) == ["1", "2"]


def test_fork_persisted_parent_history_clean(ee_home, workspace):
    from ee.config import load_config
    from ee.core.session import Session
    from ee.core.store import SessionStore
    from conftest import FakeClient
    cfg = load_config(workspace)
    fake = FakeClient([
        tool_chunks(0, "p1", "task", {"description": "A", "prompt": "查 A", "fork": True}) + finish("tool_calls"),
        text_chunks("分支结论") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    s = Session(cfg, cwd=workspace, client=fake, persist=True)
    run(collect(s, "go"))
    msgs = SessionStore.load_messages(s.store.path)
    assert msgs == s.history
    assert not any(FORK_PREAMBLE in (m.get("content") or "") for m in msgs)
