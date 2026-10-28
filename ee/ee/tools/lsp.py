"""lsp 工具：给模型看当前文件的语言服务器诊断（D6-2 / T9 U5）。

设计要点：

- `read_only = True`：只读诊断，不改任何东西，可以和别的只读工具并发。
- **取不到诊断 ≠ 工具失败。** 没服务器、超时、协议错误都返回 `ok=True`，
  但把 `status` 和原因写清楚。理由是：工具失败会让模型以为"看诊断"这个动作错了，
  而真实情况是"环境没准备好"——它应该继续改代码，只是知道没验证过。
- 返回的文本必须让模型能直接判断「这是本版本的结论」还是「没拿到」。
  把 unavailable 显示成"没有错误"是最糟的：模型会以为验过了。
"""
from __future__ import annotations

from pathlib import Path

from ee.core.lsp import diagnostics as D
from ee.tools.base import Tool, ToolContext, ToolResult, rel

SEVERITY_NAME = {1: "错误", 2: "警告", 3: "提示", 4: "建议"}
MAX_ITEMS = 50


class LspTool(Tool):
    name = "lsp"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要看诊断的文件；省略则用当前工作区的入口文件"},
            "severity": {"type": "integer", "description": "只显示不低于这个等级：1 错误 / 2 警告 / 3 提示"},
        },
        "required": [],
    }

    def __init__(self, manager=None, store: D.DiagStore | None = None, root: str | None = None):
        super().__init__()
        self.manager = manager
        self.store = store if store is not None else D.DiagStore()
        self.root = root

    def summary(self, args: dict) -> str:
        p = args.get("path") or "当前文件"
        return f"查看诊断 {p}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        store = self.store or _store_from_ctx(ctx)
        if store is None:
            return _unavailable("这个会话没有启用 LSP 诊断")

        target = args.get("path")
        path = ctx.resolve(target) if target else None
        uri = uri_for(path) if path else None

        if uri is None:
            return _unavailable("没有指定文件，无法确定要看哪份诊断")

        res = store.get(uri, default_status=D.NOT_OPEN)
        min_sev = int(args.get("severity") or 0)

        if res.status == D.FRESH:
            items = [d for d in res.diagnostics
                     if not min_sev or int(d.get("severity") or 0) <= min_sev]
            if not items:
                return ToolResult(f"{_where(path, ctx.cwd)}：本版本没有诊断问题。", summary=self.summary(args),
                                  meta={"status": D.FRESH, "clean": True, "count": 0})
            lines = [f"{_where(path, ctx.cwd)}：{len(items)} 个诊断（本版本）"]
            for d in items[:MAX_ITEMS]:
                lines.append(_fmt(d))
            if len(items) > MAX_ITEMS:
                lines.append(f"…另有 {len(items) - MAX_ITEMS} 条未显示")
            return ToolResult("\n".join(lines), summary=self.summary(args),
                              meta={"status": D.FRESH, "clean": False, "count": len(items)})

        # 非 fresh：如实说明没拿到，而不是「没有错误」
        why = {
            D.UNAVAILABLE: "语言服务器不可用（可能没安装）",
            D.TIMEOUT: "等待诊断超时",
            D.ERROR: "语言服务器出错",
            D.CLOSED: "文件已关闭",
            D.STALE: "现有诊断属于旧版本，已作废",
            D.NOT_OPEN: "还没向语言服务器请求过这个文件",
        }.get(res.status, res.status)
        detail = f"（{res.reason}）" if res.reason else ""
        body = (f"{_where(path, ctx.cwd)}：**未取得诊断**——{why}{detail}。\n"
                f"这不代表没有问题；改动是否类型正确尚未验证。")
        return ToolResult(body, summary=self.summary(args),
                          meta={"status": res.status, "clean": False, "count": 0})


def _store_from_ctx(ctx: ToolContext) -> D.DiagStore | None:
    sess = (ctx.extra or {}).get("session")
    return getattr(sess, "lsp_store", None)


def _unavailable(reason: str) -> ToolResult:
    return ToolResult(f"**未取得诊断**——{reason}。这不代表没有问题。", ok=True,
                      meta={"status": D.UNAVAILABLE, "clean": False, "count": 0})


def _where(path: Path | None, cwd: Path | None = None) -> str:
    if path is None:
        return "当前文件"
    return rel(path, cwd) if cwd is not None else path.name


def uri_for(path: Path | str) -> str:
    from urllib.parse import quote
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p)


def _fmt(d: dict) -> str:
    sev = SEVERITY_NAME.get(int(d.get("severity") or 0), "信息")
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    line = int(start.get("line") or 0) + 1
    col = int(start.get("character") or 0) + 1
    src = d.get("source") or ""
    code = d.get("code")
    tail = f" [{src}{':' + str(code) if code else ''}]" if src or code else ""
    msg = str(d.get("message") or "").replace("\n", " ")
    return f"- {line}:{col} {sev}：{msg}{tail}"
