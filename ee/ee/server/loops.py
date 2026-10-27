"""会话内 /loop（D5-3）：在当前会话里按间隔 / 每天定时重复发送一段提示词。

- 只存在内存里：ee 退出就没了（要长期运行请「转为自动化」，那是持久化、无人值守、每次新会话）。
- 间隔型创建后立即发一次，之后每 N 分钟；每天型等到下一个时刻。
- 到点时会话正忙（回合进行中 / 等审批）就标记 pending，等这一回合结束后作为新的一回合发出；
  忙的期间又到点不重复排（记 skipped）。
- 用会话当前的模型与权限模式发送；用户条目 kind="loop"。
- 会话删除时它的循环一并停止。

RPC：loop/create {thread_id, text} | loop/list {thread_id?} | loop/stop {thread_id, id}（id 可为 all）
通知：loop/updated {loop}、loop/removed {thread_id, ids}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from ee.core import loopspec as LS
from ee.core import schedule as SC

if TYPE_CHECKING:  # pragma: no cover
    from ee.server.app import Backend

log = logging.getLogger("ee.loops")

MAX_PER_THREAD = 10
TICK = 30.0


class Loops:
    def __init__(self, backend: "Backend", clock: Callable[[], float] = time.time, tick: float = TICK):
        self.backend = backend
        self.clock = clock
        self.tick = tick
        self.loops: dict[str, dict] = {}
        self._seq = 0
        self._waiters: dict[str, asyncio.Task] = {}
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # ---------------------------------------------------------- 输出
    def view(self, lp: dict) -> dict:
        return {**lp, "schedule_text": SC.describe(lp["schedule"])}

    def _changed(self, lp: dict) -> None:
        self.backend.notify("loop/updated", {"loop": self.view(lp)})

    def list(self, thread_id: str | None = None) -> list[dict]:
        out = [lp for lp in self.loops.values() if not thread_id or lp["thread_id"] == thread_id]
        return [self.view(lp) for lp in sorted(out, key=lambda x: int(x["id"]))]

    # ---------------------------------------------------------- 增删
    def create(self, thread_id: str, text: str) -> dict:
        spec = LS.parse(text)
        if sum(1 for lp in self.loops.values() if lp["thread_id"] == thread_id) >= MAX_PER_THREAD:
            raise ValueError(f"一个会话最多 {MAX_PER_THREAD} 个循环，先用 /loop stop 停掉一些")
        now = self.clock()
        self._seq += 1
        lp = {"id": str(self._seq), "thread_id": thread_id, "prompt": spec.prompt, "schedule": spec.schedule,
              "note": spec.note, "created": now, "next_run": None, "last_run": None, "runs": 0, "skipped": 0,
              "pending": False, "last_error": ""}
        self.loops[lp["id"]] = lp
        if spec.schedule["kind"] == "interval":
            self._fire(lp, now)                       # 间隔型：马上发一次
        else:
            lp["next_run"] = self._next(lp, now)
        self._changed(lp)
        self.poke()
        log.info("会话 %s 新建循环 #%s：%s", thread_id, lp["id"], SC.describe(lp["schedule"]))
        return self.view(lp)

    def cancel(self, thread_id: str, lid: str) -> list[str]:
        lid = str(lid or "").strip().lstrip("#")
        mine = [x for x in self.loops.values() if x["thread_id"] == thread_id]
        if lid in ("", "all", "全部"):
            if not lid and len(mine) > 1:
                raise ValueError("这个会话有多个循环，请写编号：/loop stop <编号>，或 /loop stop all")
            ids = [x["id"] for x in mine]
        else:
            ids = [x["id"] for x in mine if x["id"] == lid]
        if not ids:
            raise ValueError(f"这个会话没有编号为 {lid} 的循环" if lid not in ("", "all", "全部") else "这个会话没有循环")
        self._drop(thread_id, ids)
        return ids

    def remove_thread(self, thread_id: str) -> None:
        ids = [x["id"] for x in self.loops.values() if x["thread_id"] == thread_id]
        if ids:
            self._drop(thread_id, ids)

    def _drop(self, thread_id: str, ids: list[str]) -> None:
        for i in ids:
            self.loops.pop(i, None)
            w = self._waiters.pop(i, None)
            if w is not None:
                w.cancel()
        self.backend.notify("loop/removed", {"thread_id": thread_id, "ids": ids})

    # ---------------------------------------------------------- 调度
    def _next(self, lp: dict, after: float) -> float | None:
        try:
            return SC.next_run(lp["schedule"], after, after if lp["schedule"]["kind"] == "interval" else None)
        except (SC.ScheduleError, ValueError, OverflowError):
            return None

    def _thread(self, lp: dict):
        return self.backend.threads.get(lp["thread_id"])

    def _fire(self, lp: dict, now: float) -> None:
        lp["next_run"] = self._next(lp, now)
        th = self._thread(lp)
        if th is None:                                 # 会话已经不在了
            self._drop(lp["thread_id"], [lp["id"]])
            return
        if th.running:
            if lp["pending"]:
                lp["skipped"] += 1
            else:
                lp["pending"] = True
                self._waiters[lp["id"]] = asyncio.get_running_loop().create_task(self._after_idle(lp))
            return
        self._send(lp, th)

    def _send(self, lp: dict, th) -> None:
        lp["pending"] = False
        try:
            th.start_turn(lp["prompt"], kind="loop")
            lp["runs"] += 1
            lp["last_run"] = self.clock()
            lp["last_error"] = ""
        except Exception as e:  # noqa: BLE001
            log.warning("循环 #%s 发送失败", lp["id"], exc_info=True)
            lp["last_error"] = str(e)[:300]

    async def _after_idle(self, lp: dict) -> None:
        try:
            while True:
                th = self._thread(lp)
                if th is None or lp["id"] not in self.loops:
                    return
                if not th.running:
                    await asyncio.sleep(0.05)          # 让回合收尾的通知先发出去
                    if th.running:                     # 用户抢先开始了新回合：继续等
                        continue
                    self._send(lp, th)
                    self._changed(lp)
                    return
                task = th.task
                if task is not None:
                    await asyncio.wait({task})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("循环 #%s 等待会话空闲出错", lp["id"], exc_info=True)
        finally:
            if self._waiters.get(lp["id"]) is asyncio.current_task():
                self._waiters.pop(lp["id"], None)

    async def tick_once(self, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        fired = []
        for lp in list(self.loops.values()):
            nr = lp.get("next_run")
            if nr is None or nr > now:
                continue
            self._fire(lp, now)
            if lp["id"] in self.loops:
                self._changed(lp)
                fired.append(self.view(lp))
        return fired

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="ee-loops")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        for w in list(self._waiters.values()):
            w.cancel()
        self._waiters.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.error("循环调度出错", exc_info=True)
            delay = self.tick
            nxt = [lp["next_run"] for lp in self.loops.values() if lp.get("next_run")]
            if nxt:
                delay = max(0.2, min(self.tick, min(nxt) - self.clock() + 0.1))
            assert self._wake is not None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass


def register(methods: dict, backend: "Backend") -> Loops:
    from ee.server.app import RpcError

    lo = Loops(backend)

    async def create(thread_id: str, text: str) -> dict:
        await backend._athread(thread_id)
        try:
            lp = lo.create(thread_id, text)
        except ValueError as e:
            raise RpcError(str(e))
        return {"loop": lp}

    async def list_(thread_id: str | None = None) -> dict:
        return {"loops": lo.list(thread_id)}

    async def stop(thread_id: str, id: str = "") -> dict:  # noqa: A002
        try:
            return {"stopped": lo.cancel(thread_id, id)}
        except ValueError as e:
            raise RpcError(str(e))

    methods.update({"loop/create": create, "loop/list": list_, "loop/stop": stop})
    return lo
