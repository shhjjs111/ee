"""模型请求的网络设置：代理选择、超时、HTTP 客户端构造、错误说明。

配置（config.toml 的 [network]）：
  proxy = "system"           # system = 跟随系统（环境变量 HTTPS_PROXY 等，Windows 上再读 IE/系统代理注册表）
                             # direct = 直连；或直接写代理地址，如 "http://127.0.0.1:7890"
  connect_timeout = 15       # 秒：建立连接（含 TLS、代理握手）
  read_timeout = 90          # 秒：两次收到数据之间最长等多久（含等第一个字节）
  retries = 2                # 连接失败 / 超时 / 5xx / 429 限流时自动重试次数（指数退避，服务端给了 Retry-After 就照它等，
                             # 超过 60 秒不等）；余额不足、key 无效、参数错误、上下文超长不重试
本机地址（127.0.0.1 / localhost / ::1）在“跟随系统”时永远直连。
"""
from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
DEFAULT_NETWORK = {"proxy": "system", "connect_timeout": 15, "read_timeout": 90, "retries": 2}
_ENV_PROXY_KEYS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")


@dataclass
class NetPlan:
    proxy: str | None          # 实际用的代理地址；None = 直连
    describe: str              # 给人看的说明（已隐去代理密码）
    connect_timeout: float
    read_timeout: float
    retries: int


def redact(url: str) -> str:
    return re.sub(r"//[^/@]*@", "//***@", url or "")


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def plan(base_url: str, network: dict | None) -> NetPlan:
    """决定这个 base_url 走不走代理、走哪个。可能做 DNS 查询（Windows 代理例外列表），别在事件循环上调。"""
    n = {**DEFAULT_NETWORK, **(network or {})}
    ct = _num(n.get("connect_timeout"), 15, 1, 300)
    rt = _num(n.get("read_timeout"), 90, 1, 3600)
    retries = int(_num(n.get("retries"), 2, 0, 10))
    setting = str(n.get("proxy") or "system").strip()
    u = urlparse(base_url or "https://api.openai.com/v1")
    host = (u.hostname or "").lower()
    scheme = u.scheme or "https"
    if setting.lower() == "direct":
        return NetPlan(None, "直连（设置里选了不使用代理）", ct, rt, retries)
    if setting.lower() != "system":
        p = setting if "://" in setting else "http://" + setting
        return NetPlan(p, f"自定义代理 {redact(p)}", ct, rt, retries)
    if host in LOOPBACK:
        return NetPlan(None, "直连（本机地址不走代理）", ct, rt, retries)
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        proxies = {}
    p = proxies.get(scheme) or proxies.get("all") or proxies.get("https") or ""
    if not p:
        return NetPlan(None, "直连（系统没有设置代理）", ct, rt, retries)
    try:
        if urllib.request.proxy_bypass(host):
            return NetPlan(None, "直连（在系统代理的例外列表里）", ct, rt, retries)
    except Exception:  # noqa: BLE001
        pass
    if "://" not in p:
        p = "http://" + p
    src = "环境变量" if any(os.environ.get(k) for k in _ENV_PROXY_KEYS) else "系统代理设置"
    return NetPlan(p, f"系统代理 {redact(p)}（来自{src}）", ct, rt, retries)


def make_http_client(np: NetPlan):
    """openai 自带默认值的 httpx AsyncClient，换上我们的超时和代理；trust_env=False 保证只按 NetPlan 走。"""
    import openai
    timeout = openai.Timeout(np.read_timeout, connect=np.connect_timeout, write=np.connect_timeout + 15,
                             pool=np.connect_timeout)
    kw = {"timeout": timeout, "trust_env": False}
    if np.proxy:
        kw["proxy"] = np.proxy
    return openai.DefaultAsyncHttpxClient(**kw), timeout


def _chain(e: BaseException) -> list[BaseException]:
    out, seen = [], set()
    while e is not None and id(e) not in seen:
        out.append(e)
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return out


# ------------------------------------------------------------------ 错误分类
# rate_limit 限流 / server 服务端出错或过载 / timeout 超时 / network 网络 —— 可以自动重试；
# quota 余额或额度不足 / auth 鉴权 / overflow 上下文超长 / bad_request 参数 / not_found / other —— 重试没用。
RETRYABLE = frozenset({"rate_limit", "server", "timeout", "network"})
MAX_RETRY_AFTER = 60.0        # 服务端要求等更久就不自动等了，直接告诉用户
_OVERFLOW = re.compile(
    r"context[_ ]length|maximum context|context window|context_window|too many tokens|prompt is too long|"
    r"input is too long|reduce the length|reduce your prompt|exceeds? the (model'?s? )?(maximum|max|context)|"
    r"token limit|tokens? exceed|上下文.{0,8}(超|过长|长度|上限)|超(出|过).{0,6}(最大|上下文|长度)|输入.{0,4}过长", re.I)
# 只认明确的余额/欠费/账单信号；单独一个 quota 在 429 里常常只是每分钟配额（按限流处理）
_QUOTA = re.compile(
    r"insufficient[_ ]?(balance|quota|funds|credit)|current[_ ]quota|exceeded your current quota|billing|"
    r"arrear|credit balance|balance (is )?(not enough|insufficient)|payment required|"
    r"余额|欠费|充值|账户.{0,6}不足|额度(已)?(用完|用尽|不足)", re.I)


def _status_of(e: BaseException) -> int | None:
    for x in _chain(e):
        st = getattr(x, "status_code", None)
        if isinstance(st, int):
            return st
    return None


def classify(e: BaseException, text: str = "") -> str:
    """把一次失败的模型请求归类，决定要不要重试、怎么提示。text：服务端返回的错误正文（可空）。"""
    status = _status_of(e)
    t = text or str(e)
    if status is not None:
        if status == 402:
            return "quota"
        if status in (400, 403, 429) and _QUOTA.search(t):
            return "quota"
        if status == 413 or (status in (400, 422) and _OVERFLOW.search(t)):
            return "overflow"
        if status == 429:
            return "rate_limit"
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "not_found"
        if status in (408, 409) or status >= 500:
            return "server"
        if status in (400, 422):
            return "bad_request"
        return "other"
    names = [type(x).__name__ for x in _chain(e)]
    if any("Timeout" in n for n in names):
        return "timeout"
    if any(n in ("APIConnectionError", "ConnectError", "ProxyError", "RemoteProtocolError", "ConnectionError",
                 "ReadError", "WriteError", "SSLError", "LocalProtocolError", "ConnectionResetError",
                 "ConnectionRefusedError") for n in names):
        return "network"
    return "other"


def retry_after(e: BaseException) -> float | None:
    """服务端给的 Retry-After（秒）：支持 retry-after-ms、秒数、HTTP 日期。没有或看不懂返回 None。"""
    resp = next((getattr(x, "response", None) for x in _chain(e) if getattr(x, "response", None) is not None), None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            return max(0.0, float(ms) / 1000.0)
        v = headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
    if v is None:
        return None
    v = str(v).strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        import time as _time
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(v).timestamp() - _time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def describe_error(e: BaseException, base_url: str, model: str, np: NetPlan | None, detail: str = "") -> str:
    """把 openai/httpx 异常翻译成一句清楚的中文，带上接口地址、HTTP 状态和代理情况（已脱敏）。"""
    from ee.core.secrets import redact
    return redact(_describe_error(e, base_url, model, np, detail))


def _describe_error(e: BaseException, base_url: str, model: str, np: NetPlan | None, detail: str = "") -> str:
    status = getattr(e, "status_code", None)
    names = [type(x).__name__ for x in _chain(e)]
    where = f"接口 {base_url or '（默认）'}，模型 {model}，{np.describe if np else '网络设置未知'}"
    detail = (detail or "").strip()
    if len(detail) > 400:
        detail = detail[:400] + "…"
    if status:
        kind = classify(e, detail)
        if kind == "quota":
            what = (f"账户余额或额度不足（HTTP {status}），重试没有用，已停止。请到服务商后台充值或检查套餐，"
                    "或到 设置 → 模型与供应商 换一个模型")
        elif kind == "overflow":
            what = (f"对话超出了模型的上下文长度上限（HTTP {status}）。输入 /compact 压缩上下文，或新建对话，"
                    "也可以换上下文更长的模型")
        elif kind == "rate_limit":
            what = "请求太频繁，被服务端限流（HTTP 429），请稍等一会儿再试"
        elif status == 403:
            what = "没有权限（HTTP 403）：key 无权使用这个模型或账户受限，请到 设置 → 模型与供应商 检查 key 和模型名"
        else:
            what = {
                400: "请求被拒绝（HTTP 400），常见原因：模型名不对或参数不被支持",
                401: "API key 无效或未授权（HTTP 401），请到 设置 → 模型与供应商 检查 key",
                404: "接口地址或模型不存在（HTTP 404），检查 base_url（中转站通常以 /v1 结尾）和模型名",
                422: "请求参数不被接受（HTTP 422）",
                503: "服务端过载（HTTP 503），请稍后再试",
            }.get(status) or (f"服务端出错（HTTP {status}）" if status >= 500 else f"请求失败（HTTP {status}）")
        return f"{what}。{where}。" + (f"\n服务端返回：{detail}" if detail else "")
    cause = next((x for x in _chain(e)[1:] if str(x)), None)
    cause_s = f"{type(cause).__name__}: {cause}" if cause is not None else (str(e) or type(e).__name__)
    hint = ""
    if np is not None:
        hint = ("如果代理软件没有运行，到 设置 → 网络 改成“直连”试试。" if np.proxy else
                "检查网络；如果需要代理才能访问，到 设置 → 网络 选择代理。")
    if any("Timeout" in n for n in names):
        kind = "连接超时" if any("ConnectTimeout" in n or "PoolTimeout" in n for n in names) else "读取超时"
        secs = (f"连接 {np.connect_timeout:.0f} 秒" if kind == "连接超时" else f"{np.read_timeout:.0f} 秒内没有收到数据") \
            if np else ""
        return f"等待模型响应超时（{kind}，{secs}）。{where}。{hint}"
    if any(n in ("APIConnectionError", "ConnectError", "ProxyError", "RemoteProtocolError", "ConnectionError",
                 "ReadError", "WriteError", "SSLError", "LocalProtocolError") for n in names):
        return f"连不上模型服务（{cause_s}）。{where}。{hint}"
    return f"{type(e).__name__}: {str(e)[:400]}。{where}。"
