"""OpenAI Chat Completions 协议适配器（流式、tools、并行工具、usage）。

对所有 OpenAI 兼容服务通用；家族差异全部交给 transforms 和档案。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ee.core.providers import net
from ee.core.providers import transforms as T

log = logging.getLogger("ee.model")


class ModelError(RuntimeError):
    """模型请求最终失败：消息已是给用户看的中文说明。status_code 保留原 HTTP 状态（没有则 None）；
    kind 是错误分类（net.classify：quota / overflow / auth / rate_limit / server / timeout / network …）。"""

    def __init__(self, message: str, status_code: int | None = None, kind: str = "other"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


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
    reasoning = None                     # 推理 token（包含在 completion 里），服务端给了才有
    det = g(u, "completion_tokens_details")
    if det is not None:
        reasoning = g(det, "reasoning_tokens")
    if reasoning is None:
        reasoning = g(u, "reasoning_tokens")
    out = {"prompt": int(prompt), "completion": int(completion), "hit": None if hit is None else int(hit)}
    if reasoning is not None:
        try:
            out["reasoning"] = int(reasoning)
        except (TypeError, ValueError):
            pass
    return out


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
    """注意：不传 client 时构造会解析代理（可能有 DNS 查询）并建 TLS 上下文，别在事件循环上直接构造。"""

    def __init__(self, spec, client: Any = None, state_path: Path | None = None, retries: int | None = None,
                 network: dict | None = None):
        self.spec = spec
        self.profile = spec.profile
        self.state = QuirkState(state_path)
        self.key = f"{spec.base_url}|{spec.model}"
        self.np: net.NetPlan | None = None
        if client is None:
            key = spec.api_key()
            if not key:
                raise ModelError(f"模型 {spec.alias} 没有可用的 API key：{spec.key_hint()}。请在 设置 → 模型与供应商 里填写。")
            from openai import AsyncOpenAI
            self.np = net.plan(spec.base_url, network)
            http, timeout = net.make_http_client(self.np)
            client = AsyncOpenAI(api_key=key, base_url=spec.base_url or None, default_headers=spec.headers or None,
                                 max_retries=0, timeout=timeout, http_client=http)
            log.info("模型客户端 %s：base_url=%s model=%s key=…%s %s；超时 连接 %.0fs / 读取 %.0fs，重试 %d 次",
                     spec.alias, spec.base_url, spec.model, key[-4:] if len(key) > 8 else "?", self.np.describe,
                     self.np.connect_timeout, self.np.read_timeout, self.np.retries)
        self.retries = retries if retries is not None else (self.np.retries if self.np else 3)
        self.client = client
        self.notices: list[str] = []

    def where(self) -> str:
        host = self.spec.base_url or "默认接口"
        via = f"，{self.np.describe}" if self.np else ""
        return f"{host}{via}"

    # ------------------------------------------------------------ 请求参数
    def build_params(self, messages: list[dict], tools: list[dict] | None, effort: str = "",
                     strip_reasoning: bool = False) -> dict:
        """effort：推理强度档位（见 core/reasoning.py）；strip_reasoning：本次不发推理相关参数（400 后的试探重试）。"""
        from ee.core import reasoning as R
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
        top, body = R.request_params(self.profile, effort or "",
                                     strip_effort=strip_reasoning or bool(st.get("no_reasoning_effort")),
                                     strip_thinking=strip_reasoning or bool(st.get("no_thinking")))
        p.update(top)
        if body:
            p["extra_body"] = {**p.get("extra_body", {}), **body}
        return p

    @staticmethod
    def has_reasoning_params(p: dict) -> bool:
        eb = p.get("extra_body") or {}
        return "reasoning_effort" in p or "thinking" in eb or "enable_thinking" in eb or any(
            k.endswith("effort") for k in eb)

    # ------------------------------------------------------------ 流式调用
    async def stream(self, messages: list[dict], tools: list[dict] | None,
                     effort: str = "") -> AsyncIterator[tuple[str, Any]]:
        """产出 ("text", s) / ("reasoning", s) / ("notice", s) / ("done", Completion)。
        effort：推理强度档位；服务端不认时（400）去掉推理参数重试一次，成功就记住这个模型不支持。"""
        attempt = 0
        degrade_budget = 4
        strip_reasoning = False
        while True:
            params = self.build_params(messages, tools, effort, strip_reasoning)
            sent_reasoning = self.has_reasoning_params(params)
            emitted = False
            t0 = time.monotonic()
            log.info("模型请求开始：%s（%s）messages=%d tools=%d 第 %d 次", self.spec.alias, self.where(), len(messages),
                     len(tools or []), attempt + 1)
            yield ("status", f"正在连接模型 {self.spec.alias}（{self.where()}）…" if attempt == 0 else
                   f"正在重试连接模型 {self.spec.alias}（第 {attempt} 次）…")
            try:
                async for ev in self._stream_once(params, tools):
                    if ev[0] in ("text", "reasoning") and not emitted:
                        emitted = True
                        yield ("status", "")
                    if ev[0] == "done":
                        if not emitted:
                            yield ("status", "")
                        c = ev[1]
                        if strip_reasoning:         # 去掉推理参数后成功：记住，以后不再发
                            st = self.state.get(self.key)
                            if effort == "off":
                                st["no_thinking"] = True
                            else:
                                st["no_reasoning_effort"] = True
                            self.state.save()
                            yield ("notice", f"模型 {self.spec.alias} 不接受推理强度参数，已去掉并记住（以后不再发送）")
                        log.info("模型请求结束：%s 用时 %.1fs finish=%s 文本 %d 字 工具调用 %d 个 usage=%s", self.spec.alias,
                                 time.monotonic() - t0, c.finish_reason, len(c.content), len(c.tool_calls), c.usage)
                    yield ev
                return
            except asyncio.CancelledError:
                log.info("模型请求被取消：%s 用时 %.1fs", self.spec.alias, time.monotonic() - t0)
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                text = _err_text(e)
                kind = "other" if isinstance(e, ModelError) else net.classify(e, text)
                log.warning("模型请求失败：%s 用时 %.1fs status=%s %s", self.spec.alias, time.monotonic() - t0, status,
                            text[:500], exc_info=True)
                if status == 400 and degrade_budget > 0 and kind not in ("overflow", "quota"):
                    # 上下文超长 / 余额不足的 400 常常也提到 max_tokens 等参数名：不能当成“参数不支持”永久降级
                    bad = T.unsupported_param(text)
                    if bad:
                        degrade_budget -= 1
                        flag = {"stream_options": "no_stream_options", "parallel_tool_calls": "no_parallel_tool_calls",
                                "max_tokens": "no_max_tokens", "temperature": "no_temperature",
                                "enable_thinking": "no_thinking"}.get(bad, f"no_{bad}")
                        self.state.get(self.key)[flag] = True
                        self.state.save()
                        yield ("notice", f"服务端不支持参数 {bad}，已自动降级并记住")
                        continue
                    if sent_reasoning and not strip_reasoning and not emitted:
                        # 说不清是哪个参数的 400：先去掉推理参数试一次（成功才记住）
                        strip_reasoning = True
                        degrade_budget -= 1
                        yield ("notice", "服务端拒绝了这次请求，先去掉推理强度参数重试一次")
                        continue
                wait = net.retry_after(e) if kind in net.RETRYABLE else None
                too_long = wait is not None and wait > net.MAX_RETRY_AFTER
                if kind in net.RETRYABLE and not emitted and attempt < self.retries and not too_long:
                    attempt += 1
                    # 服务端给了 Retry-After 就照它等；否则指数退避加抖动
                    delay = wait if wait is not None else min(20.0, 1.5 * 2 ** (attempt - 1)) + random.random() * 0.5
                    why = net.describe_error(e, self.spec.base_url, self.spec.model, self.np, text if status else "")
                    yield ("notice", f"请求失败：{why}\n{max(delay, 1):.0f}s 后第 {attempt} 次重试")
                    await asyncio.sleep(delay)
                    continue
                if isinstance(e, ModelError):
                    raise
                msg = net.describe_error(e, self.spec.base_url, self.spec.model, self.np, text if status else "")
                if too_long:
                    msg += f"\n服务端要求 {wait:.0f} 秒后再试，超过自动等待上限（{net.MAX_RETRY_AFTER:.0f} 秒），请稍后再发。"
                log.error("模型请求最终失败（%s）：%s", kind, msg)
                raise ModelError(msg, status, kind) from e

    async def _stream_once(self, params: dict, tools: list[dict] | None):
        known = [t["function"]["name"] for t in (tools or [])]
        stream = await self.client.chat.completions.create(**params)
        yield ("status", "已连接，等待模型输出…")
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
