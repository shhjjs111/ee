"""web_fetch 拦内网/本机/链路本地/云元数据地址：字面 IP、DNS 解析结果、重定向每一跳、连接时再查。"""
import socket

import httpx
import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools import web as W


def ctx(tmp_path, data=None):
    class Cfg:
        pass
    c = Cfg()
    c.data = data or {}
    return ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path, extra={"config": c})


DIRECT = {"web": {"fetch_backend": "direct"}}


@pytest.fixture
def mock(monkeypatch):
    calls, handlers = [], {}

    def handler(req):
        calls.append(req)
        for key, fn in handlers.items():
            if key in str(req.url):
                return fn(req)
        return httpx.Response(200, text="<p>内网内容</p>", headers={"content-type": "text/html"})

    monkeypatch.setattr(W, "_transport", httpx.MockTransport(handler))
    return calls, handlers


@pytest.fixture
def dns(monkeypatch):
    table: dict[str, list[str]] = {}

    def fake(host):
        if host in table:
            return table[host]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(W, "_resolve", fake)
    return table


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.1.2.3", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
                                "::ffff:169.254.169.254", "224.0.0.1", "64:ff9b::a9fe:a9fe"])
def test_blocked_ips(ip):
    assert W.blocked_reason_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "198.18.0.7"])
def test_public_ips_allowed(ip):
    assert W.blocked_reason_ip(ip) is None      # 198.18/15 是 Clash 等代理的 fake-ip 段，必须放行


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://localhost/", "http://foo.localhost/",
                                 "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
                                 "http://metadata.google.internal/computeMetadata/v1/", "http://2130706433/",
                                 "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://127.1/",
                                 "http://[::ffff:7f00:1]/", "http://192.168.1.1./"])
def test_fetch_literal_private_blocked(mock, dns, tmp_path, url):
    calls, _ = mock
    with pytest.raises(ToolError, match="内网|本机|元数据|allow_private"):
        run(W.WebFetchTool(DIRECT).run({"url": url}, ctx(tmp_path, DIRECT)))
    assert calls == []


def test_fetch_dns_resolves_to_private_blocked(mock, dns, tmp_path):
    calls, _ = mock
    dns["evil.example"] = ["93.184.216.34", "10.1.2.3"]      # 任一地址是内网就拦
    with pytest.raises(ToolError, match="内网"):
        run(W.WebFetchTool(DIRECT).run({"url": "https://evil.example/"}, ctx(tmp_path, DIRECT)))
    assert calls == []


def test_fetch_redirect_hop_to_private_blocked(mock, dns, tmp_path):
    calls, h = mock
    dns["pub.example"] = ["93.184.216.34"]
    h["pub.example"] = lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
    with pytest.raises(ToolError, match="重定向|元数据"):
        run(W.WebFetchTool(DIRECT).run({"url": "https://pub.example/a"}, ctx(tmp_path, DIRECT)))
    assert [str(c.url) for c in calls] == ["https://pub.example/a"]


def test_fetch_redirect_public_followed(mock, dns, tmp_path):
    calls, h = mock
    dns["pub.example"] = ["93.184.216.34"]
    dns["b.example"] = ["93.184.216.35"]
    h["pub.example"] = lambda r: httpx.Response(301, headers={"location": "https://b.example/final"})
    h["b.example"] = lambda r: httpx.Response(200, text="<p>终点</p>", headers={"content-type": "text/html"})
    res = run(W.WebFetchTool(DIRECT).run({"url": "https://pub.example/a"}, ctx(tmp_path, DIRECT)))
    assert "终点" in res.output and res.meta["url"] == "https://b.example/final"


def test_fetch_redirect_loop_limited(mock, dns, tmp_path):
    calls, h = mock
    dns["loop.example"] = ["93.184.216.34"]
    h["loop.example"] = lambda r: httpx.Response(302, headers={"location": "/again"})
    with pytest.raises(ToolError, match="重定向次数过多"):
        run(W.WebFetchTool(DIRECT).run({"url": "https://loop.example/"}, ctx(tmp_path, DIRECT)))
    assert len(calls) <= 11


def test_allow_private_setting(mock, dns, tmp_path):
    data = {"web": {"fetch_backend": "direct", "allow_private": True}}
    res = run(W.WebFetchTool(data).run({"url": "http://127.0.0.1:8000/"}, ctx(tmp_path, data)))
    assert "内网内容" in res.output


def test_default_allow_private_false():
    assert W.DEFAULT_WEB["allow_private"] is False


def test_connect_time_guard_blocks_rebinding(monkeypatch):
    """预检之后 DNS 变成内网地址（DNS rebinding）：真正连接时再解析一次并检查。"""
    import anyio
    seen = {}

    class Inner:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            seen["host"] = host
            return "stream"

    monkeypatch.setattr(W, "_resolve", lambda h: ["10.0.0.9"])
    g = W._GuardBackend(Inner())
    with pytest.raises(Exception, match="内网"):
        anyio.run(g.connect_tcp, "rebind.example", 80)
    assert "host" not in seen
    monkeypatch.setattr(W, "_resolve", lambda h: ["93.184.216.34"])
    assert anyio.run(g.connect_tcp, "ok.example", 443) == "stream"
    assert seen["host"] == "93.184.216.34"       # 连到检查过的那个 IP（TLS 的 SNI 仍是原主机名）
