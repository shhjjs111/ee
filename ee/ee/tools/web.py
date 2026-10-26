"""web_search / web_fetch：搜索后端可插拔（设置里选），抓取有 API 用 API、没有就 httpx 直接抓 + HTML 转文本。

配置（config.toml 的 [web]）：
  search_backend = "deepseek"   # deepseek | tavily | brave | off（off = 不提供 web_search 工具）
  fetch_backend  = "auto"       # auto（DeepSeek 搜索且有 key 时用 DeepSeek Fetch，失败退回直连）| deepseek | direct | off
  timeout = 20                  # 秒，每个网络请求的总超时
  max_results = 8               # 搜索默认返回条数
  fetch_max_chars = 20000       # 抓取结果默认最多返回多少字符
  tavily_api_key / brave_api_key  # 设置页保存的 key；没有就读环境变量 TAVILY_API_KEY / BRAVE_API_KEY
DeepSeek Search/Fetch 复用 providers.deepseek 的 key（设置页保存的 api_key 或环境变量 DEEPSEEK_API_KEY）。
接口（2026-10-26 核对 https://api-docs.deepseek.com/guides/search）：
  POST https://api.deepseek.com/v1/tools/search  {query(≤128 字), count(1-50), need_content}
       → {results: [{title, url, snippet, summary, content, site_name, date, rerank_score, authority}]}
  POST https://api.deepseek.com/v1/tools/fetch   {url} → {title, content(markdown)}
  鉴权 Authorization: Bearer <DeepSeek API Key>；按成功请求计费，各 $0.75/千次。
代理：跟随 [network] 设置（ee.core.providers.net.plan），没有就跟随系统代理。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

SEARCH_BACKENDS = ["deepseek", "tavily", "brave", "off"]
FETCH_BACKENDS = ["auto", "deepseek", "direct", "off"]
BACKEND_LABEL = {"deepseek": "DeepSeek", "tavily": "Tavily", "brave": "Brave", "direct": "直接抓取"}
DEFAULT_WEB = {"search_backend": "deepseek", "fetch_backend": "auto", "timeout": 20, "max_results": 8,
               "fetch_max_chars": 20000}
KEY_ENV = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}
MAX_DOWNLOAD = 3 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 ee-agent")

# 测试注入：httpx.MockTransport；为 None 时走真实网络
_transport = None


class WebError(ToolError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# ------------------------------------------------------------------ 配置

def web_config(data: dict | None) -> dict:
    return {**DEFAULT_WEB, **((data or {}).get("web") or {})}


def deepseek_key(data: dict | None) -> str:
    data = data or {}
    w = data.get("web") or {}
    if w.get("deepseek_api_key"):
        return str(w["deepseek_api_key"])
    p = (data.get("providers") or {}).get("deepseek") or {}
    if p.get("api_key"):
        return str(p["api_key"])
    return os.environ.get(p.get("api_key_env") or "DEEPSEEK_API_KEY", "") or ""


def deepseek_base(data: dict | None) -> str:
    p = ((data or {}).get("providers") or {}).get("deepseek") or {}
    base = str(p.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if "deepseek.com" not in base:      # deepseek 供应商被指向了中转站：Search/Fetch 仍走官方
        base = "https://api.deepseek.com"
    return base


def backend_key(name: str, data: dict | None) -> str:
    if name == "deepseek":
        return deepseek_key(data)
    w = (data or {}).get("web") or {}
    return str(w.get(f"{name}_api_key") or "") or os.environ.get(w.get(f"{name}_api_key_env") or KEY_ENV[name], "")


def fetch_mode(data: dict | None) -> str:
    """auto 实际解析成 deepseek 或 direct。"""
    w = web_config(data)
    m = str(w.get("fetch_backend") or "auto")
    if m == "auto":
        return "deepseek" if w.get("search_backend") == "deepseek" and deepseek_key(data) else "direct"
    return m


# ------------------------------------------------------------------ HTTP

@dataclass
class Resp:
    status: int
    headers: dict
    content: bytes
    url: str
    truncated: bool = False

    def json(self):
        return json.loads(self.content.decode("utf-8", errors="replace") or "null")


def _proxy_for(url: str, data: dict | None) -> str | None:
    try:
        from ee.core.providers import net
        return net.plan(url, (data or {}).get("network")).proxy
    except Exception:  # noqa: BLE001  net 模块不可用时退回系统代理
        import urllib.request
        host = (urlparse(url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return None
        try:
            px = urllib.request.getproxies()
            p = px.get(urlparse(url).scheme) or px.get("all") or ""
            if p and urllib.request.proxy_bypass(host):
                return None
            return (p if "://" in p else "http://" + p) if p else None
        except Exception:  # noqa: BLE001
            return None


async def request(method: str, url: str, *, data: dict | None = None, headers: dict | None = None,
                  json_body=None, params: dict | None = None, timeout: float = 20,
                  max_bytes: int = MAX_DOWNLOAD) -> Resp:
    """一个带总超时、跟随重定向、限制下载大小的请求。网络错误翻译成中文 WebError。"""
    import httpx
    proxy = None if _transport is not None else await asyncio.to_thread(_proxy_for, url, data)
    kw: dict = {"timeout": httpx.Timeout(timeout), "follow_redirects": True, "trust_env": False,
                "headers": {"User-Agent": UA}}
    if _transport is not None:
        kw["transport"] = _transport
    elif proxy:
        kw["proxy"] = proxy

    async def go() -> Resp:
        async with httpx.AsyncClient(**kw) as c:
            async with c.stream(method, url, headers=headers, json=json_body, params=params) as r:
                buf = bytearray()
                trunc = False
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        trunc = True
                        del buf[max_bytes:]
                        break
                return Resp(r.status_code, {k.lower(): v for k, v in r.headers.items()}, bytes(buf), str(r.url), trunc)

    host = urlparse(url).hostname or url
    try:
        return await asyncio.wait_for(go(), timeout + 2)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise WebError(f"请求 {host} 超时（{timeout:.0f} 秒）。" + (f"当前代理 {proxy}。" if proxy else "") +
                       "可以稍后重试，或在 设置 → 网络 检查代理。") from None
    except httpx.ProxyError as e:
        raise WebError(f"代理 {proxy} 连接失败（{e}）。代理软件没开的话，到 设置 → 网络 改成直连。") from None
    except httpx.ConnectError as e:
        raise WebError(f"连不上 {host}（{e or type(e).__name__}）。检查网络或代理设置。") from None
    except httpx.TooManyRedirects:
        raise WebError(f"{host} 重定向次数过多，放弃。") from None
    except httpx.HTTPError as e:
        raise WebError(f"请求 {host} 失败：{type(e).__name__}: {e}") from None


def _api_error(r: Resp, who: str) -> WebError:
    detail = ""
    try:
        j = r.json()
        if isinstance(j, dict):
            e = j.get("error") or j.get("detail") or j.get("message") or ""
            if isinstance(e, dict):
                e = e.get("message") or e.get("error") or json.dumps(e, ensure_ascii=False)
            detail = str(e)
    except Exception:  # noqa: BLE001
        detail = r.content[:300].decode("utf-8", errors="replace")
    what = {400: "请求参数不对（400）", 401: "API key 无效或缺失（401），请到 设置 → 网页搜索 检查 key",
            402: "账户余额不足（402），请到服务商后台充值", 403: "被拒绝（403）", 404: "没有拿到可用内容（404）",
            408: "请求超时（408）", 413: "请求太大（413）", 422: "请求参数不被接受（422）",
            429: "请求太频繁或额度用完（429），稍后再试", 432: "超出套餐额度（432）", 433: "超出按量付费上限（433）"
            }.get(r.status) or (f"服务端出错（{r.status}），稍后再试" if r.status >= 500 else f"请求失败（HTTP {r.status}）")
    return WebError(f"{who}：{what}" + (f"。服务端返回：{detail[:300]}" if detail else ""), r.status)


# ------------------------------------------------------------------ 搜索后端

@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    date: str = ""
    site: str = ""


def _s(v) -> str:
    return "" if v is None else str(v).strip()


async def search_deepseek(query: str, count: int, data: dict, timeout: float) -> list[Hit]:
    key = deepseek_key(data)
    if not key:
        raise WebError("DeepSeek 搜索需要 DeepSeek API key：在 设置 → 模型与供应商 给 deepseek 填 key，"
                       "或设置环境变量 DEEPSEEK_API_KEY；也可以在 设置 → 网页搜索 换成别的后端。")
    q = query[:128]
    r = await request("POST", deepseek_base(data) + "/v1/tools/search", data=data, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json_body={"query": q, "count": max(1, min(50, count)), "need_content": False})
    if r.status != 200:
        raise _api_error(r, "DeepSeek 搜索失败")
    j = r.json() or {}
    out = []
    for x in j.get("results") or []:
        out.append(Hit(_s(x.get("title")), _s(x.get("url")), _s(x.get("summary") or x.get("snippet")),
                       _s(x.get("date"))[:10], _s(x.get("site_name"))))
    return out


async def search_tavily(query: str, count: int, data: dict, timeout: float) -> list[Hit]:
    key = backend_key("tavily", data)
    if not key:
        raise WebError("Tavily 搜索需要 API key：在 设置 → 网页搜索 填写，或设置环境变量 TAVILY_API_KEY。")
    r = await request("POST", "https://api.tavily.com/search", data=data, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json_body={"query": query, "max_results": max(1, min(20, count)), "search_depth": "basic",
                                 "include_published_date": True})
    if r.status != 200:
        raise _api_error(r, "Tavily 搜索失败")
    j = r.json() or {}
    return [Hit(_s(x.get("title")), _s(x.get("url")), _s(x.get("content")), _s(x.get("published_date"))[:16])
            for x in j.get("results") or []]


async def search_brave(query: str, count: int, data: dict, timeout: float) -> list[Hit]:
    key = backend_key("brave", data)
    if not key:
        raise WebError("Brave 搜索需要 API key：在 设置 → 网页搜索 填写，或设置环境变量 BRAVE_API_KEY。")
    r = await request("GET", "https://api.search.brave.com/res/v1/web/search", data=data, timeout=timeout,
                      headers={"X-Subscription-Token": key, "Accept": "application/json"},
                      params={"q": query, "count": max(1, min(20, count))})
    if r.status != 200:
        raise _api_error(r, "Brave 搜索失败")
    j = r.json() or {}
    res = ((j.get("web") or {}).get("results")) or []
    return [Hit(_s(x.get("title")), _s(x.get("url")), _strip_tags(_s(x.get("description"))), _s(x.get("age")))
            for x in res]


SEARCHERS = {"deepseek": search_deepseek, "tavily": search_tavily, "brave": search_brave}


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def format_hits(query: str, backend: str, hits: list[Hit], snippet_chars: int = 500) -> str:
    if not hits:
        return f"搜索「{query}」（{BACKEND_LABEL.get(backend, backend)}）没有结果。换个关键词或去掉限定条件再试。"
    lines = [f"搜索「{query}」（{BACKEND_LABEL.get(backend, backend)}），{len(hits)} 条结果："]
    for i, h in enumerate(hits, 1):
        lines.append(f"\n{i}. {h.title or '（无标题）'}\n   {h.url}")
        meta = " · ".join(x for x in (h.site, h.date) if x)
        if meta:
            lines.append(f"   {meta}")
        if h.snippet:
            lines.append("   " + _clip(h.snippet, snippet_chars))
    lines.append("\n引用时请附上来源 URL；需要细节就用 web_fetch 打开具体页面。")
    return "\n".join(lines)


# ------------------------------------------------------------------ HTML → 文本

_BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "ul", "ol", "table", "tr",
          "blockquote", "form", "figure", "figcaption", "dl", "dt", "dd", "hr", "br"}
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head", "nav", "button", "select",
         "option"}


class _Html2Text(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.title = ""
        self._in_title = False
        self.links: list[tuple[str, int]] = []   # (href, out 中开始位置)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in _BLOCK:
            self.out.append("\n" if tag in ("br", "tr") else "\n\n")
        elif tag == "a" and a.get("href") and not str(a["href"]).startswith(("#", "javascript:")):
            self.links.append((urljoin(self.base, a["href"]), len(self.out)))
            self.out.append("[")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[图片：{a['alt']}]")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag == "a" and self.links and self.out and self.links[-1][1] < len(self.out) and \
                self.out[self.links[-1][1]] == "[":
            href, start = self.links.pop()
            text = "".join(self.out[start + 1:]).strip()
            del self.out[start:]
            if text:
                self.out.append(f"[{' '.join(text.split())}]({href})" if not href.startswith("mailto:") else text)
        elif re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str, base: str = "") -> tuple[str, str]:
    p = _Html2Text(base)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001  坏 HTML：尽量用已解析的部分
        pass
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return " ".join(p.title.split()), text


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _decode(r: Resp) -> str:
    ct = r.headers.get("content-type", "")
    m = re.search(r"charset=([\w\-]+)", ct, re.I)
    cands = [m.group(1)] if m else []
    head = r.content[:4096].decode("ascii", errors="ignore")
    m2 = re.search(r"<meta[^>]+charset=[\"']?([\w\-]+)", head, re.I)
    if m2:
        cands.append(m2.group(1))
    cands += ["utf-8", "gb18030"]
    for enc in cands:
        try:
            return r.content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return r.content.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ 抓取

def _check_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise WebError(f"不是合法的 http/https 网址：{url or '（空）'}")
    if u.username or u.password:
        raise WebError("网址里不能带用户名或密码")
    if len(url) > 8192:
        raise WebError("网址太长（超过 8192 字符）")
    return url


async def fetch_direct(url: str, data: dict, timeout: float) -> tuple[str, str, str]:
    r = await request("GET", url, data=data, timeout=timeout,
                      headers={"Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
                               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    if r.status >= 400:
        raise WebError(f"打开 {url} 失败：HTTP {r.status}" +
                       ("（网站拒绝了自动访问）" if r.status in (401, 403) else "（页面不存在）" if r.status == 404 else ""),
                       r.status)
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    note = "（页面超过 3MB，只读取了前 3MB）" if r.truncated else ""
    if ct in ("text/html", "application/xhtml+xml") or (not ct and r.content.lstrip()[:1] == b"<"):
        title, text = html_to_text(_decode(r), r.url)
        if not text:
            raise WebError(f"{url} 没有可提取的正文（可能需要执行 JavaScript 才能显示内容）")
        return title, (note + "\n\n" if note else "") + text, r.url
    if ct.startswith("text/") or ct in ("application/json", "application/xml", "application/javascript", ""):
        return "", (note + "\n\n" if note else "") + _decode(r), r.url
    raise WebError(f"{url} 的内容类型是 {ct}，不是网页或文本，web_fetch 不支持（PDF/图片等请换别的办法）")


async def fetch_deepseek(url: str, data: dict, timeout: float) -> tuple[str, str, str]:
    key = deepseek_key(data)
    if not key:
        raise WebError("DeepSeek Fetch 需要 DeepSeek API key")
    r = await request("POST", deepseek_base(data) + "/v1/tools/fetch", data=data, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json_body={"url": url})
    if r.status != 200:
        raise _api_error(r, "DeepSeek Fetch 失败")
    j = r.json() or {}
    content = _s(j.get("content"))
    if not content:
        raise WebError("DeepSeek Fetch 没有返回内容", 404)
    return _s(j.get("title")), content, url


# ------------------------------------------------------------------ 工具

def _live(tool, ctx: ToolContext) -> dict:
    """优先用会话当前的配置（设置页改了 key / 后端立即生效），没有就用构造时的。"""
    cfg = ctx.extra.get("config") if ctx.extra else None
    return getattr(cfg, "data", None) or tool.data

class WebSearchTool(Tool):
    name = "web_search"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词（简洁，128 字以内；可用 site: \"精确短语\" -排除词 等语法）"},
            "max_results": {"type": "integer", "description": "返回条数，默认 8，最多 20"},
        },
        "required": ["query"],
    }

    def __init__(self, data: dict | None = None):
        super().__init__()
        self.data = data or {}

    def summary(self, args: dict) -> str:
        return f"Searched {args.get('query', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        q = " ".join(str(args.get("query") or "").split())
        if not q:
            raise ToolError("query 不能为空")
        data = _live(self, ctx)
        w = web_config(data)
        backend = str(w.get("search_backend") or "deepseek")
        if backend == "off" or backend not in SEARCHERS:
            raise ToolError("网页搜索已在设置里关闭")
        n = int(args.get("max_results") or w.get("max_results") or 8)
        n = max(1, min(20, n))
        hits = await SEARCHERS[backend](q, n, data, float(w.get("timeout") or 20))
        hits = [h for h in hits if h.url][:n]
        return ToolResult(format_hits(q, backend, hits), summary=f"Searched {q}",
                          meta={"kind": "web_search", "backend": backend, "query": q,
                                "results": [{"title": h.title, "url": h.url, "snippet": _clip(h.snippet, 200),
                                             "date": h.date, "site": h.site} for h in hits]})


class WebFetchTool(Tool):
    name = "web_fetch"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要打开的网址（http/https）"},
            "offset": {"type": "integer", "description": "从第几个字符开始返回（续读长页面用），默认 0"},
            "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 20000"},
        },
        "required": ["url"],
    }

    def __init__(self, data: dict | None = None):
        super().__init__()
        self.data = data or {}

    def summary(self, args: dict) -> str:
        return f"Fetched {args.get('url', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = _check_url(str(args.get("url") or ""))
        data = _live(self, ctx)
        w = web_config(data)
        mode = fetch_mode(data)
        if mode == "off":
            raise ToolError("网页抓取已在设置里关闭")
        timeout = float(w.get("timeout") or 20)
        notes: list[str] = []
        used = mode
        if mode == "deepseek":
            try:
                title, text, final = await fetch_deepseek(url, data, timeout)
            except WebError as e:
                if e.status in (401, 402):
                    notes.append(f"（{e}；已改用直接抓取）")
                else:
                    notes.append("（DeepSeek Fetch 没拿到内容，已改用直接抓取）")
                used = "direct"
                title, text, final = await fetch_direct(url, data, timeout)
        else:
            title, text, final = await fetch_direct(url, data, timeout)
        total = len(text)
        off = max(0, int(args.get("offset") or 0))
        limit = max(500, min(100_000, int(args.get("max_chars") or w.get("fetch_max_chars") or 20000)))
        part = text[off:off + limit]
        head = f"来源：{final}\n" + (f"标题：{title}\n" if title else "") + \
            f"（{BACKEND_LABEL.get(used, used)}，全文 {total} 字符）" + "".join(notes) + "\n\n"
        tail = ""
        if off + limit < total:
            tail = f"\n\n[已截断：显示第 {off}–{off + len(part)} 字符，共 {total}。需要后面的内容用 offset={off + len(part)} 再调用]"
        if off >= total and total:
            part = f"（offset {off} 超出全文长度 {total}）"
        return ToolResult(head + part + tail, summary=f"Fetched {url}",
                          meta={"kind": "web_fetch", "url": final, "title": title, "backend": used,
                                "chars": total, "truncated": bool(tail)})
