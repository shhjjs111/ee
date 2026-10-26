"""测试工具：假的 OpenAI 兼容客户端（按脚本吐流式 chunk），不调用任何真实 API。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def text_chunks(s: str, n: int = 3) -> list[dict]:
    return [{"choices": [{"index": 0, "delta": {"content": s[i:i + n]}, "finish_reason": None}]}
            for i in range(0, len(s), n)]


def reasoning_chunk(s: str, key: str = "reasoning_content") -> dict:
    return {"choices": [{"index": 0, "delta": {key: s}, "finish_reason": None}]}


def tool_chunks(index: int, cid: str, name: str, args: dict | str, split: int = 2) -> list[dict]:
    a = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    parts = [a[i:i + max(1, len(a) // split)] for i in range(0, len(a), max(1, len(a) // split))] or [""]
    out = [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": index, "id": cid, "type": "function", "function": {"name": name, "arguments": parts[0]}}]},
        "finish_reason": None}]}]
    for p in parts[1:]:
        out.append({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": index, "function": {"arguments": p}}]}, "finish_reason": None}]})
    return out


def finish(reason: str, prompt: int = 100, completion: int = 10, hit: int | None = 0) -> list[dict]:
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if hit is not None:
        usage["prompt_cache_hit_tokens"] = hit
    return [{"choices": [{"index": 0, "delta": {}, "finish_reason": reason}], "usage": usage}]


class FakeStream:
    def __init__(self, chunks, delay=0.0):
        self.chunks = list(chunks)
        self.delay = delay
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield c

    async def close(self):
        self.closed = True


class FakeError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status
        self.body = {"error": {"message": msg}}


class FakeClient:
    """script: 每次 create 调用依次取一项；项可以是 chunk 列表、异常、或 callable(params)->chunk 列表。"""

    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.calls: list[dict] = []
        self.delay = delay
        outer = self

        class _Completions:
            async def create(self, **params):
                outer.calls.append(json.loads(json.dumps(params, ensure_ascii=False, default=str)))
                if not outer.script:
                    raise AssertionError("FakeClient 脚本用完了")
                item = outer.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                if callable(item):
                    item = item(params)
                return FakeStream(item, outer.delay)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def ee_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EE_HOME", str(home))
    return home


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def make_session(workspace, script, mode="default", delay=0.0, max_steps=None, model=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    if max_steps:
        cfg.data["max_steps"] = max_steps
    fake = FakeClient(script, delay=delay)
    s = Session(cfg, model=model, cwd=workspace, mode=mode, client=fake, persist=False)
    return s, fake


async def collect(session, text, approve=None):
    """跑一个回合并收集事件；approve(ev) 返回 ApprovalDecision。"""
    from ee.core.events import ApprovalDecision, ApprovalRequest
    events = []
    async for ev in session.run_turn(text):
        events.append(ev)
        if isinstance(ev, ApprovalRequest):
            dec = approve(ev) if approve else ApprovalDecision("once")
            ev.future.set_result(dec)
    return events


def run(coro):
    return asyncio.run(coro)


os.environ.pop("DEEPSEEK_API_KEY", None)


@pytest.fixture(autouse=True)
def _memory_secrets():
    """测试一律用内存凭据存储，绝不碰真实的 Windows 凭据管理器 / 钥匙串。"""
    from ee.core import secrets
    secrets.use(secrets.MemoryStore())
    yield
    secrets.use(secrets.MemoryStore())
