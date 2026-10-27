"""精简模式（profile=lean）、todo / present_plan / memory 开关、规则随工具拼装、注入块精简。"""
import asyncio
import json
import sys

import pytest
from conftest import FakeClient, collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.context import system_prompt
from ee.core.events import ApprovalRequest, ToolEnd
from ee.tools import build_tools, tool_rules


def session(workspace, script=(), mode="default", **data):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    cfg.data.update(data)
    fake = FakeClient(list(script))
    return Session(cfg, cwd=workspace, mode=mode, client=fake, persist=False), fake


def names(s):
    return [t.name for t in s.tools]


def test_lean_tools_and_system(ee_home, workspace):
    s, fake = session(workspace, [text_chunks("好") + finish("stop")], profile="lean")
    assert s.agent_profile == "lean" and names(s) == ["read", "write", "edit", "shell"]
    run(collect(s, "你好"))
    req = fake.calls[0]
    sys_text = req["messages"][0]["content"]
    assert sys_text.startswith("你是 ee，用户电脑上的编程 agent，用 read/write/edit/shell")
    assert "# 工具使用规则" in sys_text and "context-update" in sys_text
    assert "todo" not in sys_text and "grep" not in sys_text and "shell_kill" not in sys_text
    shell = next(t for t in req["tools"] if t["function"]["name"] == "shell")["function"]
    props = shell["parameters"]["properties"]
    assert "description" not in props and "read 看日志" in props["background"]["description"]
    # 工具表与 full 的前四个工具完全相同（shell 除外），方便以后对比
    full = {t.name: t.schema() for t in build_tools(s.profile, s.config, agent="full")}
    for t in req["tools"][:3]:
        assert t == full[t["function"]["name"]]


def test_lean_switch_mid_session_does_not_change_tools(ee_home, workspace):
    s, _ = session(workspace, profile="lean")
    s.config.data["profile"] = "full"           # 设置页改了：已开始的对话不变
    s.set_model(s.spec.alias)
    assert names(s) == ["read", "write", "edit", "shell"]


def test_default_is_full_and_unknown_falls_back(ee_home, workspace):
    s, _ = session(workspace)
    assert s.agent_profile == "full" and "todo" in names(s) and "present_plan" in names(s)
    s2, _ = session(workspace, profile="weird")
    assert s2.agent_profile == "full"


def test_rules_follow_enabled_tools_and_dedupe(ee_home, workspace):
    s, _ = session(workspace)
    text = system_prompt(s.profile, "", None, s.tools, "full")
    shared = "搜内容用 grep、找文件用 glob，不要用 shell 代替。"
    assert text.count(shared) == 1                     # grep 和 glob 带同一条规则，只出现一次
    assert text.count("用到网页内容时附上来源 URL。") == 1
    assert "todo 列计划" in text and "shell_kill" in text
    s2, _ = session(workspace, todo=False, present_plan=False, background_shell=False)
    assert "todo" not in names(s2) and "present_plan" not in names(s2) and "shell_output" not in names(s2)
    t2 = system_prompt(s2.profile, "", None, s2.tools, "full")
    assert "todo 列计划" not in t2 and "shell_kill" not in t2
    # 没有后台命令工具时，shell 的 background 说明不再提 shell_output
    bg = s2.tool_map["shell"].parameters["properties"]["background"]["description"]
    assert "shell_output" not in bg
    assert len(set(tool_rules(s.tools))) == len(tool_rules(s.tools))


def test_base_prompt_has_no_tool_specific_lines(ee_home, workspace):
    """工具专属的细则只在规则段里出现：关掉 todo 后整个 system 不再提 todo。"""
    s, _ = session(workspace, todo=False)
    assert "todo" not in system_prompt(s.profile, "", None, s.tools, "full")


def test_edit_description_is_truthful():
    from ee.tools.edit import EditTool
    d = EditTool().description
    assert "diff" not in d                             # 返回给模型的只有增删行数，diff 只给界面


def test_plan_mode_without_present_plan(ee_home, workspace):
    s, fake = session(workspace, [text_chunks("方案：……") + finish("stop")], mode="plan", present_plan=False)
    run(collect(s, "帮我规划"))
    first_user = [m for m in fake.calls[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "计划模式" in first_user and "直接用文字给出方案" in first_user and "present_plan" not in first_user
    s2, fake2 = session(workspace, [text_chunks("x") + finish("stop")], mode="plan", profile="lean")
    run(collect(s2, "规划"))
    u2 = [m for m in fake2.calls[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "直接用文字给出方案" in u2


def test_lean_keeps_permissions(ee_home, workspace):
    """精简模式只动提示词和工具表：写文件照样要批准，只读模式照样拒绝。"""
    cmd = "touch made.txt" if sys.platform != "win32" else "New-Item made.txt"
    for profile in ("full", "lean"):
        script = [tool_chunks(0, "c1", "shell", {"command": cmd}) + finish("tool_calls"),
                  text_chunks("完成") + finish("stop")]
        s, _ = session(workspace, script, profile=profile)
        ev = run(collect(s, "建文件"))
        assert any(isinstance(e, ApprovalRequest) for e in ev), profile      # 非只读命令照样要批准
        (workspace / "made.txt").unlink()
    s2, _ = session(workspace, [tool_chunks(0, "c1", "write", {"path": "b.txt", "content": "x"}) + finish("tool_calls"),
                                text_chunks("被拒") + finish("stop")], mode="read-only", profile="lean")
    ev2 = run(collect(s2, "写文件"))
    assert not (workspace / "b.txt").exists()
    assert any(isinstance(e, ToolEnd) and not e.ok for e in ev2)


@pytest.mark.skipif(sys.platform == "win32", reason="用 sleep 演示常驻进程")
def test_lean_background_shell_points_to_log_and_kill(ee_home, workspace):
    script = [tool_chunks(0, "c1", "shell", {"command": "echo hi; sleep 30", "background": True}) + finish("tool_calls"),
              text_chunks("起来了") + finish("stop")]
    s, fake = session(workspace, script, mode="auto", profile="lean")

    async def go():
        ev = await collect(s, "后台跑")
        await s.bg.close()
        return ev
    run(go())
    out = next(m for m in fake.calls[1]["messages"] if m["role"] == "tool")["content"]
    assert "完整日志" in out and "用 read 查看" in out and "kill " in out and "shell_output" not in out


def test_memory_inject_and_tool_switches(ee_home, workspace):
    (ee_home / "memory.md").write_text("- 用 pnpm\n", encoding="utf-8")

    def first_msgs(**mem):
        s, fake = session(workspace, [text_chunks("ok") + finish("stop")],
                          memory={"enabled": True, "workspace": True, "max_bytes": 8192, **mem})
        run(collect(s, "hi"))
        msgs = [m["content"] for m in fake.calls[0]["messages"] if m["role"] == "user"]
        return s, [m for m in msgs if "<MEMORY>" in m]

    s, mem = first_msgs()
    assert "memory" in names(s) and mem and "用 memory 工具增删" in mem[0] and "- 用 pnpm" in mem[0]
    s, mem = first_msgs(tool=False)
    assert "memory" not in names(s) and mem and "memory 工具" not in mem[0]
    s, mem = first_msgs(inject=False)
    assert "memory" in names(s) and not mem
    s, mem = first_msgs(enabled=False)
    assert "memory" not in names(s) and not mem


def test_lean_skills_block_uses_read(ee_home, workspace):
    d = ee_home / "skills" / "pdf"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf\ndescription: 处理 PDF\n---\n正文\n", encoding="utf-8")
    s, fake = session(workspace, [text_chunks("ok") + finish("stop")], profile="lean")
    run(collect(s, "hi"))
    blk = next(m["content"] for m in fake.calls[0]["messages"] if "<SKILLS>" in (m.get("content") or ""))
    assert "用 read 读取" in blk and "skill 工具" not in blk and "pdf：处理 PDF" in blk
    s2, fake2 = session(workspace, [text_chunks("ok") + finish("stop")])
    run(collect(s2, "hi"))
    blk2 = next(m["content"] for m in fake2.calls[0]["messages"] if "<SKILLS>" in (m.get("content") or ""))
    assert "用 skill 工具" in blk2


def test_no_injection_blocks_when_empty(ee_home, workspace):
    s, fake = session(workspace, [text_chunks("ok") + finish("stop")], profile="lean")
    run(collect(s, "hi"))
    users = [m["content"] for m in fake.calls[0]["messages"] if m["role"] == "user"]
    assert len(users) == 1 and "<MEMORY>" not in users[0] and "<SKILLS>" not in users[0]


def test_settings_roundtrip_profile(ee_home):
    from test_server import make_backend

    from ee.server.app import RpcError

    async def go():
        b, _, _ = make_backend(ee_home, [])
        st = (await b.call("settings/get", {}))["settings"]
        assert st["profile"] == "full" and st["todo"] is True and st["present_plan"] is True
        assert st["memory"]["inject"] is True and st["memory"]["tool"] is True
        st.update(profile="lean", todo=False, present_plan=False)
        st["memory"] = {**st["memory"], "tool": False}
        out = (await b.call("settings/set", {"settings": st}))["settings"]
        assert out["profile"] == "lean" and out["todo"] is False and out["present_plan"] is False
        assert out["memory"]["tool"] is False and out["memory"]["inject"] is True
        bad = json.loads(json.dumps(out))
        bad["profile"] = "tiny"
        with pytest.raises(RpcError):
            await b.call("settings/set", {"settings": bad})
    asyncio.run(go())
    text = (ee_home / "config.toml").read_text(encoding="utf-8")
    assert 'profile = "lean"' in text


def test_exec_profile_and_resume_keeps_it(ee_home, workspace):
    from test_exec import events_of, run_exec
    code, out, _, fake = run_exec(ee_home, [text_chunks("好") + finish("stop")],
                                  ["-C", str(workspace), "--json", "--profile", "lean", "你好"])
    assert code == 0
    ev = events_of(out)
    assert ev[0]["profile"] == "lean"
    assert [t["function"]["name"] for t in fake.calls[0]["tools"]] == ["read", "write", "edit", "shell"]
    sid = ev[0]["id"]
    code, out, _, fake = run_exec(ee_home, [text_chunks("还是精简") + finish("stop")], ["--resume", sid, "--json", "再来"])
    assert code == 0 and events_of(out)[0]["profile"] == "lean"
    assert len(fake.calls[0]["tools"]) == 4
    # 不带 --profile 的新会话按配置（默认 full）
    code, out, _, fake = run_exec(ee_home, [text_chunks("好") + finish("stop")], ["-C", str(workspace), "--json", "x"])
    assert events_of(out)[0]["profile"] == "full" and len(fake.calls[0]["tools"]) > 4


def test_builtin_base_and_system_md_in_lean(ee_home, workspace):
    from ee.core.context import builtin_base, load_prompt
    assert builtin_base(None, "lean") == load_prompt("system/lean.md")
    assert builtin_base() == load_prompt("system/base.md")
    s, fake = session(workspace, [text_chunks("好") + finish("stop")], profile="lean")
    run(collect(s, "hi"))
    assert fake.calls[0]["messages"][0]["content"].startswith(builtin_base(None, "lean"))
    # SYSTEM.md 只替换内置基础段，工具使用规则照常按启用的 4 个工具追加
    (ee_home / "SYSTEM.md").write_text("我的提示词", encoding="utf-8")
    s2, fake2 = session(workspace, [text_chunks("好") + finish("stop")], profile="lean")
    run(collect(s2, "hi"))
    text = fake2.calls[0]["messages"][0]["content"]
    assert text.startswith("我的提示词\n\n# 工具使用规则\n") and builtin_base(None, "lean") not in text
    assert "grep" not in text and len(fake2.calls[0]["tools"]) == 4


def test_sysprompt_preview_uses_profile(ee_home, workspace):
    from test_server import make_backend
    (ee_home / "config.toml").write_text('profile = "lean"\n', encoding="utf-8")

    async def go():
        b, _, _ = make_backend(ee_home, [])
        return await b.call("sysprompt/preview", {"cwd": str(workspace)})
    pv = asyncio.run(go())
    from ee.core.context import builtin_base
    assert pv["text"].startswith(builtin_base(None, "lean")) and "# 工具使用规则" in pv["text"]
