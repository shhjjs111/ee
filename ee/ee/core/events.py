"""内核 → 界面的事件类型。界面只消费这些事件，不碰内核状态。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    pass


@dataclass
class TurnStart(Event):
    turn: int
    tree: str | None = None     # 回合开始时的磁盘快照（D4-1），没有则 None


@dataclass
class StepStart(Event):
    step: int


@dataclass
class TextDelta(Event):
    text: str


@dataclass
class ReasoningDelta(Event):
    text: str


@dataclass
class AssistantMessage(Event):
    """一步结束时的完整助手文本（界面可用来做最终 markdown 渲染）。"""
    text: str
    reasoning: str = ""


@dataclass
class ToolStart(Event):
    call_id: str
    name: str
    args: dict
    summary: str = ""


@dataclass
class ToolOutput(Event):
    """工具运行中的增量输出（bash 流式）。"""
    call_id: str
    text: str


@dataclass
class ToolEnd(Event):
    call_id: str
    name: str
    ok: bool
    output: str
    summary: str = ""
    diff: str = ""          # 改文件类工具的统一 diff
    meta: dict = field(default_factory=dict)


@dataclass
class ApprovalRequest(Event):
    """请求用户审批。界面调用 future.set_result(ApprovalDecision)。"""
    call_id: str
    tool: str
    title: str              # 一行说明，如 “运行命令：git commit -m ...”
    detail: str = ""        # 命令全文 / diff
    prefix: str = ""        # “以后都行”选项要记住的前缀（bash 命令前缀或工具名）
    future: asyncio.Future | None = None


@dataclass
class ApprovalDecision:
    choice: str             # once | always | deny | deny_with_reason
    reason: str = ""


@dataclass
class PlanUpdate(Event):
    items: list[dict]


@dataclass
class Usage(Event):
    prompt_tokens: int
    completion_tokens: int
    cache_hit: int | None   # None = 服务端没给
    cost: float | None
    totals: dict = field(default_factory=dict)


@dataclass
class Status(Event):
    """临时状态（“正在连接模型…”），不进历史；空字符串 = 清除。"""
    text: str


@dataclass
class Notice(Event):
    """提示类消息（缓存漂移警告、降级提示等）。"""
    text: str
    level: str = "info"     # info | warn


@dataclass
class Compacted(Notice):
    """上下文已压缩。是 Notice 的子类：只认 Notice 的界面也会显示“已压缩上下文”这条提示。"""
    reason: str = "auto"            # auto | manual
    tokens_before: int = 0
    tokens_after: int = 0
    summary: str = ""


@dataclass
class SubagentStart(Event):
    """主代理的 task 工具派生了一个子代理。call_id = 父级 task 工具调用的 id（界面据此把进度挂到那张卡片上）。"""
    call_id: str
    agent: str                      # 子代理类型：general / explore / 自定义名
    description: str = ""
    model: str = ""
    mode: str = ""                  # 子代理实际的权限模式
    max_steps: int = 0


@dataclass
class SubagentUpdate(Event):
    """子代理进度：kind = step | tool_start | tool_end | text | notice。"""
    call_id: str
    kind: str
    step: int = 0
    tool: str = ""
    tool_call_id: str = ""
    summary: str = ""
    ok: bool | None = None
    text: str = ""


@dataclass
class SubagentEnd(Event):
    call_id: str
    status: str                     # completed | max_steps | interrupted | error | loop
    summary: str = ""               # 子代理最终给主代理的报告
    steps: int = 0
    usage: dict = field(default_factory=dict)


@dataclass
class TurnDone(Event):
    reason: str             # completed | max_steps | interrupted | error | loop
    text: str = ""
    tree_after: str | None = None   # 回合结束时的磁盘快照（D4-1）


@dataclass
class Error(Event):
    message: str
    fatal: bool = False


# 方便类型注解
EventT = Any
