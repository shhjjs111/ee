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
    diff: str = ""          # 改文件类工具的统一 diff（也会放进 details.diff）
    details: dict = field(default_factory=dict)   # A6：只给界面的结构化数据（tools/details.py），不进模型上下文

    @property
    def meta(self) -> dict:  # 旧名
        return self.details


@dataclass
class ApprovalRequest(Event):
    """请求用户审批。界面调用 future.set_result(ApprovalDecision)。"""
    call_id: str
    tool: str
    title: str              # 一行说明，如 “运行命令：git commit -m ...”
    detail: str = ""        # 命令全文 / diff
    prefix: str = ""        # “以后都行”选项要记住的前缀（bash 命令前缀或工具名）
    future: asyncio.Future | None = None
    force: bool = False     # 危险操作的强制询问（全自动模式下也问；ee exec --approve auto 也不自动批）
    source: dict = field(default_factory=dict)   # 来自子代理时：{call_id, agent, description}（D5-2，界面标明来源）


@dataclass
class QuestionRequest(Event):
    """ask_user 工具向用户提问（D4-8）。界面调用 future.set_result(回答文字)。"""
    call_id: str
    question: str
    options: list = field(default_factory=list)
    multiple: bool = False
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
    status: str = "running"         # running | queued（并发名额已满，排队等待，D5-2）
    worktree: dict = field(default_factory=dict)   # 在独立 git worktree 里写时：{path, branch, base}
    fork: bool = False              # fork 模式：继承主会话上下文（D5-10）


@dataclass
class SubagentUpdate(Event):
    """子代理进度：kind = step | tool_start | tool_end | text | notice | usage | worktree。"""
    call_id: str
    kind: str
    step: int = 0
    tool: str = ""
    tool_call_id: str = ""
    summary: str = ""
    ok: bool | None = None
    text: str = ""
    usage: dict = field(default_factory=dict)     # kind=usage：这个子代理到目前的用量
    totals: dict = field(default_factory=dict)    # kind=usage：主会话（含所有子代理）到目前的总用量
    data: dict = field(default_factory=dict)      # kind=worktree：分支、改动文件统计


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
class QueueDelivered(Event):
    """排队的用户消息送进了模型上下文（ids 是送入的条目，kind = steer | follow）。"""
    ids: list[str]
    kind: str


@dataclass
class QueueReturned(Event):
    """回合结束时还没送入的排队消息，退回界面输入框：[{id, text, queue}]。"""
    items: list[dict]


@dataclass
class Error(Event):
    message: str
    fatal: bool = False


# 方便类型注解
EventT = Any
