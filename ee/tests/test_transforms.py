import json

from ee.core.providers import transforms as T
from ee.core.providers.openai_chat import _accumulate, parse_usage
from ee.core.providers.profiles import profile_for

KNOWN = ["read", "write", "edit", "shell", "grep", "glob", "todo"]


def test_think_splitter_across_chunks():
    sp = T.ThinkSplitter()
    out = []
    for ch in ["<thi", "nk>想一想", "，再想</th", "ink>正文"]:
        out += sp.feed(ch)
    out += sp.flush()
    text = "".join(p for k, p in out if k == "text")
    reasoning = "".join(p for k, p in out if k == "reasoning")
    assert text == "正文" and reasoning == "想一想，再想"


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
    assert w[3]["reasoning_content"] == "r2" and "reasoning" not in w[3]   # DeepSeek：所有助手轮次回传
    assert hist[3]["reasoning"] == "r2"                                     # 原历史不被修改
    gpt = profile_for("gpt-5")
    w2 = T.wire_messages(hist, gpt)
    assert w2[1]["content"] is None and "reasoning_content" not in w2[1]
    assert T.wire_messages(hist, ds) == w                                   # 确定性


# ---------------------------------------------------------------- 大字段省略（elide）

def _edit_call(path, old, new, cid="c1"):
    import json as _j
    return {"id": cid, "type": "function", "function": {
        "name": "edit", "arguments": _j.dumps({"path": path, "edits": [
            {"old_string": old, "new_string": new}]}, ensure_ascii=False)}}


def test_elide_big_args_keeps_small_fields():
    """小改动本身就是要逐字复用的上下文，不能裁。"""
    tc = _edit_call("a.py", "x = 1", "x = 2")
    out = T.elide_big_args([tc], 150)
    assert out[0]["function"]["arguments"] == tc["function"]["arguments"]


def test_elide_big_args_replaces_large_fields_with_pointer():
    import json as _j
    old, new = "a" * 300, "b" * 300
    out = T.elide_big_args([_edit_call("src/a.py", old, new)], 150)
    args = _j.loads(out[0]["function"]["arguments"])
    ed = args["edits"][0]
    assert old not in ed["old_string"] and new not in ed["new_string"]
    assert "src/a.py" in ed["old_string"] and "read" in ed["old_string"]   # 必须指向文件与 read
    assert "1 行已省略" in ed["old_string"]                              # 单行也是 1 行，占位带行数
    assert args["path"] == "src/a.py"                                      # 路径本身保留


def test_elide_big_args_is_deterministic_and_non_destructive():
    """位置无关、同样输入同样输出 → 每条消息首次发送即终态，缓存零断点。"""
    import json as _j
    tc = _edit_call("a.py", "x" * 400, "y" * 400)
    before = _j.dumps(tc, ensure_ascii=False, sort_keys=True)
    a = T.elide_big_args([tc], 150)
    b = T.elide_big_args([tc], 150)
    assert _j.dumps(a, ensure_ascii=False, sort_keys=True) == _j.dumps(b, ensure_ascii=False, sort_keys=True)
    assert _j.dumps(tc, ensure_ascii=False, sort_keys=True) == before      # 原调用没被改


def test_elide_big_args_write_content_and_bad_json():
    import json as _j
    big = "w" * 500
    write = {"id": "w1", "type": "function", "function": {
        "name": "write", "arguments": _j.dumps({"path": "b.py", "content": big}, ensure_ascii=False)}}
    out = T.elide_big_args([write], 150)
    assert big not in out[0]["function"]["arguments"]
    # 解析不了的 arguments 原样返回：宁可多发也不能弄坏请求
    bad = {"id": "x", "type": "function", "function": {"name": "edit", "arguments": "{not json"}}
    assert T.elide_big_args([bad], 150)[0] is bad
    # 非 edit/write 的工具不碰
    read = {"id": "r", "type": "function", "function": {
        "name": "read", "arguments": _j.dumps({"path": "c" * 500})}}
    assert T.elide_big_args([read], 150)[0] is read


def test_wire_messages_elide_only_when_enabled():
    """elide_min_chars=0（默认关）时一个字节都不动。"""
    ds = profile_for("deepseek-flash")
    hist = [{"role": "assistant", "content": "", "tool_calls": [
        _edit_call("a.py", "x" * 400, "y" * 400)]}]
    assert T.wire_messages(hist, ds)[0]["tool_calls"] == hist[0]["tool_calls"]
    cut = T.wire_messages(hist, ds, -1, 150)[0]["tool_calls"]
    assert "x" * 400 not in json.dumps(cut, ensure_ascii=False)
    assert hist[0]["tool_calls"][0]["function"]["arguments"].count("x") == 400   # 历史未动


def test_elide_min_chars_reads_config():
    assert T.elide_min_chars(None) == 150                 # 默认值
    assert T.elide_min_chars({"elide_args": {"enabled": False}}) == 0
    assert T.elide_min_chars({"elide_args": {"min_chars": 40}}) == 40
    assert T.elide_min_chars({"elide_args": {"min_chars": "x"}}) == 150   # 坏值回退


def test_elided_placeholder_always_shorter_than_source():
    """占位自己也要花 token。阈值边界上原文只比阈值多一个字符时，占位必须仍然更短，
    否则这条裁剪就是纯亏（换来的是丢信息 + 多花钱）。实测旧的长占位在 150~200 字符档就吃过亏。"""
    path = "some/dir/file.py"
    for n in (150, 151, 200, 1500):
        text = "x" * n
        assert len(T._elided(text, path)) < n
    # 占位里必须有路径和行数（否则模型不知道去哪找）
    ph = T._elided("a\nb\nc", path)
    assert path in ph and "3" in ph


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


# ---------------------------------------------------------------- 前缀缓存：id 必须确定性
#
# 这两个 bug 的共同特征：命中率莫名掉一截，但没有任何报错或外显信号。
# 原因是 wire_messages / _mk_call 每次请求都会重跑，用 uuid4() 生成的 id 每次都不同，
# 从该消息起的前缀字节全变 —— 服务端只能整段重算。实测用户日志里出现过单回合
# miss/req 高达 13,000-16,000 token 的异常回合。

def test_wire_messages_tool_id_is_deterministic():
    """缺 tool_call_id 的 tool 消息：两次调用必须得到同一个 id（否则缓存永久失效）。"""
    hist = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "type": "function",
                                                             "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "content": "文件内容很长" * 100},      # 故意不带 tool_call_id
    ]
    ds = T.PROFILE if hasattr(T, "PROFILE") else None
    prof = ds or __import__("ee.core.providers.profiles", fromlist=["x"]).Profile()
    first = T.wire_messages(hist, prof)
    second = T.wire_messages(hist, prof)
    assert first[1]["tool_call_id"] == second[1]["tool_call_id"], (
        f"两次生成的 id 不同（{first[1]['tool_call_id']} vs {second[1]['tool_call_id']}）——"
        "前缀缓存会永久失效")


def test_wire_messages_tool_id_differs_for_different_content():
    """确定性不等于所有消息共用一个 id：不同内容的占位 id 要能区分。"""
    from ee.core.providers.profiles import Profile
    prof = Profile()
    a = T.wire_messages([{"role": "tool", "content": "AAA"}], prof)[0]["tool_call_id"]
    b = T.wire_messages([{"role": "tool", "content": "BBB"}], prof)[0]["tool_call_id"]
    assert a != b


def test_mk_call_id_is_deterministic():
    """兜底解析（模型把工具调用写进正文）生成的 id 也必须确定性。"""
    c1 = T._mk_call("read", {"path": "a.py"})
    c2 = T._mk_call("read", {"path": "a.py"})
    assert c1["id"] == c2["id"], "同一调用两次解析得到不同 id —— 历史字节每次都在变"
    assert c1["id"] != T._mk_call("read", {"path": "b.py"})["id"]
