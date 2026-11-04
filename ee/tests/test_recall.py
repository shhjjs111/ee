"""recall 工具：检索被压缩掉的旧对话原文。

守的是「压缩后信息黑洞」这一条：压缩把 history 整段换成摘要 + 保留段，摘要的标识符
覆盖率实测只有 8.6%（目标 35%）。被压掉的原文其实还在会话 JSONL 的 item 里，也进了
search.db，缺的是给模型用的检索入口。
"""
import asyncio

import pytest

from ee.core.search_index import SearchIndex
from ee.core.store import SessionStore
from ee.tools.base import ToolContext, ToolError, ToolState
from ee.tools.recall import RecallTool


def _ctx(tmp_path, home, thread_id=None):
    return ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path,
                       extra={"home": home, "thread_id": thread_id})


def _session(home, sid, cwd, items, title=None):
    s = SessionStore(home, session_id=sid)
    s.meta({"cwd": str(cwd), "title": title or ""})
    for i, it in enumerate(items):
        s.item({"seq": i, **it})
    return s


def _run(tool, args, ctx):
    return asyncio.run(tool.run(args, ctx))


def _close(home):
    """SearchIndex 按 home 缓存连接，测试间必须显式关，否则临时目录删不掉（Windows）。"""
    idx = SearchIndex(home)
    idx.close()


def test_recall_finds_compacted_tool_output(ee_home, workspace):
    """压缩前的工具输出（原文）要能被召回：这是摘要吃掉的细节的唯一退路。"""
    _session(ee_home, "sess1", workspace, [
        {"id": "u1", "type": "user", "text": "跑一下并发测试"},
        {"id": "t1", "type": "tool", "name": "shell", "summary": "运行 pytest",
         "output": "ee/tests/test_server.py: async def wait_for(events, method, timeout=30.0)"},
    ])
    ctx = _ctx(workspace, ee_home, thread_id="sess1")
    res = _run(RecallTool(), {"query": "wait_for"}, ctx)
    assert "wait_for" in res.output
    assert "test_server.py" in res.output
    assert res.meta["kind"] == "recall"
    _close(ee_home)


def test_recall_is_scoped_to_the_current_thread(ee_home, workspace):
    """默认只查本会话：别的会话命中更多时也不能把本会话挤掉（全局截断那个坑）。"""
    for i in range(6):
        _session(ee_home, f"noise{i}", workspace, [
            {"id": "t1", "type": "tool", "name": "shell", "summary": "", "output": "wait_for " * 20},
        ])
    _session(ee_home, "mine", workspace, [
        {"id": "t1", "type": "tool", "name": "shell", "summary": "", "output": "本会话独有的 wait_for 片段"},
    ])
    ctx = _ctx(workspace, ee_home, thread_id="mine")
    res = _run(RecallTool(), {"query": "wait_for"}, ctx)
    # 噪声会话里有大量同名命中，但结果必须来自本会话
    assert "本会话独有" in res.output
    assert res.meta["scope"] == "thread"
    _close(ee_home)


def test_recall_falls_back_to_all_threads_without_context(ee_home, workspace):
    """CLI / 单跑没有 thread_id：退回全局检索，而不是报错。"""
    _session(ee_home, "s1", workspace, [
        {"id": "t1", "type": "tool", "name": "shell", "summary": "", "output": "唯一命中 unique_marker"},
    ])
    res = _run(RecallTool(), {"query": "unique_marker"}, _ctx(workspace, ee_home, thread_id=None))
    assert "unique_marker" in res.output
    assert res.meta["scope"] == "all"
    _close(ee_home)


def test_recall_empty_query_raises(ee_home, workspace):
    with pytest.raises(ToolError):
        _run(RecallTool(), {"query": "   "}, _ctx(workspace, ee_home, thread_id="s1"))


def test_recall_no_hit_is_a_normal_result(ee_home, workspace):
    """没找到是正常结果（附上下一步建议），不是异常——否则会把回合打红。"""
    _session(ee_home, "s1", workspace, [{"id": "u1", "type": "user", "text": "你好"}])
    res = _run(RecallTool(), {"query": "绝不可能出现的词zzz"}, _ctx(workspace, ee_home, thread_id="s1"))
    assert "没有找到" in res.output
    assert res.meta["hits"] == 0
    _close(ee_home)


def test_recall_supports_chinese(ee_home, workspace):
    """中文必须能召回（unicode61 分词对中文完全失效，索引用的是 trigram）。"""
    _session(ee_home, "s1", workspace, [
        {"id": "t1", "type": "tool", "name": "shell", "summary": "", "output": "实测 16 路并发会把超时打红"},
    ])
    res = _run(RecallTool(), {"query": "并发"}, _ctx(workspace, ee_home, thread_id="s1"))
    assert "并发" in res.output
    _close(ee_home)


def test_recall_is_read_only_and_registered(ee_home, workspace):
    """只读工具：只读/计划模式也放行；且默认注册进工具表。"""
    from ee.core.permissions import READ_TOOLS, PermissionPolicy
    from ee.tools import build_tools

    assert RecallTool.read_only is True
    assert "recall" in READ_TOOLS
    tools = build_tools(None, None, agent="full")
    assert "recall" in [t.name for t in tools]
    # 只读模式：recall 不写文件，不该被拦
    pol = PermissionPolicy(mode="plan")
    assert pol.check("recall", {"query": "x"}).action == "allow"


def test_recall_keeps_originating_session_visible_globally(ee_home, workspace):
    """limit 截断不能在聚合前生效：本会话命中即使全局排名靠后也要能被会话内检索到。"""
    for i in range(8):
        _session(ee_home, f"buzz{i}", workspace, [
            {"id": "t1", "type": "tool", "name": "shell", "summary": "",
             "output": ("shared_token " * 40)},
        ])
    _session(ee_home, "target", workspace, [
        {"id": "t1", "type": "tool", "name": "shell", "summary": "", "output": "shared_token 在这里"},
    ])
    idx = SearchIndex(ee_home)
    idx.sync()
    hits = idx.search("shared_token", limit=3, session_id="target")
    assert len(hits) == 1 and hits[0]["thread_id"] == "target"
    assert hits[0]["hits"], "会话内检索被别的会话挤成了空结果"
    idx.close()
