"""子代理定义加载（M3 用；M1 只提供加载接口）。

定义文件：~/.ee/agents/*.md 或项目 .ee/agents/*.md，frontmatter：name / description / tools / model / max_steps，
正文是该子代理的系统提示词。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentDef:
    name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_steps: int = 30
    prompt: str = ""
    source: str = ""


def parse_agent_md(text: str, source: str = "") -> AgentDef | None:
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
            meta[k.strip()] = v.strip()
    if not meta.get("name"):
        return None
    tools = [t.strip() for t in meta.get("tools", "").strip("[]").split(",") if t.strip()]
    return AgentDef(name=meta["name"], description=meta.get("description", ""), tools=tools,
                    model=meta.get("model") or None, max_steps=int(meta.get("max_steps", 30) or 30),
                    prompt=body.strip(), source=source)


def load_agents(dirs: list[Path]) -> dict[str, AgentDef]:
    out: dict[str, AgentDef] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            a = parse_agent_md(f.read_text(encoding="utf-8"), str(f))
            if a:
                out[a.name] = a
    return out
