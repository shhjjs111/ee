"""审查修复重做（fix/redo-kernel）：每个 bug 一组用例，全部用假模型（conftest.FakeClient），不调真实 API。"""
from __future__ import annotations

import asyncio
import json

from conftest import (FakeClient, collect, finish, make_session, reasoning_chunk, run, text_chunks,
                      tool_chunks)

from ee.core.providers import transforms as T
from ee.core.providers.profiles import profile_for


# ---------------------------------------------------------------- 1) DeepSeek reasoning_content 回传
# 依据：https://api-docs.deepseek.com/guides/thinking_mode
#   请求带 tools 时，之前所有轮次的 reasoning_content 都要回传（包括没有调用工具的轮次），否则 400；
#   不带 tools 时回传会被忽略。ee 的主循环请求总带 tools → DeepSeek 对所有助手消息都回传。

def test_deepseek_passback_all_assistant_turns_when_tools(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        [reasoning_chunk("先读文件")] + tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        [reasoning_chunk("完成")] + text_chunks("好了") + finish("stop"),
        text_chunks("再见") + finish("stop"),
    ])
    run(collect(s, "a"))
    run(collect(s, "b"))
    call = fake.calls[2]
    assert call.get("tools")
    asst = [m for m in call["messages"] if m["role"] == "assistant"]
    assert asst[0]["reasoning_content"] == "先读文件"
    assert asst[1]["reasoning_content"] == "完成"         # 纯文本轮也要回传（文档：even for turns without tool call）
    assert all("reasoning" not in m for m in asst)


def test_deepseek_passback_missing_reasoning_is_empty_string():
    hist = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "hi"}]
    w = T.wire_messages(hist, profile_for("deepseek-flash"))
    assert w[1]["reasoning_content"] == ""


# ---------------------------------------------------------------- 2) 压缩与回合并发：互相覆盖历史

def _responder(params):
    last = params["messages"][-1]
    content = last.get("content") or ""
    if isinstance(content, str) and ("摘要" in content or "压缩" in content) and last["role"] == "user" \
            and "<context-update" not in content:
        return text_chunks("这是摘要" * 15, 1) + finish("stop")     # 压缩故意慢（约 1.2s）
    if isinstance(content, str) and content.startswith("二"):
        return text_chunks("答二", 1) + finish("stop")
    return text_chunks("答", 1) + finish("stop")


def test_compact_and_turn_are_serialized(ee_home, workspace):
    s, fake = make_session(workspace, [_responder] * 6, delay=0.02)

    async def main():
        await collect(s, "一")
        ctask = asyncio.create_task(s.compact())
        await asyncio.sleep(0.01)                       # 压缩已在等模型输出
        assert s.running
        ev = await collect(s, "二")                     # 回合必须等压缩结束，不能并行跑
        res = await ctask
        return res, ev

    res, _ = run(main())
    assert res["ok"], res
    kinds = [m.get("_kind") for m in s.history]
    assert "summary" in kinds
    # 回合的消息排在摘要之后，没被压缩重建的历史覆盖掉
    assert s.history[-1]["role"] == "assistant" and s.history[-1]["content"] == "答二"
    assert kinds.index("summary") < len(s.history) - 1
    assert not s.running


def test_compact_refused_while_turn_running(ee_home, workspace):
    s, fake = make_session(workspace, [_responder] * 4, delay=0.03)

    async def main():
        await collect(s, "一")
        t = asyncio.create_task(collect(s, "二"))
        await asyncio.sleep(0.01)
        r = await s.compact()
        await t
        return r

    r = run(main())
    assert not r["ok"] and "运行中" in r["message"]
    assert s.history[-1]["content"] == "答二" and not s.running


# ---------------------------------------------------------------- 3) 重启恢复会话后前缀缓存整段失效

def _persisted_session(workspace, home, script, session_id=None):
    from ee.config import load_config
    from ee.core.session import Session
    from ee.core.store import SessionStore
    fake = FakeClient(script)
    s = Session(load_config(workspace), cwd=workspace, client=fake, persist=True, home=home)
    if session_id:
        s.store = SessionStore(home, session_id=session_id)
        s.history = SessionStore.load_messages(s.store.path)
    return s, fake


def _ser(ms):
    return [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in ms]


def test_resume_keeps_prefix_bytes(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    a, fa = _persisted_session(workspace, ee_home, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("一好") + finish("stop"),
        text_chunks("二好") + finish("stop"),
    ])
    run(collect(a, "一"))
    a.set_style("concise")                      # 会话中途切换风格：system 里冻结的仍是开始时的风格
    run(collect(a, "二"))
    last = fa.calls[-1]
    sent_before = last["messages"] + [{"role": "assistant", "content": "二好", "reasoning_content": ""}]

    # 模拟重启：新进程从磁盘恢复（threads.ensure_session 的做法：读历史、按 meta 恢复风格）
    b, fb = _persisted_session(workspace, ee_home, [text_chunks("三好") + finish("stop")], session_id=a.store.id)
    b.set_style("concise")
    run(collect(b, "三"))
    first = fb.calls[0]
    assert json.dumps(first["tools"], ensure_ascii=False) == json.dumps(last["tools"], ensure_ascii=False)
    assert first["messages"][0] == last["messages"][0], "恢复后 system 必须与重启前逐字节一致"
    n = len(sent_before)
    want = _ser(sent_before)
    got = _ser(first["messages"][:n])
    # 重启前最后一次请求 + 模型回复，必须是恢复后第一次请求的字节前缀
    assert got[:-1] == want[:-1]
    assert json.loads(got[-1])["content"] == "二好"


# ---------------------------------------------------------------- 4) 正文里出现 <think> 字样时后面的回答被吞

def _split(chunks):
    sp = T.ThinkSplitter()
    out = []
    for c in chunks:
        out += sp.feed(c)
    out += sp.flush()
    text = "".join(p for k, p in out if k == "text")
    reasoning = "".join(p for k, p in out if k == "reasoning")
    return text, reasoning


def test_think_literal_in_body_is_not_swallowed():
    body = "MiniMax 会把思考放在 <think> 标签里，解析时要注意。后面还有很多回答内容。"
    text, reasoning = _split([body[i:i + 4] for i in range(0, len(body), 4)])
    assert text == body and reasoning == ""


def test_think_block_at_start_still_parsed():
    text, reasoning = _split(["\n<thi", "nk>先想想</th", "ink>\n答案里提到 <think> 也照常显示"])
    assert reasoning == "先想想"
    assert text == "\n答案里提到 <think> 也照常显示"


def test_think_literal_end_to_end(ee_home, workspace):
    body = "用 `<think>` 标签包裹推理。剩下的说明。"
    s, fake = make_session(workspace, [text_chunks(body, 2) + finish("stop")])
    run(collect(s, "问"))
    assert s.history[-1]["content"] == body and not s.history[-1].get("reasoning")
