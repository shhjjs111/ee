"""用本地假的 OpenAI 兼容 HTTP 服务器（SSE）跑真实的 openai SDK，验证协议解析。不联网。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import collect, run

from ee.config import load_config
from ee.core.events import TurnDone
from ee.core.session import Session

REQUESTS: list[dict] = []


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append({"body": body, "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        n = len(REQUESTS)
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        if n == 1:
            frames = [
                {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "想"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "glob", "arguments": "{\"pattern\":"}}]}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"*.md\"}"}}]},
                                      "finish_reason": None}]},
                # DeepSeek：usage 与 finish_reason 同帧
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 500, "completion_tokens": 20, "prompt_cache_hit_tokens": 0,
                           "prompt_cache_miss_tokens": 500}},
            ]
        else:
            frames = [
                {**base, "choices": [{"index": 0, "delta": {"content": "找到了 README.md"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {**base, "choices": [], "usage": {"prompt_tokens": 600, "completion_tokens": 5,
                                                   "prompt_tokens_details": {"cached_tokens": 448}}},
            ]
        for f in frames:
            self.wfile.write(sse(f))
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def server():
    REQUESTS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_real_sdk_against_fake_server(server, ee_home, workspace, monkeypatch):
    (workspace / "README.md").write_text("# hi\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_KEY", "sk-test-not-real")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg = load_config(workspace)
    cfg.data["providers"]["fake"] = {"base_url": server, "api_key_env": "FAKE_KEY"}
    cfg.data["models"]["fake-ds"] = {"provider": "fake", "model": "deepseek-flash"}
    s = Session(cfg, model="fake-ds", cwd=workspace, persist=False)
    ev = run(collect(s, "有哪些 md 文件"))
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "completed"
    assert len(REQUESTS) == 2
    b1 = REQUESTS[0]["body"]
    assert b1["stream"] is True and b1["stream_options"] == {"include_usage": True}
    assert [t["function"]["name"] for t in b1["tools"]][:3] == ["read", "write", "edit"]
    assert REQUESTS[0]["auth"] == "Bearer sk-test-not-real"
    b2 = REQUESTS[1]["body"]
    asst = [m for m in b2["messages"] if m["role"] == "assistant"][0]
    assert asst["reasoning_content"] == "想" and asst["tool_calls"][0]["function"]["arguments"] == '{"pattern":"*.md"}'
    tool = [m for m in b2["messages"] if m["role"] == "tool"][0]
    assert "README.md" in tool["content"]
    assert s.stats.hit == 448 and s.stats.prompt == 1100


def test_missing_key_is_clear_error(ee_home, workspace, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    s = Session(load_config(workspace), cwd=workspace, persist=False)
    ev = run(collect(s, "hi"))
    errs = [e for e in ev if type(e).__name__ == "Error"]
    assert errs and "DEEPSEEK_API_KEY" in errs[0].message
