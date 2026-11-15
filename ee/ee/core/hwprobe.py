"""Local resource snapshot. Unknown readings remain unknown, never unlimited."""
from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import shutil


@dataclass(frozen=True)
class Hardware:
    cpu_count: int
    available_gb: float | None
    disk_free_gb: float | None
    # 工作区体积：给"每个候选需要多少内存/磁盘"的估算提供项目侧信号。
    # 有界扫描（时间/条目上限），完整与否单独记，避免把"没扫完"当成"很小"。
    project_gb: float | None = None
    project_complete: bool = False


def project_size(root, *, budget_s=1.5, max_entries: int = 200_000) -> tuple[float, bool]:
    """工作区体积的**有界**估算：不跑构建/测试，只看文件大小。

    巨型仓库里扫全量可能几十秒，所以有时间与条目上限；`complete=False` 表示
    只是"至少这么多"，上层据此决定是保守取值还是当作未知。
    """
    import time

    start = time.monotonic()
    total = 0
    seen = 0
    stack = [Path(root)]
    complete = True
    while stack:
        if time.monotonic() - start > budget_s:
            complete = False
            break
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                seen += 1
                if seen > max_entries:
                    complete = False
                    break
                if entry.name == ".git":
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        if not complete:
            break
    return total / 1024 ** 3, complete


def probe(cwd, worktree_root=None) -> Hardware:
    available = None
    try:
        if os.name == "nt":
            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page_total", "page_available", "virtual_total",
                     "virtual_available", "extended")]
            memory = Memory()
            memory.length = ctypes.sizeof(memory)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                available = memory.available / 1024 ** 3
        elif Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError, AttributeError):
        pass
    try:
        disk = shutil.disk_usage(cwd).free / 1024 ** 3
        if worktree_root is not None:
            target = Path(worktree_root)
            existing = next((p for p in (target, *target.parents) if p.is_dir()), None)
            disk = min(disk, shutil.disk_usage(existing).free / 1024 ** 3) if existing else None
    except OSError:
        disk = None
    try:
        project_gb, project_complete = project_size(cwd)
    except OSError:
        project_gb, project_complete = None, False
    return Hardware(os.cpu_count() or 1, available, disk, project_gb, project_complete)
