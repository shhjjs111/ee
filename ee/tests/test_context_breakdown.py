"""A12：上下文构成视图（thread/context_breakdown）。

各部分（系统提示/工具定义/AGENTS.md/记忆/技能/历史消息/工具输出/图片……）各多少 token、占比，修剪/压缩节省；
查看本身不改任何状态（不冻结前缀、不动历史），前缀缓存不受影响。
"""
import json
from types import SimpleNamespace

from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core import breakdown as B
from ee.core import compaction as C


def _items(d):
    return {x["key"]: x for x in d["items"]}


def test_compute_categories_and_percent():
    hist = [
        {"role": "user", "content": "规则" * 50, "_kind": "agents"},
        {"role": "user", "content": "记忆" * 30, "_kind": "agents", "_sub": "memory"},
        {"role": "user", "content": "技能" * 20, "_kind": "agents", "_sub": "skills"},
        {"role": "user", "content": "看图", "_images": ["/x/a.png", "/x/b.png"]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 4000},
        {"role": "assistant", "content": "好的"},
    ]
    prof = SimpleNamespace(vision=True, reasoning_passback="none", empty_assistant_content="")
    tools = [{"type": "function", "function": {"name": "read", "description": "读文件", "parameters": {}}},
             {"type": "function", "function": {"name": "shell", "description": "运行命令" * 30, "parameters": {}}}]
    stats = SimpleNamespace(pruned_tokens=123, prunes=1, compacted_tokens=456)
    d = B.compute("你是 ee", tools, hist, prof, 100_000, stats=stats)
    it = _items(d)
    assert [x["key"] for x in d["items"]] == ["system", "tools", "agents", "memory", "skills", "history",
                                               "tool_output", "images"]     # 没有的可选类不出现
    assert it["agents"]["label"] == "AGENTS.md" and it["memory"]["label"] == "记忆"
    assert it["agents"]["tokens"] == C.estimate_tokens("规则" * 50) + 4
    assert it["tool_output"]["tokens"] == C.estimate_tokens("x" * 4000) + 4
    assert it["images"]["tokens"] == 2 * B.IMAGE_TOKENS and d["images"] == 2
    assert d["total"] == sum(x["tokens"] for x in d["items"])
    assert abs(sum(x["percent"] for x in d["items"]) - 100) < 1
    assert d["tools"][0]["name"] == "shell"                                  # 按大小排序
    assert d["savings"] == {"pruned": 123, "prunes": 1, "pruned_outputs": 0, "compacted": 456}
    # 不看图的模型：图片降级成文字说明，不计图片 token
    d2 = B.compute("s", [], hist, SimpleNamespace(vision=False, reasoning_passback="none",
                                                 empty_assistant_content=""), 1000)
    assert _items(d2)["images"]["tokens"] == 0


def test_summary_and_repomap_shown_when_present():
    hist = [{"role": "user", "content": "地图", "_kind": "agents", "_sub": "repomap"},
            {"role": "user", "content": "摘要", "_kind": "summary"}]
    prof = SimpleNamespace(vision=False, reasoning_passback="none", empty_assistant_content="")
    keys = [x["key"] for x in B.compute("s", [], hist, prof, 1000)["items"]]
    assert "repomap" in keys and "summary" in keys


def test_session_breakdown_is_read_only(ee_home, workspace):
    (workspace / "AGENTS.md").write_text("项目规则：中文回答\n", encoding="utf-8")
    (workspace / "a.txt").write_text("内容\n" * 300, encoding="utf-8")
    s, fake = make_session(workspace, [])
    before = B.for_session(s)                       # 会话还没开始：不冻结前缀
    assert s.prefix is None and before["frozen"] is False and before["messages"] == 0
    assert _items(before)["system"]["tokens"] > 0 and _items(before)["tools"]["tokens"] > 0
    fake.script += [tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls", prompt=4000),
                    text_chunks("好") + finish("stop", prompt=4100, completion=20)]
    run(collect(s, "读一下"))
    sha, hist = s.prefix.sha256, json.dumps(s.history, ensure_ascii=False)
    d = B.for_session(s)
    assert s.prefix.sha256 == sha and json.dumps(s.history, ensure_ascii=False) == hist
    it = _items(d)
    assert it["agents"]["tokens"] > 0 and it["tool_output"]["tokens"] > 0 and it["history"]["tokens"] > 0
    assert d["frozen"] is True and d["actual"] is True and d["used"] == 4120
    assert d["window"] == s.profile.context_window and d["thresholds"]["compact"] == 0.8
    assert d["thresholds"]["prune"] is None             # 修剪默认关闭（会让 DeepSeek 缓存失效）
    assert {t["name"] for t in d["tools"]} >= {"read", "shell"}
    # 系统提示用的就是冻结的那份
    assert it["system"]["tokens"] == C.estimate_tokens(s.prefix.system["content"])


def test_rpc_context_breakdown(ee_home, workspace):
    from test_server import make_backend, wait_for

    async def go():
        b, _, ev = make_backend(ee_home, [text_chunks("好") + finish("stop", prompt=900, completion=5)])
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        d0 = await b.call("thread/context_breakdown", {"thread_id": tid})
        assert d0["frozen"] is False and d0["total"] > 0
        await b.call("turn/start", {"thread_id": tid, "text": "你好"})
        await wait_for(ev, "turn/completed")
        d = await b.call("thread/context_breakdown", {"thread_id": tid})
        assert d["used"] == 905 and d["actual"] and _items(d)["history"]["tokens"] > 0
        assert all({"key", "label", "tokens", "percent"} <= set(x) for x in d["items"])

    run(go())
