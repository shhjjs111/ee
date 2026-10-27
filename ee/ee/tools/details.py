"""工具结果的 details 契约：只给界面、不进模型上下文、持久化到条目。

每个工具结果 = content（ToolResult.output，给模型）+ details（这里定义的结构化字段，给界面渲染）。
界面按 details.kind 渲染工具卡片，不再去解析 content 文本。所有 kind 都可能带的公共字段见 CommonDetails。
"""
from __future__ import annotations

from typing import Any, TypedDict

DIFF_LIMIT = 30_000          # 界面 diff 上限（与条目 output 的界面上限一致）
FILES_LIMIT = 200            # 文件列表最多带多少个


class Truncation(TypedDict, total=False):
    lines: int               # 原始输出总行数
    bytes: int               # 原始输出总字节
    path: str | None         # 全文保存位置（保存失败为 None）


class CommonDetails(TypedDict, total=False):
    kind: str
    duration_ms: int         # 工具执行耗时
    truncated: Truncation    # 给模型的 content 被截断时才有
    denied: bool             # 被权限规则 / 用户 / Hook 拒绝
    by_user: bool


class FileChange(TypedDict):
    verb: str                # Created / Edited / Deleted / Moved …
    path: str
    added: int
    removed: int


class EditDetails(CommonDetails, total=False):       # write / edit / apply_patch
    files: list[FileChange]
    path: str
    verb: str
    added: int
    removed: int
    diff: str                # 统一 diff（界面截断到 DIFF_LIMIT）
    strategy: str            # edit 的匹配策略（exact / 容错）
    edits: int


class ShellDetails(CommonDetails, total=False):
    command: str
    exit: int | None
    timed_out: bool
    output: str              # 原始输出末尾 4000 字符（界面显示用）
    background: bool
    bg_id: str


class ReadDetails(CommonDetails, total=False):
    path: str
    start: int
    end: int
    total_lines: int
    ref: bool                # 未改变的重复读，只回了引用
    doc: str                 # pdf / docx / xlsx …
    doc_info: dict


class ListDetails(CommonDetails, total=False):       # read 目录 / glob
    path: str
    files: list[str]
    total: int


class SearchDetails(CommonDetails, total=False):     # grep
    pattern: str
    matches: int
    files: list[str]


DETAIL_KINDS: dict[str, type] = {
    "edit": EditDetails, "shell": ShellDetails, "read": ReadDetails, "list": ListDetails,
    "search": SearchDetails,
    # 下面这些各工具自带字段，界面已有专门卡片：subagent / question / plan / plan_proposal / memory /
    # skill / repo_map / web_search / web_fetch / mcp / shell_bg
    "subagent": CommonDetails, "question": CommonDetails, "plan": CommonDetails, "plan_proposal": CommonDetails,
    "memory": CommonDetails, "skill": CommonDetails, "repo_map": CommonDetails, "web_search": CommonDetails,
    "web_fetch": CommonDetails, "mcp": CommonDetails, "shell_bg": CommonDetails, "tool": CommonDetails,
}


def _clip(s: str, n: int = DIFF_LIMIT) -> str:
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half] + f"\n…（界面省略了中间 {len(s) - n} 个字符）…\n" + s[-half:]


def finalize(name: str, res: Any, duration_ms: int | None, truncation: dict | None) -> dict:
    """把工具返回的 details 补齐成契约形状（就地不改 res，返回新 dict）。"""
    d: dict = dict(res.details or {})
    d.setdefault("kind", "tool")
    if d["kind"] == "edit":
        if not d.get("files") and d.get("path"):
            d["files"] = [{"verb": d.get("verb") or "Edited", "path": d["path"],
                           "added": int(d.get("added") or 0), "removed": int(d.get("removed") or 0)}]
        d.setdefault("files", [])
        d.setdefault("added", sum(int(f.get("added") or 0) for f in d["files"]))
        d.setdefault("removed", sum(int(f.get("removed") or 0) for f in d["files"]))
        if res.diff and not d.get("diff"):
            d["diff"] = _clip(res.diff)
    if isinstance(d.get("files"), list) and len(d["files"]) > FILES_LIMIT and d["kind"] != "edit":
        d.setdefault("total", len(d["files"]))
        d["files"] = d["files"][:FILES_LIMIT]
    if truncation and not isinstance(d.get("truncated"), dict):
        d["truncated"] = dict(truncation)
    elif not d.get("truncated"):
        d.pop("truncated", None)
    if duration_ms is not None:
        d["duration_ms"] = int(duration_ms)
    return d
