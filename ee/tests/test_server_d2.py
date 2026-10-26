"""server：memory/* RPC 与 web/subagent/memory 设置字段。"""
import pytest
from conftest import run

from ee.server.app import Backend


def test_memory_rpc(ee_home, workspace):
    b = Backend(home=ee_home, persist=False)
    r = run(b.call("memory/get", {"cwd": str(workspace)}))
    assert r["user"]["entries"] == [] and r["workspace"]["path"].endswith(".ee/memory.md".replace("/", __import__("os").sep))
    r = run(b.call("memory/add", {"text": "我喜欢简洁的回答"}))
    assert r["user"]["entries"] == ["我喜欢简洁的回答"]
    r = run(b.call("memory/add", {"text": "项目用 uv", "scope": "workspace", "cwd": str(workspace)}))
    assert r["workspace"]["entries"] == ["项目用 uv"]
    r = run(b.call("memory/remove", {"index": 1}))
    assert r["user"]["entries"] == []
    r = run(b.call("memory/save", {"text": "# x\n- a\n- b", "cwd": str(workspace)}))
    assert r["user"]["entries"] == ["a", "b"] and r["workspace"]["entries"] == ["项目用 uv"]
    with pytest.raises(Exception, match="."):
        run(b.call("memory/remove", {"index": 9}))


def test_d2_settings_roundtrip(ee_home, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    b = Backend(home=ee_home, persist=False)
    g = run(b.call("settings/get", {}))
    st = g["settings"]
    assert st["web"]["search_backend"] == "exa" and st["web"]["fetch_backend"] == "direct" and st["subagent"]["max_steps"] == 30
    assert st["memory"]["enabled"] is True and "tavily" in g["choices"]["search_backends"]
    res = run(b.call("settings/set", {"settings": {
        "web": {"search_backend": "tavily", "keys": {"tavily": {"api_key": "tvly-abcdefgh1234"}}},
        "subagent": {"max_steps": 12}, "memory": {"enabled": False}}}))
    st = res["settings"]
    assert st["web"]["search_backend"] == "tavily"
    assert st["web"]["keys"]["tavily"]["api_key_set"] and st["web"]["keys"]["tavily"]["api_key_preview"] == "…1234"
    assert "tvly-abcdefgh1234" not in str(res)
    assert st["subagent"]["max_steps"] == 12 and st["memory"]["enabled"] is False
    # key 不传 = 不变；"" = 清除
    st = run(b.call("settings/set", {"settings": {"web": {"timeout": 15}}}))["settings"]
    assert st["web"]["keys"]["tavily"]["api_key_set"] and st["web"]["timeout"] == 15
    st = run(b.call("settings/set", {"settings": {"web": {"keys": {"tavily": {"api_key": ""}}}}}))["settings"]
    assert not st["web"]["keys"]["tavily"]["api_key_set"]
    with pytest.raises(Exception, match="."):
        run(b.call("settings/set", {"settings": {"web": {"search_backend": "google"}}}))
    with pytest.raises(Exception, match="."):
        run(b.call("settings/set", {"settings": {"subagent": {"max_steps": 0}}}))
