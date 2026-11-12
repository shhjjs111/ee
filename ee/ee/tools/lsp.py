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

import asyncio
from pathlib import Path

from ee.core.coerce import as_int
from ee.core.lsp import diagnostics as D
from ee.tools.base import Tool, ToolContext, ToolResult, rel

SEVERITY_NAME = {1: "错误", 2: "警告", 3: "提示", 4: "建议"}
MAX_ITEMS = 50
WAIT_SECONDS = 120.0      # 等一次诊断的上限。首次调用还要叠上服务器启动（init_timeout 20s）
POLL_SECONDS = 0.15


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

    def __init__(self, manager=None, store: D.DiagStore | None = None, root: str | None = None,
                 notifier=None, spec=None, language: str = "python"):
        super().__init__()
        self.manager = manager
        self.store = store if store is not None else D.DiagStore()
        self.notifier = notifier    # 与 store 成对：负责 didOpen/didChange 与 uri→client 绑定
        self.spec = spec            # 启动规格（LspManager.acquire 需要），按需取进程时用
        self.language = language
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
        uri = uri_for(path) if path is not None else None

        if path is None or uri is None:
            return _unavailable("没有指定文件，无法确定要看哪份诊断")

        res = await self._current(path, uri, store)
        min_sev = as_int(args.get("severity"))

        if res.status == D.FRESH:
            items = [d for d in res.diagnostics
                     if not min_sev or as_int(d.get("severity")) <= min_sev]
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

    # ------------------------------------------------------------ 按需取诊断
    async def _current(self, path: Path, uri: str, store: D.DiagStore) -> D.Result:
        """取这个文件**当前磁盘内容**的诊断：缓存对得上就直接用，否则现取一次。

        为什么必须比对磁盘内容：服务器不常驻（用完就还、空闲回收），缓存里的结果
        可能属于旧内容。`DiagStore` 的 fresh 只保证「当时那个版本」，文件被别的
        途径改过（上一个会话 / 用户手改 / 模型在别的回合写的）就不再成立。
        拿旧诊断当新诊断是最坏的失败方式：模型会照着过时的类型信息改代码。
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            store.set_status(uri, D.ERROR, f"读不到文件：{e}")
            return store.get(uri)
        doc = store.doc(uri)
        cached = store.get(uri, default_status=D.NOT_OPEN)
        if doc is not None and cached.status == D.FRESH and doc.fp == D.fingerprint(text):
            return cached
        return await self._refresh(path, uri, text, store)

    async def _refresh(self, path: Path, uri: str, text: str, store: D.DiagStore) -> D.Result:
        """取一个服务器 → 打开当前内容 → 等本版本诊断 → 归还。

        为什么要在这里做“打开”：服务器不常驻，打开文档这件事只能由这次调用自己做。
        也正因为如此，「没写过的文件」也能查——以前只有写过的文件才会被 didOpen，
        模型对一个只读文件问诊断永远只能得到 not_open。
        """
        if self.manager is None or self.notifier is None:
            store.set_status(uri, D.UNAVAILABLE, "这个会话没有启用 LSP 诊断")
            return store.get(uri)
        if self.spec is None or not getattr(self.spec, "available", True):
            store.set_status(uri, D.UNAVAILABLE,
                             getattr(self.spec, "reason", "") or "没有可用的语言服务器")
            return store.get(uri)
        root = self.root or str(path.parent)
        try:
            handle = await self.manager.acquire(root, self.language, self.spec.identity, spec=self.spec)
        except Exception as e:  # noqa: BLE001  启动失败也是一种「没拿到诊断」
            store.set_status(uri, D.UNAVAILABLE, str(e))
            return store.get(uri)
        try:
            client = handle.entry.client
            # 这一行是整条链的接口：没有它永远收不到诊断。
            client.on_notification["textDocument/publishDiagnostics"] = store.on_publish
            self.notifier.attach(uri, client, path=str(path))
            self.notifier.default_client = client      # 单语言会话：不维护 uri 表时的兜底
            if store.doc(uri) is None:
                await self.notifier.opened(str(path), uri, text, language=self.language)
            else:
                await self.notifier.changed(str(path), uri, text, language=self.language)
            return await self._wait(uri, store)
        except Exception as e:  # noqa: BLE001  通知失败 / 协议错误都归到「没拿到」
            store.set_status(uri, D.ERROR, str(e))
            return store.get(uri)
        finally:
            # 一定要撕干净：留着死 client 会让下一次 `_notify_lsp` / 下次调用用到已回收的进程
            self.notifier.detach(uri)
            self.notifier.default_client = None
            await self.manager.release(handle)

    async def _wait(self, uri: str, store: D.DiagStore) -> D.Result:
        """等这个 uri 的**本版本**诊断。超时如实标 timeout，不拿旧结果充数。"""
        deadline = D.Deadline(WAIT_SECONDS)
        while not deadline.expired():
            cur = store.get(uri)
            doc = store.doc(uri)
            if cur.status == D.FRESH and doc is not None and cur.version == doc.version:
                return cur
            await asyncio.sleep(POLL_SECONDS)
        store.set_status(uri, D.TIMEOUT, f"{WAIT_SECONDS:.0f} 秒内没有拿到诊断")
        return store.get(uri)


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
    sev = SEVERITY_NAME.get(as_int(d.get("severity")), "信息")
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    line = as_int(start.get("line")) + 1
    col = as_int(start.get("character")) + 1
    src = d.get("source") or ""
    code = d.get("code")
    tail = f" [{src}{':' + str(code) if code else ''}]" if src or code else ""
    msg = str(d.get("message") or "").replace("\n", " ")
    return f"- {line}:{col} {sev}：{msg}{tail}"
