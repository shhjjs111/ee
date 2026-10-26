"""D4-8：永久允许规则、ask_user 提问工具、.eeignore。"""
import asyncio
import json
import sys

import pytest
from conftest import FakeClient, finish, run, text_chunks, tool_chunks

from ee.core import rules as R
from ee.core.eeignore import EeIgnore, for_dir
from ee.core.permissions import PermissionPolicy
from ee.core.providers.openai_chat import ChatClient
from ee.server.app import Backend, RpcError
from ee.tools import ToolContext, ToolState
from ee.tools.search import GlobTool, GrepTool


def make_backend(home, script):
    fake = FakeClient(script)
    b = Backend(home=home, client_factory=lambda spec: ChatClient(spec, client=fake))
    ev = []
    b.listeners.append(lambda m, p: ev.append((m, json.loads(json.dumps(p, default=str)))))
    return b, fake, ev


async def wait_for(ev, method, pred=lambda p: True, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while True:
        for m, p in ev:
            if m == method and pred(p):
                return p
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError(f"等不到 {method}：{[m for m, _ in ev]}")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- 永久允许规则
def test_rule_from_decision_and_store(ee_home, workspace):
    assert R.rule_from_decision("shell", "npm run") == "shell(npm run *)"
    assert R.rule_from_decision("shell", "") is None                     # 强制询问的不能永久允许
    assert R.rule_from_decision("write", "D:\\data") == "write(D:/data/*)"
    assert R.rule_from_decision("mcp__fs__write", "mcp__fs__write") == "mcp__fs__write"
    assert R.add(workspace, "shell(npm run *)", ee_home) is True
    assert R.add(workspace, "shell(npm run *)", ee_home) is False
    sub = workspace / "pkg"
    sub.mkdir()
    assert R.rules_for(sub, ee_home) == ["shell(npm run *)"]            # 子目录也生效
    assert R.rules_for(workspace.parent, ee_home) == []
    lst = R.list_all(ee_home)
    assert lst[0]["rule"] == "shell(npm run *)" and lst[0]["project"] == R.project_key(workspace)
    assert R.remove(lst[0]["project"], "shell(npm run *)", ee_home) is True
    assert R.remove(lst[0]["project"], "shell(npm run *)", ee_home) is False
    assert R.list_all(ee_home) == []


def test_allow_rule_matches_bare_prefix(workspace):
    p = PermissionPolicy(mode="default", workspace=workspace, allow=["shell(npm run *)"])
    assert p.check("shell", {"command": "npm run build"}).action == "allow"
    assert p.check("shell", {"command": "npm run"}).action == "allow"
    assert p.check("shell", {"command": "npm install left-pad"}).action == "ask"


CMD = "npm run lint-fake" if sys.platform != "win32" else "npm run lint-fake"


def test_forever_persists_and_applies_to_new_sessions(ee_home, workspace):
    script = [
        tool_chunks(0, "c1", "shell", {"command": "pip download nothing-here --no-deps -d x"}) + finish("tool_calls"),
        text_chunks("好了") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "下载"})
        appr = (await wait_for(ev, "item/started", lambda p: p["item"]["type"] == "approval"))["item"]
        assert appr["rule"] == "shell(pip download *)"
        await b.call("approval/respond", {"thread_id": tid, "call_id": appr["call_id"], "choice": "forever"})
        await wait_for(ev, "turn/completed")
        lst = (await b.call("permissions/list", {}))["rules"]
        assert [r["rule"] for r in lst] == ["shell(pip download *)"]
        # 新会话直接放行
        tid2 = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        s2 = await b.threads[tid2].ensure_session_async()
        assert s2.policy.check("shell", {"command": "pip download other"}).action == "allow"
        # 设置页删除：已打开会话里也去掉
        await b.call("permissions/remove", {"project": lst[0]["project"], "rule": "shell(pip download *)"})
        assert s2.policy.check("shell", {"command": "pip download other"}).action == "ask"

    asyncio.run(go())


# ---------------------------------------------------------------- ask_user
def test_ask_user_flow(ee_home, workspace):
    script = [
        tool_chunks(0, "q1", "ask_user", {"question": "用哪个数据库？", "options": ["SQLite（推荐）", "PostgreSQL"]})
        + finish("tool_calls"),
        lambda params: text_chunks("好，用 " + [m for m in params["messages"] if m.get("role") == "tool"][-1]["content"])
        + finish("stop"),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "建个项目"})
        q = (await wait_for(ev, "item/started", lambda p: p["item"]["type"] == "question"))["item"]
        assert q["question"] == "用哪个数据库？" and q["options"] == ["SQLite（推荐）", "PostgreSQL"]
        lst = (await b.call("thread/list", {}))["threads"]
        assert lst[0]["status"] == "waiting_approval"
        with pytest.raises(RpcError):          # 不能当审批回答
            await b.call("approval/respond", {"thread_id": tid, "call_id": q["call_id"], "choice": "once"})
        with pytest.raises(RpcError):
            await b.call("question/respond", {"thread_id": tid, "call_id": q["call_id"], "answer": "  "})
        await b.call("question/respond", {"thread_id": tid, "call_id": q["call_id"], "answer": "PostgreSQL"})
        done = await wait_for(ev, "turn/completed")
        assert done["reason"] == "completed"
        upd = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "question"][-1]
        assert upd["status"] == "answered" and upd["answer"] == "PostgreSQL"
        assert "用户回答：PostgreSQL" in [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"][-1]["content"]

    asyncio.run(go())


def test_ask_user_cancelled_on_interrupt(ee_home, workspace):
    script = [tool_chunks(0, "q1", "ask_user", {"question": "继续吗？"}) + finish("tool_calls")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "x"})
        await wait_for(ev, "item/started", lambda p: p["item"]["type"] == "question")
        await b.call("turn/interrupt", {"thread_id": tid})
        done = await wait_for(ev, "turn/completed")
        assert done["reason"] == "interrupted"
        q = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "question"][-1]
        assert q["status"] == "cancelled"

    asyncio.run(go())


def test_subagent_has_no_ask_user():
    from types import SimpleNamespace

    from ee.tools import build_tools
    prof = SimpleNamespace(edit_style="edit")
    assert "ask_user" in [t.name for t in build_tools(prof, {})]
    assert "ask_user" not in [t.name for t in build_tools(prof, {"ask_user": False})]


# ---------------------------------------------------------------- .eeignore
def test_eeignore_matcher(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".eeignore").write_text("# 注释\n*.env\nsecrets/\n/data/raw\n!keep.env\ndocs/**/private*.md\n",
                                        encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "src" / "secrets").mkdir(parents=True)
    ig = EeIgnore.load(tmp_path)
    cases = {"a.env": True, "src/b.env": True, "keep.env": False, "secrets": True, "secrets/x.txt": True,
             "src/secrets/y": True, "data/raw/1.csv": True, "src/data/raw/1": False,
             "docs/a/b/private1.md": True, "docs/private.md": True, "src/main.py": False}
    for p, want in cases.items():
        assert ig.ignored(tmp_path / p) is want, p
    # 子目录里的 .eeignore 叠加
    (tmp_path / "src" / ".eeignore").write_text("*.log\n", encoding="utf-8")
    ig2 = EeIgnore.load(tmp_path / "src")
    assert ig2.ignored(tmp_path / "src" / "x.log") and not ig2.ignored(tmp_path / "x.log")
    assert ig2.ignored(tmp_path / "a.env")


def test_eeignore_blocks_tools(workspace):
    (workspace / ".eeignore").write_text(".env\nprivate/\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (workspace / "private").mkdir()
    (workspace / "private" / "notes.txt").write_text("SECRET diary\n", encoding="utf-8")
    (workspace / "ok.txt").write_text("SECRET but fine\n", encoding="utf-8")
    p = PermissionPolicy(mode="auto", workspace=workspace)
    d = p.check("read", {"path": ".env"})
    assert d.action == "deny" and ".eeignore" in d.reason
    assert p.check("write", {"path": "private/new.txt"}).action == "deny"
    assert p.check("read", {"path": "ok.txt"}).action == "allow"
    ctx = ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / ".out")
    r = run(GrepTool().run({"pattern": "SECRET"}, ctx))
    assert "ok.txt" in r.output and "notes.txt" not in r.output and ".env" not in r.output
    r = run(GlobTool().run({"pattern": "**/*.txt"}, ctx))
    assert "ok.txt" in r.output and "notes.txt" not in r.output
    # 缓存：改了 .eeignore 立刻生效
    import os
    (workspace / ".eeignore").write_text("ok.txt\n", encoding="utf-8")
    st = os.stat(workspace / ".eeignore")
    os.utime(workspace / ".eeignore", ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert for_dir(workspace).ignored(workspace / "ok.txt")
    assert p.check("read", {"path": ".env"}).action == "allow"
