"""测试工具：假的 OpenAI 兼容客户端（按脚本吐流式 chunk），不调用任何真实 API。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
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


@pytest.fixture(autouse=True)
def _fixed_pricing_clock(monkeypatch):
    """让计价测试与真实时钟脱钩。

    DEFAULTS 里 offpeak_discount = 0.5，而 session._current_discount 用 datetime.now()
    判断峰谷。周末、节假日、工作日 18:00 之后跑，所有断言"全价"的用例都会拿到半价而失败
    （见 test_style_budget / test_reasoning_roles 的历史红灯）。
    这里把测试默认折扣钉成 1.0（全价），需要验证折扣的用例自己传 cfg/discount
    （test_pricing_deepseek、test_pricing_hours、test_price_discount）。
    """
    from ee import config
    monkeypatch.setitem(config.DEFAULTS, "pricing",
                        {**(config.DEFAULTS.get("pricing") or {}), "offpeak_discount": 1.0})


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


@pytest.fixture(autouse=True)
def _isolated_user_home(tmp_path, monkeypatch):
    """把「真实主目录」隔离到临时目录。

    背景：技能的兼容目录（~/.agents/skills、~/.claude/skills、~/.ee/skills）默认从
    Path.home() 找。开发机上装了技能，每个会话开头就会多注入一条技能清单消息，让
    「history 有几条消息」这类断言随机器变化而随机失败（CI 上是干净的，所以一直没暴露）。

    实现要点：
    - **只替换 Path.home 的返回值，不改 HOME/USERPROFILE 环境变量。** 改环境变量会让
      ee.config.user_ee_dirs() 里的 os.path.expanduser("~") 一起漂移，真实 ~/.ee
      反而漏进排除集之外，把真实用户配置当成项目级配置叠进来（这正是本目录
      test_config_scope.py 守的那条不变量）。
    - 测试如果自己显式改了 USERPROFILE/HOME（test_skills 的 uhome、test_snapshot 的
      home_dir 用例），Path.home 跟随它——那些用例要的就是自己造的用户目录。
    """
    real_home = Path(os.path.expanduser("~")).resolve()
    fake_home = (tmp_path / "user-home").resolve()
    fake_home.mkdir(exist_ok=True)

    def _home(cls) -> Path:
        cur = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        if cur:
            try:
                p = Path(cur).resolve()
                if p != real_home:      # 用例自己改过 → 用它的
                    return p
            except OSError:
                pass
        return fake_home

    monkeypatch.setattr(Path, "home", classmethod(_home))
    return fake_home

