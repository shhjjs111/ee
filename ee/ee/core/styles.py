"""回答风格（D5-9）：默认 / 简洁，以及用户自己的 ~/.ee/styles/<名>.md。

每个风格是一段提示词（ee/prompts/styles/<名>.md，首行 `# 显示名`，其余是正文）。注入方式兼顾前缀缓存：
- 会话首次请求时把当前风格段附在 system 提示末尾，随 system 一起冻结 —— 会话内每次请求字节不变；
- 会话中途切换风格不动 system（否则整段缓存失效），而是作为 <context-update> 的一个子块挂在下一条
  用户消息尾部（只追加），说明“回答风格已切换为 X，以此为准”。之后不再变化就不再发送。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

BUILTIN_ORDER = ("default", "concise")
_NAME_RE = re.compile(r"^[\w\-]{1,40}$")
MAX_CHARS = 4000


@dataclass
class Style:
    id: str
    label: str
    body: str
    source: str = "builtin"      # builtin | user
    path: str = ""

    def summary(self) -> dict:
        first = next((ln.strip("-* ").strip() for ln in self.body.splitlines() if ln.strip()), "")
        return {"id": self.id, "label": self.label, "description": first[:80], "source": self.source,
                "path": self.path}


def _parse(text: str, fallback: str) -> tuple[str, str]:
    text = (text or "").replace("\r\n", "\n").strip()
    lines = text.split("\n")
    if lines and lines[0].startswith("#"):
        return lines[0].lstrip("#").strip() or fallback, "\n".join(lines[1:]).strip()
    return fallback, text


def _builtin(sid: str) -> Style | None:
    if sid == "default":
        return Style("default", "默认", "")
    try:
        f = resources.files("ee") / "prompts" / "styles" / f"{sid}.md"
        label, body = _parse(f.read_text(encoding="utf-8"), sid)
    except (FileNotFoundError, OSError):
        return None
    return Style(sid, label, body)


def user_dir(home: Path) -> Path:
    return Path(home) / "styles"


def available(home: Path) -> list[Style]:
    out = [s for s in (_builtin(x) for x in BUILTIN_ORDER) if s is not None]
    d = user_dir(home)
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            sid = p.stem
            if sid in BUILTIN_ORDER or not _NAME_RE.match(sid):
                continue
            try:
                label, body = _parse(p.read_text(encoding="utf-8")[:MAX_CHARS], sid)
            except (OSError, UnicodeDecodeError):
                continue
            out.append(Style(sid, label, body, "user", str(p)))
    return out


def get(home: Path, sid: str | None) -> Style:
    """找不到的风格按默认处理。"""
    sid = (sid or "default").strip() or "default"
    if sid in BUILTIN_ORDER:
        return _builtin(sid) or Style("default", "默认", "")
    for s in available(home):
        if s.id == sid:
            return s
    return Style("default", "默认", "")


def exists(home: Path, sid: str) -> bool:
    return any(s.id == sid for s in available(home))


def system_segment(style: Style) -> str:
    """冻结进 system 末尾的风格段；默认风格为空串（system 与以前完全一样）。"""
    if not style.body:
        return ""
    return f"# 回答风格：{style.label}\n{style.body}"


def switch_part(style: Style) -> str:
    """会话中途切换风格时挂在用户消息尾部的子块（<context-update> 里）。"""
    if not style.body:
        return ("<response-style>\n回答风格已切换为：默认。之前要求的回答风格不再适用，按常规方式回答。\n</response-style>")
    return (f"<response-style>\n回答风格已切换为：{style.label}（以此为准，之前的回答风格要求不再适用）。\n"
            f"{style.body}\n</response-style>")
