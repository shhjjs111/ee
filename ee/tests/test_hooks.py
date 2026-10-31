"""D5-4：Hooks（6 个事件、matcher、退出码 0/2/其他、JSON 输出、超时、项目 hooks 信任）。

hook 命令一律用 sys.executable 跑一个小 python 脚本，Windows / POSIX 都能跑。
"""
import asyncio
import json
import sys
import time

import pytest
from conftest import collect, finish, make_session, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.config import dump_toml, load_user_file
from ee.core import hooks as H
from ee.core.events import ApprovalRequest, Notice, TurnDone

HOOK_PY = r'''
import json, os, sys, time
log, mode, arg = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
data = json.loads(sys.stdin.read() or "{}")
data["_env"] = {k: os.environ.get(k) for k in ("EE_PROJECT_DIR", "EE_SESSION_ID", "EE_HOOK_EVENT", "EE_HOOK_SOURCE")}
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(data, ensure_ascii=False) + "\n")
if mode == "exit2":
    sys.stderr.write(arg); sys.exit(2)
if mode == "exit":
    sys.stderr.write("坏掉了"); sys.exit(int(arg))
if mode == "print":
    print(arg); sys.exit(0)
if mode == "json":
    print(arg); sys.exit(0)
if mode == "sleep":
    time.sleep(float(arg)); sys.exit(0)
if mode == "stop-once":
    if data.get("stop_hook_active"):
        sys.exit(0)
    sys.stderr.write(arg); sys.exit(2)
sys.exit(0)
'''


@pytest.fixture
def hook_py(tmp_path):
    p = tmp_path / "hook.py"
    p.write_text(HOOK_PY, encoding="utf-8")
    return p


def cmd(hook_py, log, mode="ok", arg=""):
    return [sys.executable, str(hook_py), str(log), mode, arg]


def write_user_hooks(ee_home, hooks: dict, **extra):
    data = {"hooks": {**extra, **hooks}}
    (ee_home / "config.toml").write_text(dump_toml(data), encoding="utf-8")


def read_log(log):
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]


def notices(events):
    return [e.text for e in events if isinstance(e, Notice)]


def done(events):
    return [e for e in events if isinstance(e, TurnDone)][-1]


# ------------------------------------------------------------------ matcher / 配置
def test_matcher():
    assert H.match("", "shell") and H.match("*", "anything")
    assert H.match("shell|write", "write") and not H.match("shell|write", "read")
    assert H.match("Bash", "shell")                      # 别名、不分大小写
    assert H.match("mcp__github__*", "mcp__github__create_issue")
    assert not H.match("mcp__github__*", "mcp__gitlab__x")
    assert H.match("(write|edit)$", "edit") and not H.match("^wri", "write")   # 正则整体匹配
    assert not H.match("[bad", "x")


def test_parse_and_errors(tmp_path):
    hooks, errs = H.parse_table({
        "PreToolUse": [{"matcher": "shell", "command": "echo hi"},
                       {"matcher": "write", "hooks": [{"type": "command", "command": ["py", "a"], "timeout": 5}]}],
        "Nope": [{"command": "x"}],
        "Stop": [{"command": ""}],
        "PostToolUse": [{"command": "x", "timeout": 9999}],
    })
    assert [h.matcher for h in hooks] == ["shell", "write"]
    assert hooks[1].command == ["py", "a"] and hooks[1].timeout == 5
    assert len(errs) == 3 and any("Nope" in e for e in errs)


def test_dump_toml_array_of_tables(ee_home, hook_py, tmp_path):
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "shell", "command": cmd(hook_py, tmp_path / "l")},
                                              {"command": "echo 1", "timeout": 5}]}, enabled=True)
    text = (ee_home / "config.toml").read_text(encoding="utf-8")
    assert "[[hooks.PreToolUse]]" in text
    data = load_user_file()
    assert len(data["hooks"]["PreToolUse"]) == 2 and data["hooks"]["PreToolUse"][1]["timeout"] == 5


# ------------------------------------------------------------------ PreToolUse
def test_pre_tool_block_exit2(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "pre.log"
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, log, "exit2", "不许写 secret")}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "secret.txt", "content": "x"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "."}) + finish("tool_calls"),
        text_chunks("好的") + finish("stop"),
    ])
    ev = asyncio.run(collect(s, "写个文件"))
    assert not (workspace / "secret.txt").exists()
    tool_msgs = [m["content"] for m in s.history if m.get("role") == "tool"]
    assert "被 Hook 拦截" in tool_msgs[0] and "不许写 secret" in tool_msgs[0]
    rec = read_log(log)
    assert len(rec) == 1                                   # read 不匹配 matcher，不触发
    r = rec[0]
    assert r["hook_event_name"] == "PreToolUse" and r["tool_name"] == "write"
    assert r["tool_input"] == {"path": "secret.txt", "content": "x"} and r["tool_call_id"] == "c1"
    assert r["cwd"] == str(s.cwd) and r["session_id"] == s.store.id and r["agent"] == "main"
    assert r["_env"]["EE_HOOK_EVENT"] == "PreToolUse" and r["_env"]["EE_HOOK_SOURCE"] == "user"
    assert r["_env"]["EE_PROJECT_DIR"] == str(s.cwd) and r["_env"]["EE_SESSION_ID"] == s.store.id
    assert done(ev).reason == "completed"


def test_pre_tool_json_updated_input_and_allow(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "pre.log"
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow",
                                             "updatedInput": {"path": "b.txt", "content": "改过的"}}},
                     ensure_ascii=False)
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, log, "json", out)}]})
    outside = workspace.parent / "outside.txt"           # 工作区外写入本来要审批
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": str(outside), "content": "原来的"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    asked = []
    ev = asyncio.run(collect(s, "写", approve=lambda e: asked.append(e)))
    assert not asked and not any(isinstance(e, ApprovalRequest) for e in ev)
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "改过的" and not outside.exists()


def test_pre_tool_ask_forces_approval_in_auto(ee_home, workspace, hook_py, tmp_path):
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}})
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ], mode="auto")
    ev = asyncio.run(collect(s, "写"))
    assert any(isinstance(e, ApprovalRequest) for e in ev)
    assert (workspace / "a.txt").exists()


def test_pre_tool_decision_block_json(ee_home, workspace, hook_py, tmp_path):
    out = json.dumps({"decision": "block", "reason": "用 apply_patch"}, ensure_ascii=False)
    write_user_hooks(ee_home, {"PreToolUse": [{"command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    asyncio.run(collect(s, "写"))
    assert not (workspace / "a.txt").exists()
    assert "用 apply_patch" in [m for m in s.history if m.get("role") == "tool"][0]["content"]


# ------------------------------------------------------------------ PostToolUse
def test_post_tool_feedback(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "post.log"
    ctx = json.dumps({"additionalContext": "格式化过了"}, ensure_ascii=False)
    write_user_hooks(ee_home, {"PostToolUse": [
        {"matcher": "write", "command": cmd(hook_py, log, "exit2", "lint 失败：第 1 行")},
        {"matcher": "write", "command": cmd(hook_py, tmp_path / "post2.log", "json", ctx)},
    ]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists()                  # 已经执行了
    out = [m for m in s.history if m.get("role") == "tool"][0]["content"]
    assert "[Hook 反馈] lint 失败：第 1 行" in out and "格式化过了" in out
    r = read_log(log)[0]
    assert r["tool_name"] == "write" and r["tool_ok"] is True and isinstance(r["tool_response"], str)


def test_other_exit_code_is_notice_only(ee_home, workspace, hook_py, tmp_path):
    write_user_hooks(ee_home, {"PreToolUse": [{"command": cmd(hook_py, tmp_path / "l", "exit", "1")}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    ev = asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists()
    assert any("退出码 1" in t and "坏掉了" in t for t in notices(ev))


def test_timeout_kills_and_continues(ee_home, workspace, hook_py, tmp_path):
    write_user_hooks(ee_home, {"PreToolUse": [{"command": cmd(hook_py, tmp_path / "l", "sleep", "30"), "timeout": 1}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    t0 = time.monotonic()
    ev = asyncio.run(collect(s, "写"))
    assert time.monotonic() - t0 < 15
    assert (workspace / "a.txt").exists()
    assert any("超过 1 秒" in t for t in notices(ev))


# ------------------------------------------------------------------ UserPromptSubmit / SessionStart
def test_user_prompt_block(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "ups.log"
    write_user_hooks(ee_home, {"UserPromptSubmit": [{"command": cmd(hook_py, log, "exit2", "别贴密码")}]})
    s, fake = make_session(workspace, [])
    ev = asyncio.run(collect(s, "我的密码是 123"))
    assert done(ev).reason == "blocked" and not fake.calls
    assert not any(m.get("role") == "user" and "123" in str(m.get("content")) for m in s.history)
    assert any("别贴密码" in t for t in notices(ev))
    assert read_log(log)[0]["prompt"] == "我的密码是 123"


def test_session_start_and_prompt_context(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "ss.log"
    write_user_hooks(ee_home, {
        "SessionStart": [{"matcher": "startup", "command": cmd(hook_py, log, "print", "当前分支 feat-x")}],
        "UserPromptSubmit": [{"command": cmd(hook_py, tmp_path / "u.log", "print", "今天是周一")}],
    })
    s, fake = make_session(workspace, [text_chunks("一") + finish("stop"), text_chunks("二") + finish("stop")])
    asyncio.run(collect(s, "你好"))
    asyncio.run(collect(s, "再来"))
    first = [m for m in fake.calls[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "当前分支 feat-x" in first and "今天是周一" in first
    second = [m for m in fake.calls[1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "当前分支 feat-x" not in second and "今天是周一" in second
    rec = read_log(log)
    assert len(rec) == 1 and rec[0]["source"] == "startup"   # 只在会话开始跑一次


# ------------------------------------------------------------------ Stop
def test_stop_continue_once(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "stop.log"
    write_user_hooks(ee_home, {"Stop": [{"command": cmd(hook_py, log, "stop-once", "还没跑测试")}]})
    s, fake = make_session(workspace, [text_chunks("做完了") + finish("stop"), text_chunks("测试也跑了") + finish("stop")])
    ev = asyncio.run(collect(s, "做事"))
    assert len(fake.calls) == 2 and done(ev).reason == "completed"
    last_user = [m for m in fake.calls[1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "还没跑测试" in last_user
    rec = read_log(log)
    assert [r["stop_hook_active"] for r in rec] == [False, True]
    assert rec[0]["last_assistant_message"] == "做完了"


def test_stop_continue_capped(ee_home, workspace, hook_py, tmp_path):
    write_user_hooks(ee_home, {"Stop": [{"command": cmd(hook_py, tmp_path / "l", "exit2", "继续")}]})
    s, fake = make_session(workspace, [text_chunks(f"第{i}次") + finish("stop") for i in range(6)])
    ev = asyncio.run(collect(s, "做事"))
    assert len(fake.calls) == H.MAX_STOP_CONTINUES + 1
    assert any("连续" in t for t in notices(ev))


# ------------------------------------------------------------------ Notification
def test_notification_on_approval(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "n.log"
    write_user_hooks(ee_home, {"Notification": [{"matcher": "permission_prompt", "command": cmd(hook_py, log)}]})
    outside = workspace.parent / "o.txt"
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": str(outside), "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])

    async def go():
        evs = await collect(s, "写")
        for _ in range(200):
            if log.exists():
                break
            await asyncio.sleep(0.05)
        return evs
    ev = asyncio.run(go())
    assert any(isinstance(e, ApprovalRequest) for e in ev)
    r = read_log(log)[0]
    assert r["hook_event_name"] == "Notification" and r["notification_type"] == "permission_prompt"
    assert "o.txt" in r["message"]


# ------------------------------------------------------------------ 字符串命令走 shell
def test_string_command_via_shell(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "sh.log"
    exe, script = sys.executable, str(hook_py)
    if sys.platform == "win32":
        command = f'& "{exe}" "{script}" "{log}" exit2 "shell 拦截"'      # 默认 shell 是 PowerShell
    else:
        command = f'"{exe}" "{script}" "{log}" exit2 "shell 拦截"'
    write_user_hooks(ee_home, {"PreToolUse": [{"command": command}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    asyncio.run(collect(s, "写"))
    assert not (workspace / "a.txt").exists()
    assert "shell 拦截" in [m for m in s.history if m.get("role") == "tool"][0]["content"]


# ------------------------------------------------------------------ 项目 hooks 信任
def _project(workspace, hook_py, log, msg="项目拦截"):
    (workspace / ".git").mkdir(exist_ok=True)
    (workspace / ".ee").mkdir(exist_ok=True)
    p = workspace / ".ee" / "hooks.toml"
    p.write_text(dump_toml({"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, log, "exit2", msg)}]}),
                 encoding="utf-8")
    return p


def _write_turn(workspace, name):
    return make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": name, "content": "x"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])


def test_project_hooks_need_allow(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "proj.log"
    p = _project(workspace, hook_py, log)
    # 未允许：不执行，提示一次
    s, _ = _write_turn(workspace, "a.txt")
    ev = asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists() and not read_log(log)
    assert any("还没被允许" in t for t in notices(ev))
    cfg = H.load(workspace)
    assert len(cfg.projects) == 1 and not cfg.projects[0].allowed
    aid = cfg.projects[0].allow_id
    # 允许后：执行
    write_user_hooks(ee_home, {}, allowed_project=[aid])
    s, _ = _write_turn(workspace, "b.txt")
    asyncio.run(collect(s, "写"))
    assert not (workspace / "b.txt").exists() and len(read_log(log)) == 1
    assert read_log(log)[0]["_env"]["EE_HOOK_SOURCE"] == "project"
    # 内容变了：需要重新允许
    _project(workspace, hook_py, log, msg="改过的规则")
    assert H.load(workspace).projects[0].allow_id != aid
    s, _ = _write_turn(workspace, "c.txt")
    asyncio.run(collect(s, "写"))
    assert (workspace / "c.txt").exists() and len(read_log(log)) == 1
    assert p.exists()


def test_project_config_cannot_self_allow(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "pc.log"
    (workspace / ".git").mkdir()
    (workspace / ".ee").mkdir()
    table = {"PreToolUse": [{"command": cmd(hook_py, log, "exit2", "x")}]}
    aid = H.allow_id(workspace / ".ee" / "config.toml", table)
    (workspace / ".ee" / "config.toml").write_text(
        dump_toml({"hooks": {"enabled": True, "allowed_project": [aid], **table}}), encoding="utf-8")
    s, _ = _write_turn(workspace, "a.txt")
    asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists() and not read_log(log)
    cfg = H.load(workspace)
    assert cfg.projects[0].file.endswith("config.toml") and not cfg.projects[0].allowed and not cfg.user


def test_disabled_globally(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "d.log"
    write_user_hooks(ee_home, {"PreToolUse": [{"command": cmd(hook_py, log, "exit2", "x")}]}, enabled=False)
    s, _ = _write_turn(workspace, "a.txt")
    asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists() and not read_log(log)


def test_parallel_hooks_combined(ee_home, workspace, hook_py, tmp_path):
    write_user_hooks(ee_home, {"PreToolUse": [
        {"command": cmd(hook_py, tmp_path / "a.log", "sleep", "1")},
        {"command": cmd(hook_py, tmp_path / "b.log", "sleep", "1")},
        {"command": cmd(hook_py, tmp_path / "c.log", "exit2", "第三个拦截")},
    ]})
    s, _ = _write_turn(workspace, "a.txt")
    t0 = time.monotonic()
    asyncio.run(collect(s, "写"))
    assert time.monotonic() - t0 < 2.5 or sys.platform == "win32"   # 并行（Windows 启动慢，不卡时间）
    assert not (workspace / "a.txt").exists()
    assert all((tmp_path / n).exists() for n in ("a.log", "b.log", "c.log"))


# ------------------------------------------------------------------ 设置 / RPC / 界面条目
def test_settings_roundtrip(ee_home, hook_py, tmp_path):
    from ee.server.settings import get_settings, set_settings
    got = get_settings()["settings"]["hooks"]
    assert got["enabled"] is True and got["events"]["PreToolUse"] == []
    c = cmd(hook_py, tmp_path / "l")
    r = set_settings({"hooks": {"enabled": True, "allowed_project": ["aaaa:bbbb"], "events": {
        "PreToolUse": [{"matcher": "shell", "command": c, "timeout": 30, "enabled": True, "name": "检查"}],
        "Stop": [{"command": "echo hi", "shell": "bash", "enabled": False}]}}})
    h = r["settings"]["hooks"]
    assert h["allowed_project"] == ["aaaa:bbbb"]
    assert h["events"]["PreToolUse"][0] == {"matcher": "shell", "command": c, "timeout": 30, "shell": "",
                                           "enabled": True, "name": "检查"}
    assert h["events"]["Stop"][0]["enabled"] is False
    data = load_user_file()
    assert data["hooks"]["PreToolUse"][0]["command"] == c
    # 其他设置保存时不丢 hooks
    set_settings({"shell": "auto"})
    assert load_user_file()["hooks"]["PreToolUse"][0]["name"] == "检查"
    with pytest.raises(ValueError):
        set_settings({"hooks": {"events": {"PreToolUse": [{"command": ""}]}}})
    with pytest.raises(ValueError):
        set_settings({"hooks": {"events": {"Bogus": [{"command": "x"}]}}})


def test_rpc_list_test_and_notice_items(ee_home, workspace, hook_py, tmp_path):
    log = tmp_path / "r.log"
    write_user_hooks(ee_home, {"UserPromptSubmit": [{"command": cmd(hook_py, log, "exit2", "不行")}]})
    _project(workspace, hook_py, tmp_path / "p.log")

    async def go():
        b, fake, ev = make_backend(ee_home, [])
        lst = await b.call("hooks/list", {"cwd": str(workspace)})
        assert [h["event"] for h in lst["user"]] == ["UserPromptSubmit"]
        assert lst["projects"][0]["allowed"] is False and len(lst["events"]) == 6
        t = await b.call("hooks/test", {"event": "PreToolUse", "hook": {"command": cmd(hook_py, log, "exit2", "测试拦截")},
                                        "cwd": str(workspace)})
        assert t["code"] == 2 and t["verdict"] == "拦截" and t["reason"] == "测试拦截"
        assert t["payload"]["tool_name"] == "shell"
        t2 = await b.call("hooks/test", {"event": "UserPromptSubmit", "hook": {"command": cmd(hook_py, log, "print", "上下文")},
                                         "cwd": str(workspace)})
        assert t2["verdict"] == "继续" and t2["context"] == "上下文"
        aid = lst["projects"][0]["allow_id"]
        try:
            await b.call("hooks/test", {"event": "PreToolUse", "hook": {"command": "x"}, "cwd": str(workspace),
                                        "allow_id": aid})
            raise AssertionError("未允许的项目 hook 不应能测试")
        except Exception as e:  # noqa: BLE001
            assert "允许" in str(e)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "你好"})
        await wait_for(ev, "turn/completed")
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        texts = [it["text"] for it in d["items"] if it["type"] == "notice"]
        assert any("Hook 拦下了这条消息" in x and "不行" in x for x in texts)
        assert any("还没被允许" in x for x in texts)
        assert not fake.calls
    asyncio.run(go())


def test_hook_allow_does_not_skip_forced_ask():
    """Hook 返回 allow 不能跳过危险命令的强制确认（2026-10-26 审查补）。"""
    from ee.core.hooks import PreResult, adjust_decision
    from ee.core.permissions import Decision
    forced = Decision("ask", "危险", "", force=True)
    assert adjust_decision(forced, PreResult(args={}, permission="allow")).action == "ask"
    normal = Decision("ask", "要问", "npm run")
    assert adjust_decision(normal, PreResult(args={}, permission="allow")).action == "allow"


# ------------------------------------------------------------------ 审查补充（ToolStart 参数 / continue:false / 取消不阻塞）
def test_tool_start_shows_hook_updated_input(ee_home, workspace, hook_py, tmp_path):
    from ee.core.events import ToolStart
    out = json.dumps({"hookSpecificOutput": {"updatedInput": {"path": "b.txt", "content": "改过的"}}},
                     ensure_ascii=False)
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "原来的"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    ev = asyncio.run(collect(s, "写"))
    starts = [e for e in ev if isinstance(e, ToolStart)]
    assert len(starts) == 1
    assert starts[0].args == {"path": "b.txt", "content": "改过的"}
    assert (workspace / "b.txt").exists() and not (workspace / "a.txt").exists()


def test_post_tool_continue_false_ends_turn(ee_home, workspace, hook_py, tmp_path):
    out = json.dumps({"continue": False, "stopReason": "构建坏了，先停下"}, ensure_ascii=False)
    write_user_hooks(ee_home, {"PostToolUse": [{"matcher": "write", "command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("继续干") + finish("stop"),
    ])
    ev = asyncio.run(collect(s, "写"))
    assert (workspace / "a.txt").exists()                  # 工具已经执行
    assert len(fake.calls) == 1                            # 不再请求模型
    assert any("构建坏了，先停下" in t for t in notices(ev))
    assert [m for m in s.history if m.get("role") == "tool"]   # 工具结果仍记入历史
    assert done(ev).reason == "completed"


def test_pre_tool_continue_false_ends_turn(ee_home, workspace, hook_py, tmp_path):
    out = json.dumps({"continue": False, "stopReason": "今天不许动代码"}, ensure_ascii=False)
    write_user_hooks(ee_home, {"PreToolUse": [{"matcher": "write", "command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "x"}) + finish("tool_calls"),
        text_chunks("继续干") + finish("stop"),
    ])
    ev = asyncio.run(collect(s, "写"))
    assert not (workspace / "a.txt").exists()
    assert len(fake.calls) == 1
    assert any("今天不许动代码" in t for t in notices(ev))


def test_stop_continue_false_wins_over_block(ee_home, workspace, hook_py, tmp_path):
    out = json.dumps({"decision": "block", "reason": "再跑一遍", "continue": False, "stopReason": "预算用完"},
                     ensure_ascii=False)
    write_user_hooks(ee_home, {"Stop": [{"command": cmd(hook_py, tmp_path / "l", "json", out)}]})
    s, fake = make_session(workspace, [text_chunks(f"第{i}次") + finish("stop") for i in range(3)])
    ev = asyncio.run(collect(s, "做事"))
    assert len(fake.calls) == 1
    assert any("预算用完" in t for t in notices(ev))
    assert not any("要求继续" in t for t in notices(ev))


def test_cancel_hook_does_not_block_event_loop(monkeypatch, tmp_path):
    """Windows 上取消 hook 时 taskkill 可能要等好几秒，不能在事件循环里同步调用。"""
    from ee import proc as P
    real_kill = P.kill_tree
    calls = []

    def slow_kill(pid):
        calls.append(pid)
        time.sleep(1.5)
        real_kill(pid)

    monkeypatch.setattr(H, "IS_WINDOWS", True)
    monkeypatch.setattr(H, "kill_tree", slow_kill)
    pidf = tmp_path / "pid"
    code = f"import os,time;open({str(pidf)!r},'w').write(str(os.getpid()));time.sleep(30)"
    hook = H.HookDef(event="PreToolUse", command=[sys.executable, "-c", code], timeout=60)

    async def main():
        t = asyncio.create_task(H.run_hook(hook, {}, tmp_path))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if pidf.exists() and pidf.read_text():
                break
        t0 = time.monotonic()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        took = time.monotonic() - t0
        await asyncio.sleep(2.5)                       # 后台把进程树杀掉
        return took

    took = asyncio.run(main())
    assert took < 0.8, took
    assert calls
    pid = int(pidf.read_text())
    try:
        state = open(f"/proc/{pid}/status").read()
        assert "\nState:\tZ" in state                    # 已死（尚未回收）
    except FileNotFoundError:
        pass


def test_one_broken_hook_does_not_break_the_turn(ee_home, workspace, hook_py, tmp_path, monkeypatch):
    """一个 hook 内部抛异常，不能让整个回合挂掉。

    run_hook 的契约写着「绝不抛异常（取消除外）」，但 fire() 用 asyncio.gather
    没有 return_exceptions=True：任何一个 hook 真抛出来（编码错误、ctypes、
    子解释器崩、第三方库 bug），整个 UserPromptSubmit 就炸，回合变 error。
    Hook 是用户的旁挂脚本，它的 bug 不能吃掉主流程。

    用 UserPromptSubmit 做触发点：它每条用户消息都会跑，不依赖工具调用。
    """
    log = tmp_path / "hook.log"
    write_user_hooks(ee_home, {"UserPromptSubmit": [{"command": cmd(hook_py, log)}]})

    called = []

    async def boom(*a, **kw):
        called.append(1)
        raise RuntimeError("hook 内部崩了")

    monkeypatch.setattr(H, "run_hook", boom)
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop")])
    ev = asyncio.run(collect(s, "你好"))
    assert called, "hook 没被触发，测试是空转的"
    assert done(ev).reason == "completed", "hook 抛异常不该打断回合"
