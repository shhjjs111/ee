"""把 MCP 工具包装成 ee 的 Tool。名字 mcp__<服务器>__<工具>；描述前加 [MCP 服务器名]。"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from ee.mcp.schema import normalize, safe_name
from ee.tools.base import Tool, ToolContext, ToolResult

MAX_DESC = 2000


def tool_name(server: str, tool: str) -> str:
    return safe_name(f"mcp__{server}__{tool}")


class McpTool(Tool):
    def __init__(self, manager, server: str, spec: dict, trusted: bool = False, timeout: float = 120):   # 不调用父类（没有内置描述文件）
        self.manager = manager
        self.server = server
        self.remote_name = str(spec.get("name"))
        self.name = tool_name(server, self.remote_name)
        ann = spec.get("annotations") or {}
        self.read_only = bool(ann.get("readOnlyHint", False))
        self.trusted = trusted
        self.timeout = timeout
        desc = str(spec.get("description") or ann.get("title") or self.remote_name).strip()
        if len(desc) > MAX_DESC:
            desc = desc[:MAX_DESC] + "…"
        self.description = f"[MCP {server}] {desc}"
        self.parameters = normalize(spec.get("inputSchema") or {})

    def summary(self, args: dict) -> str:
        brief = json.dumps(args or {}, ensure_ascii=False)
        if len(brief) > 80:
            brief = brief[:80] + "…"
        return f"{self.server} · {self.remote_name} {brief}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            res = await self.manager.call(self.server, self.remote_name, args or {}, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"MCP 调用失败（{self.server}.{self.remote_name}）：{e}", ok=False,
                              meta={"kind": "mcp", "server": self.server, "tool": self.remote_name})
        text = render_result(res, ctx.output_dir)
        return ToolResult(text or "（空结果）", ok=not res.get("isError"),
                          meta={"kind": "mcp", "server": self.server, "tool": self.remote_name})


def render_result(res: dict, out_dir: Path | None) -> str:
    parts: list[str] = []
    for c in res.get("content") or []:
        t = c.get("type")
        if t == "text":
            parts.append(str(c.get("text") or ""))
        elif t in ("image", "audio"):
            path = _save_blob(c, out_dir)
            parts.append(f"[{ '图片' if t == 'image' else '音频' }：{c.get('mimeType', '')}，已保存到 {path}]" if path
                         else f"[{t}：{c.get('mimeType', '')}，无法保存]")
        elif t == "resource":
            r = c.get("resource") or {}
            if "text" in r:
                parts.append(f"[资源 {r.get('uri', '')}]\n{r.get('text')}")
            else:
                parts.append(f"[二进制资源 {r.get('uri', '')}（{r.get('mimeType', '')}）]")
        elif t == "resource_link":
            parts.append(f"[资源链接 {c.get('name') or ''} {c.get('uri', '')}]")
        else:
            parts.append(json.dumps(c, ensure_ascii=False)[:2000])
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False, indent=2))
    return "\n\n".join(p for p in parts if p)


def _save_blob(c: dict, out_dir: Path | None) -> str | None:
    if out_dir is None:
        return None
    try:
        data = base64.b64decode(c.get("data") or "")
    except Exception:  # noqa: BLE001
        return None
    ext = (c.get("mimeType") or "application/octet-stream").split("/")[-1].split(";")[0][:8] or "bin"
    d = Path(out_dir) / "mcp-images"
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{uuid.uuid4().hex[:10]}.{ext}"
        p.write_bytes(data)
        return str(p)
    except OSError:
        return None
