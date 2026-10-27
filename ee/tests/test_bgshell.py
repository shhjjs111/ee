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
        # 首屏不一定已经拿到子进程的第一行输出（Windows 上起一个 Python 子进程要 1-3 秒，
        # PowerShell 更慢），所以只断言“启动回执”本身；子进程输出用 shell_output 等。
        assert "仍在运行" in r.output
        out = r.output
        if "starting" not in out:     # 慢机器上开头可能还没打印完
            r2 = await ShellOutputTool().run({"id": "b1", "wait": 10}, ctx)
            out += r2.output
        assert "starting" in out
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
        # 启动回执的 ok 只反映「那一刻还在跑」，不代表命令最终成功与否；
        # 命令是 `exit 2`，所以要验的是退出码如实传出来（shell_output 的 meta["exit"]）。
        # 首屏输出同样是尽力而为：Windows 起进程要 1-3 秒，可能还没来得及跑完。
        assert r.meta.get("bg_id")
        out = r.output
        bg_id = r.meta["bg_id"]
        r2 = await ShellOutputTool().run({"id": bg_id, "wait": 10}, _ctx(workspace, sh, mgr))
        out += r2.output
        for _ in range(100):                  # 起进程慢时再等一会儿结束
            if r2.meta.get("exit") is not None:
                break
            await asyncio.sleep(0.2)
            r2 = await ShellOutputTool().run({"id": bg_id}, _ctx(workspace, sh, mgr))
            out += r2.output
        assert "done" in out and "已退出 · exit 2" in out
        assert r2.meta.get("exit") == 2, f"退出码应为 2，实际 {r2.meta.get('exit')}"

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

    async def go():
        # 命令语法必须和会话最终用的 shell 一致：会话的 shell 来自配置（用户的
        # ~/.ee 可能设了 git-bash），而 resolve_shell("auto") 只按平台挑默认值。
        # 两者不一致时会生成错误语法（例如 PowerShell 的 `& '...'` 交给 bash 执行）。
        # 这里不建真实会话（那会去要 API key），直接按配置解析出会话会用哪个 shell。
        from ee.config import load_config
        cfg = load_config(workspace)
        try:
            sh = resolve_shell(cfg.data.get("shell", "auto"))
        except Exception:  # noqa: BLE001
            sh = resolve_shell("auto")
        script = [
            tool_chunks(0, "c1", "shell", {"command": _py(sh, "loop.py"), "background": True}) + finish("tool_calls"),
            text_chunks("已在后台运行") + finish("stop"),
        ]
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
        assert tool["details"]["background"] and tool["details"]["bg_id"] == "b1"
        # 启动回执里的首屏输出是尽力而为：Windows PowerShell 5.1 在某类机器上光是
        # 进程初始化就要 4 秒以上（实测同一台机器 cmd/git 是 0.05s、csc.exe 是 0.09s，
        # 只有 powershell.exe 是 4.3s），这段时间后台进程一个字节都还没产出。
        # 这里要验的是「模型能读到子进程输出」，不是「启动瞬间就有输出」，所以轮询到有为止。
        got = ""
        for _ in range(100):                      # 最多等 20 秒
            r = await b.call("shell/read", {"thread_id": tid, "id": "b1"})
            got = r["output"]
            if "tick" in got:
                break
            await asyncio.sleep(0.2)
        assert "tick" in got, f"20 秒内没读到子进程输出，实际={got[:200]!r}"
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


def test_crlf_split_across_reads_is_normalized(tmp_path):
    """\\r\\n 恰好被拆在两次读取之间时，也要合成 \\n（不留孤立的 \\r）。"""
    sh = resolve_shell("auto")
    script = tmp_path / "crlf.py"
    script.write_text("import sys, time\n"
                      "sys.stdout.buffer.write(b'a\\r'); sys.stdout.flush(); time.sleep(0.4)\n"
                      "sys.stdout.buffer.write(b'\\nb\\r\\n'); sys.stdout.flush()\n", encoding="utf-8")

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        p = await mgr.start(_py(sh, str(script)), sh, tmp_path, dict(__import__("os").environ))
        for _ in range(200):
            if not p.running:
                break
            await asyncio.sleep(0.05)
        # running=False 不等于输出已经读完：读线程可能还在收最后一块。等到日志落全。
        for _ in range(100):
            try:
                if p.log_path.read_text(encoding="utf-8") == "a\nb\n":
                    break
            except OSError:
                pass
            await asyncio.sleep(0.05)
        return p

    p = asyncio.run(go())
    assert p.tail() == "a\nb\n", repr(p.tail())
    assert p.log_path.read_text(encoding="utf-8") == "a\nb\n"


def test_worktree_remove_kills_bg_before_removing(tmp_path, ee_home, monkeypatch):
    """Windows 上运行中的进程会占住 worktree 目录，必须先结束后台命令再删目录。"""
    import shutil
    import subprocess
    import pytest
    if shutil.which("git") is None:
        pytest.skip("需要 git")
    from conftest import FakeClient
    from ee.core import worktree as W
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.app import Backend
    repo = tmp_path / "proj"
    repo.mkdir()
    for a in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                       capture_output=True)
    b = Backend(home=ee_home, client_factory=lambda spec: ChatClient(spec, client=FakeClient([])))
    seen = []
    real_remove = W.remove

    def spy(*a, **k):
        seen.append([p.status for p in procs])
        return real_remove(*a, **k)

    monkeypatch.setattr(W, "remove", spy)
    procs = []

    async def go():
        th = (await b.call("thread/start", {"cwd": str(repo), "worktree": {"name": "bg"}}))["thread"]
        s = await b.threads[th["id"]].ensure_session_async()
        sh = resolve_shell("auto")
        p = await s.bg.start(_py(sh, '-c "import time; time.sleep(30)"'), sh, s.cwd, dict(__import__("os").environ))
        procs.append(p)
        await b.call("worktree/remove", {"path": th["cwd"]})
        if p.running:
            await s.bg.kill(p)

    asyncio.run(go())
    assert seen == [["killed"]] or seen == [["exited"]], seen
