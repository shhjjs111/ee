""".eeignore（D4-8）：项目里不让 ee 碰的文件（密钥、隐私资料、巨大的数据目录…），语法同 .gitignore。

- 位置：工作区目录及其上级（到 git 根为止）里的 `.eeignore`；规则相对它所在的目录。
- 生效：read / write / edit / apply_patch 碰到被忽略的路径一律拒绝；glob / grep 的结果里排除；
  文件面板标出。shell 命令无法可靠拦截（模型被要求不要用 shell 绕过）。
- 支持：# 注释、! 取反、结尾 / 只匹配目录（及其下所有内容）、开头或中间带 / 表示相对 .eeignore 所在目录、
  * ? [..] 与 **。按顺序匹配，后面的规则覆盖前面的（与 git 相同）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

FILENAME = ".eeignore"


def _translate(pat: str) -> str:
    """gitignore 通配 → 正则（不含锚点）。"""
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                # ** ：/**/ 匹配零或多级目录；开头 **/ ；结尾 /**
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass
class _Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool
    base: str          # .eeignore 所在目录（规范化、/ 分隔）


class EeIgnore:
    def __init__(self, rules: list[_Rule], files: list[Path]):
        self.rules = rules
        self.files = files

    @property
    def empty(self) -> bool:
        return not self.rules

    @classmethod
    def load(cls, cwd: Path) -> "EeIgnore":
        cwd = Path(cwd).resolve()
        found: list[Path] = []
        for d in [cwd, *cwd.parents]:
            f = d / FILENAME
            if f.is_file():
                found.append(f)
            if (d / ".git").exists():
                break
        rules: list[_Rule] = []
        for f in reversed(found):            # 上级的先，近的后（近的优先）
            base = _norm(f.parent)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw in text.splitlines():
                line = raw.rstrip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                if neg:
                    line = line[1:]
                if line.startswith("\\"):
                    line = line[1:]
                dir_only = line.endswith("/")
                line = line.rstrip("/")
                if not line:
                    continue
                anchored = "/" in line
                line = line.lstrip("/")
                body = _translate(line)
                prefix = "" if anchored else "(?:.*/)?"
                tail = "/.*$" if dir_only else "(?:/.*)?$"     # 目录规则：目录本身（带 /）及其下所有
                rx = re.compile("^" + prefix + body + tail, re.I if os.name == "nt" else 0)
                rules.append(_Rule(rx, neg, dir_only, base))
        return cls(rules, found)

    def ignored(self, path: Path | str, is_dir: bool | None = None) -> bool:
        if not self.rules:
            return False
        p = _norm(Path(path))
        hit = False
        for r in self.rules:
            if p == r.base or not p.startswith(r.base.rstrip("/") + "/"):
                continue
            rel = p[len(r.base.rstrip("/")) + 1:]
            d = is_dir if is_dir is not None else Path(path).is_dir()
            if r.regex.match(rel + "/" if d else rel):
                hit = not r.negate
        return hit


def _norm(p: Path) -> str:
    try:
        p = p.resolve()
    except OSError:
        pass
    s = os.path.normpath(str(p)).replace("\\", "/")
    return s.lower() if os.name == "nt" else s


_CACHE: dict[str, tuple[tuple, EeIgnore]] = {}


def for_dir(cwd: Path) -> EeIgnore:
    """按目录缓存；.eeignore 文件增删改后自动重新加载。"""
    cwd = Path(cwd)
    key = _norm(cwd)
    probe = []
    for d in [cwd.resolve(), *cwd.resolve().parents]:
        f = d / FILENAME
        try:
            st = f.stat()
            probe.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
        if (d / ".git").exists():
            break
    sig = tuple(probe)
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    ig = EeIgnore.load(cwd)
    _CACHE[key] = (sig, ig)
    return ig
