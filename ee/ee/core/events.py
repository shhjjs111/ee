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
class Notice(Event):
    """提示类消息（缓存漂移警告、降级提示等）。"""
    text: str
    level: str = "info"     # info | warn


@dataclass
class TurnDone(Event):
    reason: str             # completed | max_steps | interrupted | error
    text: str = ""


@dataclass
class Error(Event):
    message: str
    fatal: bool = False


# 方便类型注解
EventT = Any
