"""SYSTEM.md / APPEND_SYSTEM.md：加载顺序、项目信任（路径+内容指纹）、内容变更要重新信任、模型补丁开关、冻结、RPC。"""
import asyncio

import pytest
from conftest import collect, finish, make_session, run, text_chunks
from test_server import make_backend, wait_for

from ee.core import sysprompt as SP
from ee.core.context import builtin_base, load_prompt, system_prompt
from ee.core.events import Notice


def _sent_system(fake) -> str:
    return fake.calls[0]["messages"][0]["content"]


def _proj(workspace, name="SYSTEM.md", text="项目自定义提示词"):
    (workspace / ".git").mkdir(exist_ok=True)
    d = workspace / ".ee"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _turn(workspace, script_n=1):
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop") for _ in range(script_n)])
    ev = run(collect(s, "你好"))
    return s, fake, ev


def test_default_is_byte_identical(ee_home, workspace):
    s, fake, ev = _turn(workspace)
    from ee.core import styles as ST
    expect = system_prompt(s.profile, ST.system_segment(ST.get(ee_home, "default")), None, s.tools, s.agent_profile)
    assert _sent_system(fake) == expect
    assert _sent_system(fake).startswith(builtin_base())
    assert not s.system_sources().customized
    assert not [e for e in ev if isinstance(e, Notice) and "系统提示词" in e.text]


def test_user_system_replaces_base_and_patch_still_appended(ee_home, workspace):
    # 按字节写：write_text 在 Windows 上会把 \n 再译成 \r\n，得到 \r\r\n，测的就不是“记事本存的 BOM+CRLF”了
    (ee_home / "SYSTEM.md").write_bytes("\ufeff我的全局提示词\r\n第二行\r\n".encode("utf-8"))
    s, fake, ev = _turn(workspace)
    text = _sent_system(fake)
    assert text.startswith("我的全局提示词\n第二行\n\n")
    assert builtin_base() not in text
    assert load_prompt(f"system/{s.profile.prompt_patch}.md") in text      # 模型补丁仍追加
    assert any(isinstance(e, Notice) and "自定义系统提示词" in e.text and "SYSTEM.md" in e.text for e in ev)


def test_append_after_patch_and_patch_switch(ee_home, workspace):
    (ee_home / "APPEND_SYSTEM.md").write_text("追加：回答末尾写“完”。", encoding="utf-8")
    s, fake, _ = _turn(workspace)
    text = _sent_system(fake)
    patch = load_prompt(f"system/{s.profile.prompt_patch}.md")
    assert text.startswith(builtin_base())
    assert text.index(patch) < text.index("追加：回答末尾写“完”。")
    # 关掉模型补丁
    (ee_home / "config.toml").write_text("[system_prompt]\nmodel_patch = false\n", encoding="utf-8")
    s2, fake2, _ = _turn(workspace)
    t2 = _sent_system(fake2)
    assert patch not in t2 and t2.startswith(builtin_base()) and t2.endswith("\n\n追加：回答末尾写“完”。\n")
    assert t2 == system_prompt(s2.profile, "", s2.system_sources(), s2.tools, s2.agent_profile)


def test_untrusted_project_file_ignored_with_notice(ee_home, workspace):
    _proj(workspace)
    (ee_home / "SYSTEM.md").write_text("全局", encoding="utf-8")
    s, fake, ev = _turn(workspace)
    text = _sent_system(fake)
    assert "项目自定义提示词" not in text and text.startswith("全局\n")      # 退回用户级
    assert any(isinstance(e, Notice) and e.level == "warn" and "还没被信任" in e.text for e in ev)
    src = s.system_sources()
    assert len(src.pending) == 1 and src.pending[0].scope == "project"


def test_trusted_project_wins_and_content_change_requires_retrust(ee_home, workspace):
    p = _proj(workspace)
    (ee_home / "SYSTEM.md").write_text("全局", encoding="utf-8")
    src = SP.resolve(workspace, ee_home)
    aid = src.pending[0].allow_id
    SP.trust(workspace, ee_home, aid)
    s, fake, ev = _turn(workspace)
    assert _sent_system(fake).startswith("项目自定义提示词\n")
    assert not [e for e in ev if isinstance(e, Notice) and "还没被信任" in e.text]
    # 内容一改，指纹变了，要重新信任
    p.write_text("项目自定义提示词（被悄悄改了）", encoding="utf-8")
    s2, fake2, ev2 = _turn(workspace)
    assert _sent_system(fake2).startswith("全局\n")
    assert any(isinstance(e, Notice) and "还没被信任" in e.text for e in ev2)
    # 旧 allow_id 不能用来信任新内容
    with pytest.raises(ValueError):
        SP.trust(workspace, ee_home, aid)
    # 撤销信任
    new_aid = SP.resolve(workspace, ee_home).pending[0].allow_id
    SP.trust(workspace, ee_home, new_aid)
    assert SP.resolve(workspace, ee_home).system.scope == "project"
    SP.untrust(new_aid)
    assert SP.resolve(workspace, ee_home).system.scope == "user"


def test_project_config_cannot_trust_itself(ee_home, workspace):
    p = _proj(workspace)
    aid = SP.resolve(workspace, ee_home).pending[0].allow_id
    (workspace / ".ee" / "config.toml").write_text(f'[system_prompt]\nallowed_project = ["{aid}"]\n', encoding="utf-8")
    s, fake, _ = _turn(workspace)
    assert "项目自定义提示词" not in _sent_system(fake)
    assert p.exists()


def test_hooks_trust_does_not_trust_system_md(ee_home, workspace):
    _proj(workspace)
    aid = SP.resolve(workspace, ee_home).pending[0].allow_id
    (ee_home / "config.toml").write_text(f'[hooks]\nallowed_project = ["{aid}"]\n', encoding="utf-8")
    assert SP.resolve(workspace, ee_home).system is None


def test_project_append_independent_of_system(ee_home, workspace):
    _proj(workspace, "APPEND_SYSTEM.md", "项目追加")
    (ee_home / "APPEND_SYSTEM.md").write_text("全局追加", encoding="utf-8")
    src = SP.resolve(workspace, ee_home)
    assert src.system is None and src.append.scope == "user"
    SP.trust(workspace, ee_home, src.pending[0].allow_id)
    src = SP.resolve(workspace, ee_home)
    assert src.append.scope == "project" and src.append.text == "项目追加"


def test_frozen_for_session(ee_home, workspace):
    (ee_home / "SYSTEM.md").write_text("版本一", encoding="utf-8")
    s, fake = make_session(workspace, [text_chunks("a") + finish("stop"), text_chunks("b") + finish("stop")])
    run(collect(s, "一"))
    (ee_home / "SYSTEM.md").write_text("版本二", encoding="utf-8")
    run(collect(s, "二"))
    assert fake.calls[0]["messages"][0] == fake.calls[1]["messages"][0]
    assert fake.calls[1]["messages"][0]["content"].startswith("版本一")


def test_bad_files_ignored(ee_home, workspace):
    (ee_home / "SYSTEM.md").write_text("   \n", encoding="utf-8")
    (ee_home / "APPEND_SYSTEM.md").write_bytes(b"x" * (SP.MAX_BYTES + 1))
    src = SP.resolve(workspace, ee_home)
    assert src.system is None and src.append is None
    errs = [f.error for f in src.files]
    assert "文件是空的" in errs[0] and "太大" in errs[1]
    assert any("没有使用" in t for t, _ in src.notices())


def test_rpc_info_preview_trust(ee_home, workspace):
    _proj(workspace)

    async def go():
        b, fake, ev = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        info = await b.call("sysprompt/info", {"cwd": str(workspace)})
        assert info["current"]["system"] == "" and len(info["current"]["pending"]) == 1
        pv = await b.call("sysprompt/preview", {"cwd": str(workspace)})
        assert pv["frozen"] is False and pv["text"].startswith(builtin_base())
        aid = info["current"]["pending"][0]
        try:
            await b.call("sysprompt/trust", {"cwd": str(workspace), "allow_id": "bad:bad"})
            raise AssertionError("错误的 allow_id 不应被接受")
        except Exception as e:  # noqa: BLE001
            assert "改动" in str(e)
        r = await b.call("sysprompt/trust", {"cwd": str(workspace), "allow_id": aid})
        assert aid in r["allowed_project"]
        info = await b.call("sysprompt/info", {"cwd": str(workspace)})
        assert info["current"]["system"].endswith("SYSTEM.md") and info["current"]["pending"] == []
        await b.call("sysprompt/set", {"model_patch": False})
        pv = await b.call("sysprompt/preview", {"cwd": str(workspace)})
        # SYSTEM.md 只替换基础段：工具使用规则照常追加，关掉的只是模型补丁
        assert pv["text"].startswith("项目自定义提示词\n\n# 工具使用规则\n- ")
        assert "补充说明" not in pv["text"]
        # 对话开始后 preview 返回冻结版
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "你好"})
        await wait_for(ev, "turn/completed")
        pv = await b.call("sysprompt/preview", {"thread_id": tid})
        assert pv["frozen"] is True and pv["text"] == fake.calls[0]["messages"][0]["content"]
        info = await b.call("sysprompt/info", {"thread_id": tid})
        assert info["frozen"]["system"].endswith("SYSTEM.md")
        await b.call("sysprompt/untrust", {"allow_id": aid})
        from ee.config import load_user_file
        assert load_user_file()["system_prompt"]["allowed_project"] == []
    asyncio.run(go())
