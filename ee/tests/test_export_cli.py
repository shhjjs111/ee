"""`ee export` 子命令（T10-a / U2b）：把导出能力接到可用入口。

**为什么需要它**：`thread/export` 是给桌面端用的 RPC，但桌面端入口还没做
（那部分要 Electron 保存对话框，本轮不做）。没有这个子命令，整个导出能力
就只有一个没有生产调用方的后端函数——和 LSP 那次「能力实现、链路没通」
是同一类问题。CLI 不依赖 Electron，能让能力立刻可用。

守的边界：不指定输出时不写盘、默认不覆盖、路径与格式错要明确报错。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ee.cli import build_parser, main


def _make_session(home: Path, tid: str = "20261029-020202-abcdab") -> str:
    from ee.core.store import SessionStore

    s = SessionStore(home, tid)
    s.item({"type": "user", "text": "导出我", "id": "us_1", "seq": 1})
    s.item({"type": "assistant", "text": "好的", "id": "as_1", "seq": 2})
    s.meta({"title": "CLI 导出", "created": 1793205821.0, "cwd": str(home)})
    return tid


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把 ee_home 指到临时目录，避免碰真实会话。"""
    monkeypatch.setenv("EE_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_export_subcommand_is_registered():
    """子命令要出现在帮助里，不能只是隐藏功能。"""
    p = build_parser()
    assert "export" in p._subparsers._group_actions[0].choices      # type: ignore[union-attr]


def test_export_prints_to_stdout_without_output(home, capsys):
    """不给 -o 时只打印，不写盘。

    只断言「没有新文件」而不枚举全部目录内容：`ee` 启动时别的模块会建
    `user-home` 之类的目录，那是既有行为，与导出无关。
    """
    tid = _make_session(home)
    before = {p.name for p in home.iterdir()}
    code = main(["export", tid])
    out = capsys.readouterr().out
    assert code == 0
    assert "导出我" in out
    after = {p.name for p in home.iterdir()}
    assert after == before, f"不传 -o 却多出了：{after - before}"


def test_export_writes_file_with_output_flag(home, tmp_path):
    """给 -o 才落盘。"""
    tid = _make_session(home)
    target = tmp_path / "out" / "s.md"
    code = main(["export", tid, "-o", str(target)])
    assert code == 0
    assert target.is_file()
    assert "导出我" in target.read_text(encoding="utf-8")


def test_export_format_json(home, capsys):
    """--format json 输出可解析的 JSON。"""
    tid = _make_session(home)
    code = main(["export", tid, "--format", "json"])
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert doc["meta"]["title"] == "CLI 导出"


def test_export_refuses_overwrite_without_force(home, tmp_path):
    """已存在时不加 --force 必须拒绝，且原文件不变。"""
    tid = _make_session(home)
    target = tmp_path / "exists.md"
    target.write_text("原内容", encoding="utf-8")
    code = main(["export", tid, "-o", str(target)])
    assert code != 0
    assert target.read_text(encoding="utf-8") == "原内容"


def test_export_force_overwrites(home, tmp_path):
    """--force 才覆盖。"""
    tid = _make_session(home)
    target = tmp_path / "exists2.md"
    target.write_text("旧", encoding="utf-8")
    code = main(["export", tid, "-o", str(target), "--force"])
    assert code == 0
    assert "导出我" in target.read_text(encoding="utf-8")


def test_export_unknown_thread_fails(home, capsys):
    """会话不存在要非零退出并说明，不能输出空内容骗人。"""
    code = main(["export", "20261029-999999-ffffff"])
    assert code != 0
    err = capsys.readouterr().err
    assert "不存在" in err or "not found" in err.lower()


def test_export_unknown_format_fails(home, capsys):
    """格式错要非零退出。

    `--format` 用 argparse 的 choices 约束，非法值由 argparse 自己拒绝并以
    SystemExit(2) 结束——和 `ee serve --port abc` 一致。这里验证退出码，
    不要求 main() 捕获 argparse 的异常（那是全项目统一行为）。
    """
    tid = _make_session(home)
    with pytest.raises(SystemExit) as ei:
        main(["export", tid, "--format", "xml"])
    assert ei.value.code == 2
