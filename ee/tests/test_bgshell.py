"""D4-6 后台长命令：shell background=true、shell_output / shell_write / shell_kill、权限、RPC、会话删除时清理。"""
import asyncio
import sys
from types import SimpleNamespace

from ee.core.bgproc import BgManager
from ee.core.permissions import PermissionPolicy
from ee.tools import ToolContext, ToolState
from ee.tools.bgshell import ShellKillTool, ShellOutputTool, ShellWriteTool
from ee.tools.shell import ShellTool
from ee.tools.shellenv import resolve_shell


def _py(sh, script: str) -> str:
    exe = sys.executable
    if sh.syntax == "powershell":
        return f"& '{exe}' -u {script}"
    return f'"{exe}" -u {script}'


def _ctx(ws, sh, mgr):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out", shell=sh,
                       extra={"session": SimpleNamespace(bg=mgr)})


SERVER = """
import sys, time
print("starting", flush=True)
time.sleep(0.3)
print("ready on http://127.0.0.1:5173", flush=True)
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        print("bye", flush=True)
        sys.exit(7)
    print("echo:" + line, flush=True)
"""


def test_background_lifecycle(workspace, tmp_path):
    (workspace / "srv.py").write_text(SERVER, encoding="utf-8")
    sh = resolve_shell("auto")
    events = []

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        mgr.on_change = events.append
        ctx = _ctx(workspace, sh, mgr)
        r = await ShellTool().run({"command": _py(sh, "srv.py"), "background": True}, ctx)
        assert r.ok and r.meta["background"] and r.meta["running"] and r.meta["bg_id"] == "b1"
        assert "仍在运行" in r.output and "starting" in r.output
        out = r.output
        if "ready on" not in out:     # 慢机器上开头 3 秒可能还没打印完
            r2 = await ShellOutputTool().run({"id": "b1", "wait": 10}, ctx)
            out += r2.output
        assert "ready on" in out
        # 没有新输出时 wait 到期返回
        r = await ShellOutputTool().run({"id": "b1", "wait": 1}, ctx)
        assert "没有新输出" in r.output and "运行中" in r.output
        # 交互：写 stdin
        r = await ShellWriteTool().run({"id": "b1", "input": "hello\n", "wait": 5}, ctx)
        assert "echo:hello" in r.output
        await ShellWriteTool().run({"id": "b1", "input": "a\nb\nc\n", "wait": 0}, ctx)
        r = await ShellOutputTool().run({"id": "b1", "wait": 5, "filter": "echo:c"}, ctx)
        assert "echo:c" in r.output and "echo:a" not in r.output
        # 退出码
        r = await ShellWriteTool().run({"id": "b1", "input": "quit\n", "wait": 5}, ctx)
        for _ in range(50):
            if not mgr.get("b1").running:
                break
            await asyncio.sleep(0.1)
        r = await ShellOutputTool().run({"id": "b1"}, ctx)
        assert mgr.get("b1").exit_code == 7 and "exit 7" in r.output
        log = mgr.get("b1").log_path.read_text(encoding="utf-8")
        assert "ready on" in log and "bye" in log
        # 已结束后不能再写
        r = None
        try:
            await ShellWriteTool().run({"id": "b1", "input": "x\n"}, ctx)
        except Exception as e:  # ToolError
            assert "已经结束" in str(e)
        # 未知 id
        try:
            await ShellOutputTool().run({"id": "b9"}, ctx)
            raise AssertionError("应报错")
        except Exception as e:
            assert "b9" in str(e) and "b1" in str(e)

    asyncio.run(go())
    assert [e["status"] for e in events] == ["running", "exited"]


def test_quick_command_finishes_in_yield(workspace, tmp_path):
    sh = resolve_shell("auto")

    async def go():
        mgr = BgManager(tmp_path / "logs")
        cmd = "echo done; exit 2" if sh.syntax == "powershell" else "echo done && exit 2"
        r = await ShellTool().run({"command": cmd, "background": True}, _ctx(workspace, sh, mgr))
        assert "done" in r.output and "已结束 · exit 2" in r.output and not r.ok

    asyncio.run(go())


LOOP = """
import time
i = 0
while True:
    print("tick", i, flush=True)
    i += 1
    time.sleep(0.05)
"""


def test_kill_and_close(workspace, tmp_path):
    (workspace / "loop.py").write_text(LOOP, encoding="utf-8")
    sh = resolve_shell("auto")

    async def go():
        mgr = BgManager(tmp_path / "logs")
        ctx = _ctx(workspace, sh, mgr)
        await ShellTool().run({"command": _py(sh, "loop.py"), "background": True}, ctx)
        await ShellTool().run({"command": _py(sh, "loop.py"), "background": True}, ctx)
        r = await ShellKillTool().run({"id": "b1"}, ctx)
        assert "已结束后台命令 b1" in r.output
        assert mgr.get("b1").status == "killed" and mgr.get("b2").running
        r = await ShellKillTool().run({"id": "b1"}, ctx)
        assert "本来就已结束" in r.output
        await mgr.close()             # 会话结束：全部结束
        assert not mgr.get("b2").running
        assert [p["status"] for p in mgr.list()] == ["killed", "killed"]

    asyncio.run(go())


def test_permissions(workspace):
    for mode in ("read-only", "plan"):
        p = PermissionPolicy(mode=mode, workspace=workspace)
        assert p.check("shell_output", {"id": "b1"}).action == "allow"
        assert p.check("shell_kill", {"id": "b1"}).action == "allow"
        assert p.check("shell_write", {"id": "b1", "input": "y\n"}).action == "deny"
    p = PermissionPolicy(mode="default", workspace=workspace)
    assert p.check("shell_write", {"id": "b1", "input": "y\n"}).action == "allow"
    # 后台启动和前台一样按命令走 shell 权限
    assert p.check("shell", {"command": "rm -rf build", "background": True}).action == "ask"
    p = PermissionPolicy(mode="default", workspace=workspace, deny=["shell_write"])
    assert p.check("shell_write", {"id": "b1"}).action == "deny"


def test_tools_registered_and_toggle():
    from ee.tools import build_tools
    prof = SimpleNamespace(edit_style="edit")
    names = [t.name for t in build_tools(prof, {})]
    assert names[:11] == ["read", "write", "edit", "shell", "grep", "glob", "todo", "present_plan",
                          "shell_output", "shell_write", "shell_kill"]
    names = [t.name for t in build_tools(prof, {"background_shell": False})]
    assert "shell_output" not in names


def test_server_rpc_and_turn_end_keeps_running(ee_home, workspace):
    """模型后台启动 → 回合结束进程还在 → shell/list|read|kill → 删除会话时清理。"""
    import json as _json
    from conftest import FakeClient, finish, text_chunks, tool_chunks
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.app import Backend

    (workspace / "loop.py").write_text(LOOP, encoding="utf-8")
    sh = resolve_shell("auto")
    script = [
        tool_chunks(0, "c1", "shell", {"command": _py(sh, "loop.py"), "background": True}) + finish("tool_calls"),
        text_chunks("已在后台运行") + finish("stop"),
    ]

    async def go():
        fake = FakeClient(script)
        b = Backend(home=ee_home, client_factory=lambda spec: ChatClient(spec, client=fake))
        ev = []
        b.listeners.append(lambda m, p: ev.append((m, _json.loads(_json.dumps(p, default=str)))))
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        assert (await b.call("shell/list", {"thread_id": tid}))["procs"] == []
        await b.call("turn/start", {"thread_id": tid, "text": "跑起来"})
        await b.threads[tid].task
        procs = (await b.call("shell/list", {"thread_id": tid}))["procs"]
        assert len(procs) == 1 and procs[0]["status"] == "running" and procs[0]["id"] == "b1"
        assert any(m == "shell/updated" and p["proc"]["status"] == "running" for m, p in ev)
        tool = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "tool"][-1]
        assert tool["meta"]["background"] and tool["meta"]["bg_id"] == "b1"
        r = await b.call("shell/read", {"thread_id": tid, "id": "b1"})
        assert "tick" in r["output"]
        r = await b.call("shell/kill", {"thread_id": tid, "id": "b1"})
        assert r["proc"]["status"] == "killed"
        # 再起一个，删除会话时被清理
        mgr = b.threads[tid].session.bg
        ctx = _ctx(workspace, sh, mgr)
        await ShellTool().run({"command": _py(sh, "loop.py"), "background": True}, ctx)
        assert mgr.get("b2").running
        b.thread_delete(tid)
        for _ in range(50):
            if not mgr.get("b2").running:
                break
            await asyncio.sleep(0.1)
        assert mgr.get("b2").status == "killed"

    asyncio.run(go())
