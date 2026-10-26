import json

from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.context import ContextUpdates, FrozenPrefix
from ee.core.events import Notice


def test_prefix_frozen_and_hash_stable(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
        text_chunks("ok2") + finish("stop"),
    ])
    run(collect(s, "第一句"))
    h1 = s.prefix.sha256
    s.set_mode("auto")                      # 切换权限模式不动前缀
    s.set_shell("auto")                     # 切换 shell 不动前缀
    ev = run(collect(s, "第二句"))
    assert s.prefix.sha256 == h1 and s.prefix.verify()
    assert not [e for e in ev if isinstance(e, Notice) and e.level == "warn"]
    systems = [json.dumps(c["messages"][0], ensure_ascii=False) for c in fake.calls]
    tools = [json.dumps(c["tools"], ensure_ascii=False) for c in fake.calls]
    assert len(set(systems)) == 1 and len(set(tools)) == 1
    # system 里没有动态信息
    sys_text = fake.calls[0]["messages"][0]["content"]
    assert str(workspace) not in sys_text and "context-update" in sys_text


def test_history_append_only(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
        tool_chunks(0, "c2", "shell", {"command": "echo hi"}) + finish("tool_calls"),
        text_chunks("done") + finish("stop"),
    ], mode="auto")
    run(collect(s, "一"))
    run(collect(s, "二"))
    reqs = [c["messages"] for c in fake.calls]
    for a, b in zip(reqs, reqs[1:]):
        ser = lambda ms: [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in ms]  # noqa: E731
        assert ser(b)[: len(a)] == ser(a), "后一次请求必须以前一次请求的全部消息为前缀（字节不变）"


def test_context_update_delta(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop"),
                                       text_chunks("c") + finish("stop")])
    run(collect(s, "一"))
    run(collect(s, "二"))
    s.set_mode("read-only")
    run(collect(s, "三"))
    users = [m["content"] for m in s.history if m["role"] == "user"]
    assert '<context-update seq="1">' in users[0] and "<env>" in users[0] and "<shell>" in users[0]
    assert users[1].endswith('<context-update seq="2"/>')
    assert 'mode="delta"' in users[2] and "只读" in users[2] and "<env>" not in users[2]


def test_context_updates_unit():
    cu = ContextUpdates()
    a = cu.build({"x": "1", "y": "2"})
    b = cu.build({"x": "1", "y": "2"})
    c = cu.build({"x": "1", "y": "3"})
    assert "1" in a and "2" in a and b == '<context-update seq="2"/>' and "3" in c and "\n1\n" not in c
    cu.reset()
    assert 'mode="delta"' not in cu.build({"x": "1"})


def test_prefix_tamper_detected():
    fp = FrozenPrefix.freeze("sys", [{"type": "function", "function": {"name": "a"}}])
    assert fp.verify()
    fp.system["content"] = "changed"
    assert not fp.verify()


def test_model_switch_refreezes(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop")])
    run(collect(s, "一"))
    h = s.prefix.sha256
    s.config.data["models"]["gpt-x"] = {"provider": "deepseek", "model": "gpt-5"}
    msg = s.set_model("gpt-x")
    assert "缓存" in msg and s.prefix is None
    assert "apply_patch" in [t.name for t in s.tools]
    s._client_override = fake
    run(collect(s, "二"))
    assert s.prefix.sha256 != h
    assert '<context-update seq="2">' in s.history[-2]["content"]   # 换模型后补发全量
