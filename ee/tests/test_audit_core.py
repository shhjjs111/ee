"""后端内核审查（2026-10-27）发现的问题：每条先写成失败测试再修。"""
import asyncio
import threading

from conftest import FakeError, collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.events import SubagentUpdate, TurnDone


def done(ev):
    return [e for e in ev if isinstance(e, TurnDone)][-1]


# ------------------------------------------------------------------ fork 子代理不应误报前缀变化
def test_fork_child_prefix_matches_no_spurious_warning(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "分支", "prompt": "看看", "agent": "explore", "fork": True})
        + finish("tool_calls"),
        tool_chunks(0, "k1", "grep", {"pattern": "x"}) + finish("tool_calls"),
        text_chunks("看完了") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    ev = run(collect(s, "开始"))
    assert done(ev).reason == "completed"
    notes = [e.text for e in ev if isinstance(e, SubagentUpdate) and e.kind == "notice"]
    assert not any("与会话开始时不同" in (t or "") for t in notes), notes


# ------------------------------------------------------------------ 400 上下文超长不能被当成“参数不支持”降级
def test_overflow_400_mentioning_max_tokens_is_not_degraded(ee_home, workspace):
    from ee.core.providers.openai_chat import ModelError
    msg = ("max_tokens is too large: 8192. This model's maximum context length is 131072 tokens. "
           "Please reduce the length of the messages. (type: invalid_request_error)")
    s, fake = make_session(workspace, [FakeError(400, msg), FakeError(400, msg), FakeError(400, msg)])
    s.config.data.setdefault("compact", {})["auto"] = False
    client = s.client()
    st = client.state.get(client.key)

    async def go():
        out = []
        try:
            async for kind, val in client.stream([{"role": "user", "content": "hi"}], None):
                out.append((kind, val))
        except ModelError as e:
            return e, out
        return None, out

    err, out = run(go())
    assert err is not None and err.kind == "overflow"
    assert not st.get("no_max_tokens"), st
    assert not any(k == "notice" and "降级" in str(v) for k, v in out)


# ------------------------------------------------------------------ grep/glob 的纯 Python 回退不能卡事件循环
def test_grep_glob_fallback_runs_off_event_loop(ee_home, workspace, monkeypatch):
    from ee.tools import search
    from ee.tools.base import ToolContext, ToolState
    (workspace / "a.py").write_text("hello world\n", encoding="utf-8")
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    seen = []
    orig = search._walk

    def walk(root):
        seen.append(threading.current_thread() is threading.main_thread())
        yield from orig(root)

    monkeypatch.setattr(search, "_walk", walk)
    ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / ".out")

    async def go():
        r1 = await search.GrepTool().run({"pattern": "hello"}, ctx)
        r2 = await search.GlobTool().run({"pattern": "*.py"}, ctx)
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert "a.py" in r1.output and "a.py" in r2.output
    assert seen and not any(seen), "回退遍历在事件循环线程上执行"

