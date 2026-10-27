"""长期记忆：用户级 ~/.ee/memory.md + 可选工作区级 <项目根>/.ee/memory.md。

文件格式就是普通 markdown：每条记忆一行 `- 内容`（缩进的续行归上一条），其他行（标题、空行）原样保留。
用户可以直接用编辑器改，也可以让模型用 memory 工具增删，或在桌面端设置页编辑。

注入（前缀缓存友好）：会话开始（历史为空）时，把两份记忆渲染成一条固定的 user 消息，放在 AGENTS.md 之后、
第一条用户消息之前；之后整个会话不再改它。本会话里 memory 工具增删的条目只写文件，
下次新会话才进入注入内容（模型本会话内已经从工具结果知道了）。总大小有上限（默认 8KiB）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ee.core.atomic import write_text as atomic_write
from ee.core.instructions import project_root

FILE_NAME = "memory.md"
DEFAULT_MAX_BYTES = 8 * 1024
MAX_ENTRY_CHARS = 1000
HEADER = "# ee 记忆\n\n<!-- 每条一行，以 \"- \" 开头。ee 会在每次新会话开始时读入。 -->\n"
TRUNC_NOTE = "\n[已截断：记忆总长超过 {limit} 字节上限，后面的条目没有加载。请让用户清理记忆。]"
SCOPES = ("user", "workspace")


class MemoryError_(ValueError):
    """记忆操作的可预期错误（中文说明）。"""


def user_path(home: Path) -> Path:
    return Path(home) / FILE_NAME


def workspace_path(cwd: Path) -> Path:
    root = project_root(Path(cwd)) or Path(cwd).resolve()
    return root / ".ee" / FILE_NAME


def path_for(scope: str, home: Path, cwd: Path | None) -> Path:
    if scope == "user":
        return user_path(home)
    if scope == "workspace":
        if cwd is None:
            raise MemoryError_("没有工作区，不能使用工作区记忆")
        return workspace_path(cwd)
    raise MemoryError_(f"未知记忆范围：{scope}（只能是 user 或 workspace）")


_ITEM = re.compile(r"^\s{0,3}[-*]\s+(.*)$")


def parse_entries(text: str) -> list[str]:
    out: list[str] = []
    in_comment = False
    for line in (text or "").splitlines():
        s = line.strip()
        if in_comment:
            if "-->" in s:
                in_comment = False
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        m = _ITEM.match(line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
        elif out and line.startswith(("  ", "\t")) and s:
            out[-1] += " " + s
    return out


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        return ""


def _clean(text: str) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        raise MemoryError_("记忆内容不能为空")
    if len(t) > MAX_ENTRY_CHARS:
        raise MemoryError_(f"单条记忆太长（{len(t)} 字），请精简到 {MAX_ENTRY_CHARS} 字以内")
    return t


def add_entry(path: Path, text: str) -> tuple[int, list[str]]:
    """追加一条，返回 (序号, 全部条目)。重复的内容不再追加。"""
    t = _clean(text)
    cur = read_text(path)
    entries = parse_entries(cur)
    if t in entries:
        return entries.index(t) + 1, entries
    body = cur if cur.strip() else HEADER
    if not body.endswith("\n"):
        body += "\n"
    body += f"- {t}\n"
    atomic_write(path, body)
    entries.append(t)
    return len(entries), entries


def remove_entry(path: Path, index: int | None = None, match: str | None = None) -> tuple[str, list[str]]:
    """按序号（从 1 开始）或按内容子串删除一条，返回 (被删内容, 剩余条目)。保留文件里其他非条目行。"""
    cur = read_text(path)
    entries = parse_entries(cur)
    if not entries:
        raise MemoryError_("这份记忆是空的，没有可删除的条目")
    if index is not None:
        if not 1 <= int(index) <= len(entries):
            raise MemoryError_(f"序号 {index} 超出范围（共 {len(entries)} 条）")
        k = int(index) - 1
    elif match:
        hits = [i for i, e in enumerate(entries) if match.strip() and match.strip() in e]
        if not hits:
            raise MemoryError_(f"没有找到包含「{match}」的记忆")
        if len(hits) > 1:
            raise MemoryError_(f"有 {len(hits)} 条记忆包含「{match}」，请改用序号：" +
                               "，".join(str(i + 1) for i in hits))
        k = hits[0]
    else:
        raise MemoryError_("删除需要 index（序号）或 match（内容片段）")
    # 重写文件：按顺序找到第 k 个条目行，连同它的续行一起删掉
    lines = cur.splitlines()
    out: list[str] = []
    seen = -1
    skipping = False
    in_comment = False
    for line in lines:
        s = line.strip()
        if in_comment or s.startswith("<!--"):
            in_comment = (in_comment or "-->" not in s) and "-->" not in s
            out.append(line)
            skipping = False
            continue
        m = _ITEM.match(line)
        if m and m.group(1).strip():
            seen += 1
            skipping = seen == k
            if skipping:
                continue
        elif skipping and line.startswith(("  ", "\t")) and s:
            continue
        else:
            skipping = False
        out.append(line)
    atomic_write(path, "\n".join(out) + "\n")
    removed = entries.pop(k)
    return removed, entries


def save_text(path: Path, text: str) -> list[str]:
    """整份覆盖（设置页编辑用）。"""
    t = text if text.endswith("\n") or not text else text + "\n"
    atomic_write(path, t)
    return parse_entries(t)


def describe(home: Path, cwd: Path | None, workspace_enabled: bool = True) -> dict:
    """给设置页 / RPC 用：两份记忆的路径、原文、条目。"""
    up = user_path(home)
    out = {"user": {"path": str(up), "exists": up.is_file(), "text": read_text(up),
                    "entries": parse_entries(read_text(up))}, "workspace": None}
    if cwd is not None and workspace_enabled:
        wp = workspace_path(cwd)
        out["workspace"] = {"path": str(wp), "exists": wp.is_file(), "text": read_text(wp),
                            "entries": parse_entries(read_text(wp))}
    return out


def _clip_bytes(s: str, n: int) -> str:
    return s.encode("utf-8")[:max(0, n)].decode("utf-8", errors="ignore")


@dataclass
class MemoryBlock:
    text: str = ""
    sources: list[str] = None  # type: ignore[assignment]
    truncated: bool = False

    def message(self) -> dict | None:
        if not self.text:
            return None
        # _kind=agents：压缩时和 AGENTS.md 一样作为开头锚点保留，位置和字节都不变
        return {"role": "user", "content": self.text, "_kind": "agents", "_sub": "memory"}


def load_memory(home: Path, cwd: Path | None, cfg: dict | None = None, tool: bool = True) -> MemoryBlock:
    """tool：本会话有没有 memory 工具（决定说明里提不提它）。"""
    cfg = cfg or {}
    if not cfg.get("enabled", True) or not cfg.get("inject", True):
        return MemoryBlock("", [])
    max_bytes = int(cfg.get("max_bytes", DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES)
    parts: list[str] = []
    sources: list[str] = []
    up = user_path(home)
    ue = parse_entries(read_text(up))
    if ue:
        parts.append("## 用户级\n" + "\n".join(f"- {e}" for e in ue))
        sources.append(str(up))
    if cwd is not None and cfg.get("workspace", True):
        wp = workspace_path(cwd)
        we = parse_entries(read_text(wp))
        if we:
            parts.append("## 本项目\n" + "\n".join(f"- {e}" for e in we))
            sources.append(str(wp))
    if not parts:
        return MemoryBlock("", [])
    body = "\n\n".join(parts)
    truncated = False
    if max_bytes > 0 and len(body.encode("utf-8")) > max_bytes:
        body = _clip_bytes(body, max_bytes) + TRUNC_NOTE.format(limit=max_bytes)
        truncated = True
    head = ("# 长期记忆（用户让你记住的偏好和事实；与用户当前要求冲突时以当前为准"
            + ("；用 memory 工具增删，下个新会话生效" if tool else "") + "）\n<MEMORY>\n")
    return MemoryBlock(head + body + "\n</MEMORY>", sources, truncated)
