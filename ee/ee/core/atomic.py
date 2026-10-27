"""原子写文件：同目录临时文件 → 写完 fsync → os.replace 覆盖目标。

写到一半崩溃/断电时目标文件要么是旧内容、要么是新内容，不会出现半截文件。
Windows 上目标文件正被别的进程（杀毒、索引、另一个 ee 进程）短暂打开时 os.replace 会报
PermissionError，稍等重试；最终仍失败就删掉临时文件并把异常抛给调用方，目标文件保持原样。
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

REPLACE_RETRIES = 20


def _replace(src: str, dst: str, retries: int) -> None:
    for i in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(min(0.02 * (i + 1), 0.2))


def write_bytes(path: Path | str, data: bytes, retries: int = REPLACE_RETRIES) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # 0o666 交给 umask 决定最终权限，和直接 open(..., "w") 新建的文件一样；已有文件沿用原权限
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if os.name != "nt":
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            except OSError:
                pass
        _replace(str(tmp), str(path), retries)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path | str, text: str, encoding: str = "utf-8", newline: str | None = None,
               retries: int = REPLACE_RETRIES) -> None:
    """同 Path.write_text：newline=None 时 \\n 按平台换行写出（Windows 上是 \\r\\n），给 "" 则原样。"""
    if newline is None:
        newline = os.linesep
    if newline != "\n":
        text = text.replace("\n", newline) if newline else text
    write_bytes(path, text.encode(encoding), retries)
