"""指挥会话的并发名额（D6-1 U3）。

为什么不用"创建前数一下 running"：那只挡 create。用户/模型可以先把 10 个子会话建好
（都是 idle），再同时 send，运行时并发照样超限。所以名额在**发起运行**时预留，
create 与 send 共用同一个控制器。

`_active` 是正在运行的名额，`_total` 是已存在的子会话（含 idle）。两者都要限制：
只限运行数会让 meta 无限增长，只限总数会让"先建后跑"绕过并发保护。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

DEFAULT_MAX_CHILDREN = 4
DEFAULT_MAX_TOTAL = 16


class SlotLimit(Exception):
    """名额不够。消息直接给模型/用户看，要说明是哪一种上限。"""


@dataclass
class SlotController:
    max_children: int = DEFAULT_MAX_CHILDREN
    max_total: int = DEFAULT_MAX_TOTAL
    _active: set[str] = field(default_factory=set)
    _total: set[str] = field(default_factory=set)
    _lock: asyncio.Lock | None = None

    @property
    def active(self) -> int:
        return len(self._active)

    @property
    def total(self) -> int:
        return len(self._total)

    def _guard(self) -> asyncio.Lock:
        # 惰性创建：控制器可能在还没有事件循环时被构造（测试/配置校验）
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def reserve(self, key: str) -> None:
        """占一个名额。已经在跑（同 key 重复派发被合并）时直接返回。"""
        async with self._guard():
            if key in self._active:
                return
            if len(self._active) >= self.max_children:
                raise SlotLimit(
                    f"同时运行的子会话已达上限（{self.max_children} 个）；"
                    "等它们完成，或调大 [orchestrator] max_children")
            if key not in self._total and len(self._total) >= self.max_total:
                raise SlotLimit(
                    f"子会话总数已达上限（{self.max_total} 个）；先删除不用的子会话，"
                    "或调大 [orchestrator] max_total_children")
            self._active.add(key)
            self._total.add(key)

    def release(self, key: str) -> None:
        """回合结束释放运行名额（子会话仍存在，总数占用保留）。"""
        self._active.discard(key)

    def forget(self, key: str) -> None:
        """子会话被删除：运行名额与总数占用一起回收。"""
        self._active.discard(key)
        self._total.discard(key)


def controller_from_config(data: dict | None) -> SlotController | None:
    """按配置构造；未启用返回 None（调用方据此不注入工具、不拦请求）。"""
    o = (data or {}).get("orchestrator") or {}
    if not o.get("enabled"):
        return None
    try:
        max_children = max(1, int(o.get("max_children") or DEFAULT_MAX_CHILDREN))
        max_total = max(1, int(o.get("max_total_children") or DEFAULT_MAX_TOTAL))
    except (TypeError, ValueError):
        max_children, max_total = DEFAULT_MAX_CHILDREN, DEFAULT_MAX_TOTAL
    return SlotController(max_children=max_children, max_total=max(1, max_total))
