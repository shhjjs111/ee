"""JSON Schema 规范化：很多模型（尤其国产模型和 OpenAI 兼容网关）不认 $ref、anyOf、type 数组等。"""
from __future__ import annotations

import copy
import hashlib
import re

DROP = {"format", "additionalProperties", "title", "examples", "$schema", "$id", "$comment", "default",
        "readOnly", "writeOnly", "deprecated", "contentMediaType", "contentEncoding", "unevaluatedProperties",
        "patternProperties", "dependentRequired", "dependentSchemas", "if", "then", "else", "not"}
MAX_DEPTH = 24


def _resolve_ref(ref: str, root: dict) -> dict:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        return {}
    cur = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return {}
        cur = cur[part]
    return cur if isinstance(cur, dict) else {}


def _infer_type(s: dict) -> str:
    if "properties" in s:
        return "object"
    if "items" in s:
        return "array"
    if "enum" in s and s["enum"]:
        v = s["enum"][0]
        return {bool: "boolean", int: "integer", float: "number", str: "string"}.get(type(v), "string")
    return "string"


def _norm(s, root: dict, depth: int, seen: tuple) -> dict:
    if not isinstance(s, dict) or depth > MAX_DEPTH:
        return {"type": "string"}
    s = dict(s)
    if "$ref" in s:
        ref = s.pop("$ref")
        if ref in seen:                       # 递归引用：截断成 object
            return {"type": "object", "description": s.get("description", "")} if s.get("description") else {"type": "object"}
        target = _resolve_ref(ref, root)
        merged = {**target, **s}
        return _norm(merged, root, depth + 1, seen + (ref,))
    if "allOf" in s:
        parts = s.pop("allOf") or []
        merged: dict = {}
        props: dict = {}
        req: list = []
        for p in parts:
            p = _norm(p, root, depth + 1, seen)
            props.update(p.get("properties") or {})
            req += p.get("required") or []
            merged.update({k: v for k, v in p.items() if k not in ("properties", "required")})
        merged.update(s)
        if props or s.get("properties"):
            merged["properties"] = {**props, **(s.get("properties") or {})}
        if req or s.get("required"):
            merged["required"] = list(dict.fromkeys(req + list(s.get("required") or [])))
        s = merged
    for key in ("anyOf", "oneOf"):
        if key in s:
            opts = [o for o in (s.pop(key) or []) if not (isinstance(o, dict) and o.get("type") == "null")]
            if opts:
                pick = _norm(opts[0], root, depth + 1, seen)
                desc = s.get("description")
                s = {**pick, **{k: v for k, v in s.items() if k != "description"}}
                if desc:
                    s["description"] = desc
    for k in list(s):
        if k in DROP or k.startswith("$"):
            s.pop(k, None)
    if "const" in s:
        s["enum"] = [s.pop("const")]
    t = s.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        s["type"] = non_null[0] if non_null else "string"
    if not s.get("type"):
        s["type"] = _infer_type(s)
    if s["type"] == "object":
        props = s.get("properties") or {}
        s["properties"] = {str(k): _norm(v, root, depth + 1, seen) for k, v in props.items()}
        if "required" in s:
            s["required"] = [r for r in (s.get("required") or []) if r in s["properties"]]
            if not s["required"]:
                s.pop("required")
    elif s["type"] == "array":
        s["items"] = _norm(s.get("items") or {"type": "string"}, root, depth + 1, seen) \
            if not isinstance(s.get("items"), list) else _norm((s["items"] or [{}])[0], root, depth + 1, seen)
    for k in ("definitions", "$defs"):
        s.pop(k, None)
    return s


def normalize(schema) -> dict:
    root = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    out = _norm(root, root, 0, ())
    if out.get("type") != "object":
        out = {"type": "object", "properties": {}}
    out.setdefault("properties", {})
    return out


_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_name(name: str, limit: int = 64) -> str:
    """工具名只留 [A-Za-z0-9_-]，过长截断并加短 hash 防冲突。"""
    n = _SAFE.sub("_", name or "tool")
    if len(n) > limit:
        h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        n = n[: limit - 9] + "_" + h
    return n
