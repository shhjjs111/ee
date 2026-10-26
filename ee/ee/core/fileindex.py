"""工作区文件索引（@文件 提及用）：有 rg 用 rg --files，否则 os.walk；模糊匹配。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ee.tools.search import SKIP_DIRS


class FileIndex:
    def __init__(self, root: Path, limit: int = 20000):
        self.root, self.limit = root, limit
        self.files: list[str] = []
        self.ready = False

    def build(self) -> None:
        out: list[str] = []
        rg = shutil.which("rg")
        try:
            if rg:
                kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
                p = subprocess.run([rg, "--files", "--hidden", "-g", "!.git"], cwd=str(self.root), **kw,
                                   capture_output=True, timeout=10)
                out = p.stdout.decode("utf-8", "replace").splitlines()[: self.limit]
            else:
                for dp, dns, fns in os.walk(self.root):
                    dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
                    for f in fns:
                        out.append(os.path.relpath(os.path.join(dp, f), self.root))
                        if len(out) >= self.limit:
                            break
                    if len(out) >= self.limit:
                        break
        except Exception:
            pass
        self.files = [f.replace("\\", "/") for f in out]
        self.ready = True

    def search(self, q: str, n: int = 20) -> list[str]:
        if not q:
            return sorted(self.files, key=len)[:n]
        ql = q.lower().replace("\\", "/")
        scored = []
        for f in self.files:
            fl = f.lower()
            i = fl.find(ql)
            if i >= 0:
                base = fl.rsplit("/", 1)[-1]
                score = 0 if base.startswith(ql) else (1 if ql in base else 2)
                scored.append((score, len(f), f))
                continue
            # 子序列匹配
            pos = 0
            for ch in ql:
                pos = fl.find(ch, pos)
                if pos < 0:
                    break
                pos += 1
            else:
                scored.append((3, len(f), f))
        scored.sort()
        return [f for _, _, f in scored[:n]]

