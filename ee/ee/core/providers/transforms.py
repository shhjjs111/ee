"""各家模型怪癖集中处理。纯函数为主，方便测试。

- 推理字段：reasoning_content / reasoning / content 开头的 <think>…</think>
- 回传规则：按档案 reasoning_passback（none | tool_turns | all），规则确定 → 历史字节稳定
  （DeepSeek 用 all：官方文档要求带 tools 的请求回传之前所有轮次的 reasoning_content，见 profiles/deepseek.toml）
- 坏工具调用兜底：content 里整段 JSON、DSML 标记、<tool_call> / <function=…> XML
- 工具名大小写纠正、参数类型修复
- 不支持参数（stream_options / parallel_tool_calls 等）时识别错误以便降级
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from typing import Any

# ---------------------------------------------------------------- 推理字段

REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")


def delta_reasoning(delta: Any) -> str:
    """从流式 delta（openai 对象或 dict）里取推理增量。"""
    for k in REASONING_KEYS:
        v = _get(delta, k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):  # 某些网关 {"content": "..."}
            t = v.get("content") or v.get("text")
            if isinstance(t, str) and t:
                return t
    return ""


def _get(obj: Any, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    v = getattr(obj, key, None)
    if v is None:
        extra = getattr(obj, "model_extra", None) or {}
        v = extra.get(key)
    return v


class ThinkSplitter:
    """把 content 流开头的 <think>…</think> 拆成推理（MiniMax 等把思考写进正文的模型靠它），能处理标签跨 chunk。

    只认回答开头（前面只有空白）的那一个思考块：思考块结束后、或开头第一个非空白字符不是 <think> 时，
    之后的一切都按正文原样输出。否则模型在正文里提到 "<think>" 字样（比如解释这个标签）时，
    后面的回答会被当成推理吞掉，界面和历史里都看不到。"""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.state = "start"          # start（还没见到非空白）| inside（思考块内）| done（之后全是正文）

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if self.state == "done":
            return [("text", chunk)] if chunk else []
        self.buf += chunk
        out: list[tuple[str, str]] = []
        if self.state == "start":
            lead = self.buf[: len(self.buf) - len(self.buf.lstrip())]
            rest = self.buf[len(lead):]
            if rest.startswith(self.OPEN):
                self.state = "inside"
                self.buf = rest[len(self.OPEN):]
                # 开头的空白属于正文之前，丢掉不影响显示；保持与旧行为一致不输出
            elif self.OPEN.startswith(rest):
                return out             # 还可能是半个 <think>（或只有空白）：等下一块
            else:
                self.state = "done"
                text, self.buf = self.buf, ""
                return [("text", text)] if text else []
        # inside
        i = self.buf.find(self.CLOSE)
        if i >= 0:
            part, rest = self.buf[:i], self.buf[i + len(self.CLOSE):]
            self.buf = ""
            self.state = "done"
            if part:
                out.append(("reasoning", part))
            if rest:
                out.append(("text", rest))
            return out
        keep = 0                       # 保留可能是半个 </think> 的尾巴
        for n in range(min(len(self.CLOSE) - 1, len(self.buf)), 0, -1):
            if self.CLOSE.startswith(self.buf[-n:]):
                keep = n
                break
        emit = self.buf[: len(self.buf) - keep]
        self.buf = self.buf[len(self.buf) - keep:]
        if emit:
            out.append(("reasoning", emit))
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        r = [("reasoning" if self.state == "inside" else "text", self.buf)]
        self.buf = ""
        return r


# ---------------------------------------------------------------- 请求消息变换

def wire_messages(history: list[dict], profile) -> list[dict]:
    """历史（内部格式）→ 发给服务端的消息。总是复制，绝不原地改历史。

    内部 assistant 消息可带 "reasoning" 键（推理全文），这里按回传规则决定是否变成
    reasoning_content。规则只依赖消息本身 → 同一条消息每次产出的字节都一样。
    """
    mode = profile.reasoning_passback
    out = []
    for m in history:
        role = m.get("role")
        w = {k: v for k, v in m.items() if not k.startswith("_") and k != "reasoning"}
        if role == "assistant":
            has_tools = bool(m.get("tool_calls"))
            if mode == "all" or (mode == "tool_turns" and has_tools):
                w["reasoning_content"] = m.get("reasoning") or ""
            if w.get("content") in (None, ""):
                w["content"] = None if profile.empty_assistant_content == "null" and has_tools else ""
            if not has_tools:
                w.pop("tool_calls", None)
        elif role == "user" and m.get("_images"):
            from ee.core.attachments import wire_content
            w["content"] = wire_content(w.get("content") or "", list(m["_images"]), bool(profile.vision))
        elif role == "tool":
            if not w.get("tool_call_id"):
                w["tool_call_id"] = "call_" + uuid.uuid4().hex[:12]
            if not isinstance(w.get("content"), str):
                w["content"] = json.dumps(w.get("content"), ensure_ascii=False)
        out.append(w)
    return out


# ---------------------------------------------------------------- 工具名 / 参数修复

def normalize_tool_name(name: str, known: list[str]) -> str:
    if name in known:
        return name
    n = (name or "").strip()
    for pre in ("functions.", "functions:", "tool.", "tools."):
        if n.lower().startswith(pre):
            n = n[len(pre):]
    low = n.lower().replace("-", "_")
    aliases = {"read_file": "read", "write_file": "write", "edit_file": "edit", "str_replace": "edit",
               "bash": "shell", "run": "shell", "exec_command": "shell", "powershell": "shell", "run_command": "shell",
               "todowrite": "todo", "todo_write": "todo", "update_plan": "todo", "search": "grep",
               "find": "glob", "applypatch": "apply_patch"}
    for k in known:
        if k.lower() == low:
            return k
    if aliases.get(low) in known:
        return aliases[low]
    return n


def parse_args(raw: Any) -> tuple[dict | None, str | None]:
    """解析 arguments 字符串。返回 (参数, 错误)。"""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    s = str(raw).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    for attempt in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            v = json.loads(attempt)
            if isinstance(v, str):  # 双重编码
                v = json.loads(v)
            if isinstance(v, dict):
                return v, None
        except Exception:
            pass
    try:
        v = ast.literal_eval(s)
        if isinstance(v, dict):
            return v, None
    except Exception:
        pass
    return None, f"参数不是合法 JSON：{s[:200]}"


def repair_args(args: dict, schema: dict) -> dict:
    """按 JSON schema 修类型："20"→20、字符串化数组→数组、null 可选字段删除。返回新 dict。"""
    props = (schema or {}).get("properties", {})
    required = set((schema or {}).get("required", []))
    out = {}
    for k, v in args.items():
        spec = props.get(k)
        if v is None and k not in required:
            continue
        if spec is None:
            out[k] = v
            continue
        out[k] = _coerce(v, spec)
    return out


def _coerce(v: Any, spec: dict) -> Any:
    t = spec.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    try:
        if t == "integer" and isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
            return int(v)
        if t == "integer" and isinstance(v, float) and v.is_integer():
            return int(v)
        if t == "number" and isinstance(v, str):
            return float(v)
        if t == "boolean" and isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no"):
            return v.lower() in ("true", "1", "yes")
        if t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        if t == "string" and isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        if t == "array":
            if isinstance(v, str):
                try:
                    p = json.loads(v)
                    v = p if isinstance(p, list) else [p]
                except Exception:
                    v = [v]
            elif isinstance(v, dict):
                if v and all(str(i) in v for i in range(len(v))):
                    v = [v[str(i)] for i in range(len(v))]
                else:
                    v = [v]
            items = spec.get("items")
            if isinstance(v, list) and isinstance(items, dict):
                v = [_coerce(x, items) for x in v]
            return v
        if t == "object":
            if isinstance(v, str):
                p = json.loads(v)
                if isinstance(p, dict):
                    v = p
            if isinstance(v, dict) and "properties" in spec:
                return repair_args(v, spec)
            return v
    except Exception:
        return v
    return v


# ---------------------------------------------------------------- content 里的工具调用兜底

_DSML_ENV = re.compile(r"<[｜|]DSML[｜|]tool_calls\s*>(.*?)(?:</[｜|]DSML[｜|]tool_calls\s*>|$)", re.S)
_DSML_INV = re.compile(r"<[｜|]DSML[｜|]invoke\s+name=\"([^\"]+)\"\s*>(.*?)</[｜|]DSML[｜|]invoke>", re.S)
_DSML_PAR = re.compile(r"<[｜|]DSML[｜|]parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>(.*?)</[｜|]DSML[｜|]parameter>", re.S)
_TOOLCALL_XML = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
_FUNC_XML = re.compile(r"<function=([\w.\-]+)>(.*?)</function>", re.S)
_PARAM_XML = re.compile(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", re.S)
_FENCE = re.compile(r"```.*?```", re.S)


def _fence_ranges(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _FENCE.finditer(text)]


def _in(ranges, i) -> bool:
    return any(a <= i < b for a, b in ranges)


def _mk_call(name: str, args: dict) -> dict:
    return {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _scalar(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


def parse_content_tool_calls(content: str, known: list[str]) -> tuple[list[dict], str]:
    """模型把工具调用写进了正文时，尽量恢复。返回 (tool_calls, 剩余正文)。未命中返回 ([], 原文)。"""
    if not content or not content.strip():
        return [], content
    fences = _fence_ranges(content)
    calls: list[dict] = []
    remove: list[tuple[int, int]] = []

    # 1) DSML
    for env in _DSML_ENV.finditer(content):
        if _in(fences, env.start()):
            continue
        found = False
        for inv in _DSML_INV.finditer(env.group(1)):
            args = {}
            for p in _DSML_PAR.finditer(inv.group(2)):
                val = p.group(3)
                args[p.group(1)] = val if p.group(2) == "true" else _scalar(val.strip())
            calls.append(_mk_call(normalize_tool_name(inv.group(1), known), args))
            found = True
        if found:
            remove.append((env.start(), env.end()))

    # 2) <tool_call>{json}</tool_call>（Hermes/Qwen） 和 <function=x><parameter=y>（qwen3-coder）
    if not calls:
        for m in _TOOLCALL_XML.finditer(content):
            if _in(fences, m.start()):
                continue
            body = m.group(1)
            fm = _FUNC_XML.search(body)
            if fm:
                args = {p.group(1): _scalar(p.group(2)) if p.group(2).strip()[:1] in "[{" else p.group(2)
                        for p in _PARAM_XML.finditer(fm.group(2))}
                calls.append(_mk_call(normalize_tool_name(fm.group(1), known), args))
                remove.append((m.start(), m.end()))
                continue
            obj, _ = parse_args(body)
            if obj and "name" in obj:
                a = obj.get("arguments", obj.get("parameters", {}))
                a, _e = parse_args(a)
                calls.append(_mk_call(normalize_tool_name(obj["name"], known), a or {}))
                remove.append((m.start(), m.end()))
    if not calls:
        for fm in _FUNC_XML.finditer(content):
            if _in(fences, fm.start()):
                continue
            name = normalize_tool_name(fm.group(1), known)
            if name not in known:
                continue
            args = {p.group(1): p.group(2) for p in _PARAM_XML.finditer(fm.group(2))}
            calls.append(_mk_call(name, args))
            remove.append((fm.start(), fm.end()))

    # 3) 整段正文就是 JSON
    if not calls:
        s = content.strip()
        s2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
        if s2[:1] in "{[":
            try:
                v = json.loads(s2)
            except Exception:
                v = None
            items = []
            if isinstance(v, dict):
                items = v.get("tool_calls") if isinstance(v.get("tool_calls"), list) else [v]
            elif isinstance(v, list):
                items = v
            for it in items:
                if not isinstance(it, dict):
                    continue
                fn = it.get("function") if isinstance(it.get("function"), dict) else it
                name = fn.get("name") or fn.get("tool")
                if not name:
                    continue
                name = normalize_tool_name(name, known)
                if name not in known:
                    continue
                a = fn.get("arguments", fn.get("parameters", fn.get("args", {})))
                a, _e = parse_args(a)
                calls.append(_mk_call(name, a or {}))
            if calls:
                return calls, ""

    if not calls:
        return [], content
    rest = content
    for a, b in sorted(remove, reverse=True):
        rest = rest[:a] + rest[b:]
    return calls, rest.strip()


# ---------------------------------------------------------------- 降级判断

def unsupported_param(err_text: str) -> str | None:
    """从 400 错误文本判断哪个可选参数不被支持。"""
    t = (err_text or "").lower()
    for p in ("stream_options", "parallel_tool_calls", "reasoning_effort", "max_tokens", "temperature",
              "enable_thinking", "thinking"):
        if p == "thinking" and ("reasoning_content" in t or "thinking mode" in t):
            continue                         # 思考模式下的回传要求，不是参数不支持
        if p in t and any(w in t for w in ("unsupported", "not support", "unknown", "unrecognized",
                                            "extra", "invalid", "not allowed", "not permitted")):
            return p
    return None


def needs_reasoning_passback(err_text: str) -> bool:
    t = (err_text or "").lower()
    return "reasoning_content" in t and ("pass" in t or "must" in t or "missing" in t)
