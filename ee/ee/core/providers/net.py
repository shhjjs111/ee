"""模型请求的网络设置：代理选择、超时、HTTP 客户端构造、错误说明。

配置（config.toml 的 [network]）：
  proxy = "system"           # system = 跟随系统（环境变量 HTTPS_PROXY 等，Windows 上再读 IE/系统代理注册表）
                             # direct = 直连；或直接写代理地址，如 "http://127.0.0.1:7890"
  connect_timeout = 15       # 秒：建立连接（含 TLS、代理握手）
  read_timeout = 90          # 秒：两次收到数据之间最长等多久（含等第一个字节）
  retries = 2                # 连接失败 / 超时 / 5xx / 429 时自动重试次数
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
        what = {
            400: "请求被拒绝（HTTP 400），常见原因：模型名不对或参数不被支持",
            401: "API key 无效或未授权（HTTP 401），请到 设置 → 模型与供应商 检查 key",
            402: "账户余额不足（HTTP 402），请到服务商后台充值",
            403: "没有权限（HTTP 403），检查 key 的权限或账户状态",
            404: "接口地址或模型不存在（HTTP 404），检查 base_url（中转站通常以 /v1 结尾）和模型名",
            422: "请求参数不被接受（HTTP 422）",
            429: "请求太频繁或额度用完（HTTP 429）",
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
