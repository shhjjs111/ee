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

__all__ = ["AGENT_PROFILES", "Tool", "ToolContext", "ToolError", "ToolResult", "ToolState", "agent_profile", "build_tools",
           "tool_rules"]

# 可选工具按配置追加到末尾，顺序固定：web_search、web_fetch、task（子代理）、memory、skill。
# 配置在会话开始时决定工具表（之后冻结进前缀）；设置页改开关从新会话起生效。


AGENT_PROFILES = ("full", "lean")


def agent_profile(config=None) -> str:
    """配置 profile：full（默认，完整工具表）| lean（精简：只有 read/write/edit/shell + 短系统提示词）。"""
    data = getattr(config, "data", config) or {}
    v = str(data.get("profile") or "full").strip().lower()
    return v if v in AGENT_PROFILES else "full"


def build_tools(profile, config=None, agent: str | None = None) -> list[Tool]:
    """agent：full / lean，默认取配置；会话开始时决定，之后冻结。lean 的工具表与 full 的前四个逐字节相同（除 shell 的 background 说明）。"""
    from ee.tools.plan import PresentPlanTool
    data0 = getattr(config, "data", config) or {}
    if (agent or agent_profile(config)) == "lean":
        return [ReadTool(), WriteTool(), EditTool(), ShellTool(bg_tools=False)]
    bg = bool(data0.get("background_shell", True))
    tools: list[Tool] = [ReadTool(), WriteTool(), EditTool(), ShellTool(bg_tools=bg), GrepTool(), GlobTool()]
    if data0.get("todo", True):                  # 开关：todo 计划清单（默认开）
        tools.append(TodoTool())
    if data0.get("present_plan", True):          # 开关：计划模式的 present_plan（默认开；关掉时计划模式用文字给方案）
        tools.append(PresentPlanTool())
    if bg:
        from ee.tools.bgshell import ShellKillTool, ShellOutputTool, ShellWriteTool
        tools += [ShellOutputTool(), ShellWriteTool(), ShellKillTool()]
    if data0.get("ask_user", True):
        from ee.tools.ask import AskUserTool
        tools.append(AskUserTool())
    if getattr(profile, "edit_style", "edit") == "apply_patch":
        tools.append(ApplyPatchTool())
    if config is None:
        return tools
    from ee.tools.web import WebFetchTool, WebSearchTool, fetch_mode, web_config
    data = getattr(config, "data", config) or {}
    rmap = data.get("repo_map") or {}
    if rmap.get("enabled", True):               # 仓库地图（D5-6）：固定排在其他可选工具之前；tree-sitter 不可用时不注册
        from ee.core.repomap import available
        if available():
            from ee.tools.repomap import RepoMapTool
            tools.append(RepoMapTool(rmap))
    w = web_config(data)
    if w.get("search_backend") not in (None, "", "off"):
        tools.append(WebSearchTool(data))
    if fetch_mode(data) != "off":
        tools.append(WebFetchTool(data))
    if (data.get("subagent") or {}).get("enabled", True):
        from ee.tools.agent import TaskTool
        tools.append(TaskTool())
    mem = data.get("memory") or {}
    if mem.get("enabled", True) and mem.get("tool", True):     # memory.tool：只关工具、保留注入
        from ee.tools.memory import MemoryTool
        tools.append(MemoryTool(workspace_enabled=bool(mem.get("workspace", True))))
    if (data.get("skills") or {}).get("enabled", True):      # 技能（D5-5）
        from ee.tools.skill import SkillTool
        tools.append(SkillTool())
    # 指挥会话（D6-1）：默认关，只有用户显式启用且不是 lean 才注入。
    # 五个工具是固定 token 开销，还进冻结前缀，所以不能无条件给。
    if (data.get("orchestrator") or {}).get("enabled") and (agent or agent_profile(config)) != "lean":
        from ee.tools.orchestrate import ThreadsListTool, ThreadStatusTool, ThreadWaitTool
        tools += [ThreadsListTool(), ThreadStatusTool(), ThreadWaitTool()]
    return tools


def tool_rules(tools) -> list[str]:
    """启用的工具各自带的使用规则，按工具顺序拼接、去重（拼进系统提示词，见 core/context.system_prompt）。"""
    out: list[str] = []
    for t in tools:
        for r in getattr(t, "rules", None) or []:
            if r not in out:
                out.append(r)
    return out
