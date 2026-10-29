"""诊断缓存与状态语义。

这一层负责回答「这个文件的诊断现在能信吗」，而不是「诊断是什么」。

三条不变量（做错了不会抛异常，只会让用户看到错的结论）：

1. **版本 + 内容指纹配对。** LSP 是异步的：你可能已经改了文件（v3），服务器才把 v2 的
   诊断推过来。凭「有诊断」就展示，用户会看到一个已经不存在的错误。所以每条诊断都
   绑定发布时的 (version, fingerprint)，与当前文档不一致就丢弃。
2. **unavailable ≠ 没有错误。** 没有语言服务器、超时、协议错误，都必须如实报告状态。
   把「没取到」显示成「干净」，比不显示更糟——用户会以为检查过了。
   只有 `status == "fresh"` 且 `diagnostics == []` 时 `clean` 才为 True。
3. **等待预算是一个截止时间。** 三个文件各自超时 2 秒，串行就是 6 秒；用一个
   Deadline 让它们共享同一段时间预算。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable

# 状态取值
FRESH = "fresh"              # 本版本的服务器结果（可信）
STALE = "stale"              # 收到过，但版本/指纹与当前文档不符（已丢弃）
TIMEOUT = "timeout"          # 等到预算用完还没拿到
UNAVAILABLE = "unavailable"  # 没有可用服务器
ERROR = "error"              # 协议或进程错误
CLOSED = "closed"            # 文档已关闭
NOT_OPEN = "not_open"        # 没打开过（未发起请求）

# 只有这个状态下的空诊断才算「干净」
TRUSTED = (FRESH,)


def norm_uri(uri: str) -> str:
    """把 URI 归一成可比较的键。

    **为什么必须归一**：真实的 pyright 会把 Windows 盘符小写化——我们发
    `file:///E%3A/...`，它回报 `file:///e%3A/...`。`DiagStore` 用 uri 做字典键，
    不归一的话 `_docs.get(uri)` 命不中，诊断会被当成「未知文档」整条丢弃，
    现象是明明装了服务器却永远 `not_open`。假服务器回的原样 uri，所以测不出来。

    只归一不改变语义的部分：scheme 与 Windows 盘符（`X%3A` / `x%3A`）大小写。
    路径其余部分保持原样——POSIX 下大小写是有意义的。
    """
    if not uri:
        return uri
    head, sep, rest = uri.partition("://")
    head = head.lower()
    if not sep:
        return head
    # 盘符前面还有前导斜杠：`file:///E%3A/...` 的 rest 是 `/E%3A/...`。
    slash = "/" if rest.startswith("/") else ""
    body = rest[len(slash):]
    # 盘符编码有两种写法：`E%3A`（大写十六进制）与 `e%3a`。都归一成小写盘符 + `%3A`。
    if len(body) >= 4 and body[0].isalpha() and body[1:4].lower() == "%3a":
        body = body[0].lower() + "%3A" + body[4:]
    return head + sep + slash + body


def fingerprint(text: str) -> str:
    """内容指纹。用 blake2b 截断，够区分版本内的内容变化，代价可忽略。"""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()


@dataclass
class Doc:
    uri: str
    version: int = 1
    text: str = ""
    fp: str = ""

    def __post_init__(self) -> None:
        if not self.fp:
            self.fp = fingerprint(self.text)


@dataclass
class Result:
    """一次查询的结果。`clean` 是给调用方的语义结论，别看 diagnostics 是否为空就下判断。"""
    uri: str
    status: str = NOT_OPEN
    diagnostics: list = field(default_factory=list)
    version: int | None = None
    reason: str = ""

    @property
    def clean(self) -> bool:
        """只有「可信的、确实没诊断」才算干净。"""
        return self.status in TRUSTED and not self.diagnostics


class Deadline:
    """一次请求里共享的等待预算。

    用它而不是每个文件 wait_for(2)：N 个文件串行累加会把总耗时放大 N 倍。
    """

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.seconds = max(0.0, float(seconds))
        self._clock = clock
        self._end = clock() + self.seconds

    def left(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self.left() <= 0

    def reset(self) -> None:
        self._end = self._clock() + self.seconds


class DiagStore:
    """按 uri 存文档版本与可信诊断。纯内存，不落盘。"""

    def __init__(self) -> None:
        self._docs: dict[str, Doc] = {}
        self._results: dict[str, Result] = {}

    # ------------------------------------------------------------ 文档
    def open(self, doc: Doc) -> Doc:
        # 键统一用 norm_uri：服务器回报的 uri 盘符大小写可能与发送时不同。
        doc.uri = norm_uri(doc.uri)
        self._docs[doc.uri] = doc
        # 打开时把历史结果作废：新文档的版本从 1 重新开始
        self._results.pop(doc.uri, None)
        return doc

    def change(self, uri: str, text: str) -> Doc:
        """内容变了：版本 +1、指纹重算，并把已有诊断标成过期。

        关键在最后一步：内容变了但服务器还没推新诊断，这时旧诊断不能继续当 fresh。
        """
        uri = norm_uri(uri)
        doc = self._docs.get(uri)
        if doc is None:
            doc = Doc(uri=uri, text=text)
            self._docs[uri] = doc
        else:
            doc.version += 1
            doc.text = text
            doc.fp = fingerprint(text)
        prev = self._results.get(uri)
        if prev is not None and prev.status == FRESH and (
                prev.version != doc.version or prev.version is None):
            self._results[uri] = Result(uri=uri, status=STALE, diagnostics=[],
                                        version=doc.version, reason="内容已变，等待新诊断")
        return doc

    def close(self, uri: str) -> None:
        uri = norm_uri(uri)
        self._docs.pop(uri, None)
        self._results[uri] = Result(uri=uri, status=CLOSED, diagnostics=[])

    def doc(self, uri: str) -> Doc | None:
        return self._docs.get(norm_uri(uri))

    def uris(self) -> list[str]:
        return list(self._docs)

    # ------------------------------------------------------------ 诊断
    def on_publish(self, params: dict, fingerprint: str | None = None) -> bool:
        """处理 publishDiagnostics。返回是否被采纳（False = 丢弃了旧/错的）。

        `fingerprint` 为 None 时按「服务器回的是它收到的那个版本」处理，
        仍要校验 version 不能比当前文档新也不要更旧。
        """
        uri = norm_uri(params.get("uri") or "")
        doc = self._docs.get(uri)
        if doc is None:
            return False
        ver = params.get("version")
        # 服务器不带 version 的实现（早期 pyright）按当前版本处理，靠指纹兜底
        if ver is not None and int(ver) != doc.version:
            # 丢弃，但**要留下痕迹**：调用方需要知道"收到过但不可信"，
            # 否则一个旧版本通知会让状态永远停在 not_open，看起来像"没查过"。
            self._mark_stale(uri, doc, f"丢弃旧版本诊断（v{ver} != 当前 v{doc.version}）")
            return False
        if fingerprint is not None and fingerprint != doc.fp:
            self._mark_stale(uri, doc, "丢弃指纹不符的诊断（内容已不是发布时那份）")
            return False
        diags = list(params.get("diagnostics") or [])
        self._results[uri] = Result(uri=uri, status=FRESH, diagnostics=diags, version=doc.version)
        return True

    def _mark_stale(self, uri: str, doc: Doc, reason: str) -> None:
        """把状态标成 stale，但**不覆盖已有的 fresh**——已经拿到本版本结果时，
        一条迟到的旧通知不该把好结果冲掉。"""
        cur = self._results.get(uri)
        if cur is not None and cur.status == FRESH and cur.version == doc.version:
            return
        self._results[uri] = Result(uri=uri, status=STALE, diagnostics=[], version=doc.version,
                                    reason=reason)

    def set_status(self, uri: str, status: str, reason: str = "") -> None:
        """把某个 uri 标成 timeout / unavailable / error，并清空诊断。"""
        uri = norm_uri(uri)
        self._results[uri] = Result(uri=uri, status=status, diagnostics=[], reason=reason)

    def get(self, uri: str, default_status: str = NOT_OPEN) -> Result:
        res = self._results.get(norm_uri(uri))
        if res is not None:
            return res
        return Result(uri=uri, status=default_status)

    # ------------------------------------------------------------ 汇总
    def summarize(self, uris: list[str] | None = None, default_status: str = NOT_OPEN) -> dict:
        """给工具结果用：按状态分组，并明确告诉调用方「哪些没取到」。"""
        targets = uris if uris is not None else list(self._docs)
        fresh, pending, failed = [], [], []
        for u in targets:
            r = self.get(u, default_status=default_status)
            if r.status == FRESH:
                fresh.append(r)
            elif r.status in (TIMEOUT, UNAVAILABLE, ERROR, NOT_OPEN):
                failed.append(r)
            else:
                pending.append(r)
        return {
            "fresh": fresh,
            "pending": pending,
            "failed": failed,
            "clean": bool(fresh) and not any(r.diagnostics for r in fresh) and not failed,
            "diagnostics_total": sum(len(r.diagnostics) for r in fresh),
        }
