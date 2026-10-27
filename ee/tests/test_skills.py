"""技能（D5-5）：目录层级与覆盖、兼容目录、frontmatter、长度上限、注入位置与前缀缓存、skill 工具、显式调用、禁用、RPC。"""
import json
import subprocess
from pathlib import Path

import pytest
from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core import skills as K
from ee.core.events import Notice, ToolEnd, TurnDone
from ee.core.permissions import PermissionPolicy


@pytest.fixture
def uhome(tmp_path, monkeypatch):
    """系统用户目录（~/.claude、~/.agents 所在），和 EE_HOME 分开。"""
    h = tmp_path / "userhome"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def mk(base, name, desc="做某件事", body="正文\n", fm_name=None, extra=""):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {fm_name or name}\ndescription: {desc}\n{extra}---\n{body}",
                                encoding="utf-8")
    return d


def test_hierarchy_override_and_compat(ee_home, workspace, uhome):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    sub = workspace / "pkg" / "app"
    sub.mkdir(parents=True)
    mk(uhome / ".agents" / "skills", "a", "用户 agents")
    mk(uhome / ".claude" / "skills", "a", "用户 claude")          # 覆盖 .agents
    mk(uhome / ".claude" / "skills", "only-claude", "只在 claude")
    mk(ee_home / "skills", "b", "用户 ee")
    mk(uhome / ".claude" / "skills", "b", "被 ee 覆盖")
    mk(workspace / ".claude" / "skills", "b", "项目 claude")       # 项目覆盖用户
    mk(workspace / ".ee" / "skills", "c", "项目根 ee")
    mk(sub / ".agents" / "skills", "c", "更深一层 agents")        # 更深的目录优先
    mk(workspace / ".ee" / "skills", "中文技能", "可以用中文名")
    cat = K.discover(ee_home, sub, {"skills": {"claude_home": True}})     # ~/.claude/skills 默认不读，这里显式打开
    got = {n: (s.description, s.source, s.origin) for n, s in cat.skills.items()}
    assert got["a"] == ("用户 claude", "user", "claude")
    assert got["b"] == ("项目 claude", "project", "claude")
    assert got["c"] == ("更深一层 agents", "project", "agents")
    assert got["only-claude"][0] == "只在 claude" and "中文技能" in got
    assert len(cat.skills["b"].shadows) == 2 and cat.errors == []
    assert list(cat.skills) == sorted(cat.skills, key=str.lower)      # 顺序稳定
    # 关闭兼容目录：只剩 .ee
    cat2 = K.discover(ee_home, sub, {"skills": {"compat": False}})
    assert set(cat2.skills) == {"b", "c", "中文技能"}
    assert cat2.skills["b"].description == "用户 ee" and cat2.skills["c"].description == "项目根 ee"
    # 同一文件经软链接出现两次只算一次
    (workspace / ".agents").mkdir()
    try:
        (workspace / ".agents" / "skills").symlink_to(workspace / ".ee" / "skills", target_is_directory=True)
    except (OSError, NotImplementedError):          # Windows 没有创建软链接的权限
        return
    cat3 = K.discover(ee_home, workspace)
    assert cat3.skills["c"].origin == "ee" and not cat3.errors


def test_frontmatter_parsing_and_errors(ee_home, workspace, uhome):
    base = ee_home / "skills"
    d = base / "multi"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes("\ufeff---\r\nname: multi\r\ndescription: >\r\n  第一行\r\n  第二行\r\n"
                                 "metadata:\r\n  version: 1\r\nallowed-tools: [read, shell]\r\n---\r\n# 标题\r\n正文".encode())
    mk(base, "quoted", desc='"带: 冒号 和 \\"引号\\""')
    mk(base, "comment", desc="说明 # 这是注释")
    (base / "nofm").mkdir()
    (base / "nofm" / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")
    mk(base, "noname", fm_name=" ")
    (base / "nodesc").mkdir()
    (base / "nodesc" / "SKILL.md").write_text("---\nname: nodesc\n---\nx", encoding="utf-8")
    (base / "badline").mkdir()
    (base / "badline" / "SKILL.md").write_text("---\nname: badline\n这一行不对\n---\n", encoding="utf-8")
    (base / "unclosed").mkdir()
    (base / "unclosed" / "SKILL.md").write_text("---\nname: unclosed\ndescription: x\n", encoding="utf-8")
    mk(base, "badname", fm_name="bad name!")
    (base / "binary").mkdir()
    (base / "binary" / "SKILL.md").write_bytes(b"---\nname: x\n\xff\xfe\n---\n")
    (base / "empty-dir").mkdir()                       # 没有 SKILL.md：静默跳过
    mk(base, "longdesc", desc="长" * 2000)
    cat = K.discover(ee_home, None)
    assert set(cat.skills) == {"multi", "quoted", "comment", "longdesc"}
    assert cat.skills["multi"].description == "第一行 第二行"
    assert K.read_body(cat.skills["multi"]) == "# 标题\n正文"
    assert cat.skills["quoted"].description == '带: 冒号 和 "引号"'
    assert cat.skills["comment"].description == "说明"
    assert len(cat.skills["longdesc"].description) == K.MAX_DESC_CHARS
    errs = {e["path"].split("skills")[-1].strip("/\\").split("/")[0].split("\\")[0]: e["error"] for e in cat.errors}
    assert "缺少 frontmatter" in errs["nofm"] and "缺少 name" in errs["noname"]
    assert "缺少 description" in errs["nodesc"] and "格式不对" in errs["badline"]
    assert "没有结束" in errs["unclosed"] and "name 不合法" in errs["badname"] and "UTF-8" in errs["binary"]
    assert "empty-dir" not in errs


def test_catalog_length_cap():
    sks = [K.Skill(f"s{i:02d}", "说明" * 100, f"/x/s{i:02d}/SKILL.md", "user", "ee") for i in range(40)]
    full, om = K.render_catalog(sks[:2], 100000)
    assert om == 0 and full.count("\n") == 1 and "说明" * 100 in full
    short, om = K.render_catalog(sks[:10], 2000)       # 放不下全文 → 说明缩短到 120 字
    assert om == 0 and "说明" * 100 not in short and short.count("…") == 10
    cut, om = K.render_catalog(sks, 3000)               # 还放不下 → 省略并注明
    assert om > 0 and f"另有 {om} 个技能" in cut and len(cut) < 3200
    assert cut.count("\n- s") == 40 - om - 1
    blk = K.load_block(None, None, {}, catalog=K.Catalog({s.name: s for s in sks[:3]}))
    assert blk.text.startswith("# 技能（Skills）") and "<SKILLS>" in blk.text and blk.names == ["s00", "s01", "s02"]
    assert blk.message()["_kind"] == "agents" and blk.message()["_sub"] == "skills"
    assert K.load_block(None, None, {"skills": {"enabled": False}},
                        catalog=K.Catalog({"s00": sks[0]})).message() is None


def test_mentions():
    names = {"pdf", "my-skill", "中文"}
    assert K.mentions("/pdf 把它拆开", names) == ["pdf"]
    assert K.mentions("先用 $my-skill 再用 $中文，再 $pdf.", names) == ["my-skill", "中文", "pdf"]
    assert K.mentions("价格 $5 和 a$pdf 和 /usr/bin 和 $unknown", names) == []
    assert K.mentions("看 /pdf", names) == []           # / 只认开头


def test_injection_position_and_prefix_cache(ee_home, workspace, uhome):
    (ee_home / "memory.md").write_text("- 记忆 A\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("项目指令\n", encoding="utf-8")
    mk(ee_home / "skills", "pdf", "处理 PDF 文件")
    mk(uhome / ".agents" / "skills", "excel", "处理 Excel")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop")])
    run(collect(s, "一"))
    h = s.prefix.sha256
    msgs = fake.calls[0]["messages"]
    kinds = [("AGENTS" if "项目指令" in (m.get("content") or "") else "MEM" if "<MEMORY>" in (m.get("content") or "")
              else "SKILLS" if "<SKILLS>" in (m.get("content") or "") else m["role"]) for m in msgs]
    assert kinds[:5] == ["system", "AGENTS", "MEM", "SKILLS", "user"]
    cat_msg = msgs[3]["content"]
    assert "- excel：处理 Excel（" in cat_msg and "- pdf：处理 PDF 文件（" in cat_msg
    assert str(ee_home / "skills" / "pdf" / "SKILL.md") in cat_msg
    assert [t["function"]["name"] for t in fake.calls[0]["tools"]][-1] == "skill"
    assert s.skill_names == ["excel", "pdf"]
    # 会话中途新增技能：清单不变（字节、位置相同），前缀不变
    mk(ee_home / "skills", "later", "后来加的")
    run(collect(s, "二"))
    assert fake.calls[1]["messages"][:4] == msgs[:4] and "later" not in fake.calls[1]["messages"][3]["content"]
    assert s.prefix.sha256 == h
    assert json.dumps(fake.calls[0]["tools"]) == json.dumps(fake.calls[1]["tools"])
    ser = lambda ms: [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in ms]  # noqa: E731
    a, b = fake.calls[0]["messages"], fake.calls[1]["messages"]
    assert ser(b)[:len(a)] == ser(a), "第二回合的请求必须以第一回合的全部消息为前缀（字节不变）"

    # 新会话才加载新技能
    s.new()
    s._ensure_instructions()
    assert "later" in [m for m in s.history if m.get("_sub") == "skills"][0]["content"]


def test_skill_tool_and_permissions(ee_home, workspace, uhome):
    d = mk(ee_home / "skills", "pdf", "处理 PDF", body="# PDF 技能\n运行 scripts/split.py\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "split.py").write_text("print(1)\n", encoding="utf-8")
    (d / "reference.md").write_text("参考\n", encoding="utf-8")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "x.js").write_text("", encoding="utf-8")
    mk(ee_home / "skills", "off", "被禁用的")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "skill", {"name": "pdf"}) + tool_chunks(1, "c2", "skill", {"name": "off"})
        + tool_chunks(2, "c3", "skill", {"name": "nope"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ], mode="plan")                                     # 计划（只读）模式下也能用
    s.config.data["skills"] = {**s.config.data.get("skills", {}), "disabled": ["off"]}
    ev = run(collect(s, "拆 PDF"))
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "completed"
    outs = {m["tool_call_id"]: m["content"] for m in s.history if m["role"] == "tool"}
    assert outs["c1"].startswith('<skill name="pdf">') and "# PDF 技能" in outs["c1"] and "name: pdf" not in outs["c1"]
    assert f"技能目录：{d}" in outs["c1"] and "<file>scripts/split.py</file>" in outs["c1"]
    assert "<file>reference.md</file>" in outs["c1"] and "node_modules" not in outs["c1"]
    assert "已在设置里禁用" in outs["c2"] and "没有名为 nope 的技能" in outs["c3"] and "pdf" in outs["c3"]
    te = [e for e in ev if isinstance(e, ToolEnd) and e.call_id == "c1"][0]
    assert te.ok and te.meta["kind"] == "skill" and te.meta["name"] == "pdf"
    # 被禁用的技能不在清单里
    cat_msg = [m for m in s.history if m.get("_sub") == "skills"][0]["content"]
    assert "- pdf：" in cat_msg and "- off：" not in cat_msg
    # 权限：只读工具，deny 规则可按名字拦
    for mode in ("read-only", "plan", "default"):
        assert PermissionPolicy(mode=mode).check("skill", {"name": "pdf"}).action == "allow"
    assert PermissionPolicy(deny=["skill(pdf)"]).check("skill", {"name": "pdf"}).action == "deny"


def test_explicit_invocation(ee_home, workspace, uhome):
    mk(ee_home / "skills", "pdf", "处理 PDF", body="PDF 技能正文\n")
    mk(ee_home / "skills", "off", "禁用的", body="不该出现\n")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop"),
                                       text_chunks("c") + finish("stop")])
    s.config.data["skills"] = {**s.config.data.get("skills", {}), "disabled": ["off"]}
    ev = run(collect(s, "/pdf 拆成两份"))
    user = fake.calls[0]["messages"][-1]["content"]
    assert "点名使用技能 pdf" in user and "PDF 技能正文" in user and "拆成两份" in user
    assert any(isinstance(e, Notice) and "已加载技能 pdf" in e.text for e in ev)
    assert s.user_text_at(s.turn_starts[-1]).endswith("/pdf 拆成两份")     # 编辑重发的原文核对仍然成立
    ev = run(collect(s, "用 $off 和 $pdf"))
    user = fake.calls[1]["messages"][-1]["content"]
    assert "不该出现" not in user and "PDF 技能正文" in user
    # 会话开始后才新建的技能也能显式调用
    mk(ee_home / "skills", "fresh", "新的", body="新技能正文\n")
    run(collect(s, "$fresh 来一下"))
    assert "新技能正文" in fake.calls[2]["messages"][-1]["content"]


def test_disabled_globally(ee_home, workspace, uhome):
    mk(ee_home / "skills", "pdf", "处理 PDF")
    from ee.config import load_config
    from ee.core.session import Session
    from conftest import FakeClient
    cfg = load_config(workspace)
    cfg.data["skills"] = {"enabled": False}
    fake = FakeClient([text_chunks("a") + finish("stop")])
    s = Session(cfg, cwd=workspace, client=fake, persist=False)
    run(collect(s, "$pdf 你好"))
    msgs = fake.calls[0]["messages"]
    assert not any("<SKILLS>" in (m.get("content") or "") for m in msgs)
    assert "skill" not in [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "点名使用技能" not in msgs[-1]["content"]


def test_subagent_sees_catalog(ee_home, workspace, uhome):
    mk(ee_home / "skills", "pdf", "处理 PDF")
    s, fake = make_session(workspace, [
        tool_chunks(0, "p1", "task", {"description": "看看", "prompt": "调研", "agent": "explore"}) + finish("tool_calls"),
        text_chunks("报告") + finish("stop"),
        text_chunks("好") + finish("stop"),
    ])
    run(collect(s, "派个子代理"))
    child = fake.calls[1]
    assert any("<SKILLS>" in (m.get("content") or "") for m in child["messages"])
    assert "skill" in {t["function"]["name"] for t in child["tools"]}


def test_rpc_and_settings(ee_home, workspace, uhome):
    from ee.server.app import Backend
    mk(ee_home / "skills", "pdf", "处理 PDF", body="正文 P\n")
    mk(workspace / ".claude" / "skills", "proj", "项目技能")
    (workspace / ".ee" / "skills" / "bad").mkdir(parents=True)
    (workspace / ".ee" / "skills" / "bad" / "SKILL.md").write_text("无 frontmatter", encoding="utf-8")
    b = Backend(home=ee_home, persist=False)
    r = run(b.call("skills/list", {"cwd": str(workspace)}))
    assert r["enabled"] and r["compat"]
    assert [(x["name"], x["source"], x["origin"], x["enabled"]) for x in r["skills"]] == [
        ("pdf", "user", "ee", True), ("proj", "project", "claude", True)]
    assert len(r["errors"]) == 1 and "frontmatter" in r["errors"][0]["error"]
    assert r["dirs"]["user"] == str(ee_home / "skills")
    assert Path(r["dirs"]["project"]) == (workspace / ".ee" / "skills").resolve()
    rd = run(b.call("skills/read", {"name": "pdf", "cwd": str(workspace)}))
    assert "正文 P" in rd["text"]
    with pytest.raises(Exception, match="没有这个技能"):
        run(b.call("skills/read", {"name": "nope"}))
    p = run(b.call("skills/dir", {"scope": "project", "cwd": str(workspace)}))["path"]
    assert Path(p) == (workspace / ".ee" / "skills").resolve() and Path(p).is_dir()
    p = run(b.call("skills/dir", {"scope": "user"}))["path"]
    assert p == str(ee_home / "skills")
    # 设置：禁用写进 config，skills/list 立刻反映
    st = run(b.call("settings/get", {}))["settings"]["skills"]
    assert st == {"enabled": True, "compat": True, "disabled": [], "max_chars": 8000}
    st = run(b.call("settings/set", {"settings": {"skills": {"disabled": ["pdf"], "compat": False}}}))["settings"]["skills"]
    assert st["disabled"] == ["pdf"] and st["compat"] is False
    assert "disabled" in (ee_home / "config.toml").read_text(encoding="utf-8")
    r = run(b.call("skills/list", {"cwd": str(workspace)}))
    assert [(x["name"], x["enabled"]) for x in r["skills"]] == [("pdf", False)]
    with pytest.raises(Exception, match="1000"):
        run(b.call("settings/set", {"settings": {"skills": {"max_chars": 10}}}))
    st = run(b.call("settings/set", {"settings": {"skills": {"disabled": [], "compat": True}}}))["settings"]["skills"]
    assert st["disabled"] == [] and st["compat"] is True
