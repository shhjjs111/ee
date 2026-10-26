"""D3-A：MCP 客户端、schema 规范化、权限、会话接入、RPC。"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import finish, make_session, collect, text_chunks, tool_chunks

from ee.core.permissions import PermissionPolicy
from ee.mcp.config import load_servers, parse_server
from ee.mcp.manager import McpManager
from ee.mcp.schema import normalize, safe_name

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def stdio_cfg(name="fake", **kw):
    return parse_server(name, {"command": sys.executable, "args": [FAKE], **kw})


def test_schema_normalize():
    s = normalize({"type": "object", "$defs": {"N": {"type": "number", "format": "double"}},
                   "properties": {"a": {"$ref": "#/$defs/N"},
                                  "b": {"anyOf": [{"type": "null"}, {"type": "string", "title": "B"}]},
                                  "c": {"type": ["null", "integer"]}, "d": {"const": "x"},
                                  "e": {"type": "array"}, "f": {}, "g": {"allOf": [{"properties": {"x": {"type": "string"}}, "required": ["x"]}]}},
                   "required": ["a", "zzz"], "additionalProperties": False})
    p = s["properties"]
    assert p["a"] == {"type": "number"} and p["b"] == {"type": "string"} and p["c"] == {"type": "integer"}
    assert p["d"] == {"enum": ["x"], "type": "string"} and p["e"]["items"] == {"type": "string"}
    assert p["f"] == {"type": "string"} and p["g"]["properties"]["x"]["type"] == "string" and p["g"]["required"] == ["x"]
    assert s["required"] == ["a"] and "additionalProperties" not in s
    assert normalize({"type": "string"}) == {"type": "object", "properties": {}}
    rec = normalize({"type": "object", "properties": {"n": {"$ref": "#"}}})
    assert rec["properties"]["n"]["type"] == "object"
    assert safe_name("mcp__a b__c.d") == "mcp__a_b__c_d" and len(safe_name("x" * 100)) == 64


def test_config_parse_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "t123")
    sc = parse_server("gh", {"url": "https://x.example/mcp", "headers": {"Authorization": "Bearer ${MY_TOKEN}"}})
    assert sc.transport == "http" and sc.headers["Authorization"] == "Bearer t123"
    assert parse_server("s", {"url": "https://x.example/sse"}).transport == "sse"
    assert parse_server("s", {"command": "x", "env": {"A": "${NOPE:-dflt}"}}).env == {"A": "dflt"}
    with pytest.raises(ValueError):
        parse_server("bad name", {"command": "x"})
    with pytest.raises(ValueError):
        parse_server("x", {})
    ws = tmp_path / "proj"
    (ws / ".git").mkdir(parents=True)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"p": {"command": "pp"}, "fs": {"command": "proj-fs"}}}))
    data = {"mcp": {"servers": {"fs": {"command": "user-fs"}}}}
    servers, errs = load_servers(data, ws / "sub" if (ws / "sub").mkdir() is None else ws)
    by = {s.name: s for s in servers}
    assert by["fs"].command == "user-fs" and by["fs"].source == "user"
    assert by["p"].source == "project" and by["p"].enabled is False         # 未允许
    data["mcp"]["allowed_project"] = [by["p"].allow_id]
    by = {s.name: s for s in load_servers(data, ws)[0]}
    assert by["p"].enabled is True


def test_stdio_client_roundtrip(tmp_path):
    async def go():
        m = McpManager()
        st = await m.connect(stdio_cfg(), timeout=15, cwd=tmp_path)
        assert st.status == "ready", st.error
        assert [t["name"] for t in st.tools] == ["echo", "add", "fail", "pic"]     # 分页
        r = await m.call("fake", "echo", {"text": "你好"})
        assert r["content"][0]["text"] == "echo:你好"
        r = await m.call("fake", "add", {"a": 2, "b": 3})
        assert r["content"][0]["text"] == "5"
        # 断线后自动重连一次
        await st.client.close()
        r = await m.call("fake", "echo", {"text": "again"})
        assert r["content"][0]["text"] == "echo:again"
        await m.close_all()
    asyncio.run(go())


def test_stdio_crash_reports_stderr(tmp_path):
    async def go():
        m = McpManager()
        st = await m.connect(stdio_cfg(args=[FAKE, "--crash"]), timeout=10, cwd=tmp_path)
        assert st.status == "error" and "boom" in st.error
        st2 = await m.connect(parse_server("nope", {"command": "definitely-not-a-real-command-xyz"}), timeout=5)
        assert st2.status == "error"
    asyncio.run(go())


class _H(BaseHTTPRequestHandler):
    mode = "json"

    def log_message(self, *a):
        pass

    def do_POST(self):
        from fake_mcp_server import handle
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("method") != "initialize" and self.headers.get("Mcp-Session-Id") != "sess-1":
            self.send_response(400)
            self.end_headers()
            return
        out = handle(body)
        if out is None:
            self.send_response(202)
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "sess-1")
        if _H.mode == "sse":
            data = b"event: message\ndata: " + data + b"\n\n"
            self.send_header("Content-Type", "text/event-stream")
        else:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


@pytest.mark.parametrize("mode", ["json", "sse"])
def test_http_client(mode):
    _H.mode = mode
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async def go():
            m = McpManager()
            cfg = parse_server("web", {"url": f"http://127.0.0.1:{srv.server_address[1]}/mcp"})
            st = await m.connect(cfg, timeout=10)
            assert st.status == "ready", st.error
            assert len(st.tools) == 4
            r = await m.call("web", "echo", {"text": "hi"})
            assert r["content"][0]["text"] == "echo:hi"
            await m.close_all()
        asyncio.run(go())
    finally:
        srv.shutdown()


def test_permissions_mcp(tmp_path):
    p = PermissionPolicy(mode="default", workspace=tmp_path)
    assert p.check("mcp__fs__read", {}, info={"read_only": True}).action == "ask"
    assert p.check("mcp__fs__read", {}, info={"trusted": True}).action == "allow"
    p2 = PermissionPolicy(mode="read-only", workspace=tmp_path)
    assert p2.check("mcp__fs__write", {}, info={"read_only": False}).action == "deny"
    assert p2.check("mcp__fs__read", {}, info={"read_only": True, "trusted": True}).action == "allow"
    p3 = PermissionPolicy(mode="auto", workspace=tmp_path, deny=["mcp__fs__delete*"])
    assert p3.check("mcp__fs__delete_file", {}, info={}).action == "deny"
    assert p3.check("mcp__fs__write", {}, info={}).action == "allow"
    p4 = PermissionPolicy(mode="default", workspace=tmp_path, allow=["mcp__gh__*"])
    assert p4.check("mcp__gh__create_issue", {}, info={}).action == "allow"
    d = p.check("mcp__fs__x", {}, info={})
    p.remember(d, "mcp__fs__x")
    assert p.check("mcp__fs__x", {}, info={}).action == "allow"
    # 顺手修的：未知工具“以后都行”生效
    d = p.check("weird_tool", {})
    p.remember(d, "weird_tool")
    assert p.check("weird_tool", {}).action == "allow"


def test_session_attaches_mcp_tools(ee_home, workspace):
    (ee_home / "config.toml").write_text(
        f'config_version = 2\n\n[mcp.servers.fake]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(FAKE)}]\ntrusted = true\n',
        encoding="utf-8")
    script = [tool_chunks(0, "c1", "mcp__fake__echo", {"text": "从模型来"}) + finish("tool_calls"),
              text_chunks("好了") + finish("stop")]

    async def go():
        s, fake = make_session(workspace, script)
        evs = await collect(s, "调用 echo")
        names = [t["function"]["name"] for t in fake.calls[0]["tools"]]
        assert names[-4:] == ["mcp__fake__echo", "mcp__fake__add", "mcp__fake__fail", "mcp__fake__pic"]
        tool_msg = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"][0]
        assert "echo:从模型来" in tool_msg["content"]
        echo_schema = next(t for t in fake.calls[0]["tools"] if t["function"]["name"] == "mcp__fake__echo")
        assert echo_schema["function"]["description"].startswith("[MCP fake]")
        assert "$schema" not in json.dumps(echo_schema)
        from ee.mcp.manager import shutdown
        await shutdown()
    asyncio.run(go())


def test_mcp_rpc_list_and_test(ee_home, workspace):
    from ee.server.app import Backend

    async def go():
        b = Backend(home=ee_home, persist=False)
        st = (await b.call("settings/get", {}))["settings"]
        assert st["mcp"]["enabled"] is True and st["mcp"]["servers"] == {}
        st = (await b.call("settings/set", {"settings": {"mcp": {"servers": {
            "fake": {"command": sys.executable, "args": [FAKE], "enabled": True}}}}}))["settings"]
        assert st["mcp"]["servers"]["fake"]["args"] == [FAKE]
        r = await b.call("mcp/list", {"cwd": str(workspace)})
        assert r["servers"][0]["name"] == "fake" and r["servers"][0]["status"] == "idle"
        t = await b.call("mcp/test", {"name": "fake", "cwd": str(workspace)})
        assert t["status"] == "ready" and len(t["tools"]) == 4 and t["tools"][0]["read_only"] is True
        t = await b.call("mcp/test", {"server": {"command": sys.executable, "args": [FAKE, "--crash"]}, "name": "x"})
        assert t["status"] == "error"
        with pytest.raises(Exception):
            await b.call("settings/set", {"settings": {"mcp": {"servers": {"bad name": {"command": "x"}}}}})
    asyncio.run(go())
