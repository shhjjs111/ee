"""edit 多处编辑、参数容错、报错信息、符号归一、编码/换行保持、并发写排队。"""
import asyncio
import json

import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools import fileops
from ee.tools.edit import EditTool
from ee.tools.fileops import Edit, apply_edits, closest_window, parse_edits
from ee.tools.read import ReadTool

SRC = "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"


def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def _prep(ws, name="m.py", content=SRC, raw: bytes | None = None):
    p = ws / name
    p.write_bytes(raw if raw is not None else content.encode("utf-8"))
    ctx = _ctx(ws)
    run(ReadTool().run({"path": name}, ctx))
    return p, ctx


# ------------------------------------------------------------------ 多处

def test_multi_edit_success(workspace):
    p, ctx = _prep(workspace)
    r = run(EditTool().run({"path": "m.py", "edits": [
        {"old_string": "return 1", "new_string": "return 10"},
        {"old_string": "return 3", "new_string": "return 30"},
    ]}, ctx))
    assert r.ok and "替换 2 处" in r.output
    text = p.read_text(encoding="utf-8")
    assert "return 10" in text and "return 30" in text and "return 2\n" in text
    assert "+return 10" not in r.diff and "+    return 10" in r.diff and "+    return 30" in r.diff
    assert r.meta["edits"] == 2
    assert str(p) in ctx.state.snapshots[-1] and ctx.state.snapshots[-1][str(p)] == SRC.encode()


def test_edits_match_against_original_not_sequential():
    # 第二处的 old 是原文里的内容；第一处的 new 里虽然也出现了它，但不影响定位
    out, hits = apply_edits("x = 1\ny = 2\n", [Edit("x = 1", "x = 1\ny = 3"), Edit("y = 2", "y = 4")])
    assert out == "x = 1\ny = 3\ny = 4\n" and len(hits) == 2


def test_overlap_rejected():
    with pytest.raises(ToolError, match=r"edits\[0\].*edits\[1\].*重叠"):
        apply_edits(SRC, [Edit("def a():\n    return 1", "X"), Edit("return 1\n\n\ndef b", "Y")])
    with pytest.raises(ToolError, match="重叠"):
        apply_edits(SRC, [Edit("return 1", "A"), Edit("return 1", "B")])


def test_partial_failure_is_atomic(workspace):
    p, ctx = _prep(workspace)
    with pytest.raises(ToolError) as ei:
        run(EditTool().run({"path": "m.py", "edits": [
            {"old_string": "return 1", "new_string": "return 10"},
            {"old_string": "return 99", "new_string": "return 0"},
            {"old_string": "def", "new_string": "fn"},
        ]}, ctx))
    msg = str(ei.value)
    assert "edits[1]" in msg and "edits[2]" in msg and "edits[0]" not in msg
    assert "没有写入任何改动" in msg
    assert p.read_bytes() == SRC.encode()
    assert str(p) not in ctx.state.snapshots[-1]


def test_no_change_rejected():
    with pytest.raises(ToolError, match="相同"):
        apply_edits(SRC, [Edit("return 1", "return 1")])


# ------------------------------------------------------------------ 参数格式

@pytest.mark.parametrize("args", [
    {"edits": [{"old_string": "return 2", "new_string": "return 20"}]},
    {"edits": json.dumps([{"old_string": "return 2", "new_string": "return 20"}])},
    {"edits": {"old_string": "return 2", "new_string": "return 20"}},
    {"edits": json.dumps({"old_string": "return 2", "new_string": "return 20"})},
    {"old_string": "return 2", "new_string": "return 20"},
    {"edits": [{"oldText": "return 2", "newText": "return 20"}]},
])
def test_argument_formats(workspace, args):
    p, ctx = _prep(workspace)
    r = run(EditTool().run({"path": "m.py", **args}, ctx))
    assert r.ok and "return 20" in p.read_text(encoding="utf-8")


def test_legacy_replace_all_and_bad_args():
    es = parse_edits({"old_string": "a", "new_string": "b", "replace_all": "true"})
    assert es == [Edit("a", "b", True)]
    with pytest.raises(ToolError, match="JSON"):
        parse_edits({"edits": "[{oops"})
    with pytest.raises(ToolError, match="缺少 edits"):
        parse_edits({})
    with pytest.raises(ToolError, match=r"edits\[0\] 缺少 new_string"):
        parse_edits({"edits": [{"old_string": "a"}]})
    assert parse_edits({"edits": [{"old_string": "a", "new_string": ""}]})[0].new == ""   # 删除


# ------------------------------------------------------------------ 报错信息

def test_not_found_shows_closest_window():
    lines = [f"line {i}" for i in range(1, 41)]
    lines[19] = "    total = price * qty + tax"
    content = "\n".join(lines) + "\n"
    old = "line 19\n    total = price * qty + fee\nline 21\nline 2"   # 首尾锚点也对不上，必须报错
    with pytest.raises(ToolError) as ei:
        apply_edits(content, [Edit(old, "z")])
    msg = str(ei.value)
    assert "最接近的是第 19-22 行" in msg
    assert "从第 20 行开始不一致" in msg
    assert "你写的：    total = price * qty + fee" in msg and "文件里：    total = price * qty + tax" in msg
    assert "20\t    total = price * qty + tax" in msg
    assert "line 30" not in msg


def test_closest_window_caps_at_15_lines():
    content = "\n".join(f"v{i} = {i}" for i in range(100)) + "\n"
    old = "\n".join(f"v{i} = {i}" for i in range(50, 80)).replace("v70 = 70", "v70 = 7").replace("v79 = 79", "v79 = 0")
    with pytest.raises(ToolError) as ei:
        apply_edits(content, [Edit(old, "z")])
    msg = str(ei.value)
    shown = [l for l in msg.splitlines() if "\t" in l]
    assert len(shown) <= 15 and any(l.endswith("v70 = 70") for l in shown)
    assert "第 51-80 行" in msg and "从第 71 行开始不一致" in msg


def test_closest_window_when_every_line_differs_and_nothing_close():
    content = "def compute(a, b):\n    return a+b\n"
    w = closest_window(content, "def compute(a, c):\n    return a+c\n")
    assert w is not None and w[0] == 0 and w[1] == 2
    with pytest.raises(ToolError) as ei:
        apply_edits(content, [Edit("完全无关的中文内容，一行都对不上", "x")])
    assert "没有相近的内容" in str(ei.value)


def test_not_unique_lists_line_numbers():
    content = "a\nfoo()\nb\nfoo()\nc\nfoo()\n"
    with pytest.raises(ToolError) as ei:
        apply_edits(content, [Edit("foo()", "bar()")])
    assert "3 处" in str(ei.value) and "第 2、4、6 行" in str(ei.value)
    out, hits = apply_edits(content, [Edit("foo()", "bar()", True)])
    assert out.count("bar()") == 3 and len(hits[0].spans) == 3


def test_output_is_short(workspace):
    _, ctx = _prep(workspace)
    r = run(EditTool().run({"path": "m.py", "edits": [{"old_string": "return 2", "new_string": "return 5"}]}, ctx))
    assert "\n" not in r.output and len(r.output) < 200 and "+1 -1" in r.output


# ------------------------------------------------------------------ 符号归一

def test_unicode_normalized_match_preserves_untouched_lines():
    content = "他说：“你好”——再见\n价格 100\n“保持”\u00a0不变\n"
    old = '他说："你好"--再见\n价格 100\n"保持" 不变'
    new = '他说："你好"--再见\n价格 200\n"保持" 不变'
    out, hits = apply_edits(content, [Edit(old, new)])
    assert hits[0].strategy == "unicode"
    # 没改的行照抄原文（弯引号、破折号、特殊空格都在）；改了的那行用新文本
    assert out == "他说：“你好”——再见\n价格 200\n“保持”\u00a0不变\n"


def test_unicode_changed_text_itself_differs_rejected():
    # 要改的正是含弯引号的那段：不知道改后用哪种引号，报错并给出原文，文件不动
    content = "x = ‘a’ + ’b’  \ny = 1\n"
    with pytest.raises(ToolError, match="‘a’"):
        apply_edits(content, [Edit("'a' + 'b'", "'c'")])


def test_fullwidth_nfkc(workspace):
    p, ctx = _prep(workspace, "hw.md", "答案：（１）ＡＢＣ，共３项\n分数：90\n")
    r = run(EditTool().run({"path": "hw.md", "edits": [{"old_string": "答案：(1)ABC,共3项\n分数：90",
                                                         "new_string": "答案：(1)ABC,共3项\n分数：95"}]}, ctx))
    assert r.ok and "符号归一" in r.output
    assert p.read_text(encoding="utf-8") == "答案：（１）ＡＢＣ，共３项\n分数：95\n"


# ------------------------------------------------------------------ 换行与编码

def test_crlf_multi_edit_keeps_crlf(workspace):
    raw = "a = 1\r\nb = 2\r\nc = 3\r\n".encode()
    p, ctx = _prep(workspace, "w.py", raw=raw)
    r = run(EditTool().run({"path": "w.py", "edits": [
        {"old_string": "a = 1\r\nb = 2", "new_string": "a = 9\r\nb = 8"},
        {"old_string": "c = 3\n", "new_string": "c = 7\nd = 4\n"},
    ]}, ctx))
    assert r.ok
    assert p.read_bytes() == b"a = 9\r\nb = 8\r\nc = 7\r\nd = 4\r\n"


def test_gbk_and_bom_preserved(workspace):
    p, ctx = _prep(workspace, "g.txt", raw="标题\r\n内容一\r\n".encode("gbk"))
    run(EditTool().run({"path": "g.txt", "edits": [{"old_string": "内容一", "new_string": "内容二"}]}, ctx))
    assert p.read_bytes() == "标题\r\n内容二\r\n".encode("gbk")
    with pytest.raises(ToolError, match="GBK"):
        run(EditTool().run({"path": "g.txt", "edits": [{"old_string": "内容二", "new_string": "内容😀"}]}, ctx))
    assert p.read_bytes() == "标题\r\n内容二\r\n".encode("gbk")
    q, ctx2 = _prep(workspace, "b.txt", raw="\ufeffhello\n".encode("utf-8"))
    run(EditTool().run({"path": "b.txt", "old_string": "hello", "new_string": "hi"}, ctx2))
    assert q.read_bytes() == "\ufeffhi\n".encode("utf-8")


def test_undecodable_file_refused(workspace):
    raw = b"ok\n\xff\xfe\x80 bad\n"
    p, ctx = _prep(workspace, "x.bin.txt", raw=raw)
    with pytest.raises(ToolError, match="编码"):
        run(EditTool().run({"path": "x.bin.txt", "edits": [{"old_string": "ok", "new_string": "ko"}]}, ctx))
    assert p.read_bytes() == raw


def test_requires_read_still(workspace):
    (workspace / "n.py").write_text("a\n", encoding="utf-8")
    with pytest.raises(ToolError, match="read"):
        run(EditTool().run({"path": "n.py", "edits": [{"old_string": "a", "new_string": "b"}]}, _ctx(workspace)))


# ------------------------------------------------------------------ 并发写排队

def test_parallel_edits_same_file_are_serialized(workspace, monkeypatch):
    p = workspace / "c.py"
    p.write_text("a = 1\nb = 2\n", encoding="utf-8")
    ctx1, ctx2 = _ctx(workspace), _ctx(workspace)
    run(ReadTool().run({"path": "c.py"}, ctx1))
    run(ReadTool().run({"path": "c.py"}, ctx2))
    real = fileops.read_text
    inside = {"now": 0, "max": 0}

    def slow_read(path):
        inside["now"] += 1
        inside["max"] = max(inside["max"], inside["now"])
        import time
        time.sleep(0.05)   # 放大读-写之间的窗口：没有锁时两个调用都读到旧内容，后写的覆盖先写的
        inside["now"] -= 1
        return real(path)

    monkeypatch.setattr("ee.tools.edit.read_text", slow_read)

    async def both():
        return await asyncio.gather(
            EditTool().run({"path": "c.py", "edits": [{"old_string": "a = 1", "new_string": "a = 10"}]}, ctx1),
            EditTool().run({"path": "c.py", "edits": [{"old_string": "b = 2", "new_string": "b = 20"}]}, ctx2),
        )

    r1, r2 = run(both())
    assert r1.ok and r2.ok
    assert inside["max"] == 1
    assert p.read_text(encoding="utf-8") == "a = 10\nb = 20\n"


def test_path_lock_is_per_path(workspace):
    async def go():
        a = fileops.path_lock(workspace / "x.py")
        b = fileops.path_lock(workspace / "sub" / ".." / "x.py")
        c = fileops.path_lock(workspace / "y.py")
        return a is b, a is c
    same, other = run(go())
    assert same and not other


# ------------------------------------------------------------------ 走完整会话（假模型）

def test_session_parallel_edit_calls_and_arg_formats(ee_home, workspace):
    from conftest import collect, finish, make_session, text_chunks, tool_chunks
    p = workspace / "s.py"
    p.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\nd = 4\r\n")
    script = [
        tool_chunks(0, "r1", "read", {"path": "s.py"}) + finish("tool_calls"),
        # 同一步里对同一文件发三个 edit，三种参数写法
        tool_chunks(0, "e1", "edit", {"path": "s.py", "edits": json.dumps([{"old_string": "a = 1", "new_string": "a = 10"}])})
        + tool_chunks(1, "e2", "edit", {"path": "s.py", "edits": {"old_string": "b = 2", "new_string": "b = 20"}})
        + tool_chunks(2, "e3", "edit", {"path": "s.py", "old_string": "c = 3", "new_string": "c = 30"})
        + finish("tool_calls"),
        tool_chunks(0, "e4", "edit", {"path": "s.py", "edits": [{"old_string": "d = 5", "new_string": "d = 6"},
                                                                {"old_string": "a = 10", "new_string": "a = 0"}]})
        + finish("tool_calls"),
        text_chunks("好了") + finish("stop"),
    ]
    s, fake = make_session(workspace, script)
    run(collect(s, "改一下"))
    assert p.read_bytes() == b"a = 10\r\nb = 20\r\nc = 30\r\nd = 4\r\n"   # e4 一处失败，整体没写
    tool_msgs = {m["tool_call_id"]: m["content"] for m in fake.calls[-1]["messages"] if m.get("role") == "tool"}
    assert all("已编辑" in tool_msgs[k] for k in ("e1", "e2", "e3"))
    assert "edits[0]" in tool_msgs["e4"] and "最接近的是第 4 行" in tool_msgs["e4"] and "没有写入任何改动" in tool_msgs["e4"]
    edit_schema = next(t for t in fake.calls[0]["tools"] if t["function"]["name"] == "edit")
    assert edit_schema["function"]["parameters"]["required"] == ["path", "edits"]
