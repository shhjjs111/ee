"""自定义提示 / 斜杠命令（D3-B）。

来源（后者同名覆盖前者）：
  1. 内置：ee/core/builtin_prompts/*.md（/init 生成 AGENTS.md）
  2. 用户级：~/.ee/prompts/*.md
  3. 项目级：<项目根>/.ee/prompts/*.md（项目根 = 含 .git 的最近上级目录，没有就是 cwd）

文件名（去掉 .md）就是命令名：只允许 [A-Za-z0-9_-]，其余文件忽略。
可选 frontmatter（--- 之间的 key: value）：description、argument-hint。
展开：正文里的 $ARGUMENTS 换成命令后面的文字；正文里没有 $ARGUMENTS 而用户带了参数，就把参数追加在最后。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ee.core.instructions import project_root

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BYTES = 64 * 1024
# 界面自己处理的命令，不能被自定义提示覆盖
RESERVED = {"compact", "new", "undo", "model", "mode", "memory", "mcp", "help", "review", "commit"}


@dataclass
class Prompt:
    name: str
    description: str
    argument_hint: str
    body: str
    source: str        # builtin | user | project
    path: str

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "argument_hint": self.argument_hint,
                "source": self.source, "path": self.path}


def parse(text: str) -> tuple[dict, str]:
    """返回 (frontmatter, 正文)。frontmatter 只支持一行一个 key: value。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            meta: dict = {}
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            body = text[end + 4:]
            return meta, body[1:] if body.startswith("\n") else body
    return {}, text


def _first_line(body: str) -> str:
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:80]
    return ""


def _load_file(path: Path, source: str) -> Prompt | None:
    name = path.stem
    if not NAME_RE.match(name):
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse(text)
    return Prompt(name=name, description=meta.get("description") or _first_line(body),
                  argument_hint=meta.get("argument-hint") or meta.get("argument_hint") or "",
                  body=body, source=source, path=str(path))


def dirs(home: Path, cwd: Path | None) -> list[tuple[str, Path]]:
    out = [("user", home / "prompts")]
    if cwd:
        root = project_root(Path(cwd)) or Path(cwd)
        out.append(("project", root / ".ee" / "prompts"))
    return out


def load_prompts(home: Path, cwd: Path | None) -> dict[str, Prompt]:
    found: dict[str, Prompt] = {}
    pkg = resources.files("ee.core") / "builtin_prompts"
    for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            meta, body = parse(entry.read_text(encoding="utf-8"))
            name = entry.name[:-3]
            found[name] = Prompt(name, meta.get("description") or _first_line(body),
                                 meta.get("argument-hint") or "", body, "builtin", f"builtin:{entry.name}")
    for source, d in dirs(home, cwd):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            pr = _load_file(p, source)
            if pr and pr.name.lower() not in RESERVED:
                found[pr.name] = pr
    return dict(sorted(found.items()))


def expand(prompt: Prompt, args: str = "") -> str:
    args = (args or "").strip()
    body = prompt.body
    if "$ARGUMENTS" in body:
        out = body.replace("$ARGUMENTS", args)
    else:
        out = body.rstrip() + (("\n\n" + args) if args else "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()
