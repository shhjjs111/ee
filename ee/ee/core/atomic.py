"""原子写文件：同目录临时文件 → 写完 fsync → os.replace 覆盖目标。

写到一半崩溃/断电时目标文件要么是旧内容、要么是新内容，不会出现半截文件。
Windows 上目标文件正被别的进程（杀毒、索引、另一个 ee 进程）短暂打开时 os.replace 会报
PermissionError，稍等重试；最终仍失败就删掉临时文件并把异常抛给调用方，目标文件保持原样。
"""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

REPLACE_RETRIES = 20

_TMP_RE = re.compile(r"^\.(.+)\.(\d+)\.([0-9a-f]{8})\.tmp$")
STALE_TMP_SECONDS = 60.0      # 刚建的先不动：可能正有一个写者在写它
_SWEPT: set[str] = set()


def _pid_alive(pid: int) -> bool:
    try:
        from ee.server.lifeline import pid_alive      # 延迟导入：core 不该在导入期依赖 server
        return pid_alive(pid)
    except Exception:  # noqa: BLE001
        return True          # 判断不了就当它活着：宁可漏删，不可错删


def sweep_leftovers(directory: Path | str, now: float | None = None) -> list[Path]:
    """删掉这个目录里「写者已死」的原子写残留，返回真正删掉的文件。

    临时文件落在**目标文件所在目录**，也就是用户的仓库里；进程被 taskkill /F、
    `os._exit`、断电杀掉时解释器不执行 finally，它就永久留下了。而这些残渣会被
    glob/grep（`--hidden`）、@文件 索引、文件面板、磁盘快照的 `add -A` 当成工作区内容。

    只认自己留下的名字，而且要**同时**满足「pid 不再存活」与「早于窗口期」才动手：
    删掉另一个进程正在写的临时文件，等于毁掉它这次写入。
    """
    d = Path(directory)
    now = time.time() if now is None else now
    removed: list[Path] = []
    try:
        it = os.scandir(d)
    except OSError:
        return removed
    with it:
        for e in it:
            m = _TMP_RE.match(e.name)
            if not m:
                continue
            try:
                pid = int(m.group(2))
                age = now - e.stat().st_mtime
            except (OSError, ValueError):
                continue
            if age < STALE_TMP_SECONDS or _pid_alive(pid):
                continue
            try:
                os.unlink(e.path)
                removed.append(Path(e.path))
            except OSError:
                pass
    return removed


def _sweep_once(d: Path) -> None:
    """每个目录本进程只扫一次：原子写是热路径，不要每次写都 scandir 一个大目录。"""
    key = os.path.normcase(str(d))
    if key in _SWEPT:
        return
    _SWEPT.add(key)
    try:
        sweep_leftovers(d)
    except Exception:  # noqa: BLE001
        pass


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
    _sweep_once(path.parent)          # 顺手清掉上次被强杀留下的同类残渣（每个目录只扫一次）
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
