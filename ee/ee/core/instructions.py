"""AGENTS.md 自动加载。

来源（按顺序拼接，越靠后越具体、优先级越高）：
1. 用户级：~/.ee/AGENTS.md（没有则 ~/.ee/CLAUDE.md）
2. 项目级：从项目根（向上找到含 .git 的目录；找不到就只看 cwd）到 cwd 的每一层目录，
   每层取 AGENTS.md，没有则 CLAUDE.md。

结果在会话开始时作为一条固定的 user 消息放在 system 之后（不进冻结的 system 提示词，保住前缀缓存），
之后不再改动。总大小有上限（默认 32KiB），超出部分截断并附说明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

NAMES = ("AGENTS.md", "CLAUDE.md")
DEFAULT_MAX_BYTES = 32 * 1024
TRUNC_NOTE = "\n\n[已截断：AGENTS.md 指令总长超过 {limit} 字节上限，后面的内容没有加载。]"


@dataclass
class Instructions:
    text: str = ""
    sources: list[Path] = field(default_factory=list)
    truncated: bool = False

    def message(self) -> dict | None:
        if not self.text:
            return None
        return {"role": "user", "content": self.text, "_kind": "agents"}


def _pick(d: Path) -> Path | None:
    for n in NAMES:
        p = d / n
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def project_root(cwd: Path) -> Path | None:
    cur = cwd.resolve()
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d
    return None


def find_files(cwd: Path, home: Path) -> list[Path]:
    cwd = cwd.resolve()
    out: list[Path] = []
    user = _pick(home)
    if user is not None:
        out.append(user)
    root = project_root(cwd)
    if root is None:
        dirs = [cwd]
    else:
        rel = cwd.relative_to(root)
        dirs = [root]
        cur = root
        for part in rel.parts:
            cur = cur / part
            dirs.append(cur)
    seen = {p.resolve() for p in out}
    for d in dirs:
        p = _pick(d)
        if p is not None and p.resolve() not in seen:
            out.append(p)
            seen.add(p.resolve())
    return out


def _clip_bytes(s: str, n: int) -> str:
    return s.encode("utf-8")[:max(0, n)].decode("utf-8", errors="ignore")


def load_instructions(cwd: Path, home: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> Instructions:
    files = find_files(cwd, home)
    blocks: list[str] = []
    used: list[Path] = []
    for p in files:
        try:
            body = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not body:
            continue
        label = "用户级（个人偏好）" if p.parent.resolve() == Path(home).resolve() else str(p.parent)
        blocks.append(f"## {p.name} — {label}\n{body}")
        used.append(p)
    if not blocks:
        return Instructions()
    body = "\n\n--- project-doc ---\n\n".join(blocks)
    truncated = False
    if max_bytes > 0 and len(body.encode("utf-8")) > max_bytes:
        body = _clip_bytes(body, max_bytes) + TRUNC_NOTE.format(limit=max_bytes)
        truncated = True
    head = ("# AGENTS.md 指令\n以下是用户和项目提供的工作指令（越靠后的越具体，冲突时以靠后的为准；"
            "用户在对话里的直接要求优先于这些指令）。\n<INSTRUCTIONS>\n")
    return Instructions(head + body + "\n</INSTRUCTIONS>", used, truncated)
