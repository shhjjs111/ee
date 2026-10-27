"""A6：工具结果拆成给模型的 content 与只给界面的 details。

- details 是结构化数据（diff、文件列表、退出码、行范围、截断信息……），按工具类型有固定字段；
- details 不进模型上下文（history / 发给模型的请求里只有 content）；
- details 持久化到条目，重启后还在；旧会话里条目的 meta/diff 读取时迁成 details。
"""
import asyncio
import json
import sys

from conftest import FakeClient, collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.events import ToolEnd
from ee.tools.details import DETAIL_KINDS, finalize
from ee.tools.base import ToolResult

ECHO_BIG = ("python3 -c \"print('\\n'.join('line%d' % i for i in range(5000)))\""
            if sys.platform != "win32" else
            "python -c \"print('\\n'.join('line%d' % i for i in range(5000)))\"")


def _ends(events):
    return {e.call_id: e for e in events if isinstance(e, ToolEnd)}


def _wire(fake) -> str:
    return json.dumps([c["messages"] for c in fake.calls], ensure_ascii=False)


def test_toolresult_meta_alias():
    r = ToolResult("x", meta={"kind": "read"})
    assert r.details == {"kind": "read"} and r.meta is r.details
    r2 = ToolResult("y", details={"kind": "shell", "exit": 0})
    assert r2.meta["exit"] == 0
    assert ToolResult("z").details == {}


def test_edit_details_have_diff_and_files_not_sent_to_model(ee_home, workspace):
    (workspace / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.py"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "edit", {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3  # MARKER"})
        + finish("tool_calls"),
        text_chunks("改好了") + finish("stop"),
    ], mode="auto")
    ev = run(collect(s, "改一下"))
    ends = _ends(ev)
    rd, ed = ends["c1"].details, ends["c2"].details
    # read：路径 + 行范围
    assert rd["kind"] == "read" and rd["path"] == "a.py"
    assert rd["start"] == 1 and rd["end"] == 2 and rd["total_lines"] == 2
    # edit：文件列表 + 统计 + diff 都在 details
    assert ed["kind"] == "edit"
    assert ed["files"] == [{"verb": "Edited", "path": "a.py", "added": 1, "removed": 1}]
    assert ed["added"] == 1 and ed["removed"] == 1
    assert "+y = 3  # MARKER" in ed["diff"]
    assert isinstance(ed["duration_ms"], int)
    # 模型只看到 content：没有 diff 文本，也没有任何 details 字段
    tool_msgs = [m for m in s.history if m["role"] == "tool"]
    assert all(set(m) <= {"role", "tool_call_id", "content", "_pruned"} for m in tool_msgs)
    wire = _wire(fake)
    assert "+y = 3" not in wire and "duration_ms" not in wire and '"details"' not in wire
    assert "替换 1 处" in tool_msgs[-1]["content"]


def test_shell_details_exit_code_and_truncation(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "shell", {"command": ECHO_BIG}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ], mode="auto")
    ev = run(collect(s, "跑一下"))
    d = _ends(ev)["c1"].details
    assert d["kind"] == "shell" and d["exit"] == 0 and d["timed_out"] is False
    assert d["command"] == ECHO_BIG
    tr = d["truncated"]
    assert tr["lines"] >= 5000 and tr["bytes"] > 0
    assert tr["path"] and open(tr["path"], encoding="utf-8").read().count("line") == 5000
    # 模型拿到的是截断后的 content（带全文路径），不是 details
    content = [m for m in s.history if m["role"] == "tool"][0]["content"]
    assert "输出过长已截断" in content and "[exit 0" in content
    assert '"truncated"' not in _wire(fake)


def test_search_and_glob_details_list_files(ee_home, workspace):
    (workspace / "a.py").write_text("hello\n", encoding="utf-8")
    (workspace / "b.py").write_text("hello world\n", encoding="utf-8")
    s, _ = make_session(workspace, [
        tool_chunks(0, "c1", "grep", {"pattern": "hello"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "glob", {"pattern": "*.py"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    ends = _ends(run(collect(s, "找")))
    g, l = ends["c1"].details, ends["c2"].details
    assert g["kind"] == "search" and g["matches"] == 2 and sorted(g["files"]) == ["a.py", "b.py"]
    assert l["kind"] == "list" and sorted(l["files"]) == ["a.py", "b.py"] and l["total"] == 2


def test_finalize_fills_kind_and_error_flags():
    d = finalize("mcp__x__y", ToolResult("boom", ok=False), duration_ms=5, truncation=None)
    assert d["kind"] == "tool" and d["duration_ms"] == 5
    d = finalize("write", ToolResult("ok", diff="--- a\n+++ b\n+1\n",
                                     meta={"kind": "edit", "verb": "Created", "path": "n.txt", "added": 1,
                                           "removed": 0}), duration_ms=1, truncation=None)
    assert d["files"] == [{"verb": "Created", "path": "n.txt", "added": 1, "removed": 0}]
    assert d["diff"].startswith("--- a")
    assert {"edit", "shell", "read", "list", "search"} <= set(DETAIL_KINDS)


def test_details_persisted_on_item_and_legacy_meta_migrated(ee_home, workspace):
    from test_server import make_backend, wait_for

    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = [
        tool_chunks(0, "c0", "read", {"path": "a.py"}) + finish("tool_calls"),
        tool_chunks(0, "c1", "edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})
        + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "改"})
        await wait_for(ev, "turn/completed")
        upd = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "tool"][-1]
        assert upd["details"]["kind"] == "edit" and "+x = 2" in upd["details"]["diff"]
        assert "meta" not in upd and not upd.get("diff")
        # 新进程重新打开：details 从磁盘恢复
        b2, _, _ = make_backend(ee_home, [])
        items = (await b2.call("thread/get", {"thread_id": tid}))["thread"]["items"]
        tool = [it for it in items if it["type"] == "tool"][-1]
        assert tool["details"]["files"][0]["path"] == "a.py" and "+x = 2" in tool["details"]["diff"]

    run(go())


def test_legacy_item_meta_becomes_details(ee_home, workspace):
    from ee.server.threads import Thread
    legacy = [{"id": "tool_c1", "seq": 1, "type": "tool", "name": "edit", "status": "ok",
               "meta": {"kind": "edit", "path": "a.py", "verb": "Edited", "added": 1, "removed": 0},
               "diff": "+x\n"}]
    t = Thread("t1", ee_home, lambda m, p: None, {"cwd": str(workspace)}, items=legacy)
    it = t.items[0]
    assert it["details"]["kind"] == "edit" and it["details"]["diff"] == "+x\n"
    assert "meta" not in it and "diff" not in it
