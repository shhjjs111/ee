"""会话导出（T10-a / U1）。纯函数：items + meta -> Markdown / JSON。

这些用例守的都是「导出看起来成功了，但内容不全」的地方：
工具参数是 repr 字符串冒充结构、未知条目被静默丢弃、空会话产出空文件。
"""
from __future__ import annotations

import json

import pytest


def _session() -> dict:
    """一个真实形态的会话：2 轮、3 次工具调用、含推理与 turn_end。

    字段形状照实测的真实会话（见任务书「数据形态」）。
    """
    return {
        "meta": {"title": "修 calc.py", "cwd": "C:\\proj", "model": "gpt-x",
                 "created": 1793205821.899, "updated": 1793205830.0,
                 "mode": "full", "profile": "full"},
        "items": [
            {"type": "user", "text": "看看 calc.py 的 add 有没有 bug", "id": "us_1", "seq": 1},
            {"type": "assistant", "text": "用减法实现加法，是 bug。", "reasoning": "简单任务直接修",
             "id": "as_1", "seq": 2},
            {"type": "tool", "id": "tc_1", "call_id": "call_1", "name": "read",
             "args": "{'path': 'calc.py'}", "summary": "Read calc.py", "status": "ok",
             "output": "1\tdef add(a,b):\n2\t    return a-b", "seq": 3},
            {"type": "tool", "id": "tc_2", "call_id": "call_2", "name": "edit",
             "args": "{'path': 'calc.py', 'old': 'return a-b'}", "summary": "Edited calc.py",
             "status": "ok", "output": "已编辑 calc.py", "seq": 4},
            {"type": "turn_end", "reason": "completed", "duration_ms": 4046,
             "usage": "{'requests': 1, 'prompt': 4146}", "id": "tu_1", "seq": 5},
            {"type": "user", "text": "再跑一次测试", "id": "us_2", "seq": 6},
            {"type": "assistant", "text": "跑完了，通过。", "reasoning": "", "id": "as_2", "seq": 7},
            {"type": "tool", "id": "tc_3", "call_id": "call_3", "name": "shell",
             "args": "{'command': 'pytest -q'}", "summary": "Ran pytest",
             "status": "ok", "output": "3 passed", "seq": 8},
        ],
    }


# ---------------------------------------------------------------- Markdown

def test_markdown_contains_all_turns():
    """两条用户消息、两条助手回复、三次工具调用，一个都不能少。"""
    from ee.core.export import to_markdown

    md = to_markdown(_session())
    assert "看看 calc.py 的 add 有没有 bug" in md
    assert "再跑一次测试" in md
    assert "用减法实现加法" in md
    assert "跑完了，通过。" in md
    for name in ("read", "edit", "shell"):
        assert name in md, f"工具 {name} 没出现在导出里"


def test_markdown_renders_tool_output_in_code_block():
    """工具输出要进围栏代码块，否则会被当成正文渲染。"""
    from ee.core.export import to_markdown

    md = to_markdown(_session())
    assert "```" in md
    # 输出内容在某个 ``` 块之内
    blocks = md.split("```")
    assert any("3 passed" in b for b in blocks), md


def test_markdown_includes_reasoning_when_present():
    """有推理时默认导出；关掉时不出。"""
    from ee.core.export import to_markdown

    assert "简单任务直接修" in to_markdown(_session())
    assert "简单任务直接修" not in to_markdown(_session(), include_reasoning=False)


def test_markdown_marks_turn_end():
    """回合结束原因要可见，用户才知道那轮是正常结束还是被打断。"""
    from ee.core.export import to_markdown

    assert "completed" in to_markdown(_session())


def test_unknown_item_type_is_not_silently_dropped():
    """未知类型必须留痕。静默丢弃会让用户以为导出完整。"""
    from ee.core.export import to_markdown

    data = _session()
    data["items"].append({"type": "telemetry", "id": "x", "seq": 9, "blob": "重要内容"})
    md = to_markdown(data)
    assert "telemetry" in md


def test_export_is_deterministic():
    """同输入两次导出字节相同——纯函数，不许有隐藏状态。"""
    from ee.core.export import to_markdown

    assert to_markdown(_session()) == to_markdown(_session())


def test_empty_session_exports_valid_document():
    """空会话也要产出合法文件：有标题骨架，不是空串。"""
    from ee.core.export import to_markdown, to_json

    empty = {"meta": {"title": "空会话"}, "items": []}
    md = to_markdown(empty)
    assert md.strip()
    assert "空会话" in md
    assert json.loads(to_json(empty))["meta"]["title"] == "空会话"


# ---------------------------------------------------------------- JSON

def test_json_tool_args_are_structured():
    """repr 字符串必须还原成对象——这是 JSON 导出的意义所在。"""
    from ee.core.export import to_json

    d = json.loads(to_json(_session()))
    tools = [it for it in d["items"] if it["type"] == "tool"]
    assert tools[0]["args"]["path"] == "calc.py"
    assert tools[1]["args"]["old"] == "return a-b"
    assert tools[2]["args"]["command"] == "pytest -q"


def test_json_unparsable_args_kept_with_marker():
    """解析不了的参数保留原文并标记，不能丢、不能假装是结构。"""
    from ee.core.export import to_json

    data = {"meta": {"title": "t"}, "items": [
        {"type": "tool", "id": "tc_x", "call_id": "c", "name": "odd",
         "args": "<not a python literal>", "status": "ok", "output": "", "seq": 1}]}
    d = json.loads(to_json(data))
    tool = d["items"][0]
    assert "<not a python literal>" in json.dumps(tool, ensure_ascii=False)
    assert tool.get("args_raw") is True or tool.get("args") == "<not a python literal>"


def test_json_is_valid_utf8_not_escaped():
    """中文不能被转义成 \\uXXXX——导出文件是给人看的。"""
    from ee.core.export import to_json

    raw = to_json(_session())
    assert "跑完了" in raw
    assert "\\u8dd1" not in raw


# ---------------------------------------------------------------- 边界

def test_export_rejects_unknown_format():
    """格式只接受 markdown / json，别的明确报错而不是给个默认。"""
    from ee.core.export import export

    with pytest.raises(ValueError):
        export(_session(), fmt="xml")
