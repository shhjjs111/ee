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

# 可选工具按配置追加到末尾，顺序固定：web_search、web_fetch、task（子代理）、memory。
# 配置在会话开始时决定工具表（之后冻结进前缀）；设置页改开关从新会话起生效。


def build_tools(profile, config=None) -> list[Tool]:
    tools: list[Tool] = [ReadTool(), WriteTool(), EditTool(), ShellTool(), GrepTool(), GlobTool(), TodoTool()]
    if getattr(profile, "edit_style", "edit") == "apply_patch":
        tools.append(ApplyPatchTool())
    if config is None:
        return tools
    from ee.tools.web import WebFetchTool, WebSearchTool, fetch_mode, web_config
    data = getattr(config, "data", config) or {}
    w = web_config(data)
    if w.get("search_backend") not in (None, "", "off"):
        tools.append(WebSearchTool(data))
    if fetch_mode(data) != "off":
        tools.append(WebFetchTool(data))
    if (data.get("subagent") or {}).get("enabled", True):
        from ee.tools.agent import TaskTool
        tools.append(TaskTool())
    mem = data.get("memory") or {}
    if mem.get("enabled", True):
        from ee.tools.memory import MemoryTool
        tools.append(MemoryTool(workspace_enabled=bool(mem.get("workspace", True))))
    return tools
