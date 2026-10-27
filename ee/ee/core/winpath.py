"""Windows 路径归一（纯函数）：让 .eeignore 与工作区边界判断看到的是 Windows 实际会打开的那个文件。

Windows 打开文件时会：去掉每一级名字结尾的点和空格（`secret.env.` 就是 `secret.env`）；
把 `名字:流名:$DATA` 当成该文件的备用数据流（`secret.env::$DATA` 读到的就是 secret.env 的内容）；
`\\\\?\\` 前缀只是绕过长度限制；8.3 短名（`SECRET~1.ENV`）指向同一个文件。
normalize_win_path 把这些写法都归成长名、无流、无结尾点空格的普通路径；不改大小写（比较时另行忽略）。
"""
from __future__ import annotations

import re
from typing import Callable

_DRIVE = re.compile(r"^[A-Za-z]:")


def get_long_path_name(p: str) -> str | None:
    """系统 API GetLongPathNameW：短名 → 长名；路径不存在或不是 Windows 时返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.windll.kernel32.GetLongPathNameW       # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None
    f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    f.restype = wintypes.DWORD
    n = f(p, None, 0)
    if not n:
        return None
    buf = ctypes.create_unicode_buffer(n + 1)
    m = f(p, buf, n + 1)
    return buf.value if m and m <= n + 1 else None


def _clean_part(part: str) -> str:
    if part in (".", ".."):
        return part
    if ":" in part:                     # 备用数据流：name:stream[:type]，都算 name 本身
        part = part.split(":", 1)[0]
    stripped = part.rstrip(". ")
    return stripped if stripped else part   # 全是点/空格的名字保持原样（不会凭空变成上级目录）


def normalize_win_path(path: str, long_path: Callable[[str], str | None] | None = get_long_path_name) -> str:
    s = str(path).replace("/", "\\")
    if s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        rest = s[4:]
        s = "\\\\" + rest[4:] if rest[:4].upper() == "UNC\\" else rest
    prefix = ""
    if s.startswith("\\\\"):                          # UNC：\\server\share 原样保留
        parts = s[2:].split("\\")
        prefix = "\\\\" + "\\".join(parts[:2])
        body = parts[2:]
        if body:
            prefix += "\\"
    else:
        m = _DRIVE.match(s)
        if m:
            prefix = m.group(0)
            s = s[2:]
        if s.startswith("\\"):
            prefix += "\\"
            s = s.lstrip("\\")
        body = s.split("\\") if s else []
    body = [p for p in body if p != ""]
    body = [_clean_part(p) for p in body]
    out = prefix + "\\".join(body)
    if long_path is not None and any("~" in p for p in body):
        # 8.3 短名：GetLongPathNameW 只认存在的路径，所以从整条往上找最长的存在前缀
        for i in range(len(body), 0, -1):
            head = prefix + "\\".join(body[:i])
            try:
                lp = long_path(head)
            except OSError:
                lp = None
            if lp:
                lp = lp[4:] if lp.startswith("\\\\?\\") else lp
                out = "\\".join([lp.rstrip("\\"), *body[i:]]) if body[i:] else lp
                break
    return out
