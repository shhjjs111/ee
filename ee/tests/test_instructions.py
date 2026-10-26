from conftest import collect, finish, make_session, run, text_chunks

from ee.core.instructions import load_instructions


def test_layered_order_and_fallback(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    root = tmp_path / "repo"; (root / ".git").mkdir(parents=True)
    sub = root / "pkg" / "mod"; sub.mkdir(parents=True)
    (home / "AGENTS.md").write_text("用户偏好", encoding="utf-8")
    (root / "AGENTS.md").write_text("根规则", encoding="utf-8")
    (root / "CLAUDE.md").write_text("不该加载", encoding="utf-8")
    (root / "pkg" / "CLAUDE.md").write_text("pkg 规则（CLAUDE 兜底）", encoding="utf-8")
    (sub / "AGENTS.md").write_text("mod 规则", encoding="utf-8")
    ins = load_instructions(sub, home)
    t = ins.text
    assert [p.name for p in ins.sources] == ["AGENTS.md", "AGENTS.md", "CLAUDE.md", "AGENTS.md"]
    assert t.index("用户偏好") < t.index("根规则") < t.index("pkg 规则") < t.index("mod 规则")
    assert "不该加载" not in t and "<INSTRUCTIONS>" in t and not ins.truncated


def test_no_git_only_cwd(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (tmp_path / "AGENTS.md").write_text("父目录", encoding="utf-8")
    ws = tmp_path / "ws"; ws.mkdir()
    assert load_instructions(ws, home).text == ""
    (home / "CLAUDE.md").write_text("用户级 claude", encoding="utf-8")
    assert "用户级 claude" in load_instructions(ws, home).text


def test_size_cap(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (home / "AGENTS.md").write_text("长" * 5000, encoding="utf-8")
    ins = load_instructions(tmp_path, home, max_bytes=1000)
    assert ins.truncated and "已截断" in ins.text
    assert len(ins.text.encode("utf-8")) < 1500


def test_injected_after_system_once(ee_home, workspace):
    (ee_home / "AGENTS.md").write_text("回答要简短", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("用 pytest", encoding="utf-8")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop"),
                                       text_chunks("c") + finish("stop")])
    run(collect(s, "一"))
    run(collect(s, "二"))
    m1, m2 = fake.calls[0]["messages"], fake.calls[1]["messages"]
    assert "回答要简短" not in m1[0]["content"]           # 不进冻结的 system
    assert m1[1]["role"] == "user" and "回答要简短" in m1[1]["content"] and "用 pytest" in m1[1]["content"]
    assert m1[1]["content"].index("回答要简短") < m1[1]["content"].index("用 pytest")
    assert m2[:len(m1)] == m1                             # 只追加
    assert sum("回答要简短" in (m.get("content") or "") for m in m2) == 1
    assert len(s.instruction_sources) == 2
    # 回退第一回合不会删掉 AGENTS.md 消息
    s.rewind_last_turn(); s.rewind_last_turn()
    assert len(s.history) == 1 and s.history[0]["_kind"] == "agents"
    # /new 之后重新加载
    s.new()
    run(collect(s, "三"))
    assert "回答要简短" in fake.calls[2]["messages"][1]["content"]


def test_agents_message_survives_compaction(ee_home, workspace):
    (workspace / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"),
                                       text_chunks("摘要") + finish("stop")])
    run(collect(s, "一"))
    agents = dict(s.history[0])
    assert run(s.compact())["ok"]
    assert s.history[0] == agents
    assert [m.get("_kind") for m in s.history] == ["agents", None, "summary"]


def test_disabled(ee_home, workspace):
    (workspace / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop")])
    s.config.data["agents_md"]["enabled"] = False
    run(collect(s, "一"))
    assert [m["role"] for m in fake.calls[0]["messages"]] == ["system", "user"]
