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
      "web": {"search_backend": "deepseek", "search_fallback": []}}


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
    data = {"providers": {"deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}},
            "web": {"search_backend": "deepseek"}}
    res = run(W.WebSearchTool(data).run({"query": "字" * 300}, ctx(tmp_path, data)))
    body = json.loads(calls[0].content)
    assert len(body["query"]) == 128
    assert calls[0].headers["authorization"] == "Bearer sk-env-abcdefgh"
    assert str(calls[0].url).startswith("https://api.deepseek.com/v1/tools/")   # base 去掉了 /v1 再拼
    assert "没有结果" in res.output


def test_missing_key_and_http_errors(mock, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    nofb = {"web": {"search_backend": "deepseek", "search_fallback": []}}
    with pytest.raises(ToolError, match="DEEPSEEK_API_KEY"):
        run(W.WebSearchTool(nofb).run({"query": "x"}, ctx(tmp_path, nofb)))
    calls, h = mock
    h["/v1/tools/search"] = lambda r: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
    with pytest.raises(ToolError, match="余额不足.*Insufficient"):
        run(W.WebSearchTool(DS).run({"query": "x"}, ctx(tmp_path, DS)))


def test_timeout_message(monkeypatch, tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    monkeypatch.setattr(W, "_transport", httpx.MockTransport(handler))
    data = {**DS, "web": {"search_backend": "deepseek", "search_fallback": [], "timeout": 5}}
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
    DS = {**globals()["DS"], "web": {"search_backend": "deepseek", "fetch_backend": "auto"}}
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
    assert names[-5:] == ["web_search", "web_fetch", "task", "memory", "skill"]
    names = [t.name for t in build_tools(prof, {"web": {"search_backend": "off", "fetch_backend": "off"},
                                                "subagent": {"enabled": False}, "memory": {"enabled": False}})]
    assert not {"web_search", "web_fetch", "task", "memory"} & set(names)
    assert [t.name for t in build_tools(prof)][:7] == ["read", "write", "edit", "shell", "grep", "glob", "todo"]


def test_web_tools_allowed_in_read_only(ee_home, workspace, mock):
    calls, h = mock
    h["mcp.exa.ai"] = lambda r: httpx.Response(200, text=_sse(_exa_text(("t", "https://u", "N/A", "摘要"))),
                                                headers={"content-type": "text/event-stream"})
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "web_search", {"query": "abc"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ], mode="read-only")
    ev = run(collect(s, "搜一下"))
    tool_out = [m for m in s.history if m["role"] == "tool"][0]["content"]
    assert "https://u" in tool_out
    names = [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "web_search" in names and "web_fetch" in names


# ------------------------------------------------------------------ 免费后端：Exa / DuckDuckGo / 回退链

def _exa_text(*items):
    return "\n\n---\n\n".join(f"Title: {t}\nURL: {u}\nPublished: {p}\nAuthor: N/A\nHighlights:\n{hl}"
                                for t, u, p, hl in items)


def _sse(text):
    return "event: message\ndata: " + json.dumps({"result": {"content": [{"type": "text", "text": text}]}},
                                                 ensure_ascii=False) + "\n\n"


DDG_HTML = """<html><body><div class="results">
<div class="result results_links result--ad"><div class="links_main">
 <a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=x.com&u3=1">广告</a>
 <a class="result__snippet" href="https://duckduckgo.com/y.js?x">广告摘要</a></div></div>
<div class="result results_links results_links_deep web-result"><div class="links_main links_deep result__body">
 <h2 class="result__title"><a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python%2Dhttpx.org%2F&amp;rut=abc"><b>HTTPX</b> 官网</a></h2>
 <a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python%2Dhttpx.org%2F">www.python-httpx.org</a>
 <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A next-generation <b>HTTP</b> client &amp; more.</a>
</div></div>
<div class="result results_links web-result"><div class="links_main result__body">
 <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://github.com/encode/httpx">encode/httpx</a></h2>
 <div class="result__snippet">GitHub 仓库</div>
</div></div></div></body></html>"""


def test_default_is_exa_with_ddg_fallback_and_direct_fetch():
    from ee.config import DEFAULTS
    assert DEFAULTS["web"]["search_backend"] == "exa" and DEFAULTS["web"]["fetch_backend"] == "direct"
    assert W.search_chain({}) == ["exa", "duckduckgo"]
    assert W.search_chain({"web": {"search_backend": "duckduckgo"}}) == ["duckduckgo"]
    assert W.search_chain({"web": {"search_backend": "tavily", "search_fallback": "exa, duckduckgo, bogus"}}) == \
        ["tavily", "exa", "duckduckgo"]
    assert W.search_chain({"web": {"search_fallback": []}}) == ["exa"]
    assert W.fetch_mode(DS) == "direct"                       # DeepSeek 搜索也默认免费直连抓取
    assert "exa" in W.SEARCH_BACKENDS and "duckduckgo" in W.SEARCH_BACKENDS


def test_exa_sse(mock, tmp_path):
    calls, h = mock
    text = _exa_text(("HTTPX", "https://www.python-httpx.org/", "N/A", "A next-generation HTTP client.\n\n# HTTPX\n" + "长" * 2000),
                     ("QuickStart", "https://www.python-httpx.org/quickstart/", "2026-04-24T04:49:26.000Z", "import httpx"))
    h["mcp.exa.ai"] = lambda r: httpx.Response(200, text=_sse(text), headers={"content-type": "text/event-stream"})
    res = run(W.WebSearchTool({}).run({"query": "python httpx", "max_results": 5}, ctx(tmp_path, {})))
    req = calls[0]
    assert req.method == "POST" and str(req.url) == "https://mcp.exa.ai/mcp"
    assert "text/event-stream" in req.headers["accept"] and "application/json" in req.headers["accept"]
    body = json.loads(req.content)
    assert body["method"] == "tools/call" and body["params"]["name"] == "web_search_exa"
    assert body["params"]["arguments"]["query"] == "python httpx" and body["params"]["arguments"]["numResults"] == 5
    assert res.meta["backend"] == "exa" and len(res.meta["results"]) == 2
    out = res.output
    assert "（Exa）" in out and "https://www.python-httpx.org/quickstart/" in out and "2026-04-24" in out
    assert "N/A" not in out and "Title:" not in out and "---" not in out
    assert "长" * 801 not in out and "…" in out                # 摘要截断


def test_exa_plain_json_and_api_key(mock, tmp_path, monkeypatch):
    calls, h = mock
    monkeypatch.setenv("EXA_API_KEY", "exa-k")
    h["mcp.exa.ai"] = lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": _exa_text(("T", "https://t.example", "2025-01-02", "hl"))}]}})
    res = run(W.WebSearchTool({}).run({"query": "q"}, ctx(tmp_path, {})))
    assert "https://t.example" in res.output and "2025-01-02" in res.output
    assert calls[0].url.params["exaApiKey"] == "exa-k"


def test_duckduckgo_parse(mock, tmp_path):
    calls, h = mock
    h["html.duckduckgo.com"] = lambda r: httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html; charset=utf-8"})
    data = {"web": {"search_backend": "duckduckgo"}}
    res = run(W.WebSearchTool(data).run({"query": "python httpx"}, ctx(tmp_path, data)))
    assert calls[0].method == "GET" and calls[0].url.params["q"] == "python httpx"
    assert "ee-agent" not in calls[0].headers["user-agent"] and "Mozilla" in calls[0].headers["user-agent"]
    urls = [x["url"] for x in res.meta["results"]]
    assert urls == ["https://www.python-httpx.org/", "https://github.com/encode/httpx"]   # 广告被跳过、uddg 解包
    assert "HTTPX 官网" in res.output and "A next-generation HTTP client & more." in res.output
    assert "GitHub 仓库" in res.output and "广告" not in res.output


def test_duckduckgo_rate_limited(mock, tmp_path):
    calls, h = mock
    h["html.duckduckgo.com"] = lambda r: httpx.Response(202, text='<div class="anomaly-modal">x</div>')
    data = {"web": {"search_backend": "duckduckgo"}}
    with pytest.raises(ToolError, match="DuckDuckGo 搜索失败：被限流"):
        run(W.WebSearchTool(data).run({"query": "q"}, ctx(tmp_path, data)))


def test_exa_fails_then_duckduckgo(mock, tmp_path):
    calls, h = mock
    h["mcp.exa.ai"] = lambda r: httpx.Response(200, text="event: message\ndata: " + json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "rate limited"}}) + "\n\n")
    h["html.duckduckgo.com"] = lambda r: httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})
    res = run(W.WebSearchTool({}).run({"query": "q"}, ctx(tmp_path, {})))
    assert [str(c.url).split("?")[0] for c in calls] == ["https://mcp.exa.ai/mcp", "https://html.duckduckgo.com/html/"]
    assert res.meta["backend"] == "duckduckgo" and "rate limited" in res.output and "已改用 DuckDuckGo" in res.output
    assert "https://github.com/encode/httpx" in res.output


def test_all_backends_fail(mock, tmp_path):
    calls, h = mock
    h["mcp.exa.ai"] = lambda r: httpx.Response(503, text="down")
    h["html.duckduckgo.com"] = lambda r: httpx.Response(429, text="slow down")
    with pytest.raises(ToolError, match="(?s)已依次尝试 Exa、DuckDuckGo.*服务端出错.*被限流"):
        run(W.WebSearchTool({}).run({"query": "q"}, ctx(tmp_path, {})))
    # 关闭回退：只报主后端的错
    data = {"web": {"search_fallback": []}}
    with pytest.raises(ToolError) as ei:
        run(W.WebSearchTool(data).run({"query": "q"}, ctx(tmp_path, data)))
    assert "Exa 搜索失败" in str(ei.value) and "DuckDuckGo" not in str(ei.value)


def test_exa_unparseable_falls_back(mock, tmp_path):
    calls, h = mock
    h["mcp.exa.ai"] = lambda r: httpx.Response(200, text="<html>gateway</html>")
    h["html.duckduckgo.com"] = lambda r: httpx.Response(200, text=DDG_HTML)
    res = run(W.WebSearchTool({}).run({"query": "q"}, ctx(tmp_path, {})))
    assert res.meta["backend"] == "duckduckgo" and "无法解析" in res.output


def test_deepseek_falls_back_to_free(mock, tmp_path):
    calls, h = mock
    h["/v1/tools/search"] = lambda r: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
    h["html.duckduckgo.com"] = lambda r: httpx.Response(200, text=DDG_HTML)
    data = {**DS, "web": {"search_backend": "deepseek"}}
    res = run(W.WebSearchTool(data).run({"query": "q"}, ctx(tmp_path, data)))
    assert res.meta["backend"] == "duckduckgo" and "余额不足" in res.output
