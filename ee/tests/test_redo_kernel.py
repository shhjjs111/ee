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
