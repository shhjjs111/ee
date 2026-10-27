"""子进程输出解码：先按 UTF-8，不是合法 UTF-8 时改用本机编码（中文 Windows 是 cp936/GBK），不再乱码。

- smart_decode(bytes)：一次性输出（Hook 等）。
- StreamDecoder：分块读取的输出（shell / 后台命令）。多字节字符被拆在两块之间也能拼对；
  一旦遇到非法 UTF-8，本条输出后面都按回退编码解（同一个程序的输出编码通常一致）。
"""
from __future__ import annotations

import codecs
import locale


def fallback_encoding() -> str:
    """本机的非 UTF-8 编码；本机本身就是 UTF-8 / ASCII 时用 cp936（中文 Windows 控制台程序最常见）。"""
    try:
        enc = locale.getpreferredencoding(False) or ""
        name = codecs.lookup(enc).name if enc else ""
    except LookupError:
        name = ""
    if not name or name in ("utf-8", "ascii", "utf-8-sig"):
        return "cp936"
    return name


def _utf8_prefix(b: bytes) -> tuple[str, bytes] | None:
    """按 UTF-8 严格解码；末尾不完整的多字节序列留作下一块。中间有非法字节返回 None。"""
    try:
        return b.decode("utf-8"), b""
    except UnicodeDecodeError as e:
        if e.start >= len(b) - 3 and "unexpected end" in e.reason:
            try:
                return b[:e.start].decode("utf-8"), b[e.start:]
            except UnicodeDecodeError:
                return None
        return None


def smart_decode(b: bytes, fallback: str | None = None) -> str:
    if not b:
        return ""
    r = _utf8_prefix(b)
    if r is not None:
        text, rest = r
        return text + ("\ufffd" if rest else "")
    enc = fallback or fallback_encoding()
    try:
        return b.decode(enc)
    except (UnicodeDecodeError, LookupError):
        pass
    a = b.decode("utf-8", errors="replace")
    try:
        g = b.decode(enc, errors="replace")
    except LookupError:
        return a
    return g if g.count("\ufffd") < a.count("\ufffd") else a


class StreamDecoder:
    def __init__(self, fallback: str | None = None):
        self._fallback = fallback
        self._pending = b""
        self._dec = None          # 切到回退编码后的增量解码器

    @property
    def encoding(self) -> str:
        return "utf-8" if self._dec is None else (self._fallback or "")

    def decode(self, b: bytes, final: bool = False) -> str:
        if self._dec is not None:
            return self._dec.decode(b, final)
        data = self._pending + b
        r = _utf8_prefix(data)
        if r is not None:
            text, self._pending = r
            if final and self._pending:
                self._pending = b""
                text += "\ufffd"
            return text
        self._fallback = self._fallback or fallback_encoding()
        try:
            self._dec = codecs.getincrementaldecoder(self._fallback)("replace")
        except LookupError:
            self._fallback = "utf-8"
            self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = b""
        return self._dec.decode(data, final)
