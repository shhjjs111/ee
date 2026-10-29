"""读结果"可见性"与历史变化的反证（Wave 0 / T02，对应 ACC-43）。

不变量：read 返回「内容见上文」的前提，是那段内容**此刻仍在模型上下文里**。

现状的风险点：`session.truncate_to()`（编辑重发）/ `rewind_last_turn()` 会截掉历史，
但不像 compact/prune 那样清空 `ToolState.files[*].reads`。结果是历史里已经没有那段
读结果了，下一次同样范围的 read 仍可能回「与上次读取相同，内容见上文」。

本文件用真实 `ReadTool` + 真实 `Session.truncate_to` 复现，不用 mock 顶替被测行为。
"""
from __future__ import annotations

import asyncio

import pytest

from ee.tools.base import ToolContext, ToolState
from ee.tools.read import ReadTool


def _ctx(tmp_path, state: ToolState | None = None) -> ToolContext:
    return ToolContext(cwd=tmp_path, state=state or ToolState(), output_dir=tmp_path / ".out")


def _read(tmp_path, ctx, name="big.py", **args):
    tool = ReadTool()
    return asyncio.run(tool.run({"path": name, **args}, ctx))


def test_repeat_read_of_unchanged_file_returns_reference(tmp_path):
    """现有正确行为：文件未变时第二次读给引用（省 token）。这条是基线，不能退化。"""
    (tmp_path / "big.py").write_text("x = 1\n" * 800, encoding="utf-8")   # > REF_MIN_BYTES(2048)
    ctx = _ctx(tmp_path)

    first = _read(tmp_path, ctx)
    second = _read(tmp_path, ctx)

    assert "x = 1" in first.output, "第一次读必须给正文"
    assert "与上次读取相同" in second.output, f"第二次读应给引用，实际：{second.output[:120]}"


def test_repeat_read_gives_full_text_again_after_second_reference(tmp_path):
    """同一引用第二次再要就给全文（防死循环），现有行为，防回归。"""
    (tmp_path / "big.py").write_text("x = 1\n" * 800, encoding="utf-8")
    ctx = _ctx(tmp_path)

    _read(tmp_path, ctx)
    _read(tmp_path, ctx)                       # 第 1 次引用
    third = _read(tmp_path, ctx)               # 第二次要 → 全文

    assert "x = 1" in third.output, "引用第二次之后必须给全文，否则模型陷入死循环"


@pytest.mark.xfail(strict=True, reason="待 Wave 5/T17：truncate_to 不清 reads，历史已无正文却仍回「见上文」（ACC-43）")
def test_no_ghost_reference_after_history_truncated(tmp_path):
    """历史被截断后（那段读结果已不在上下文里），再读同一区间必须给正文。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.py").write_text("x = 1\n" * 800, encoding="utf-8")

    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(ws)
    s = Session(cfg, cwd=ws, persist=False)
    ctx = ToolContext(cwd=ws, state=s.state, output_dir=ws / ".out")

    tool = ReadTool()
    first = asyncio.run(tool.run({"path": "big.py"}, ctx))
    assert "x = 1" in first.output

    # 模拟「编辑重发」：把历史截到第 0 条之前（读结果所在的回合被丢弃）
    s.history = [
        {"role": "user", "content": "看看这个文件"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "read",
                                                                          "arguments": '{"path":"big.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": first.output},
    ]
    s.truncate_to(0)
    assert s.history == [], "截断后历史应为空（这段读结果已不在上下文）"

    second = asyncio.run(tool.run({"path": "big.py"}, ctx))
    assert "与上次读取相同" not in second.output, (
        "历史里已经没有这段读结果，却仍返回「内容见上文」——模型会看不到正文"
    )
