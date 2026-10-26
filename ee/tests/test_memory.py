from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core import memory as M


def test_add_list_remove(tmp_path):
    p = tmp_path / "memory.md"
    n, es = M.add_entry(p, "用 pnpm 不用 npm")
    assert n == 1 and p.read_text(encoding="utf-8").startswith("# ee 记忆")
    M.add_entry(p, "回答用中文")
    M.add_entry(p, "回答用中文")          # 去重
    assert M.parse_entries(p.read_text(encoding="utf-8")) == ["用 pnpm 不用 npm", "回答用中文"]
    removed, rest = M.remove_entry(p, index=1)
    assert removed == "用 pnpm 不用 npm" and rest == ["回答用中文"]
    assert "# ee 记忆" in p.read_text(encoding="utf-8")
    removed, rest = M.remove_entry(p, match="中文")
    assert rest == []


def test_parse_continuation_and_comments():
    t = "# x\n<!--\n- 注释里的不算\n-->\n- 一\n  续行\n* 二\n\n正文\n"
    assert M.parse_entries(t) == ["一 续行", "二"]


def test_injection_is_stable_and_capped(ee_home, workspace):
    (ee_home / "memory.md").write_text("- 用户偏好 A\n", encoding="utf-8")
    (workspace / ".ee").mkdir()
    (workspace / ".ee" / "memory.md").write_text("- 项目约定 B\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "memory", {"action": "add", "text": "新记的 C"}) + finish("tool_calls"),
        text_chunks("记住了") + finish("stop"),
        text_chunks("ok") + finish("stop"),
    ])
    run(collect(s, "记住 C"))
    first = fake.calls[0]["messages"]
    mem = [m for m in first if m["role"] == "user" and "<MEMORY>" in (m.get("content") or "")]
    assert len(mem) == 1 and "用户偏好 A" in mem[0]["content"] and "项目约定 B" in mem[0]["content"]
    assert first.index(mem[0]) == 1          # system 之后、用户消息之前
    assert "新记的 C" in (ee_home / "memory.md").read_text(encoding="utf-8")
    run(collect(s, "下一条"))
    # 会话内注入内容不变（字节相同、位置相同），新条目不进入当前会话的前缀
    later = fake.calls[2]["messages"]
    assert later[1] == mem[0] and "新记的 C" not in later[1]["content"]
    # 新会话重新加载
    s.new()
    s.history.clear()
    s._ensure_instructions()
    assert "新记的 C" in s.history[-1]["content"]

    big = "\n".join(f"- 条目{i} " + "长" * 50 for i in range(200))
    (ee_home / "memory.md").write_text(big, encoding="utf-8")
    blk = M.load_memory(ee_home, workspace, {"max_bytes": 2000})
    assert blk.truncated and len(blk.text.encode()) < 2600
    assert M.load_memory(ee_home, workspace, {"enabled": False}).message() is None


def test_memory_tool_read_only_mode(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "memory", {"action": "add", "text": "x"}) +
        tool_chunks(1, "c2", "memory", {"action": "list"}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
    ], mode="read-only")
    run(collect(s, "go"))
    out = {m["tool_call_id"]: m["content"] for m in s.history if m["role"] == "tool"}
    assert "只读" in out["c1"] and "空" in out["c2"]
    assert not (ee_home / "memory.md").exists()


def test_memory_tool_workspace_scope_and_remove(ee_home, workspace):
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "memory", {"action": "add", "text": "项目用 uv", "scope": "workspace"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "memory", {"action": "remove", "index": 1, "scope": "workspace"}) + finish("tool_calls"),
        tool_chunks(0, "c3", "memory", {"action": "remove", "index": 5}) + finish("tool_calls"),
        text_chunks("ok") + finish("stop"),
    ])
    run(collect(s, "go"))
    out = {m["tool_call_id"]: m["content"] for m in s.history if m["role"] == "tool"}
    assert "工作区级第 1 条" in out["c1"] and "已删除" in out["c2"] and "空" in out["c3"]
    assert M.parse_entries((workspace / ".ee" / "memory.md").read_text(encoding="utf-8")) == []
