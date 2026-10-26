"""OpenAI Chat Completions 协议适配器（流式、tools、并行工具、usage）。

对所有 OpenAI 兼容服务通用；家族差异全部交给 transforms 和档案。
"""
from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ee.core.providers import transforms as T


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None          # {prompt, completion, hit(None=未知)}
    recovered: bool = False            # 工具调用是从 content 兜底解析出来的


def parse_usage(u: Any) -> dict | None:
    if u is None:
        return None
    g = lambda o, k: T._get(o, k)  # noqa: E731
    prompt = g(u, "prompt_tokens") or 0
    completion = g(u, "completion_tokens") or 0
    hit = g(u, "prompt_cache_hit_tokens")
    if hit is None:
        det = g(u, "prompt_tokens_details")
        if det is not None:
            hit = g(det, "cached_tokens")
    if hit is None:
        hit = g(u, "cached_tokens")
    return {"prompt": int(prompt), "completion": int(completion), "hit": None if hit is None else int(hit)}


class QuirkState:
    """记住某个 base_url+模型 不支持哪些参数（自动降级后写回本地）。"""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, key: str) -> dict:
        return self.data.setdefault(key, {})

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


class ChatClient:
    def __init__(self, spec, client: Any = None, state_path: Path | None = None, retries: int = 3):
        self.spec = spec
        self.profile = spec.profile
        self.retries = retries
        self.state = QuirkState(state_path)
        self.key = f"{spec.base_url}|{spec.model}"
        if client is None:
            from openai import AsyncOpenAI
            key = spec.api_key()
            if not key:
                raise RuntimeError(f"模型 {spec.alias} 没有可用的 API key：{spec.key_hint()}。请在 设置 → 模型与供应商 里填写。")
            client = AsyncOpenAI(api_key=key, base_url=spec.base_url or None,
                                 default_headers=spec.headers or None, max_retries=0)
        self.client = client
        self.notices: list[str] = []

    # ------------------------------------------------------------ 请求参数
    def build_params(self, messages: list[dict], tools: list[dict] | None) -> dict:
        st = self.state.get(self.key)
        p: dict = {"model": self.spec.model, "messages": messages, "stream": True}
        if not st.get("no_stream_options"):
            p["stream_options"] = {"include_usage": True}
        if tools:
            p["tools"] = tools
            if self.profile.parallel_tools and not st.get("no_parallel_tool_calls"):
                p["parallel_tool_calls"] = True
        if self.profile.request_max_tokens and not st.get("no_max_tokens"):
            p["max_tokens"] = self.profile.request_max_tokens
        if self.profile.temperature is not None and not st.get("no_temperature"):
            p["temperature"] = self.profile.temperature
        if self.profile.extra_body:
            p["extra_body"] = dict(self.profile.extra_body)
        return p

    # ------------------------------------------------------------ 流式调用
    async def stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[tuple[str, Any]]:
        """产出 ("text", s) / ("reasoning", s) / ("notice", s) / ("done", Completion)。"""
        attempt = 0
        degrade_budget = 4
        while True:
            params = self.build_params(messages, tools)
            emitted = False
            try:
                async for ev in self._stream_once(params, tools):
                    if ev[0] in ("text", "reasoning"):
                        emitted = True
                    yield ev
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                text = _err_text(e)
                if status == 400 and degrade_budget > 0:
                    bad = T.unsupported_param(text)
                    if bad:
                        degrade_budget -= 1
                        flag = {"stream_options": "no_stream_options", "parallel_tool_calls": "no_parallel_tool_calls",
                                "max_tokens": "no_max_tokens", "temperature": "no_temperature"}.get(bad, f"no_{bad}")
                        self.state.get(self.key)[flag] = True
                        self.state.save()
                        yield ("notice", f"服务端不支持参数 {bad}，已自动降级并记住")
                        continue
                retryable = status in (408, 409, 429) or (status is not None and status >= 500) or \
                    type(e).__name__ in ("APIConnectionError", "APITimeoutError")
                if retryable and not emitted and attempt < self.retries:
                    attempt += 1
                    delay = min(20.0, 1.5 * 2 ** (attempt - 1)) + random.random() * 0.5
                    yield ("notice", f"请求失败（{status or type(e).__name__}），{delay:.0f}s 后第 {attempt} 次重试")
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _stream_once(self, params: dict, tools: list[dict] | None):
        known = [t["function"]["name"] for t in (tools or [])]
        stream = await self.client.chat.completions.create(**params)
        comp = Completion()
        calls: list[dict] = []
        splitter = T.ThinkSplitter()  # 所有家族都识别 <think>，MiniMax 等靠它
        try:
            async for chunk in stream:
                u = T._get(chunk, "usage")
                choices = T._get(chunk, "choices") or []
                if u is None and choices:
                    u = T._get(choices[0], "usage")
                if u is not None:
                    pu = parse_usage(u)
                    if pu and (pu["prompt"] or pu["completion"]):
                        comp.usage = pu
                if not choices:
                    continue
                ch = choices[0]
                delta = T._get(ch, "delta")
                fr = T._get(ch, "finish_reason")
                if fr:
                    comp.finish_reason = fr
                if delta is None:
                    continue
                r = T.delta_reasoning(delta)
                if r:
                    comp.reasoning += r
                    yield ("reasoning", r)
                c = T._get(delta, "content")
                if isinstance(c, str) and c:
                    for kind, piece in splitter.feed(c):
                        if kind == "reasoning":
                            comp.reasoning += piece
                        else:
                            comp.content += piece
                        yield (kind, piece)
                for tc in T._get(delta, "tool_calls") or []:
                    _accumulate(calls, tc)
            for kind, piece in splitter.flush():
                if kind == "reasoning":
                    comp.reasoning += piece
                else:
                    comp.content += piece
                yield (kind, piece)
        finally:
            close = getattr(stream, "close", None)
            if close:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
        # 规整工具调用
        for c in calls:
            if not c.get("id"):
                c["id"] = "call_" + uuid.uuid4().hex[:12]
            c["function"]["name"] = T.normalize_tool_name(c["function"]["name"], known)
        comp.tool_calls = [c for c in calls if c["function"]["name"]]
        if not comp.tool_calls and known:
            rec, rest = T.parse_content_tool_calls(comp.content, known)
            if rec:
                comp.tool_calls, comp.content, comp.recovered = rec, rest, True
        if comp.tool_calls and comp.finish_reason in (None, "stop"):
            comp.finish_reason = "tool_calls"
        yield ("done", comp)


def _accumulate(calls: list[dict], tc: Any) -> None:
    idx = T._get(tc, "index")
    tid = T._get(tc, "id")
    fn = T._get(tc, "function")
    name = T._get(fn, "name") or ""
    args = T._get(fn, "arguments") or ""
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    slot = None
    if idx is not None and idx < len(calls):
        slot = calls[idx]
        # 某些中转站同一 index 塞多个完整调用：id 变了且旧参数已完整 → 当新调用
        if tid and slot.get("id") and tid != slot["id"] and _complete(slot["function"]["arguments"]):
            slot = None
    elif idx is None and tid:
        slot = next((c for c in calls if c.get("id") == tid), None)
    elif idx is None and calls and not tid:
        slot = calls[-1]
    if slot is None:
        slot = {"id": tid or "", "type": "function", "function": {"name": "", "arguments": ""}}
        if idx is not None and idx >= len(calls):
            while len(calls) < idx:
                calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        calls.append(slot)
    if tid and not slot.get("id"):
        slot["id"] = tid
    if name and not slot["function"]["name"]:
        slot["function"]["name"] = name
    elif name and slot["function"]["name"] != name and not slot["function"]["name"].endswith(name):
        slot["function"]["name"] += name
    slot["function"]["arguments"] += args


def _complete(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def _err_text(e: Exception) -> str:
    body = getattr(e, "body", None)
    if body:
        try:
            return json.dumps(body, ensure_ascii=False)
        except Exception:
            return str(body)
    return str(e)
