"""PowerShell 临时脚本（ee-<pid>-*.ps1）的清理：目录隔离 + 按 pid 清扫残留。

背景：脚本的删除在各调用点的 finally 里，进程被强杀 / 崩溃时轮不到 finally，旧实现直接写在
%TEMP% 根目录，实测一天攒几个。这里把落点收进子目录并按 pid 清扫，且必须不误删还在跑的命令。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ee.tools import shellenv as SE


def _make(dir_: Path, name: str, age: float = 0.0) -> Path:
    p = dir_ / name
    p.write_text("echo hi", encoding="utf-8-sig")
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_prepare_puts_script_in_own_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(SE.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(SE, "_swept", True)        # 不让首次清扫动这个临时目录
    sh = SE.ShellInfo("pwsh", "pwsh.exe", "PowerShell 7 (pwsh)")
    argv, script = sh.prepare("echo hello")
    try:
        assert script.parent == tmp_path / SE.SCRIPT_DIR_NAME
        assert SE._OWN_SCRIPT.match(script.name)
        assert script.name.startswith(f"ee-{os.getpid()}-")
        assert argv[-2] == "-File" and argv[-1] == str(script)
        assert "echo hello" in script.read_text(encoding="utf-8-sig")
    finally:
        script.unlink()


def test_sweep_removes_dead_pid_scripts(tmp_path):
    dead = _dead_pid()
    gone = _make(tmp_path, f"ee-{dead}-deadbeef.ps1")
    alive = _make(tmp_path, f"ee-{os.getpid()}-cafebabe.ps1")
    assert SE.sweep_leftovers(tmp_path) == 1
    assert not gone.exists()
    assert alive.exists(), "自己进程的脚本可能正被后台命令使用，不能删"


def test_sweep_ignores_unrelated_files(tmp_path):
    keep = _make(tmp_path, "someone-elses.ps1")
    keep2 = _make(tmp_path, "ee-abc.ps1")
    assert SE.sweep_leftovers(tmp_path) == 0
    assert keep.exists() and keep2.exists()


def test_sweep_legacy_name_by_age(tmp_path):
    """旧版名字没有 pid，只能按年龄兜底：太新的可能正在跑，留着。"""
    old = _make(tmp_path, "ee-1234abcd.ps1", age=SE.LEGACY_MAX_AGE + 60)
    new = _make(tmp_path, "ee-5678efgh.ps1", age=60)
    assert SE.sweep_leftovers(tmp_path) == 1
    assert not old.exists() and new.exists()


def test_sweep_legacy_dir(tmp_path):
    """旧版直接写在临时目录根：传入 legacy_dir 时一并清掉。"""
    root, sub = tmp_path, tmp_path / SE.SCRIPT_DIR_NAME
    sub.mkdir()
    old = _make(root, "ee-9abc9abc.ps1", age=SE.LEGACY_MAX_AGE + 60)
    assert SE.sweep_leftovers(sub, legacy_dir=root) == 1
    assert not old.exists()


def test_sweep_runs_once_per_process(monkeypatch, tmp_path):
    monkeypatch.setattr(SE.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(SE, "_swept", False)
    calls = []
    monkeypatch.setattr(SE, "sweep_leftovers", lambda *a, **k: calls.append(1) or 0)
    SE._script_dir()
    SE._script_dir()
    assert len(calls) == 1, "每次进程只扫一遍，不能每条命令都遍历目录"


def test_sweep_falls_back_when_dir_unwritable(monkeypatch, tmp_path):
    """建不出子目录时退回临时目录根：功能优先，清扫可以不做。"""
    monkeypatch.setattr(SE.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(SE, "_swept", True)

    def boom(*a, **k):
        raise OSError("no permission")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert SE._script_dir() == tmp_path
