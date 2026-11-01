"""工具输出归档的清理（~/.ee/tool-output）。

这个目录只进不出：超长工具输出归档、后台命令日志都写在这里。清理只按文件名白名单动手，
别的文件一概不碰——目录在用户 home 下，误删的代价远大于留几个旧文件。
"""
import os
import time
from pathlib import Path

from ee.core import retention as R


def _mk(root: Path, name: str, age_days: float) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text("x" * 100, encoding="utf-8")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_settings_default_and_bad_values():
    assert R.settings({}) == R.DEFAULT_DAYS
    assert R.settings({"tool_output": {"keep_days": 3}}) == 3
    assert R.settings({"tool_output": {"keep_days": 0}}) == 0          # 0 = 关闭清理
    assert R.settings({"tool_output": {"keep_days": "x"}}) == R.DEFAULT_DAYS
    assert R.settings({"tool_output": {"keep_days": -5}}) == 0
    assert R.settings({"tool_output": "nope"}) == R.DEFAULT_DAYS


def test_gc_removes_only_old_known_files(tmp_path):
    root = tmp_path / "tool-output"
    old_archive = _mk(root, "20260101-000000-read-abcdef.txt", 30)
    old_log = _mk(root, "bg-ce9303e6-b1.log", 30)
    new_archive = _mk(root, "20990101-000000-shell-abcdef.txt", 0)
    user_note = _mk(root, "notes.txt", 30)                  # 不是我们写的：不动
    odd_archive = _mk(root, "20260101-000000-read.txt", 30)  # 名字不合归档格式：不动

    r = R.gc(tmp_path, keep_days=7, force=True)

    assert r["skipped"] is False and r["removed"] == 2 and r["kept"] == 1
    assert not old_archive.exists() and not old_log.exists()
    assert new_archive.exists() and user_note.exists() and odd_archive.exists()


def test_gc_keeps_files_inside_window(tmp_path):
    root = tmp_path / "tool-output"
    assert _mk(root, "20260101-000000-grep-abcdef.txt", 6).exists()
    r = R.gc(tmp_path, keep_days=7, force=True)
    assert r["removed"] == 0 and r["kept"] == 1


def test_gc_zero_days_disables(tmp_path):
    root = tmp_path / "tool-output"
    p = _mk(root, "20260101-000000-read-abcdef.txt", 30)
    r = R.gc(tmp_path, keep_days=0, force=True)
    assert r["skipped"] is True and p.exists()


def test_gc_runs_once_per_interval(tmp_path):
    root = tmp_path / "tool-output"
    _mk(root, "20260101-000000-read-abcdef.txt", 30)
    first = R.gc(tmp_path, keep_days=7)
    assert first["skipped"] is False and first["removed"] == 1
    _mk(root, "20260102-000000-read-abcdef.txt", 30)
    second = R.gc(tmp_path, keep_days=7)
    assert second["skipped"] is True
    assert R.gc(tmp_path, keep_days=7, force=True)["removed"] == 1


def test_gc_without_dir_is_noop(tmp_path):
    r = R.gc(tmp_path, keep_days=7, force=True)
    assert r["skipped"] is True and r["removed"] == 0


def test_default_config_has_the_setting():
    from ee.config import DEFAULTS
    assert R.DEFAULT_DAYS == 30                     # 写死：两处默认值不能各自漂走
    assert DEFAULTS["tool_output"]["keep_days"] == 30
