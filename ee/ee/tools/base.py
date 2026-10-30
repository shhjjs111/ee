"""工具基类与运行上下文。每个工具 = schema + 描述(prompts/tools/*.md) + run() + read_only 标记。"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable


class ToolError(Exception):
    """工具可预期的失败：原样回喂给模型让它自己修。"""


class ToolResult:
    """工具结果。output = 给模型的 content；details = 只给界面的结构化数据（见 tools/details.py），
    不进模型上下文、持久化到条目。meta 是 details 的旧名（同一个 dict）。"""

    __slots__ = ("output", "ok", "summary", "diff", "details")

    def __init__(self, output: str, ok: bool = True, summary: str = "", diff: str = "",
                 meta: dict | None = None, details: dict | None = None) -> None:
        self.output = output
        self.ok = ok
        self.summary = summary
        self.diff = diff
        self.details = dict(details if details is not None else (meta or {}))

    @property
    def meta(self) -> dict:
        return self.details

    @meta.setter
    def meta(self, v: dict) -> None:
        self.details = v

    def __repr__(self) -> str:
        return f"ToolResult(ok={self.ok}, summary={self.summary!r}, output={self.output[:60]!r})"


@dataclass
class FileRecord:
    mtime_ns: int
    size: int
    identity: str | None = None   # 内容身份（内容哈希）；None = 未计算，退回 mtime+size 判变化
    reads: dict = field(default_factory=dict)  # (offset,limit) -> 已返回 ref 次数


@dataclass
class ToolState:
    """一个会话内工具共享的状态：读过哪些文件、改前快照、todo 清单。"""
    files: dict[str, FileRecord] = field(default_factory=dict)
    snapshots: list[dict[str, bytes | None]] = field(default_factory=lambda: [{}])  # 每回合一层
    todos: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    def mark_read(self, path: Path, identity: str | None = None) -> FileRecord:
        """登记一次读取。identity 是内容身份（哈希），用于判"文件是否真的变了"。

        判据优先级：内容身份 > mtime+size。
        - 提供了 identity：身份一致即视为同一内容（touch / 原子替换不改身份，不丢记账）；
          身份变了才新建记录（内容变了、mtime/size 还原也骗不过）。
        - 未提供 identity（老调用点）：退回 mtime+size 判变化，保持原行为。
        """
        st = path.stat()
        key = str(path)
        rec = self.files.get(key)
        if rec is None:
            rec = FileRecord(st.st_mtime_ns, st.st_size, identity)
            self.files[key] = rec
            return rec
        if identity is not None:
            # 内容身份优先：身份一致 → 同一内容（即使 mtime/size 变了）；不一致 → 新建
            if rec.identity == identity:
                return rec
            rec = FileRecord(st.st_mtime_ns, st.st_size, identity)
            self.files[key] = rec
            return rec
        # 无 identity：退回 mtime+size
        if rec.mtime_ns == st.st_mtime_ns and rec.size == st.st_size:
            return rec
        rec = FileRecord(st.st_mtime_ns, st.st_size, None)
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
    max_timeout: int = 0                                 # >0 = 单次命令的硬上限（秒），0 = 只受工具自身的 MAX_TIMEOUT 约束
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


RULES_MARK = "## 规则"


def _tool_doc(name: str) -> tuple[str, list[str]]:
    """prompts/tools/<name>.md：「## 规则」之前是工具描述（进 tool schema），之后每个 `- ` 开头的行是一条规则
    （只在工具启用时拼进系统提示词的「工具使用规则」段，见 core/context.py）。"""
    f = resources.files("ee") / "prompts" / "tools" / f"{name}.md"
    text = f.read_text(encoding="utf-8")
    desc, _, rest = text.partition("\n" + RULES_MARK)
    rules = [ln[2:].strip() for ln in rest.splitlines() if ln.startswith("- ") and ln[2:].strip()]
    return desc.strip(), rules


def load_description(name: str) -> str:
    return _tool_doc(name)[0]


def load_rules(name: str) -> list[str]:
    return _tool_doc(name)[1]


class Tool:
    name: str = ""
    read_only: bool = False
    parameters: dict = {}

    def __init__(self) -> None:
        self.description, self.rules = _tool_doc(self.name)

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
