"""工具注册。顺序固定（工具表是缓存前缀的一部分，不能重排）。"""
from __future__ import annotations

from ee.tools.apply_patch import ApplyPatchTool
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, ToolState
from ee.tools.edit import EditTool
from ee.tools.read import ReadTool
from ee.tools.search import GlobTool, GrepTool
from ee.tools.shell import ShellTool
from ee.tools.todo import TodoTool
from ee.tools.write import WriteTool

__all__ = ["Tool", "ToolContext", "ToolError", "ToolResult", "ToolState", "build_tools"]

# M2+ 预留：web_search / web_fetch / task（子代理）在这里按配置追加到末尾，不插队。


def build_tools(profile) -> list[Tool]:
    tools: list[Tool] = [ReadTool(), WriteTool(), EditTool(), ShellTool(), GrepTool(), GlobTool(), TodoTool()]
    if getattr(profile, "edit_style", "edit") == "apply_patch":
        tools.append(ApplyPatchTool())
    return tools
