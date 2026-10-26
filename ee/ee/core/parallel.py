"""同一步里多个工具调用的并发调度（D5-2）。

规则：一步里的所有调用同时派发，每个调用按“访问类型”先过一道 FIFO 公平的读写闸门：
- read：只读工具、只读子代理。可以和其它 read 一起跑。
- write：会改主工作区的工具（write/edit/apply_patch/shell…、原地写的子代理）。独占：等前面的全部跑完，
  后面的也要等它跑完；多个 write 之间保持模型给出的顺序。
- free：不碰主工作区、也不需要排队的调用（在独立 git worktree 里写的子代理），不过闸门。
闸门按到达顺序放行（先到的 write 会挡住后到的 read），所以“读 → 写 → 读”不会被重排成“读读 → 写”。
结果由调用方按原调用顺序回填历史，与谁先跑完无关。
"""
from __future__ import annotations

import asyncio
from collections import deque

READ, WRITE, FREE = "read", "write", "free"


class RWGate:
    """FIFO 公平的异步读写锁：队头是一串 read 就一起放行；队头是 write 就等当前持有者全部释放后单独放行。"""

    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._queue: deque[tuple[str, asyncio.Future]] = deque()

    @property
    def readers(self) -> int:
        return self._readers

    @property
    def writer(self) -> bool:
        return self._writer

    def _grant(self) -> None:
        while self._queue:
            kind, fut = self._queue[0]
            if fut.done():                       # 等待中被取消的
                self._queue.popleft()
                continue
            if kind == READ:
                if self._writer:
                    return
                self._queue.popleft()
                self._readers += 1
                fut.set_result(None)
            else:
                if self._writer or self._readers:
                    return
                self._queue.popleft()
                self._writer = True
                fut.set_result(None)
                return

    async def acquire(self, kind: str) -> None:
        if kind == FREE:
            return
        fut = asyncio.get_running_loop().create_future()
        self._queue.append((kind, fut))
        self._grant()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():   # 刚被放行又被取消：把名额还回去
                self._release(kind)
            else:
                try:
                    self._queue.remove((kind, fut))
                except ValueError:
                    pass
                self._grant()
            raise

    def _release(self, kind: str) -> None:
        if kind == READ:
            self._readers = max(0, self._readers - 1)
        elif kind == WRITE:
            self._writer = False
        self._grant()

    def release(self, kind: str) -> None:
        if kind != FREE:
            self._release(kind)

    def hold(self, kind: str) -> "_Held":
        return _Held(self, kind)


class _Held:
    def __init__(self, gate: RWGate, kind: str):
        self.gate, self.kind = gate, kind

    async def __aenter__(self):
        await self.gate.acquire(self.kind)
        return self

    async def __aexit__(self, *exc):
        self.gate.release(self.kind)
        return False
