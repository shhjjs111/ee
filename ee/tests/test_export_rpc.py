"""会话导出的 RPC 与落盘边界（T10-a / U2）。

守的是「导出写到不该写的地方」和「看起来写成功了其实是空的」：
不传 path 不该落盘、已存在不该静默覆盖、导出不该碰会话文件本身。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ee.server.app import Backend


@pytest.fixture
def backend(tmp_path):
    """一个离线 Backend（persist=False：不写用户配置）。"""
    return Backend(home=tmp_path, persist=False)


def _seed(home: Path, tid: str, text: str = "你好") -> str:
    """造一个最小真实会话文件，返回 thread_id。"""
    from ee.core.store import SessionStore

    s = SessionStore(home, tid)
    s.message({"role": "user", "content": text})
    s.message({"role": "assistant", "content": "收到"})
    s.item({"type": "user", "text": text, "id": "us_1", "seq": 1})
    s.item({"type": "assistant", "text": "收到", "id": "as_1", "seq": 2})
    s.meta({"title": "导出用例", "created": 1793205821.0, "cwd": str(home)})
    return tid


def _call(backend, method: str, **params):
    """直接打 RPC 分发表，绕过 websocket。"""
    import asyncio
    return asyncio.run(backend.methods[method](**params))


# ---------------------------------------------------------------- 内容不落盘

def test_export_returns_content_without_path(backend, tmp_path):
    """不传 path：只返回内容，磁盘上不能多出文件。"""
    tid = _seed(backend.home, "20261029-010101-aaaaaa")
    before = {p.name for p in tmp_path.iterdir()}

    res = _call(backend, "thread/export", thread_id=tid, format="markdown")

    assert res["format"] == "markdown"
    assert "你好" in res["content"]
    assert res.get("path") in (None, "")
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before, f"不传 path 却落盘了：{after - before}"


def test_export_writes_file_when_path_given(backend, tmp_path):
    """给了 path 才写，且文件内容与返回值一致。"""
    tid = _seed(backend.home, "20261029-010102-bbbbbb")
    target = tmp_path / "out" / "会话.md"
    target.parent.mkdir()

    res = _call(backend, "thread/export", thread_id=tid, format="markdown",
                path=str(target))

    assert target.is_file()
    assert target.read_text(encoding="utf-8") == res["content"]
    assert str(target) == res["path"]


def test_export_json_format_roundtrips(backend, tmp_path):
    """format=json 返回的内容能直接 json.loads。"""
    tid = _seed(backend.home, "20261029-010103-cccccc")
    res = _call(backend, "thread/export", thread_id=tid, format="json")
    doc = json.loads(res["content"])
    assert doc["meta"]["title"] == "导出用例"
    assert any(it["type"] == "user" for it in doc["items"])


# ---------------------------------------------------------------- 覆盖与参数

def test_export_refuses_to_overwrite_silently(backend, tmp_path):
    """目标已存在时必须报错，且原文件字节不变。"""
    tid = _seed(backend.home, "20261029-010104-dddddd")
    target = tmp_path / "exists.md"
    target.write_text("原有内容", encoding="utf-8")
    original = target.read_bytes()

    with pytest.raises(Exception) as ei:
        _call(backend, "thread/export", thread_id=tid, format="markdown",
              path=str(target))
    msg = str(ei.value)
    assert "已存在" in msg or "exists" in msg.lower()
    assert target.read_bytes() == original, "被静默覆盖了"


def test_export_overwrite_allowed_when_explicit(backend, tmp_path):
    """用户显式确认后可以覆盖。"""
    tid = _seed(backend.home, "20261029-010105-eeeeee")
    target = tmp_path / "exists2.md"
    target.write_text("旧", encoding="utf-8")

    res = _call(backend, "thread/export", thread_id=tid, format="markdown",
                path=str(target), overwrite=True)
    assert "你好" in target.read_text(encoding="utf-8")


def test_export_rejects_unknown_format(backend):
    """未知格式明确报错，不给默认值。"""
    tid = _seed(backend.home, "20261029-010106-ffffff")
    with pytest.raises(Exception) as ei:
        _call(backend, "thread/export", thread_id=tid, format="xml")
    assert "格式" in str(ei.value) or "format" in str(ei.value).lower()


def test_export_rejects_relative_path(backend, tmp_path):
    """相对路径拒绝：落盘位置必须由用户明确指定。"""
    tid = _seed(backend.home, "20261029-010107-gggggg")
    with pytest.raises(Exception) as ei:
        _call(backend, "thread/export", thread_id=tid, format="markdown",
              path="relative/out.md")
    assert "绝对" in str(ei.value) or "absolute" in str(ei.value).lower()


def test_export_rejects_directory_path(backend, tmp_path):
    """给一个目录当 path 要报错，而不是写到别处。"""
    tid = _seed(backend.home, "20261029-010108-hhhhhh")
    with pytest.raises(Exception):
        _call(backend, "thread/export", thread_id=tid, format="markdown",
              path=str(tmp_path))


def test_export_unknown_thread_is_404(backend):
    """不存在的会话要报错，不能导出成空文件骗人。"""
    with pytest.raises(Exception) as ei:
        _call(backend, "thread/export", thread_id="20261029-010109-zzzzzz",
              format="markdown")
    assert "不存在" in str(ei.value) or "404" in str(ei.value)


# ---------------------------------------------------------------- 只读

def test_export_does_not_modify_session_file(backend, tmp_path):
    """导出是只读操作：会话文件字节与 mtime 都不能变。"""
    tid = _seed(backend.home, "20261029-010110-iiiiii")
    f = backend.home / "sessions" / f"{tid}.jsonl"
    before_bytes = f.read_bytes()
    before_mtime = f.stat().st_mtime_ns

    _call(backend, "thread/export", thread_id=tid, format="markdown")
    _call(backend, "thread/export", thread_id=tid, format="json")

    assert f.read_bytes() == before_bytes
    assert f.stat().st_mtime_ns == before_mtime
