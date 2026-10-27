"""审查修复：edit 模糊匹配不得改错代码；工具规则不丢；SYSTEM.md 只换基础段；eval_profiles 在 Windows 可用。"""
import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.edit import EditTool
from ee.tools.fileops import Edit, apply_edits, replace
from ee.tools.read import ReadTool


def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def _prep(ws, name, raw: bytes):
    p = ws / name
    p.write_bytes(raw)
    ctx = _ctx(ws)
    run(ReadTool().run({"path": name}, ctx))
    return p, ctx


# ------------------------------------------------------------------ 1) 模糊匹配不改错

PY = "def f(x):\n    if x:\n        a = 1\n        b = 2\n    return a\n"


def test_dedented_old_string_keeps_original_indent():
    # 模型把缩进丢了：old/new 都顶格写。命中后必须按原文缩进对齐，不能把代码顶到行首
    out, strat, _ = replace(PY, "a = 1\nb = 2", "a = 10\nb = 2")
    assert out == "def f(x):\n    if x:\n        a = 10\n        b = 2\n    return a\n"
    assert strat == "indent"


def test_over_indented_old_string_shifts_back():
    out, _, _ = replace(PY, "            a = 1\n            b = 2", "            a = 1\n            c = 3\n            b = 2")
    assert out == "def f(x):\n    if x:\n        a = 1\n        c = 3\n        b = 2\n    return a\n"


def test_relative_indent_kept_in_new_lines():
    # new 里新增的嵌套行（相对缩进更深）也按同一偏移对齐
    out, _, _ = replace(PY, "if x:\n    a = 1", "if x:\n    if a:\n        a = 1")
    assert out == "def f(x):\n    if x:\n        if a:\n            a = 1\n        b = 2\n    return a\n"


def test_relative_indent_mismatch_is_rejected():
    # old 的相对缩进和原文不同（不是整体平移）：无法确定该怎么对齐，必须报错，文件不动
    with pytest.raises(ToolError):
        replace(PY, "if x:\na = 1", "if x:\na = 5")


def test_tabs_vs_spaces_nested_rejected():
    src = "func f() {\n\tif x {\n\t\ta := 1\n\t}\n}\n"
    with pytest.raises(ToolError):
        replace(src, "    if x {\n        a := 1", "    if x {\n        a := 2")


def test_misremembered_middle_line_not_overwritten():
    # 旧实现按首尾锚点“最接近”命中，再用模型记错的 old_string 覆盖原文的中间行
    src = "def h():\n    x = compute(1, 2, retries=3)\n    y = x + 1\n    return y\n"
    old = "def h():\n    x = compute(1, 2)\n    y = x + 1\n    return y"
    new = "def h():\n    x = compute(1, 2)\n    y = x + 2\n    return y"
    with pytest.raises(ToolError, match="最接近"):
        replace(src, old, new)


def test_nfkc_semantic_chars_not_folded():
    # 上标²、连字ﬁ 之类 NFKC 会折叠成别的字符，语义不同，不能算匹配
    with pytest.raises(ToolError):
        replace('area = "m²"\n', 'area = "m2"', 'area = "m3"')
    with pytest.raises(ToolError):
        replace("name = 'ﬁle'\n", "name = 'file'", "name = 'path'")


def test_changed_line_with_symbol_mismatch_errors_not_rewrites():
    # 要改的那一行本身和原文有全角/弯引号差异：不知道改后该用哪种符号，宁可报错并给出原文
    src = 'print("价格：１００")\nx = 1\n'
    with pytest.raises(ToolError, match="价格：１００"):
        replace(src, 'print("价格:100")', 'print("价格:200")')


def test_symbol_mismatch_on_context_line_keeps_original():
    src = '# 说明：“保持”\nx = 1\n'
    out, strat, _ = replace(src, '# 说明:"保持"\nx = 1', '# 说明:"保持"\nx = 2')
    assert strat == "unicode" and out == '# 说明：“保持”\nx = 2\n'


def test_indent_fix_crlf_bom_file(workspace):
    raw = "\ufeffclass A:\r\n    def g(self):\r\n        return 1\r\n".encode("utf-8")
    p, ctx = _prep(workspace, "a.py", raw)
    r = run(EditTool().run({"path": "a.py", "edits": [{"old_string": "def g(self):\n    return 1",
                                                        "new_string": "def g(self):\n    return 2"}]}, ctx))
    assert r.ok
    assert p.read_bytes() == "\ufeffclass A:\r\n    def g(self):\r\n        return 2\r\n".encode("utf-8")


def test_trailing_newline_delete_whole_lines():
    src = "a\n    b\n    c\nd\n"
    out, _, _ = replace(src, "b\nc\n", "")
    assert out == "a\nd\n"


# ------------------------------------------------------------------ 2) 工具描述压缩后关键规则补回

def _prefix_text(workspace, profile=None):
    from test_prefix_budget import _prefix
    _, p = _prefix(workspace, **({"profile": profile} if profile else {}))
    tools = {t["function"]["name"]: t["function"]["description"] for t in p.tools}
    return p.system["content"], tools


@pytest.mark.parametrize("profile", [None, "lean"])
def test_key_edit_and_shell_rules_present(ee_home, workspace, profile):
    system, tools = _prefix_text(workspace, profile)
    blob = system + "\n" + tools["edit"] + "\n" + tools["write"]
    assert "先 read" in tools["edit"]                       # edit 前必须先 read
    assert "唯一" in blob and "replace_all" in blob         # old_string 必须唯一
    assert "缩进" in blob                                   # 保留原缩进
    assert "行号" in blob                                   # 不带行号前缀
    assert "新建" in blob                                   # 新建用 write
    # 不要用 shell 写文件改代码
    assert any(("shell" in line and "写文件" in line) for line in system.split("\n"))


def test_full_mode_keeps_dedicated_tool_rules(ee_home, workspace):
    system, _ = _prefix_text(workspace)
    assert "cat" in system and "grep" in system and "glob" in system


# ------------------------------------------------------------------ 3) SYSTEM.md 只替换基础段

def test_system_md_replaces_only_base_tool_rules_kept(ee_home, workspace):
    from conftest import collect, finish, make_session, text_chunks
    from ee.core.context import builtin_base
    from ee.tools import tool_rules
    (ee_home / "SYSTEM.md").write_bytes("\ufeff我的提示词\r\n".encode("utf-8"))
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop")])
    run(collect(s, "你好"))
    text = fake.calls[0]["messages"][0]["content"]
    assert text.startswith("我的提示词\n\n# 工具使用规则\n")
    assert builtin_base() not in text
    for r in tool_rules(s.tools):
        assert f"- {r}" in text


def test_system_md_tool_rules_follow_enabled_tools(ee_home, workspace):
    from ee.core import sysprompt as SP
    from ee.core.context import system_prompt
    from ee.tools import tool_rules
    from test_prefix_budget import _prefix
    s, _ = _prefix(workspace, profile="lean")
    (ee_home / "SYSTEM.md").write_text("X", encoding="utf-8")
    src = SP.resolve(workspace, ee_home, {}, {})
    text = system_prompt(s.profile, "", src, s.tools, s.agent_profile)
    assert text.startswith("X\n\n# 工具使用规则\n")
    rules = tool_rules(s.tools)
    assert rules and all(f"- {r}" in text for r in rules)
