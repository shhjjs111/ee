"""Windows 真机“发你好没反应 / 设置转圈”那一类问题的回归测试：子进程、事件循环不被阻塞、模型超时、代理、设置生效。"""
import asyncio
import json
import socket
import sys
import threading
import time

import pytest
from conftest import FakeClient, finish, text_chunks

from ee.config import ModelSpec, load_config
from ee.core.providers import net
from ee.core.providers.openai_chat import ChatClient, ModelError
from ee.core.providers.profiles import profile_for
from ee.proc import run_capture
from ee.server.app import Backend


# ---------------------------------------------------------------- 子进程
def test_run_capture_basic_and_stdin_devnull():
    code, out, _ = run_capture([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=10)
    assert code == 0 and out.decode().strip() == "''"     # stdin 是 DEVNULL，不继承父进程的管道
    code, out, _ = run_capture([sys.executable, "-c", "import sys; print(sys.stdin.read())"], input=b"hi", timeout=10)
    assert out.decode().strip() == "hi"
    assert run_capture(["definitely-not-a-program-xyz"])[0] == 127


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 版本的孙进程场景")
def test_run_capture_timeout_with_grandchild_holding_pipe():
    # 子进程再起一个孙进程继承 stdout（同 Windows 上 git 包装器 → 真 git），超时后也必须很快返回
    t0 = time.monotonic()
    code, _, err = run_capture(["sh", "-c", "sleep 30 & sleep 30"], timeout=1)
    assert code == 124 and err == b"timeout"
    assert time.monotonic() - t0 < 6


# ---------------------------------------------------------------- 代理 / 超时
def test_net_plan_modes(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    p = net.plan("https://api.deepseek.com", {"proxy": "direct"})
    assert p.proxy is None and "直连" in p.describe
    p = net.plan("https://api.deepseek.com", {"proxy": "127.0.0.1:7890", "connect_timeout": 3, "read_timeout": 7})
    assert p.proxy == "http://127.0.0.1:7890" and p.connect_timeout == 3 and p.read_timeout == 7
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@10.0.0.1:8080")
    p = net.plan("https://api.deepseek.com", {"proxy": "system"})
    assert p.proxy == "http://user:pw@10.0.0.1:8080" and "pw" not in p.describe and "环境变量" in p.describe
    p = net.plan("http://127.0.0.1:18080/v1", {"proxy": "system"})
    assert p.proxy is None and "本机" in p.describe


def _spec(base_url):
    return ModelSpec(alias="t", provider="p", model="m", base_url=base_url, api_key_env="", profile=profile_for("m"),
                     stored_key="sk-test-123456789")


def _blackhole():
    """接受连接、读请求、永远不回应。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns = []

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_blackhole_endpoint_times_out_with_clear_error():
    srv, port = _blackhole()
    try:
        cli = ChatClient(_spec(f"http://127.0.0.1:{port}/v1"),
                         network={"proxy": "system", "connect_timeout": 2, "read_timeout": 1.5, "retries": 1})
        assert cli.retries == 1

        async def go():
            evs = []
            with pytest.raises(ModelError) as ei:
                async for ev in cli.stream([{"role": "user", "content": "你好"}], None):
                    evs.append(ev)
            return evs, str(ei.value)
        t0 = time.monotonic()
        evs, msg = asyncio.run(go())
        assert time.monotonic() - t0 < 15
        assert evs[0][0] == "status" and "正在连接模型" in evs[0][1]
        assert any(k == "notice" and "重试" in v for k, v in evs)
        assert "超时" in msg and f"127.0.0.1:{port}" in msg and "直连" in msg
    finally:
        srv.close()


def test_connection_refused_error_message():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cli = ChatClient(_spec(f"http://127.0.0.1:{port}/v1"), network={"retries": 0, "connect_timeout": 2})

    async def go():
        async for _ in cli.stream([{"role": "user", "content": "x"}], None):
            pass
    with pytest.raises(ModelError) as ei:
        asyncio.run(go())
    assert "连不上模型服务" in str(ei.value) and "base_url" not in str(ei.value)


def test_http_status_error_message():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"error": {"message": "Insufficient Balance"}}).encode()
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cli = ChatClient(_spec(f"http://127.0.0.1:{srv.server_port}/v1"), network={"retries": 0})

        async def go():
            async for _ in cli.stream([{"role": "user", "content": "x"}], None):
                pass
        with pytest.raises(ModelError) as ei:
            asyncio.run(go())
        assert "402" in str(ei.value) and "余额" in str(ei.value) and ei.value.status_code == 402
    finally:
        srv.shutdown()


def test_missing_key_is_model_error():
    spec = _spec("https://api.deepseek.com")
    spec.stored_key = ""
    with pytest.raises(ModelError):
        ChatClient(spec)


# ---------------------------------------------------------------- 事件循环不被慢 IO 卡住
def test_slow_git_does_not_block_other_rpcs(ee_home, workspace, monkeypatch):
    from ee.core import gitinfo
    real = gitinfo.status

    def slow(cwd):
        time.sleep(1.5)
        return real(cwd)
    monkeypatch.setattr(gitinfo, "status", slow)
    fake = FakeClient([text_chunks("你好！") + finish("stop")])
    b = Backend(home=ee_home, client_factory=lambda spec: ChatClient(spec, client=fake))
    events = []
    b.listeners.append(lambda m, p: events.append((m, p)))

    async def go():
        start = asyncio.ensure_future(b.call("thread/start", {"cwd": str(workspace)}))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await b.call("settings/get", {})
        await b.call("workspace/list", {})
        fast = time.monotonic() - t0
        th = (await start)["thread"]
        await b.call("turn/start", {"thread_id": th["id"], "text": "你好"})
        for _ in range(500):
            if any(m == "turn/completed" for m, _ in events):
                break
            await asyncio.sleep(0.02)
        return fast
    fast = asyncio.run(go())
    assert fast < 1.0, f"thread/start 跑 git 时其他 RPC 被卡了 {fast:.1f}s"
    methods = [m for m, _ in events]
    assert "turn/completed" in methods
    st = [p["text"] for m, p in events if m == "turn/status"]
    assert st and "正在" in st[0] and st[-1] == ""


def test_turn_error_shows_error_item(ee_home, workspace):
    """没有 key：回合必须以 error 条目 + turn/completed(reason=error) 结束，而不是没反应。"""
    b = Backend(home=ee_home)
    events = []
    b.listeners.append(lambda m, p: events.append((m, p)))

    async def go():
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": th["id"], "text": "你好"})
        for _ in range(300):
            if any(m == "turn/completed" for m, _ in events):
                break
            await asyncio.sleep(0.02)
    import os
    old = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        asyncio.run(go())
    finally:
        if old is not None:
            os.environ["DEEPSEEK_API_KEY"] = old
    done = [p for m, p in events if m == "turn/completed"]
    assert done and done[0]["reason"] == "error"
    errs = [p["item"]["message"] for m, p in events if m == "item/started" and p["item"]["type"] == "error"]
    assert errs and "API key" in errs[0]


# ---------------------------------------------------------------- 设置生效
def test_settings_key_and_network_take_effect(ee_home, workspace, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    b = Backend(home=ee_home)

    async def go():
        th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        t = b.threads[th["id"]]
        assert not t.session.spec.api_key()
        s = (await b.call("settings/get", {}))["settings"]
        assert s["network"]["proxy_mode"] == "system"
        s["providers"]["deepseek"]["api_key"] = "sk-deepseek-abcdef1234"
        s["network"] = {"proxy_mode": "custom", "proxy_url": "127.0.0.1:7890", "connect_timeout": 10,
                        "read_timeout": 60}
        r = await b.call("settings/set", {"settings": s})
        assert r["settings"]["network"]["proxy_url"] == "http://127.0.0.1:7890"
        assert r["settings"]["providers"]["deepseek"]["api_key_preview"] == "…1234"
        # 已有会话立即用上新 key（默认模型 deepseek-flash → provider deepseek）
        assert t.session.spec.api_key() == "sk-deepseek-abcdef1234"
        assert t.session.config.data["network"]["proxy"] == "http://127.0.0.1:7890"
        th2 = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        assert b.threads[th2["id"]].session.spec.api_key() == "sk-deepseek-abcdef1234"
        with pytest.raises(Exception):
            await b.call("settings/set", {"settings": {"network": {"proxy_mode": "custom", "proxy_url": ""}}})
    asyncio.run(go())
    cfg = load_config(workspace)
    assert cfg.resolve_model().api_key() == "sk-deepseek-abcdef1234"
