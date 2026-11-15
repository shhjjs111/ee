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
    return Hardware(os.cpu_count() or 1, available, disk)
