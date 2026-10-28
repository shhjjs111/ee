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


def build_tools(profile, config=None, agent: str | None = None,
                orchestrator_role: str | None = None) -> list[Tool]:
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
    # LSP（D6-2 / T9）一旦开启，repo_map 让位——两者都是"理解代码"的能力，
    # 而 full 工具表已用到 4421/4500（只剩 79 token），lsp 的 schema 约 143 token，
    # 同时给会超 83。为什么不抬高 FULL_LIMIT：那个 4500 是 2026-10-26 用真实
    # tokenizer 实测（3,822）反推的护栏，改它等于把护栏降级成"记录当前值"。
    # 为什么砍的是 repo_map 而不是 memory：有 lsp 的实时诊断时静态仓库地图价值
    # 下降，两者层次相同；memory 和 lsp 毫无交集，砍掉会造成"开了 A 结果 B 没了"
    # 这种最难排查的现象。关掉 lsp，repo_map 会回来。
    lsp_on = bool((data.get("lsp") or {}).get("enabled"))
    rmap = data.get("repo_map") or {}
    if rmap.get("enabled", True) and not lsp_on:   # 仓库地图（D5-6）：固定排在其他可选工具之前；tree-sitter 不可用时不注册
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
    # LSP 诊断（D6-2 / T9）：默认关。理由和指挥会话一样——工具 schema 是固定 token
    # 开销还进冻结前缀；而且没装语言服务器时它只会一直报 unavailable，白占位置。
    # 用户在设置里打开、并且确实有服务器时才注入。
    if (data.get("lsp") or {}).get("enabled") and (agent or agent_profile(config)) != "lean":
        from ee.tools.lsp import LspTool
        tools.append(LspTool())
    # 指挥会话（D6-1）：默认关，只有用户显式启用且不是 lean 才注入。
    # 五个工具是固定 token 开销，还进冻结前缀，所以不能无条件给。
    # 子会话一律不给：它自己没有子会话，给了只会让它误以为还能再分派。
    if ((data.get("orchestrator") or {}).get("enabled") and (agent or agent_profile(config)) != "lean"
            and orchestrator_role != "child"):
        from ee.tools.orchestrate import (ThreadCreateTool, ThreadSendTool, ThreadsListTool,
                                          ThreadStatusTool, ThreadWaitTool)
        tools += [ThreadsListTool(), ThreadStatusTool(), ThreadWaitTool(),
                  ThreadCreateTool(), ThreadSendTool()]
    return tools


def tool_rules(tools) -> list[str]:
    """启用的工具各自带的使用规则，按工具顺序拼接、去重（拼进系统提示词，见 core/context.system_prompt）。"""
    out: list[str] = []
    for t in tools:
        for r in getattr(t, "rules", None) or []:
            if r not in out:
                out.append(r)
    return out
