"""子代理定义加载（M3 用；M1 只提供加载接口）。

定义文件：~/.ee/agents/*.md 或项目 .ee/agents/*.md，frontmatter：name / description / tools / model / max_steps，
正文是该子代理的系统提示词。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ee.agents")


@dataclass
class AgentDef:
    name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_steps: int = 30
    prompt: str = ""
    source: str = ""


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def parse_agent_md(text: str, source: str = "") -> AgentDef | None:
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---"):
        return None
    try:
        _, fm, body = text.split("---", 2)
    except ValueError:
        return None
    meta: dict = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = _unquote(v)
    if not meta.get("name"):
        return None
    tools = [_unquote(t) for t in meta.get("tools", "").strip().strip("[]").split(",") if _unquote(t)]
    try:
        max_steps = int(meta.get("max_steps", 30) or 30)
    except (TypeError, ValueError):
        log.warning("子代理定义 %s：max_steps 不是整数（%s），按 30", source, meta.get("max_steps"))
        max_steps = 30
    return AgentDef(name=meta["name"], description=meta.get("description", ""), tools=tools,
                    model=meta.get("model") or None, max_steps=max_steps, prompt=body.strip(), source=source)


def _read_md(f: Path) -> str:
    raw = f.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk")          # Windows 记事本的 ANSI（中文系统即 GBK）


def load_agents(dirs: list[Path]) -> dict[str, AgentDef]:
    """一个文件坏了（编码认不出、读不了）只跳过它并记日志，不影响其它定义。"""
    out: dict[str, AgentDef] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                a = parse_agent_md(_read_md(f), str(f))
            except (OSError, UnicodeDecodeError) as e:
                log.warning("跳过子代理定义 %s：%s", f, e)
                continue
            if a:
                out[a.name] = a
    return out
