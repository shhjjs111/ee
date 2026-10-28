"""写文件成功后通知语言服务器。

**为什么必须有这一层**：模型改完文件，如果不同步告诉服务器，服务器还在算旧内容，
下一次取诊断拿到的就是过期结果——或者干脆没有结果，模型以为改好了。

**为什么不能让它反噬编辑**：诊断是辅助信息。服务器挂了、管道断了、超时了，
写文件这件事本身已经成功了，不能因为「通知失败」就把成功变成失败。所以这里
所有异常都吞掉并记在 `last_error` 上，只留痕，不影响调用方。
"""
from __future__ import annotations

import logging

from ee.core.lsp import diagnostics as D

log = logging.getLogger("ee.lsp")


class Notifier:
    """把文件变更同步给对应 uri 的客户端，并维护 DiagStore 里的版本/指纹。

    一个 Notifier 属于一个会话；它只认识"哪些 uri 归哪个 client"，
    不负责启动服务器（那是 manager 的事）。
    """

    def __init__(self, store: D.DiagStore | None = None):
        self.store = store if store is not None else D.DiagStore()
        self._clients: dict[str, object] = {}     # uri -> client
        self._paths: dict[str, str] = {}          # path -> uri
        self.last_error: str | None = None

    # ------------------------------------------------------------ 绑定
    def attach(self, uri: str, client, path: str | None = None) -> None:
        self._clients[uri] = client
        if path:
            self._paths[_norm(path)] = uri

    def detach(self, uri: str) -> None:
        self._clients.pop(uri, None)
        for p, u in list(self._paths.items()):
            if u == uri:
                self._paths.pop(p, None)

    def client_for(self, path: str | None = None, uri: str | None = None):
        if uri is not None:
            return self._clients.get(uri)
        if path is not None:
            return self._clients.get(self._paths.get(_norm(path), ""))
        return None

    # ------------------------------------------------------------ 通知
    async def opened(self, path: str, uri: str, text: str, language: str = "python") -> None:
        """打开文档（首次）。之后用 changed。"""
        doc = self.store.doc(uri)
        if doc is None:
            doc = self.store.open(D.Doc(uri=uri, version=1, text=text))
        client = self.client_for(path=path, uri=uri)
        if client is None:
            return
        await self._notify(client, "textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language, "version": doc.version, "text": text},
        })

    async def changed(self, path: str, uri: str, text: str, language: str = "python") -> None:
        """内容变了：先更新本地版本/指纹，再发增量或全量通知。

        顺序很重要：先让 DiagStore 知道新版本，服务器随后推来的（针对新版本的）
        诊断才会被认成 fresh；推来的旧版本诊断会被丢弃。
        """
        doc = self.store.doc(uri)
        if doc is None:
            await self.opened(path, uri, text, language)
            return
        doc = self.store.change(uri, text)
        client = self.client_for(path=path, uri=uri)
        if client is None:
            return
        await self._notify(client, "textDocument/didChange", {
            "textDocument": {"uri": uri, "version": doc.version},
            # 全量发送（不维护增量区间）：文件不大，换来的是不会算错 offset
            "contentChanges": [{"text": text}],
        })

    async def closed(self, path: str, uri: str) -> None:
        self.store.close(uri)
        client = self.client_for(path=path, uri=uri)
        if client is None:
            return
        await self._notify(client, "textDocument/didClose", {"textDocument": {"uri": uri}})

    async def _notify(self, client, method: str, params: dict) -> None:
        try:
            await client.notify(method, params)
            self.last_error = None
        except Exception as e:  # noqa: BLE001  通知失败不能反噬调用方
            self.last_error = f"{method} 失败：{e}"
            log.warning("LSP 通知 %s 失败（忽略，不影响写入）：%s", method, e)


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()
