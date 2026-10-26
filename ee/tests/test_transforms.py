import json

from ee.core.providers import transforms as T
from ee.core.providers.openai_chat import _accumulate, parse_usage
from ee.core.providers.profiles import profile_for

KNOWN = ["read", "write", "edit", "shell", "grep", "glob", "todo"]


def test_think_splitter_across_chunks():
    sp = T.ThinkSplitter()
    out = []
    for ch in ["开头<thi", "nk>想一想", "，再想</th", "ink>正文"]:
        out += sp.feed(ch)
    out += sp.flush()
    text = "".join(p for k, p in out if k == "text")
    reasoning = "".join(p for k, p in out if k == "reasoning")
    assert text == "开头正文" and reasoning == "想一想，再想"


def test_delta_reasoning_fields():
    assert T.delta_reasoning({"reasoning_content": "a"}) == "a"
    assert T.delta_reasoning({"reasoning": "b"}) == "b"
    assert T.delta_reasoning({"content": "c"}) == ""


def test_dsml_parse_fullwidth_and_halfwidth():
    for bar in ("｜", "|"):
        c = (f"我来读文件。\n<{bar}DSML{bar}tool_calls>\n<{bar}DSML{bar}invoke name=\"read\">\n"
             f"<{bar}DSML{bar}parameter name=\"path\" string=\"true\">a.py</{bar}DSML{bar}parameter>\n"
             f"<{bar}DSML{bar}parameter name=\"limit\" string=\"false\">20</{bar}DSML{bar}parameter>\n"
             f"</{bar}DSML{bar}invoke>\n</{bar}DSML{bar}tool_calls>")
        calls, rest = T.parse_content_tool_calls(c, KNOWN)
        assert len(calls) == 1 and rest == "我来读文件。"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"path": "a.py", "limit": 20}


def test_dsml_inside_code_fence_ignored():
    c = "格式示例：\n```\n<｜DSML｜tool_calls><｜DSML｜invoke name=\"read\"></｜DSML｜invoke></｜DSML｜tool_calls>\n```"
    calls, rest = T.parse_content_tool_calls(c, KNOWN)
    assert calls == [] and rest == c


def test_json_content_tool_call():
    calls, rest = T.parse_content_tool_calls('{"name": "Read", "arguments": {"path": "x.py"}}', KNOWN)
    assert calls[0]["function"]["name"] == "read" and rest == ""
    calls, _ = T.parse_content_tool_calls('```json\n[{"function": {"name": "grep", "arguments": "{\\"pattern\\": \\"a\\"}"}}]\n```', KNOWN)
    assert calls[0]["function"]["name"] == "grep"
    # 普通 JSON 回答不是工具调用
    calls, rest = T.parse_content_tool_calls('{"answer": 42}', KNOWN)
    assert calls == []


def test_xml_tool_calls():
    c = '<tool_call>\n{"name": "shell", "arguments": {"command": "ls"}}\n</tool_call>'
    calls, _ = T.parse_content_tool_calls(c, KNOWN)
    assert calls[0]["function"]["name"] == "shell"
    c2 = "<tool_call>\n<function=read>\n<parameter=path>\na.py\n</parameter>\n</function>\n</tool_call>"
    calls, _ = T.parse_content_tool_calls(c2, KNOWN)
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}


def test_normalize_tool_name():
    assert T.normalize_tool_name("Read", KNOWN) == "read"
    assert T.normalize_tool_name("functions.grep", KNOWN) == "grep"
    assert T.normalize_tool_name("bash", KNOWN) == "shell"
    assert T.normalize_tool_name("str_replace", KNOWN) == "edit"
    assert T.normalize_tool_name("TodoWrite", KNOWN) == "todo"


def test_parse_and_repair_args():
    a, e = T.parse_args('{"path": "a", "limit": "20",}')
    assert e is None and a["limit"] == "20"
    schema = {"properties": {"limit": {"type": "integer"}, "replace_all": {"type": "boolean"},
                             "todos": {"type": "array", "items": {"type": "object",
                                                                  "properties": {"content": {"type": "string"}}}},
                             "offset": {"type": "integer"}}, "required": []}
    r = T.repair_args({"limit": "20", "replace_all": "true", "todos": '[{"content": 1}]', "offset": None}, schema)
    assert r == {"limit": 20, "replace_all": True, "todos": [{"content": "1"}]}
    r = T.repair_args({"todos": {"0": {"content": "a"}, "1": {"content": "b"}}}, schema)
    assert [x["content"] for x in r["todos"]] == ["a", "b"]
    a, e = T.parse_args("{'path': 'x'}")
    assert a == {"path": "x"}
    a, e = T.parse_args("not json")
    assert a is None and e
    a, _ = T.parse_args(json.dumps(json.dumps({"path": "z"})))   # 双重编码
    assert a == {"path": "z"}


def test_wire_messages_copy_and_rules():
    hist = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                                 "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "out"},
            {"role": "assistant", "content": "final", "reasoning": "r2"}]
    ds = profile_for("deepseek-flash")
    w = T.wire_messages(hist, ds)
    assert w[1]["reasoning_content"] == "" and w[1]["content"] == ""      # 缺失补 ""
    assert "reasoning_content" not in w[3] and "reasoning" not in w[3]
    assert hist[3]["reasoning"] == "r2"                                     # 原历史不被修改
    gpt = profile_for("gpt-5")
    w2 = T.wire_messages(hist, gpt)
    assert w2[1]["content"] is None and "reasoning_content" not in w2[1]
    assert T.wire_messages(hist, ds) == w                                   # 确定性


def test_unsupported_param_detection():
    assert T.unsupported_param("Unrecognized request argument supplied: parallel_tool_calls") == "parallel_tool_calls"
    assert T.unsupported_param('{"error": "stream_options is not supported"}') == "stream_options"
    assert T.unsupported_param("rate limited") is None


def test_parse_usage_variants():
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 8})["hit"] == 8
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 2,
                        "prompt_tokens_details": {"cached_tokens": 4}})["hit"] == 4
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 2})["hit"] is None


def test_accumulate_tool_call_quirks():
    calls = []
    _accumulate(calls, {"index": 0, "id": "a", "function": {"name": "read", "arguments": '{"path":'}})
    _accumulate(calls, {"index": 0, "function": {"arguments": '"x"}'}})
    # 中转站在同一 index 上塞第二个完整调用
    _accumulate(calls, {"index": 0, "id": "b", "function": {"name": "grep", "arguments": '{"pattern":"y"}'}})
    # 没有 index 的网关
    _accumulate(calls, {"id": "c", "function": {"name": "glob", "arguments": '{"pattern":"*"}'}})
    assert [c["function"]["name"] for c in calls] == ["read", "grep", "glob"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "x"}


def test_profiles_detection():
    from ee.core.providers.profiles import detect_family
    cases = {"deepseek-flash": "deepseek", "deepseek-v4-pro": "deepseek", "qwen3-coder-plus": "qwen",
             "kimi-k2-turbo": "kimi", "moonshot-v1-8k": "kimi", "glm-4.6": "glm", "MiniMax-M2": "minimax",
             "doubao-seed-1-6": "doubao", "gpt-5": "gpt", "openai/o3": "gpt", "claude-sonnet-4": "claude",
             "llama-3": "default"}
    for m, fam in cases.items():
        assert detect_family(m) == fam, m
    p = profile_for("deepseek-flash", overrides={"context_window": 5, "price": {"input": 1.0}})
    assert p.context_window == 5 and p.price.input == 1.0 and p.price.output == 8.0
