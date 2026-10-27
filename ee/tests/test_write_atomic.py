"""写文件必须原子：磁盘满 / 进程被杀 / 断电时，源文件不能变成半截。

缺陷（探针已复现）：`fileops.write_text()` 直接 `open(path, "wb")` 覆盖，
O_TRUNC 会先把原文件清零再写。写到一半失败 → 原内容永久丢失：

    原始 6800 字节 → 中途失败 → 只剩 4 字节 "half"

`write` / `edit` / `apply_patch` 三个工具共用这个函数，等于三个工具都不安全。
修法：改用 `ee.core.atomic.write_bytes`（临时文件 + fsync + os.replace）。
"""
import os
from pathlib import Path

import pytest

from ee.core import atomic
from ee.tools import fileops
from ee.tools.base import ToolError


def _tf(text: str):
    """构造一个已存在的 TextFile 描述。"""
    return fileops.TextFile(text.replace("\r\n", "\n"), "\n", False, True)


# ---------------------------------------------------------------- 缺陷复现


def test_write_failure_keeps_original(workspace, monkeypatch):
    """写到一半失败时，源文件必须还是原内容（不能被截断）。"""
    f = workspace / "a.py"
    original = "# 重要内容\n" * 300
    f.write_text(original, encoding="utf-8")
    before = f.read_bytes()

    real_replace = atomic.os.replace

    def boom(tmp, dst, retries=0):
        raise OSError(28, "No space left on device")     # 磁盘满

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        fileops.write_text(f, _tf(original), original + "# 新的一行\n")

    assert f.read_bytes() == before, "写入失败后源文件被改动了（原子性丢失）"
    monkeypatch.setattr(atomic.os, "replace", real_replace)


def test_write_failure_leaves_no_temp_files(workspace, monkeypatch):
    """失败后不能留下临时文件残渣。"""
    f = workspace / "b.py"
    f.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(atomic.os, "replace",
                        lambda tmp, dst, retries=0: (_ for _ in ()).throw(OSError(28, "disk full")))
    with pytest.raises(OSError):
        fileops.write_text(f, _tf("x = 1\n"), "x = 2\n")
    leftovers = [p.name for p in workspace.iterdir() if p.name != "b.py"]
    assert leftovers == [], f"留下了临时文件：{leftovers}"


# ---------------------------------------------------------------- 正常路径不回归


def test_normal_write_still_works(workspace):
    f = workspace / "c.py"
    f.write_text("old\n", encoding="utf-8")
    fileops.write_text(f, _tf("old\n"), "new\n")
    assert f.read_text(encoding="utf-8") == "new\n"


def test_new_file_is_created(workspace):
    f = workspace / "sub" / "new.py"
    fileops.write_text(f, fileops.TextFile("", "\n", False, False), "hello\n")
    assert f.read_text(encoding="utf-8") == "hello\n"


def test_crlf_and_bom_preserved(workspace):
    """换行与 BOM 的行为不能因为改原子写而变。"""
    f = workspace / "d.py"
    tf = fileops.TextFile("a\r\n", "\r\n", True, True)
    fileops.write_text(f, tf, "a\nb\n")
    raw = f.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM 丢了"
    assert b"\r\n" in raw and b"\n\n" not in raw.replace(b"\r\n", b""), "CRLF 没保持"


def test_gbk_fallback_still_switches_to_utf8(workspace):
    """原编码表示不了时改存 UTF-8（现有行为，不能丢）。"""
    f = workspace / "e.py"
    tf = fileops.TextFile("x", "\n", False, True, encoding="gbk")
    fileops.write_text(f, tf, "中文😀\n")
    assert "😀" in f.read_text(encoding="utf-8")


def test_uses_atomic_module(workspace, monkeypatch):
    """钉住实现方式：必须走 atomic.write_bytes，而不是自己 open+write。"""
    called = {}
    real = atomic.write_bytes

    def spy(path, data, retries=atomic.REPLACE_RETRIES):
        called["path"], called["len"] = Path(path), len(data)
        return real(path, data, retries)

    monkeypatch.setattr(fileops, "atomic_write", spy)
    f = workspace / "f.py"
    fileops.write_text(f, _tf("x"), "hello\n")
    assert called.get("path") == f and called.get("len") == len("hello\n".encode())
