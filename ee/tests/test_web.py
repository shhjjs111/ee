"""web_search / web_fetch：全部用 httpx.MockTransport，不发真实网络请求、不调付费 API。"""
import json

import httpx
import pytest
from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.tools import ToolContext, ToolError, ToolState, build_tools
from ee.tools import web as W


@pytest.fixture
def mock(monkeypatch):
    calls = []
    handlers = {}

    def handler(req: httpx.Request):
        calls.append(req)
        for key, fn in handlers.items():
            if key in str(req.url):
                return fn(req)
        return httpx.Response(404, text="not mocked")

    monkeypatch.setattr(W, "_transport", httpx.MockTransport(handler))
    return calls, handlers


def ctx(tmp_path, data=None):
    class Cfg:
        pass
    c = Cfg()
    c.data = data or {}
    return ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path, extra={"config": c})


DS = {"providers": {"deepseek": {"base_url": "https://api.deepseek.com", "api_key": "sk-test-1234567890"}},
      "web": {"search_backend": "deepseek"}}


def test_deepseek_search(mock, tmp_path):
    calls, h = mock
    h["/v1/tools/search"] = lambda r: httpx.Response(200, json={"results": [
        {"title": "DeepSeek V4", "url": "https://example.com/v4", "snippet": "短", "summary": "长摘要" * 10,
         "date": "2026-06-01T00:00:00+00:00", "site_name": "Example"},
        {"title": "无 URL", "url": ""}]})
    res = run(W.WebSearchTool(DS).run({"query": "DeepSeek V4 release", "max_results": 5}, ctx(tmp_path, DS)))
    req = calls[0]
    assert req.method == "POST" and str(req.url) == "https://api.deepseek.com/v1/tools/search"
    assert req.headers["authorization"] == "Bearer sk-test-1234567890"
    body = json.loads(req.content)
    assert body == {"query": "DeepSeek V4 release", "count": 5, "need_content": False}
    assert "https://example.com/v4" in res.output and "2026-06-01" in res.output and "Example" in res.output
    assert res.meta["kind"] == "web_search" and len(res.meta["results"]) == 1


def test_deepseek_query_clipped_and_env_key(mock, tmp_path, monkeypatch):
    calls, h = mock
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-abcdefgh")
    h["/v1/tools/search"] = lambda r: httpx.Response(200, json={"results": []})
    data = {"providers": {"deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}}}
    res = run(W.WebSearchTool(data).run({"query": "字" * 300}, ctx(tmp_path, data)))
    body = json.loads(calls[0].content)
    assert len(body["query"]) == 128
    assert calls[0].headers["authorization"] == "Bearer sk-env-abcdefgh"
    assert str(calls[0].url).startswith("https://api.deepseek.com/v1/tools/")   # base 去掉了 /v1 再拼
    assert "没有结果" in res.output


def test_missing_key_and_http_errors(mock, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ToolError, match="DEEPSEEK_API_KEY"):
        run(W.WebSearchTool({}).run({"query": "x"}, ctx(tmp_path, {})))
    calls, h = mock
    h["/v1/tools/search"] = lambda r: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
    with pytest.raises(ToolError, match="余额不足.*Insufficient"):
        run(W.WebSearchTool(DS).run({"query": "x"}, ctx(tmp_path, DS)))


def test_timeout_message(monkeypatch, tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    monkeypatch.setattr(W, "_transport", httpx.MockTransport(handler))
    data = {**DS, "web": {"search_backend": "deepseek", "timeout": 5}}
    with pytest.raises(ToolError, match="超时（5 秒）"):
        run(W.WebSearchTool(data).run({"query": "x"}, ctx(tmp_path, data)))


def test_tavily_and_brave(mock, tmp_path):
    calls, h = mock
    h["api.tavily.com"] = lambda r: httpx.Response(200, json={"results": [
        {"title": "T", "url": "https://t.example", "content": "tav content"}]})
    h["api.search.brave.com"] = lambda r: httpx.Response(200, json={"web": {"results": [
        {"title": "B", "url": "https://b.example", "description": "<strong>brave</strong> desc"}]}})
    data = {"web": {"search_backend": "tavily", "tavily_api_key": "tvly-k"}}
    res = run(W.WebSearchTool(data).run({"query": "q"}, ctx(tmp_path, data)))
    assert "https://t.example" in res.output and calls[-1].headers["authorization"] == "Bearer tvly-k"
    assert json.loads(calls[-1].content)["max_results"] == 8
    data = {"web": {"search_backend": "brave", "brave_api_key": "bk"}}
    res = run(W.WebSearchTool(data).run({"query": "q", "max_results": 3}, ctx(tmp_path, data)))
    assert calls[-1].headers["x-subscription-token"] == "bk" and calls[-1].url.params["count"] == "3"
    assert "brave desc" in res.output and "<strong>" not in res.output


def test_fetch_direct_html(mock, tmp_path):
    calls, h = mock
    html = ("<html><head><title>标题页</title><style>.x{}</style><script>var a=1</script></head><body>"
            "<nav>菜单</nav><h1>大标题</h1><p>第一段 <a href='/doc'>文档</a></p><ul><li>甲</li><li>乙</li></ul>"
            "<pre>code  line</pre></body></html>")
    h["site.example"] = lambda r: httpx.Response(200, content=html.encode("gbk"),
                                                 headers={"content-type": "text/html; charset=gbk"})
    data = {"web": {"search_backend": "off", "fetch_backend": "auto"}}
    res = run(W.WebFetchTool(data).run({"url": "https://site.example/page"}, ctx(tmp_path, data)))
    out = res.output
    assert "标题：标题页" in out and "# 大标题" in out and "[文档](https://site.example/doc)" in out
    assert "- 甲" in out and "code  line" in out
    assert "var a" not in out and "菜单" not in out
    assert res.meta["backend"] == "direct" and res.meta["url"] == "https://site.example/page"
    assert calls[0].method == "GET"


def test_fetch_truncation_and_offset(mock, tmp_path):
    calls, h = mock
    h["long.example"] = lambda r: httpx.Response(200, text="x" * 5000, headers={"content-type": "text/plain"})
    data = {"web": {"fetch_backend": "direct"}}
    res = run(W.WebFetchTool(data).run({"url": "long.example", "max_chars": 1000}, ctx(tmp_path, data)))
    assert "已截断" in res.output and "offset=1000" in res.output
    res = run(W.WebFetchTool(data).run({"url": "long.example", "max_chars": 1000, "offset": 4500}, ctx(tmp_path, data)))
    assert "已截断" not in res.output


def test_fetch_deepseek_then_fallback(mock, tmp_path):
    calls, h = mock
    h["/v1/tools/fetch"] = lambda r: httpx.Response(200, json={"title": "DS", "content": "# md 内容"})
    res = run(W.WebFetchTool(DS).run({"url": "https://a.example/x"}, ctx(tmp_path, DS)))
    assert "# md 内容" in res.output and res.meta["backend"] == "deepseek"
    assert json.loads(calls[-1].content) == {"url": "https://a.example/x"}
    h["/v1/tools/fetch"] = lambda r: httpx.Response(404, json={"error": {"message": "no content"}})
    h["a.example"] = lambda r: httpx.Response(200, text="<p>直连拿到</p>", headers={"content-type": "text/html"})
    res = run(W.WebFetchTool(DS).run({"url": "https://a.example/x"}, ctx(tmp_path, DS)))
    assert "直连拿到" in res.output and res.meta["backend"] == "direct" and "改用直接抓取" in res.output


def test_fetch_bad_url_and_binary(mock, tmp_path):
    calls, h = mock
    with pytest.raises(ToolError, match="http"):
        run(W.WebFetchTool({}).run({"url": "ftp://x"}, ctx(tmp_path)))
    h["pdf.example"] = lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    with pytest.raises(ToolError, match="application/pdf"):
        run(W.WebFetchTool({"web": {"fetch_backend": "direct"}}).run({"url": "https://pdf.example/a.pdf"}, ctx(tmp_path)))
    h["deny.example"] = lambda r: httpx.Response(403, text="no")
    with pytest.raises(ToolError, match="403"):
        run(W.WebFetchTool({"web": {"fetch_backend": "direct"}}).run({"url": "https://deny.example/"}, ctx(tmp_path)))


def test_build_tools_respects_config():
    from ee.core.providers.profiles import profile_for
    prof = profile_for("deepseek-flash")
    names = [t.name for t in build_tools(prof, {"web": {"search_backend": "deepseek"}})]
    assert names[-4:] == ["web_search", "web_fetch", "task", "memory"]
    names = [t.name for t in build_tools(prof, {"web": {"search_backend": "off", "fetch_backend": "off"},
                                                "subagent": {"enabled": False}, "memory": {"enabled": False}})]
    assert not {"web_search", "web_fetch", "task", "memory"} & set(names)
    assert [t.name for t in build_tools(prof)][:7] == ["read", "write", "edit", "shell", "grep", "glob", "todo"]


def test_web_tools_allowed_in_read_only(ee_home, workspace, mock):
    calls, h = mock
    h["/v1/tools/search"] = lambda r: httpx.Response(200, json={"results": [{"title": "t", "url": "https://u"}]})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "web_search", {"query": "abc"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ], mode="read-only")
    s.config.data["providers"]["deepseek"]["api_key"] = "sk-xxxxxxxxxxxx"
    ev = run(collect(s, "搜一下"))
    tool_out = [m for m in s.history if m["role"] == "tool"][0]["content"]
    assert "https://u" in tool_out
    names = [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "web_search" in names and "web_fetch" in names
