"""模型请求错误分类与重试：限流 / 5xx / 超时 / 网络可重试（指数退避、遵守 Retry-After），
余额不足 / 鉴权 / 参数 / 上下文超长不重试并给中文提示；上下文超长自动压缩一次再重试。"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeClient, collect, finish, make_session, run, text_chunks

from ee.config import ModelSpec
from ee.core.events import Error, Notice, TurnDone
from ee.core.providers import net
from ee.core.providers.openai_chat import ChatClient, ModelError
from ee.core.providers.profiles import profile_for

OVERFLOW_MSG = ("This model's maximum context length is 131072 tokens. However, you requested 140000 tokens "
                "(140000 in the messages, 0 in the completion). Please reduce the length of the messages or completion.")


class HttpErr(Exception):
    """模仿 openai.APIStatusError：status_code、body、response.headers。"""

    def __init__(self, status, msg, headers=None):
        super().__init__(msg)
        self.status_code = status
        self.body = {"error": {"message": msg}}
        self.response = type("R", (), {"headers": {k.lower(): v for k, v in (headers or {}).items()}})()


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


def _spec(base_url="http://127.0.0.1:1/v1"):
    return ModelSpec(alias="t", provider="p", model="m", base_url=base_url, api_key_env="", profile=profile_for("m"),
                     stored_key="sk-test-123456789")


def _drive(script, retries=2):
    fake = FakeClient(script)
    cli = ChatClient(_spec(), client=fake, retries=retries)
    evs = []

    async def go():
        async for ev in cli.stream([{"role": "user", "content": "x"}], None):
            evs.append(ev)
    err = None
    try:
        asyncio.run(go())
    except ModelError as e:
        err = e
    return fake, evs, err


OK = text_chunks("好") + finish("stop")


# ---------------------------------------------------------------- 分类
@pytest.mark.parametrize("status,msg,kind", [
    (429, "Rate limit reached for requests", "rate_limit"),
    (429, "Requests rate limit exceeded, please try again later (quota per minute)", "rate_limit"),
    (429, "You exceeded your current quota, please check your plan and billing details.", "quota"),
    (429, "您的账户已欠费，请充值后重试", "quota"),
    (402, "Insufficient Balance", "quota"),
    (400, "Access denied, please make sure your account is in good standing. Arrearage", "quota"),
    (401, "Authentication Fails, Your api key is invalid", "auth"),
    (403, "This model is not available for your account", "auth"),
    (400, OVERFLOW_MSG, "overflow"),
    (400, "输入长度超过上下文上限", "overflow"),
    (413, "Request Entity Too Large", "overflow"),
    (400, "Invalid request: tools[0].function.name is required", "bad_request"),
    (422, "Invalid Parameters", "bad_request"),
    (404, "model not found", "not_found"),
    (500, "Internal error", "server"),
    (503, "Server overloaded", "server"),
    (529, "overloaded_error", "server"),
    (408, "timeout", "server"),
])
def test_classify_status(status, msg, kind):
    assert net.classify(HttpErr(status, msg), json.dumps({"error": {"message": msg}}, ensure_ascii=False)) == kind


def test_classify_transport():
    assert net.classify(APITimeoutError("Request timed out.")) == "timeout"
    assert net.classify(APIConnectionError("Connection error.")) == "network"
    try:
        try:
            raise ConnectionResetError("reset")
        except ConnectionResetError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as e:
        assert net.classify(e) == "network"
    assert net.classify(ValueError("x")) == "other"


def test_retry_after_parsing():
    assert net.retry_after(HttpErr(429, "x", {"Retry-After": "3"})) == 3.0
    assert net.retry_after(HttpErr(429, "x", {"retry-after-ms": "250"})) == 0.25
    from email.utils import formatdate
    d = net.retry_after(HttpErr(429, "x", {"Retry-After": formatdate(time.time() + 30, usegmt=True)}))
    assert 25 <= d <= 31
    assert net.retry_after(HttpErr(429, "x", {"Retry-After": "soon"})) is None
    assert net.retry_after(HttpErr(429, "x")) is None
    assert net.retry_after(ValueError("x")) is None


# ---------------------------------------------------------------- 重试行为
def test_rate_limit_retried_honoring_retry_after():
    t0 = time.monotonic()
    fake, evs, err = _drive([HttpErr(429, "Rate limit reached", {"retry-after-ms": "200"}), OK])
    assert err is None and len(fake.calls) == 2
    # 服务端说等 200ms。断言不能卡在 0.2 这个硬边界上：CI 上实测出现过 0.188s
    # （Windows 定时器粒度 15.6ms + 事件循环取整），本机实测 0.203~0.219s。
    # 放宽到 0.15 仍能区分"真的等了"与"完全没等"（0 等待时这里会是 ~0.00x）。
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.15, f"没有按 Retry-After 等待（仅 {elapsed:.3f}s）"
    notes = [v for k, v in evs if k == "notice"]
    assert any("限流" in n and "第 1 次重试" in n for n in notes)


def test_server_error_and_network_retried():
    fake, _, err = _drive([HttpErr(503, "Server overloaded", {"Retry-After": "0"}),
                           HttpErr(500, "boom", {"Retry-After": "0"}), OK])
    assert err is None and len(fake.calls) == 3
    fake, _, err = _drive([APIConnectionError("Connection error."), OK], retries=1)
    assert err is None and len(fake.calls) == 2


def test_exponential_backoff_without_retry_after(monkeypatch):
    import ee.core.providers.openai_chat as oc
    delays = []

    async def fake_sleep(d):
        delays.append(d)
    monkeypatch.setattr(oc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(oc.random, "random", lambda: 0.0)
    fake, _, err = _drive([HttpErr(500, "a"), HttpErr(502, "b"), HttpErr(504, "c"), OK], retries=3)
    assert err is None and delays == [1.5, 3.0, 6.0]


def test_retry_after_too_long_not_waited():
    fake, _, err = _drive([HttpErr(429, "Rate limit reached", {"Retry-After": "600"})])
    assert len(fake.calls) == 1 and err is not None and err.kind == "rate_limit"
    assert "600 秒" in str(err) and "限流" in str(err)


def test_retries_exhausted_reports_last_error():
    fake, _, err = _drive([HttpErr(503, "x", {"Retry-After": "0"})] * 3, retries=2)
    assert len(fake.calls) == 3 and err.kind == "server" and "503" in str(err)


@pytest.mark.parametrize("status,msg,kind,words", [
    (402, "Insufficient Balance", "quota", ["余额", "充值", "重试没有用"]),
    (429, "You exceeded your current quota, please check your billing details.", "quota", ["额度", "设置 → 模型与供应商"]),
    (401, "Authentication Fails", "auth", ["key", "设置 → 模型与供应商"]),
    (403, "forbidden", "auth", ["403", "设置 → 模型与供应商"]),
    (400, "Invalid request body", "bad_request", ["400"]),
    (400, OVERFLOW_MSG, "overflow", ["上下文", "/compact"]),
])
def test_non_retryable_stop_immediately(status, msg, kind, words):
    fake, evs, err = _drive([HttpErr(status, msg, {"Retry-After": "0"}), OK], retries=3)
    assert len(fake.calls) == 1, "不可重试的错误不应再发请求"
    assert err is not None and err.kind == kind and err.status_code == status
    for w in words:
        assert w in str(err), (w, str(err))


def test_overflow_does_not_trigger_reasoning_strip_retry():
    fake = FakeClient([HttpErr(400, OVERFLOW_MSG), OK])
    spec = ModelSpec(alias="d", provider="deepseek", model="deepseek-flash", base_url="http://127.0.0.1:1/v1",
                     api_key_env="", profile=profile_for("deepseek-flash"), stored_key="sk-x")
    cli = ChatClient(spec, client=fake, retries=2)

    async def go():
        async for _ in cli.stream([{"role": "user", "content": "x"}], None, effort="high"):
            pass
    with pytest.raises(ModelError) as ei:
        asyncio.run(go())
    assert ei.value.kind == "overflow" and len(fake.calls) == 1


# ---------------------------------------------------------------- 真实 HTTP（openai SDK 的异常与响应头）
def _server(responses):
    """responses：依次返回的 (status, headers, body)；body 为 None 时回一个正常的流式回答。"""
    seq = list(responses)
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hits.append(time.monotonic())
            status, headers, body = seq.pop(0)
            if body is None:
                chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                         "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": "stop"}]}
                data = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                data = json.dumps({"error": {"message": body}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def _http_stream(srv, retries):
    cli = ChatClient(_spec(f"http://127.0.0.1:{srv.server_port}/v1"), network={"retries": retries})
    out = []

    async def go():
        async for k, v in cli.stream([{"role": "user", "content": "x"}], None):
            out.append((k, v))
    asyncio.run(go())
    return out


def test_http_429_retry_after_header_then_success():
    srv, hits = _server([(429, {"Retry-After": "1"}, "Rate limit reached"), (200, {}, None)])
    try:
        out = _http_stream(srv, retries=2)
        assert len(hits) == 2 and hits[1] - hits[0] >= 0.9
        assert any(k == "text" and v == "好" for k, v in out)
    finally:
        srv.shutdown()


def test_http_402_not_retried():
    srv, hits = _server([(402, {}, "Insufficient Balance"), (200, {}, None)])
    try:
        with pytest.raises(ModelError) as ei:
            _http_stream(srv, retries=2)
        assert len(hits) == 1 and ei.value.kind == "quota" and "余额" in str(ei.value)
    finally:
        srv.shutdown()


def test_http_overflow_400_classified():
    srv, hits = _server([(400, {}, OVERFLOW_MSG)])
    try:
        with pytest.raises(ModelError) as ei:
            _http_stream(srv, retries=2)
        assert len(hits) == 1 and ei.value.kind == "overflow"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 会话：上下文超长 → 自动压缩一次再重试
def test_session_overflow_compacts_and_retries(ee_home, workspace):
    s, fake = make_session(workspace, [
        HttpErr(400, OVERFLOW_MSG),
        text_chunks("这是摘要") + finish("stop"),          # 压缩请求
        text_chunks("压缩后回答") + finish("stop"),
    ])
    ev = run(collect(s, "你好"))
    done = [e for e in ev if isinstance(e, TurnDone)][-1]
    assert done.reason == "completed" and done.text == "压缩后回答"
    assert any(isinstance(e, Notice) and "自动压缩" in e.text for e in ev)
    assert s.compactions == 1 and len(fake.calls) == 3


def test_session_overflow_without_auto_compact_reports(ee_home, workspace):
    s, fake = make_session(workspace, [HttpErr(400, OVERFLOW_MSG)])
    s.config.data.setdefault("compact", {})["auto"] = False
    ev = run(collect(s, "你好"))
    errs = [e for e in ev if isinstance(e, Error)]
    assert errs and "/compact" in errs[0].message and len(fake.calls) == 1
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "error"


def test_session_overflow_twice_gives_up(ee_home, workspace):
    s, fake = make_session(workspace, [
        HttpErr(400, OVERFLOW_MSG),
        text_chunks("摘要") + finish("stop"),
        HttpErr(400, OVERFLOW_MSG),
    ])
    ev = run(collect(s, "你好"))
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "error"
    assert s.compactions == 1 and len(fake.calls) == 3
