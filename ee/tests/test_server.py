"""桌面端后端（ee serve）测试：方法分发、事件→条目、审批、持久化、设置读写、git 审阅。全部不联网。"""
import asyncio
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeClient, finish, text_chunks, tool_chunks

from ee.core.providers.openai_chat import ChatClient
from ee.server.app import Backend, RpcError, serve

SHELL_CMD = "touch made.txt" if sys.platform != "win32" else "New-Item made.txt"


def make_backend(home, script, delay=0.0):
    fake = FakeClient(script, delay=delay)
    b = Backend(home=home, client_factory=lambda spec: ChatClient(spec, client=fake))
    events: list[tuple[str, dict]] = []
    b.listeners.append(lambda m, p: events.append((m, json.loads(json.dumps(p, default=str)))))
    return b, fake, events


async def wait_for(events, method, pred=lambda p: True, timeout=30.0):
    """等某个事件出现。

    超时默认 30 秒而不是 5 秒：回合里经常要跑一次真实子进程（shell 工具），
    Windows 上 PowerShell 冷启动本身就要 5～6 秒（实测 `powershell -NoProfile
    -Command "New-Item made.txt"` 连跑三次为 5.38/5.94/5.47 秒），5 秒超时会让
    `turn/completed` 卡在临界线上随机失败。这里只放宽等待上限，不改变断言内容：
    真卡住的回合照样会在 30 秒后失败。
    """
    t0 = asyncio.get_running_loop().time()
    while True:
        for m, p in events:
            if m == method and pred(p):
                return p
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError(f"等不到 {method}；收到：{[m for m, _ in events]}")
        await asyncio.sleep(0.01)


def test_tool_call_approval_and_stream(ee_home, workspace):
    script = [
        tool_chunks(0, "c1", "shell", {"command": SHELL_CMD}) + finish("tool_calls"),
        text_chunks("已经创建好了 made.txt") + finish("stop"),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        tid = th["id"]
        r = await b.call("turn/start", {"thread_id": tid, "text": "创建一个文件"})
        assert r["queued"] is False
        appr = await wait_for(ev, "item/started", lambda p: p["item"]["type"] == "approval")
        item = appr["item"]
        assert item["status"] == "pending" and "made.txt" in item["title"]
        lst = (await b.call("thread/list", {}))["threads"]
        assert lst[0]["status"] == "waiting_approval"
        await b.call("approval/respond", {"thread_id": tid, "call_id": item["call_id"], "choice": "once"})
        done = await wait_for(ev, "turn/completed")
        assert done["reason"] == "completed"
        assert (workspace / "made.txt").exists()
        tool = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "tool"][-1]
        assert tool["status"] == "ok" and tool["name"] == "shell"
        deltas = "".join(p["delta"] for m, p in ev if m == "item/delta" and p["field"] == "text")
        assert deltas == "已经创建好了 made.txt"
        full = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        kinds = [it["type"] for it in full["items"]]
        assert kinds[0] == "user" and "approval" in kinds and "tool" in kinds and kinds[-1] == "turn_end"
        assert full["title"] == "创建一个文件"
        assert full["usage"]["requests"] == 2
        # 第二次请求里有 tool 结果
        assert any(m.get("role") == "tool" for m in fake.calls[1]["messages"])

        # 重新打开（新进程）：从磁盘恢复条目和历史
        b2, _, _ = make_backend(ee_home, [text_chunks("好的") + finish("stop")])
        lst = (await b2.call("thread/list", {}))["threads"]
        assert lst[0]["id"] == tid and lst[0]["title"] == "创建一个文件"
        again = (await b2.call("thread/get", {"thread_id": tid}))["thread"]
        assert [it["type"] for it in again["items"]] == kinds
        assert again["items"][[i for i, k in enumerate(kinds) if k == "approval"][0]]["status"] == "once"
        await b2.call("turn/start", {"thread_id": tid, "text": "谢谢"})
        t = b2.threads[tid]
        await t.task
        assert len(t.session.history) >= 5   # 旧历史 + 新回合
    asyncio.run(go())


def test_deny_and_interrupt(ee_home, workspace):
    script = [
        tool_chunks(0, "c1", "shell", {"command": SHELL_CMD}) + finish("tool_calls"),
        text_chunks("好，不创建了") + finish("stop"),
        text_chunks("这是一段很长很长的回答" * 20, n=1) + finish("stop"),
    ]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "创建文件"})
        appr = (await wait_for(ev, "item/started", lambda p: p["item"]["type"] == "approval"))["item"]
        await b.call("approval/respond", {"thread_id": tid, "call_id": appr["call_id"], "choice": "deny",
                                          "reason": "别动我的目录"})
        await wait_for(ev, "turn/completed")
        assert not (workspace / "made.txt").exists()
        tool_msg = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"][0]
        assert "别动我的目录" in tool_msg["content"]
        with pytest.raises(RpcError):
            await b.call("approval/respond", {"thread_id": tid, "call_id": appr["call_id"], "choice": "once"})
        # 中断
        ev.clear()
        fake.delay = 0.02
        await b.call("turn/start", {"thread_id": tid, "text": "长篇大论"})
        await wait_for(ev, "item/delta")
        assert (await b.call("turn/interrupt", {"thread_id": tid}))["interrupted"] is True
        done = await wait_for(ev, "turn/completed")
        assert done["reason"] == "interrupted"
    asyncio.run(go())


def test_queue_while_running(ee_home, workspace):
    script = [text_chunks("第一段回答", n=1) + finish("stop"), text_chunks("收到补充") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script, delay=0.02)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "你好"})
        await wait_for(ev, "item/delta")
        r = await b.call("turn/start", {"thread_id": tid, "text": "再补充一句"})
        assert r["queued"] is True
        await wait_for(ev, "turn/completed")
        assert len(fake.calls) == 2
    asyncio.run(go())


def test_errors(ee_home, workspace):
    async def go():
        b, _, _ = make_backend(ee_home, [])
        with pytest.raises(RpcError):
            await b.call("nope", {})
        with pytest.raises(RpcError):
            await b.call("thread/start", {"cwd": str(workspace / "missing")})
        with pytest.raises(RpcError):
            await b.call("thread/get", {"thread_id": "nope"})
        with pytest.raises(RpcError):
            await b.call("thread/start", {"bad": 1})
    asyncio.run(go())


def test_no_key_error_is_friendly(ee_home, workspace, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    async def go():
        b = Backend(home=ee_home)
        ev = []
        b.listeners.append(lambda m, p: ev.append((m, p)))
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "hi"})
        await wait_for(ev, "turn/completed")
        err = [p["item"] for m, p in ev if m == "item/started" and p["item"]["type"] == "error"][0]
        assert "DEEPSEEK_API_KEY" in err["message"] and "设置" in err["message"]
    asyncio.run(go())


def test_settings_roundtrip(ee_home, monkeypatch):
    monkeypatch.setenv("MY_ENV_KEY", "sk-from-env")

    async def go():
        b, _, ev = make_backend(ee_home, [])
        s = await b.call("settings/get", {})
        st = s["settings"]
        assert st["default_model"] == "deepseek-flash" and "deepseek" in st["providers"]
        assert st["providers"]["deepseek"]["builtin"] is True
        assert "pwsh" in s["choices"]["shells"] and "read-only" in s["choices"]["modes"]
        assert st["models"]["deepseek-flash"]["default_price"]["input"] is not None
        st["providers"]["myproxy"] = {"base_url": "https://proxy.example.com/v1", "api_key_env": "MY_ENV_KEY",
                                      "api_key": "sk-secret-123456"}
        st["models"]["gpt-x"] = {"provider": "myproxy", "model": "gpt-4.1", "family": "auto",
                                 "price": {"input": "2", "output": 8, "cache_read": None, "currency": "$"}}
        st["default_model"] = "gpt-x"
        st["shell"] = "git-bash"
        st["permissions"] = {"mode": "auto"}
        st["ui"] = {"theme": "dark"}
        res = await b.call("settings/set", {"settings": st})
        out = res["settings"]
        assert out["default_model"] == "gpt-x" and out["shell"] == "git-bash"
        assert out["ui"]["theme"] == "dark" and out["permissions"]["mode"] == "auto"
        p = out["providers"]["myproxy"]
        assert p["api_key_set"] and p["api_key_preview"] == "…3456" and p["env_present"]
        assert "sk-secret-123456" not in json.dumps(res)
        assert out["models"]["gpt-x"]["detected_family"] == "gpt"
        assert out["models"]["gpt-x"]["price"]["input"] == 2
        text = (ee_home / "config.toml").read_text(encoding="utf-8")
        assert "sk-secret-123456" not in text and "[models.gpt-x.price]" in text     # 明文不落盘
        from ee.core import secrets
        assert secrets.get("provider/myproxy") == "sk-secret-123456"
        assert p["api_key_source"] == "store" and res["settings"]["secrets"]["backend"] == "memory"
        # 再存一次但不带 api_key：key 保留
        out["providers"]["myproxy"].pop("api_key", None)
        await b.call("settings/set", {"settings": out})
        assert secrets.get("provider/myproxy") == "sk-secret-123456"
        assert not list(ee_home.glob("config.toml*.bak")) or all(
            "sk-secret-123456" not in x.read_text(encoding="utf-8") for x in ee_home.glob("config.toml*.bak"))
        # 显式清空
        out["providers"]["myproxy"]["api_key"] = ""
        res = await b.call("settings/set", {"settings": out})
        assert res["settings"]["providers"]["myproxy"]["api_key_set"] is False
        assert secrets.get("provider/myproxy") is None
        models = await b.call("models/list", {})
        assert models["default"] == "gpt-x"
        gx = [m for m in models["models"] if m["name"] == "gpt-x"][0]
        assert gx["has_key"] is True and gx["family"] == "gpt"   # 来自环境变量
        # 校验
        bad = json.loads(json.dumps(out))
        bad["default_model"] = "nope"
        with pytest.raises(RpcError):
            await b.call("settings/set", {"settings": bad})
        bad = json.loads(json.dumps(out))
        bad["models"]["z"] = {"provider": "ghost", "model": "z"}
        with pytest.raises(RpcError):
            await b.call("settings/set", {"settings": bad})
        assert any(m == "settings/changed" for m, _ in ev)
        # 其他手写的键保留
        (ee_home / "config.toml").write_text((ee_home / "config.toml").read_text(encoding="utf-8")
                                             + '\n[permissions]\nmode = "auto"\nallow = ["shell:npm test"]\n',
                                             encoding="utf-8")
    asyncio.run(go())


def test_settings_preserves_unknown_keys(ee_home):
    (ee_home / "config.toml").write_text('bash_timeout = 300\n\n[permissions]\nmode = "default"\n'
                                         'deny = ["shell:rm -rf *"]\n', encoding="utf-8")

    async def go():
        b, _, _ = make_backend(ee_home, [])
        st = (await b.call("settings/get", {}))["settings"]
        assert st["bash_timeout"] == 300
        st["permissions"]["mode"] = "read-only"
        await b.call("settings/set", {"settings": st})
        text = (ee_home / "config.toml").read_text(encoding="utf-8")
        assert 'deny = ["shell:rm -rf *"]' in text and 'mode = "read-only"' in text
        assert (ee_home / "config.toml.bak").is_file()
    asyncio.run(go())


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_diff_and_revert(ee_home, workspace):
    def git(*a):
        subprocess.run(["git", *a], cwd=workspace, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (workspace / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    (workspace / "a.py").write_text("x = 1\ny = 3\nz = 4\n", encoding="utf-8")
    (workspace / "new.txt").write_text("hello\n", encoding="utf-8")

    async def go():
        b, _, _ = make_backend(ee_home, [])
        r = await b.call("git/diff", {"cwd": str(workspace)})
        assert r["is_repo"]
        files = {f["path"]: f for f in r["files"]}
        assert files["a.py"]["additions"] == 2 and files["a.py"]["deletions"] == 1
        assert files["new.txt"]["status"] == "added" and files["new.txt"]["additions"] == 1
        assert "+z = 4" in files["a.py"]["patch"]
        assert (await b.call("git/revert_file", {"cwd": str(workspace), "path": "a.py"}))["action"] == "restored"
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"
        assert (await b.call("git/revert_file", {"cwd": str(workspace), "path": "new.txt"}))["action"] == "deleted"
        assert not (workspace / "new.txt").exists()
        assert (await b.call("git/diff", {"cwd": str(workspace)}))["files"] == []
        with pytest.raises(RpcError):
            await b.call("git/revert_file", {"cwd": str(workspace), "path": "../outside.txt"})
    asyncio.run(go())


def test_workspaces_and_files(ee_home, workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (workspace / "README.md").write_text("# x\n", encoding="utf-8")

    async def go():
        b, _, _ = make_backend(ee_home, [])
        ws = (await b.call("workspace/open", {"path": str(workspace)}))["workspaces"]
        assert ws[0]["path"] == str(workspace.resolve()) and ws[0]["exists"]
        files = (await b.call("files/search", {"cwd": str(workspace), "query": "main"}))["files"]
        assert files == ["src/main.py"]
        ws = (await b.call("workspace/remove", {"path": str(workspace.resolve())}))["workspaces"]
        assert ws == []
    asyncio.run(go())


# ------------------------------------------------------------------ 端到端：真 WebSocket + 真 openai SDK + 假 HTTP 模型服务

class _FakeModel(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeModel.requests.append({"body": body, "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        if len(_FakeModel.requests) == 1:
            args = json.dumps({"command": SHELL_CMD})
            frames = [
                {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "先建文件"},
                                      "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1",
                    "type": "function", "function": {"name": "shell", "arguments": args}}]}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 500, "completion_tokens": 20, "prompt_cache_hit_tokens": 0}},
            ]
        else:
            frames = [
                {**base, "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {"content": "了。"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 600, "completion_tokens": 5, "prompt_cache_hit_tokens": 448}},
            ]
        for f in frames:
            self.wfile.write(f"data: {json.dumps(f, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model_server(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _FakeModel.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_websocket_end_to_end(ee_home, workspace, model_server):
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async def go():
        backend = Backend(home=ee_home)
        port_f = asyncio.get_running_loop().create_future()
        srv = asyncio.create_task(serve(port=0, token="tok123", backend=backend, ready=port_f.set_result))
        port = await asyncio.wait_for(port_f, 5)
        with pytest.raises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{port}/?token=wrong"):
                pass
        async with connect(f"ws://127.0.0.1:{port}/?token=tok123") as ws:
            notes: list[dict] = []
            pending: dict[int, asyncio.Future] = {}
            seq = [0]

            async def reader():
                async for raw in ws:
                    msg = json.loads(raw)
                    if "id" in msg and msg["id"] in pending:
                        pending.pop(msg["id"]).set_result(msg)
                    elif "method" in msg:
                        notes.append(msg)
            rt = asyncio.create_task(reader())

            async def rpc(method, params=None):
                seq[0] += 1
                f = asyncio.get_running_loop().create_future()
                pending[seq[0]] = f
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params or {}}))
                msg = await asyncio.wait_for(f, 10)
                if "error" in msg:
                    raise RuntimeError(msg["error"]["message"])
                return msg["result"]

            async def note(method, pred=lambda p: True):
                for _ in range(500):
                    for n in notes:
                        if n["method"] == method and pred(n["params"]):
                            return n["params"]
                    await asyncio.sleep(0.01)
                raise AssertionError(f"没收到 {method}: {[n['method'] for n in notes]}")

            # 通过设置页接口配置一个指向假服务器的供应商（key 存在 config.toml 里）
            st = (await rpc("settings/get"))["settings"]
            st["providers"]["fake"] = {"base_url": model_server, "api_key_env": "", "api_key": "sk-fake-e2e-0001"}
            st["models"]["fake-ds"] = {"provider": "fake", "model": "deepseek-flash"}
            st["default_model"] = "fake-ds"
            await rpc("settings/set", {"settings": st})
            info = await rpc("app/info")
            assert info["default_model"] == "fake-ds"
            th = (await rpc("thread/start", {"cwd": str(workspace)}))["thread"]
            assert th["model"] == "fake-ds"
            await rpc("turn/start", {"thread_id": th["id"], "text": "建一个文件"})
            appr = (await note("item/started", lambda p: p["item"]["type"] == "approval"))["item"]
            await rpc("approval/respond", {"thread_id": th["id"], "call_id": appr["call_id"], "choice": "always"})
            done = await note("turn/completed")
            assert done["reason"] == "completed"
            assert done["usage"]["requests"] == 2 and done["usage"]["hit"] == 448
            text = "".join(n["params"]["delta"] for n in notes
                           if n["method"] == "item/delta" and n["params"]["field"] == "text")
            assert text == "完成了。"
            reasoning = [n for n in notes if n["method"] == "item/delta" and n["params"]["field"] == "reasoning"]
            assert reasoning
            assert (workspace / "made.txt").exists()
            assert _FakeModel.requests[0]["auth"] == "Bearer sk-fake-e2e-0001"
            order = [n["method"] for n in notes]
            assert order.index("turn/started") < order.index("turn/completed")
            got = (await rpc("thread/get", {"thread_id": th["id"]}))["thread"]
            assert got["items"][-1]["type"] == "turn_end"
            rt.cancel()
        srv.cancel()
        try:
            await srv
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_cli_serve_prints_ready(ee_home, tmp_path):
    """真进程：python -m ee serve 打印 READY 行；stdin 关掉就退出。"""
    import os
    env = {**os.environ, "EE_HOME": str(ee_home), "EE_SERVE_TOKEN": "abc"}
    p = subprocess.Popen([sys.executable, "-m", "ee", "serve", "--exit-on-stdin-eof"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    try:
        line = p.stdout.readline().decode()
        assert line.startswith("EE_SERVE_READY ")
        assert json.loads(line.split(" ", 1)[1])["port"] > 0
        p.stdin.close()
        assert p.wait(timeout=10) == 0
    finally:
        if p.poll() is None:
            p.kill()
