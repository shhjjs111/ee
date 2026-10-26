"""D4-3：计划模式（只读调研 → present_plan 提交方案并结束回合 → 批准后切模式执行）。"""
import asyncio

from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core.permissions import PermissionPolicy


def test_plan_mode_permissions(workspace):
    p = PermissionPolicy(mode="plan", workspace=workspace)
    assert p.check("write", {"path": "a.txt", "content": "x"}).action == "deny"
    assert p.check("shell", {"command": "rm a.txt"}).action == "deny"
    assert p.check("shell", {"command": "git status"}).action == "allow"
    assert p.check("read", {"path": "a.txt"}).action == "allow"
    assert p.check("present_plan", {"plan": "x"}).action == "allow"
    assert p.check("memory", {"action": "add", "text": "x"}).action == "deny"


def test_plan_flow(ee_home, workspace):
    plan = "## 目标\n写 a.txt\n## 步骤\n1. 写入 hello"
    script = [
        tool_chunks(0, "w0", "write", {"path": "a.txt", "content": "early"}) + finish("tool_calls"),
        tool_chunks(0, "p1", "present_plan", {"title": "写 a.txt", "plan": plan}) + finish("tool_calls"),
        # 批准后
        tool_chunks(0, "w1", "write", {"path": "a.txt", "content": "hello"}) + finish("tool_calls"),
        text_chunks("完成") + finish("stop"),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "plan"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "帮我写 a.txt"})
        await wait_for(ev, "turn/completed")
        assert not (workspace / "a.txt").exists()
        assert len(fake.calls) == 2          # present_plan 之后没有再请求模型
        first = "\n".join(str(m.get("content")) for m in fake.calls[0]["messages"])
        assert "计划模式" in first and "present_plan" in first
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        tools = [it for it in d["items"] if it["type"] == "tool"]
        assert tools[0]["status"] == "denied" or "拒绝" in tools[0]["output"]
        assert tools[1]["meta"]["kind"] == "plan_proposal" and tools[1]["meta"]["plan"] == plan
        assert d["mode"] == "plan"

        ev.clear()
        await b.call("turn/start", {"thread_id": tid, "text": "批准，按方案执行", "mode": "default"})
        await wait_for(ev, "turn/completed")
        assert (workspace / "a.txt").read_text() == "hello"
        third = "\n".join(str(m.get("content")) for m in fake.calls[2]["messages"])
        assert "权限模式：默认" in third
    asyncio.run(go())


def test_present_plan_outside_plan_mode(ee_home, workspace):
    script = [tool_chunks(0, "p1", "present_plan", {"plan": "x"}) + finish("tool_calls"),
              text_chunks("好的，直接做") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "做点事"})
        await wait_for(ev, "turn/completed")
        assert len(fake.calls) == 2          # 不是计划模式：报错后继续
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        t = [it for it in d["items"] if it["type"] == "tool"][0]
        assert t["status"] == "error" and "不是计划模式" in t["output"]
    asyncio.run(go())
