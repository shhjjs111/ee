"""工具基类与运行上下文。每个工具 = schema + 描述(prompts/tools/*.md) + run() + read_only 标记。"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable


class ToolError(Exception):
    """工具可预期的失败：原样回喂给模型让它自己修。"""


@dataclass
class ToolResult:
    output: str
    ok: bool = True
    summary: str = ""
    diff: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class FileRecord:
    mtime_ns: int
    size: int
    reads: dict = field(default_factory=dict)  # (offset,limit) -> 已返回 ref 次数


@dataclass
class ToolState:
    """一个会话内工具共享的状态：读过哪些文件、改前快照、todo 清单。"""
    files: dict[str, FileRecord] = field(default_factory=dict)
    snapshots: list[dict[str, bytes | None]] = field(default_factory=lambda: [{}])  # 每回合一层
    todos: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    def mark_read(self, path: Path) -> FileRecord:
        st = path.stat()
        key = str(path)
        rec = self.files.get(key)
        if rec is None or rec.mtime_ns != st.st_mtime_ns or rec.size != st.st_size:
            rec = FileRecord(st.st_mtime_ns, st.st_size)
            self.files[key] = rec
        return rec

    def was_read(self, path: Path) -> bool:
        return str(path) in self.files

    def snapshot(self, path: Path) -> None:
        layer = self.snapshots[-1]
        key = str(path)
        if key not in layer:
            layer[key] = path.read_bytes() if path.exists() else None
        if key not in self.changed_files:
            self.changed_files.append(key)

    def new_turn(self) -> None:
        if self.snapshots[-1]:
            self.snapshots.append({})


@dataclass
class ToolContext:
    cwd: Path
    state: ToolState
    output_dir: Path
    shell: Any = None                                    # ShellInfo，由 session 提供当前值
    emit: Callable[[str], None] = lambda s: None         # 流式输出（bash）
    timeout_default: int = 120
    extra: dict = field(default_factory=dict)

    def resolve(self, p: str) -> Path:
        if not p:
            raise ToolError("缺少路径参数")
        p = p.strip().strip('"').strip("'")
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        return Path(_normpath(path))


def _normpath(p: Path) -> str:
    import os
    return os.path.normpath(str(p))


def load_description(name: str) -> str:
    f = resources.files("ee") / "prompts" / "tools" / f"{name}.md"
    return f.read_text(encoding="utf-8").strip()


class Tool:
    name: str = ""
    read_only: bool = False
    parameters: dict = {}

    def __init__(self) -> None:
        self.description = load_description(self.name)

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def summary(self, args: dict) -> str:
        return self.name

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


def rel(path: Path | str, cwd: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except Exception:
        return str(path)
