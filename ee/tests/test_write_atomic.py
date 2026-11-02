"""写文件必须原子：磁盘满 / 进程被杀 / 断电时，源文件不能变成半截。

缺陷（探针已复现）：`fileops.write_text()` 直接 `open(path, "wb")` 覆盖，
O_TRUNC 会先把原文件清零再写。写到一半失败 → 原内容永久丢失：

    原始 6800 字节 → 中途失败 → 只剩 4 字节 "half"

`write` / `edit` / `apply_patch` 三个工具共用这个函数，等于三个工具都不安全。
修法：改用 `ee.core.atomic.write_bytes`（临时文件 + fsync + os.replace）。
"""
from pathlib import Path
import importlib.util

import pytest

from ee.core import atomic
from ee.tools import fileops


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


# ---------------------------------------------------------------- 陈旧字节码（CI 真实红灯）


def test_edit_drops_stale_bytecode(workspace):
    """等长改动必须删掉 .pyc，否则下一轮 import 会读到旧字节码。

    缺陷：CPython 只比「源码 mtime 的整秒 + 文件大小」判字节码是否过期。
    `return a - b` → `return a + b` 长度完全相等（34 字节），若与上次 import 落在同
    一秒，两项都不变 → Python 认为缓存有效，测试仍读到旧值。

    真实事故：CI 上 goal 闭环验收里 agent 已经改对了，pytest 却持续报
    `assert -1 == 5`，循环跑满 3 轮把脚本耗光才退出 3。探针可 100% 复现（把源码
    mtime 拨回原值、大小不变时，改完后 pytest 依旧报旧结果；删掉 .pyc 才转绿）。
    """
    src = workspace / "calc.py"
    src.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    pyc = Path(importlib.util.cache_from_source(str(src)))
    pyc.parent.mkdir(exist_ok=True)
    pyc.write_bytes(b"stale")                     # 假装这份缓存已经存在且过期不了

    fileops.write_text(src, fileops.read_text(src),
                       "def add(a, b):\n    return a + b\n")   # 等长：34 字节 → 34 字节

    assert not pyc.exists(), "改了 .py 却没删对应的 .pyc，下一轮 import 会读到旧字节码"


def test_edit_drops_all_versioned_bytecode(workspace):
    """__pycache__ 里同一模块的多个版本（cpython-311/312）都要删。"""
    src = workspace / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    cache = workspace / "__pycache__"
    cache.mkdir()
    mine = [cache / "mod.cpython-311.pyc", cache / "mod.cpython-312.pyc"]
    other = cache / "modhelper.cpython-312.pyc"
    for p in [*mine, other]:
        p.write_bytes(b"old")

    fileops.write_text(src, fileops.read_text(src), "x = 2\n")

    assert not any(p.exists() for p in mine), "同模块的其他版本 pyc 没被删"
    assert other.exists(), "把同目录别的模块的 pyc 也删了（前缀匹配写错了）"


def test_non_python_file_keeps_bytecode(workspace):
    """非 .py 文件不动任何缓存：避免误删。"""
    f = workspace / "data.txt"
    f.write_text("a\n", encoding="utf-8")
    pyc = workspace / "__pycache__" / "data.cpython-312.pyc"
    pyc.parent.mkdir()
    pyc.write_bytes(b"unrelated")

    fileops.write_text(f, fileops.read_text(f), "b\n")

    assert pyc.exists()


def test_bytecode_drop_failure_does_not_break_write(workspace, monkeypatch):
    """缓存删不掉（Windows 上被杀软/索引占着）不能连累文件写入。"""
    src = workspace / "locked.py"
    src.write_text("x = 1\n", encoding="utf-8")

    def boom(self):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(Path, "unlink", boom)
    fileops.write_text(src, fileops.read_text(src), "x = 2\n")

    assert src.read_text(encoding="utf-8") == "x = 2\n"
