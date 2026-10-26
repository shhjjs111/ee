"""`ee doctor [模型]`：对一个模型做逐项自检（对话、流式、工具调用、并行工具、推理字段、缓存字段），
结果写回 ~/.ee/state/doctor.json，供以后调整档案参考。会真实调用一次 API（少量 token）。
"""
from __future__ import annotations

import json
import time

from ee.config import ee_home
from ee.core.providers.openai_chat import ChatClient

PROBE_TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "查询城市天气",
                                      "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                     "required": ["city"]}}},
]


async def run_doctor(config, model: str | None) -> int:
    spec = config.resolve_model(model)
    print(f"自检 {spec.alias}（{spec.model}，家族 {spec.profile.family}，{spec.base_url}）")
    if not spec.api_key():
        print(f"✗ {spec.key_hint()}")
        return 1
    client = ChatClient(spec, state_path=ee_home() / "state" / "quirks.json")
    results: dict = {}

    async def call(messages, tools=None):
        chunks, comp = 0, None
        async for kind, val in client.stream(messages, tools):
            if kind in ("text", "reasoning"):
                chunks += 1
            elif kind == "notice":
                print("  ⚠", val)
            elif kind == "done":
                comp = val
        return chunks, comp

    checks = [
        ("对话+流式", [{"role": "user", "content": "只回复两个字：你好"}], None),
        ("工具调用", [{"role": "user", "content": "北京天气怎么样？必须调用工具。"}], PROBE_TOOLS),
        ("并行工具", [{"role": "user", "content": "同时查询北京和上海的天气，在一条回复里调用两次工具。"}], PROBE_TOOLS),
    ]
    ok_all = True
    for name, msgs, tools in checks:
        t0 = time.monotonic()
        try:
            chunks, comp = await call(msgs, tools)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {name}：{type(e).__name__}: {str(e)[:200]}")
            results[name] = {"ok": False, "error": str(e)[:500]}
            ok_all = False
            continue
        dt = time.monotonic() - t0
        info = {"ok": True, "seconds": round(dt, 2), "stream_chunks": chunks,
                "usage": comp.usage, "reasoning": bool(comp.reasoning), "tool_calls": len(comp.tool_calls),
                "recovered_from_content": comp.recovered}
        if tools and not comp.tool_calls:
            info["ok"] = False
        if name == "并行工具":
            info["ok"] = len(comp.tool_calls) >= 2
        results[name] = info
        mark = "✔" if info["ok"] else "✗"
        ok_all &= info["ok"]
        hit = (comp.usage or {}).get("hit")
        print(f"{mark} {name}：{dt:.1f}s，流式块 {chunks}，工具调用 {len(comp.tool_calls)}"
              f"{'（从正文兜底解析）' if comp.recovered else ''}，推理字段 {'有' if comp.reasoning else '无'}，"
              f"usage {'有' if comp.usage else '无'}，缓存命中字段 {'有' if hit is not None else '无'}")
    path = ee_home() / "state" / "doctor.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        data = {}
    data[f"{spec.base_url}|{spec.model}"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {path}")
    return 0 if ok_all else 1
