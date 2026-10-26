"""自动化 / 定时任务（D4-10）：在项目上按计划无人值守地跑提示词。

- 持久化：~/.ee/automations.json（原子写：临时文件 + os.replace），里面有 automations 和 runs（运行历史，
  总数上限 RUNS_LIMIT、每个自动化最多 RUNS_PER_AUTOMATION 条）。
- 调度：`ee serve` 起一个 asyncio 循环，每 TICK 秒（或到下次运行时刻，取较早者）检查一次到点的自动化。
  每个自动化记 next_run（时间戳）；到点就跑并按调度规则算下一次。关机 / 退出期间错过的，启动后只补跑一次
  （trigger=catchup），不会把错过的每一次都补上。
- 同一个自动化不并发：上次还在跑就跳过本次，并记一条 status=skipped 的运行记录。
- 每次运行新建一个会话（meta.source="automation"、automation_id），执行逻辑复用 ee exec 的无人值守部分
  （headless.unattended_configure / auto_approve）：ask_user 关闭；权限模式默认只读；需要审批时按 approve
  策略（deny 默认 / auto）自动决定；结束后结束本次的后台命令；超过 timeout_minutes 自动中断。
  可选在新 git worktree 里跑（非 git 项目退回在项目目录里跑并在结果里说明）。
- 结果写进运行历史（未读），推送 automation/run_updated、automation/run_done、automation/updated 通知；
  桌面端据此更新左栏「自动化」角标并弹 Windows 通知。

RPC：automations/list|create|update|delete|run_now|runs|mark_read|preview。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ee.core import schedule as SC
from ee.core.permissions import MODES

if TYPE_CHECKING:  # pragma: no cover
    from ee.server.app import Backend

log = logging.getLogger("ee.automations")

FILE_NAME = "automations.json"
TICK = 30.0                     # 秒：调度循环最长睡多久
RUNS_LIMIT = 1000
RUNS_PER_AUTOMATION = 100
SUMMARY_LEN = 200
DEFAULT_TIMEOUT = 60            # 分钟
MAX_TIMEOUT = 24 * 60
CATCHUP_GRACE = 90.0            # 秒：晚了这么多才算“错过后补跑”
APPROVE = ("deny", "auto")

DENY_REASON = ("这是无人值守的自动化任务，没有人能批准这个操作，已自动拒绝。"
               "请换不需要审批的做法；做不到就在最终回答里说明需要用户手动做什么。")
FORCE_DENY_REASON = ("这是危险操作，无人值守的自动化任务不会自动批准，已拒绝。"
                     "请换安全的做法，或在最终回答里说明需要用户手动执行什么。")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _summary(text: str, n: int = SUMMARY_LEN) -> str:
    t = re.sub(r"```[\s\S]*?```", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


# ------------------------------------------------------------------ 持久化
class AutomationStore:
    """~/.ee/automations.json 的读写（线程安全，原子写）。"""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / FILE_NAME
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"version": 1, "automations": [], "runs": []}
            except (OSError, json.JSONDecodeError):
                log.warning("automations.json 读取失败，按空处理（原文件另存为 .bad）", exc_info=True)
                try:
                    self.path.replace(self.path.with_suffix(".json.bad"))
                except OSError:
                    pass
                return {"version": 1, "automations": [], "runs": []}
            if not isinstance(d, dict):
                d = {}
            d.setdefault("version", 1)
            d["automations"] = [a for a in d.get("automations") or [] if isinstance(a, dict) and a.get("id")]
            d["runs"] = [r for r in d.get("runs") or [] if isinstance(r, dict) and r.get("id")]
            return d

    def save(self, d: dict) -> None:
        with self._lock:
            self.home.mkdir(parents=True, exist_ok=True)
            runs = d.get("runs") or []
            # 历史上限：每个自动化最多 N 条、总数最多 M 条（runs 按开始时间升序存）
            per: dict[str, int] = {}
            kept = []
            for r in reversed(runs):
                k = r.get("automation_id") or ""
                per[k] = per.get(k, 0) + 1
                if per[k] <= RUNS_PER_AUTOMATION and len(kept) < RUNS_LIMIT:
                    kept.append(r)
            d["runs"] = list(reversed(kept))
            data = json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")
            tmp = self.path.with_name(f".{FILE_NAME}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
            tmp.write_bytes(data)
            for i in range(10):            # Windows 上目标文件正被别的进程读时 replace 可能短暂失败
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if i == 9:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(0.05)

    def update(self, fn: Callable[[dict], object]):
        """读-改-写（同一把锁内）。fn 返回值原样返回。"""
        with self._lock:
            d = self.load()
            res = fn(d)
            self.save(d)
            return res


# ------------------------------------------------------------------ 校验
def normalize(data: dict, base: dict | None = None) -> dict:
    """新建（base=None）或修改（base=原记录）时校验字段；不合法抛 ValueError（中文原因）。"""
    if not isinstance(data, dict):
        raise ValueError("参数必须是对象")
    a = dict(base or {})
    allowed = {"name", "cwd", "prompt", "model", "mode", "approve", "worktree", "schedule", "enabled",
               "timeout_minutes"}
    unknown = set(data) - allowed - {"id", "created", "updated", "last_run", "last_status", "next_run",
                                     "running", "last_run_id"}
    if unknown:
        raise ValueError(f"未知字段：{', '.join(sorted(unknown))}")
    for k in allowed:
        if k in data:
            a[k] = data[k]
    name = str(a.get("name") or "").strip()
    if not name:
        raise ValueError("请填写名字")
    a["name"] = name[:80]
    cwd = str(a.get("cwd") or "").strip()
    if not cwd:
        raise ValueError("请选择项目")
    p = Path(cwd).expanduser()
    if not p.is_dir():
        raise ValueError(f"项目目录不存在：{cwd}")
    a["cwd"] = str(p.resolve())
    prompt = str(a.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请填写提示词")
    a["prompt"] = prompt[:20000]
    a["model"] = str(a.get("model") or "").strip()
    mode = a.get("mode") or "read-only"
    if mode not in MODES:
        raise ValueError(f"未知权限模式：{mode}")
    a["mode"] = mode
    approve = a.get("approve") or "deny"
    if approve not in APPROVE:
        raise ValueError("自动审批策略只能是 deny 或 auto")
    a["approve"] = approve
    a["worktree"] = bool(a.get("worktree"))
    a["enabled"] = bool(a.get("enabled", True))
    try:
        to = int(a.get("timeout_minutes") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise ValueError("超时分钟数要是整数")
    a["timeout_minutes"] = max(1, min(MAX_TIMEOUT, to))
    try:
        a["schedule"] = SC.normalize(a.get("schedule"))
    except SC.ScheduleError as e:
        raise ValueError(str(e))
    return a


# ------------------------------------------------------------------ 调度器 + 执行器
class Automations:
    def __init__(self, backend: "Backend", clock: Callable[[], float] = time.time, tick: float = TICK):
        self.backend = backend
        self.home = backend.home
        self.store = AutomationStore(self.home)
        self.clock = clock
        self.tick = tick
        self.startup_delay = 5.0                      # 秒：启动后先等桌面端连上，补跑的结果通知不会丢
        self.running: dict[str, str] = {}             # automation_id -> run_id
        self.tasks: dict[str, asyncio.Task] = {}      # run_id -> 执行任务
        self._loop_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # ---------------------------------------------------------- 输出
    def _notify(self, method: str, params: dict) -> None:
        self.backend.notify(method, params)

    def view(self, a: dict) -> dict:
        out = dict(a)
        out["running"] = a["id"] in self.running
        out["schedule_text"] = SC.describe(a.get("schedule") or {})
        return out

    @staticmethod
    def _unread(d: dict) -> int:
        return sum(1 for r in d.get("runs") or [] if r.get("unread"))

    # ---------------------------------------------------------- 增删改查
    def list(self) -> dict:
        d = self.store.load()
        items = sorted(d["automations"], key=lambda a: a.get("created") or 0)
        return {"automations": [self.view(a) for a in items], "unread": self._unread(d)}

    def get(self, aid: str) -> dict:
        for a in self.store.load()["automations"]:
            if a["id"] == aid:
                return a
        raise KeyError(aid)

    def create(self, data: dict) -> dict:
        a = normalize(data)
        now = self.clock()
        a.update({"id": _new_id("au"), "created": now, "updated": now, "last_run": None, "last_status": None})
        a["next_run"] = self._next(a, now) if a["enabled"] else None
        self.store.update(lambda d: d["automations"].append(a))
        self._notify("automation/updated", {"automation": self.view(a)})
        self._poke()
        return self.view(a)

    def update(self, aid: str, patch: dict) -> dict:
        now = self.clock()

        def fn(d):
            for i, cur in enumerate(d["automations"]):
                if cur["id"] == aid:
                    a = normalize(patch, cur)
                    a["updated"] = now
                    sched_changed = a["schedule"] != cur.get("schedule")
                    if not a["enabled"]:
                        a["next_run"] = None
                    elif sched_changed or not cur.get("enabled") or not cur.get("next_run"):
                        a["next_run"] = self._next(a, now)
                    d["automations"][i] = a
                    return a
            raise KeyError(aid)
        a = self.store.update(fn)
        self._notify("automation/updated", {"automation": self.view(a)})
        self._poke()
        return self.view(a)

    def delete(self, aid: str) -> dict:
        def fn(d):
            n = len(d["automations"])
            d["automations"] = [a for a in d["automations"] if a["id"] != aid]
            if len(d["automations"]) == n:
                raise KeyError(aid)
            d["runs"] = [r for r in d["runs"] if r.get("automation_id") != aid]
            return self._unread(d)
        unread = self.store.update(fn)
        self._notify("automation/deleted", {"automation_id": aid, "unread": unread})
        return {"ok": True, "unread": unread}

    def runs(self, automation_id: str | None = None, limit: int = 100) -> dict:
        d = self.store.load()
        rs = [r for r in reversed(d["runs"]) if not automation_id or r.get("automation_id") == automation_id]
        rs.sort(key=lambda r: r.get("started") or 0, reverse=True)      # 同一时刻的保持新的在前（稳定排序）
        return {"runs": rs[: max(1, int(limit))], "unread": self._unread(d)}

    def mark_read(self, run_ids: list[str] | None = None, automation_id: str | None = None,
                  thread_id: str | None = None, all: bool = False) -> dict:  # noqa: A002
        ids = set(run_ids or [])

        def fn(d):
            changed = []
            for r in d["runs"]:
                hit = all or r["id"] in ids or (automation_id and r.get("automation_id") == automation_id) \
                    or (thread_id and r.get("thread_id") == thread_id)
                if hit and r.get("unread"):
                    r["unread"] = False
                    changed.append(r["id"])
            return changed, self._unread(d)
        changed, unread = self.store.update(fn)
        if changed:
            self._notify("automation/read", {"run_ids": changed, "unread": unread})
        return {"changed": changed, "unread": unread}

    # ---------------------------------------------------------- 调度
    def _next(self, a: dict, after: float) -> float | None:
        sched = a.get("schedule") or {}
        anchor = None
        if sched.get("kind") == "interval":
            anchor = a.get("last_run") or a.get("created") or after
        try:
            return SC.next_run(sched, after, anchor)
        except (SC.ScheduleError, KeyError, ValueError, OverflowError):
            log.warning("自动化 %s 算不出下次运行时间", a.get("id"), exc_info=True)
            return None

    def start(self) -> asyncio.Task:
        """在当前事件循环里起调度循环（ee serve 启动后调用）。"""
        if self._loop_task is None or self._loop_task.done():
            try:
                self._recover_stale_runs()
            except Exception:  # noqa: BLE001
                log.warning("清理遗留的运行中记录失败", exc_info=True)
            self._wake = asyncio.Event()
            self._loop_task = asyncio.create_task(self._loop(), name="ee-automations")
        return self._loop_task

    def _recover_stale_runs(self) -> None:
        """上次进程没正常结束时留在文件里的 status=running 记录（本进程里没在跑的）标为已中断。"""
        if not self.store.path.exists():
            return
        now = self.clock()
        live = set(self.running.values())

        def fn(d):
            stale = set()
            for r in d["runs"]:
                if r.get("status") == "running" and r["id"] not in live:
                    r.update({"status": "interrupted", "finished": now,
                              "summary": r.get("summary") or "ee 上次退出时这次运行还没结束，已中断"})
                    stale.add(r["id"])
            for x in d["automations"]:
                if x.get("last_status") == "running" and (x.get("last_run_id") in stale or x["id"] not in self.running):
                    x["last_status"] = "interrupted"
            return stale
        if self.store.update(fn):
            log.info("已把上次遗留的运行中记录标为已中断")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        for th_task in list(self.tasks.values()):
            th_task.cancel()

    def _poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        log.info("自动化调度已启动（%s）", self.store.path)
        if self.startup_delay > 0:
            await asyncio.sleep(self.startup_delay)
        while True:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.error("自动化调度出错", exc_info=True)
            delay = self.tick
            try:
                nxt = [a["next_run"] for a in self.store.load()["automations"] if a.get("enabled") and a.get("next_run")]
                if nxt:
                    delay = max(0.5, min(self.tick, min(nxt) - self.clock() + 0.2))
            except Exception:  # noqa: BLE001
                pass
            assert self._wake is not None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def tick_once(self, now: float | None = None) -> list[dict]:
        """检查一次：到点的自动化启动运行（或因上次没跑完记一条 skipped）。返回本次产生的运行记录。"""
        now = self.clock() if now is None else now
        due: list[tuple[dict, str]] = []

        def fn(d):
            for a in d["automations"]:
                if not a.get("enabled"):
                    continue
                nr = a.get("next_run")
                if nr is None:
                    a["next_run"] = self._next(a, now)
                    continue
                if nr > now:
                    continue
                trigger = "catchup" if now - nr > CATCHUP_GRACE else "schedule"
                due.append((dict(a), trigger))
                a["next_run"] = self._next({**a, "last_run": now if a["id"] not in self.running else a.get("last_run")}, now)
        await asyncio.to_thread(self.store.update, fn)
        out = []
        for a, trigger in due:
            if a["id"] in self.running:
                out.append(await asyncio.to_thread(self._record_skip, a, now))
            else:
                out.append(self._launch(a, trigger, now))
        return out

    def _record_skip(self, a: dict, now: float) -> dict:
        run = {"id": _new_id("ru"), "automation_id": a["id"], "name": a["name"], "trigger": "schedule",
               "started": now, "finished": now, "status": "skipped", "thread_id": None,
               "summary": "上一次运行还没结束，跳过本次", "unread": False}
        self.store.update(lambda d: d["runs"].append(run))
        log.info("自动化 %s（%s）上次还在运行，跳过本次", a["name"], a["id"])
        self._notify("automation/run_updated", {"run": run})
        return run

    def run_now(self, aid: str) -> dict:
        a = self.get(aid)
        if aid in self.running:
            raise ValueError("这个自动化正在运行，等它结束再试")
        return self._launch(a, "manual", self.clock())

    # ---------------------------------------------------------- 执行
    def _launch(self, a: dict, trigger: str, now: float) -> dict:
        run = {"id": _new_id("ru"), "automation_id": a["id"], "name": a["name"], "trigger": trigger,
               "started": now, "finished": None, "status": "running", "thread_id": None, "summary": "",
               "unread": False, "cwd": a["cwd"], "mode": a.get("mode"), "worktree": None}
        self.running[a["id"]] = run["id"]

        def fn(d):
            d["runs"].append(run)
            for x in d["automations"]:
                if x["id"] == a["id"]:
                    x["last_run"], x["last_status"], x["last_run_id"] = now, "running", run["id"]
                    if trigger == "manual" and (x.get("schedule") or {}).get("kind") == "interval" and x.get("enabled"):
                        x["next_run"] = self._next({**x, "last_run": now}, now)
                    a.update(x)
        self.store.update(fn)
        log.info("自动化 %s（%s）开始运行：%s", a["name"], a["id"], trigger)
        self._notify("automation/run_updated", {"run": dict(run)})
        self._notify("automation/updated", {"automation": self.view(a)})
        task = asyncio.get_running_loop().create_task(self._execute(a, run), name=f"ee-automation-{run['id']}")
        self.tasks[run["id"]] = task
        return dict(run)

    def _make_thread(self, a: dict, run: dict):
        """建会话（工作线程里调用：可能要建 worktree、跑 git）。"""
        from ee.core.store import SessionStore
        from ee.headless import auto_approve, unattended_configure
        from ee.core.events import ApprovalRequest, ToolEnd
        from ee.server.threads import Thread

        class AutomationThread(Thread):
            final_text = ""

            def _on_event(self, ev) -> None:
                super()._on_event(ev)
                if isinstance(ev, ApprovalRequest):
                    decision, _ = auto_approve(self, ev, a.get("approve") or "deny", DENY_REASON, FORCE_DENY_REASON)
                    log.info("自动化 %s 自动%s：%s", a["id"], "批准" if decision == "approved" else "拒绝", ev.title)
                elif isinstance(ev, ToolEnd) and ev.meta.get("kind") == "plan_proposal":
                    self.final_text = ev.meta.get("plan", "")

        b = self.backend
        cwd = Path(a["cwd"])
        if not cwd.is_dir():
            raise RuntimeError(f"项目目录不存在：{a['cwd']}")
        note = ""
        wt = None
        if a.get("worktree"):
            from ee.core import worktree as W
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run["started"]))
            try:
                wt = W.create(cwd, f"auto-{a['id'][3:9]}-{stamp}", None, self.home)
                cwd = Path(wt["path"])
            except W.WorktreeError as e:
                note = f"没能新建 worktree（{e}），本次直接在项目目录里运行。"
        meta = {"cwd": str(cwd.resolve()), "model": a.get("model") or "", "mode": a.get("mode") or "read-only",
                "title": a["name"], "created": time.time(), "source": "automation", "automation_id": a["id"],
                "automation_run_id": run["id"]}
        if wt:
            meta["worktree"] = {"path": wt["path"], "branch": wt["branch"], "repo": wt["repo"], "name": wt["name"]}
        store = SessionStore(self.home)
        th = AutomationThread(store.id, self.home, b.notify, meta, client_factory=b.client_factory,
                              persist=b.persist, configure=unattended_configure())
        th.ensure_session()
        th.save_meta()
        return th, wt, note

    async def _execute(self, a: dict, run: dict) -> None:
        th = None
        status, text, note = "error", "", ""
        try:
            try:
                th, wt, note = await asyncio.to_thread(self._make_thread, a, run)
            except Exception as e:  # noqa: BLE001
                log.warning("自动化 %s 启动失败", a["id"], exc_info=True)
                status, text = "failed", f"启动失败：{e}"
                return
            self.backend.threads[th.id] = th
            run["thread_id"] = th.id
            if wt:
                run["worktree"] = {"path": wt["path"], "branch": wt["branch"]}
            def _alive(d) -> bool:
                _patch_run(d, run)
                return any(x.get("id") == a["id"] for x in d["automations"])
            if not self.store.update(_alive):         # 建会话期间自动化被删了：不再开跑、不再推送
                status, text = "interrupted", "自动化已删除"
                return
            self._notify("automation/run_updated", {"run": dict(run)})
            self.backend.notify("thread/updated", {"thread": th.summary()})
            th.start_turn(a["prompt"], mode=a.get("mode") or "read-only")
            th.meta["title"] = a["name"]                    # 标题用自动化名字（start_turn 会按提示词起标题）
            th.save_meta()
            self.backend.notify("thread/updated", {"thread": th.summary()})
            task = th.task
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), a.get("timeout_minutes", DEFAULT_TIMEOUT) * 60)
                except asyncio.TimeoutError:
                    note = (note + " " if note else "") + f"运行超过 {a.get('timeout_minutes')} 分钟，已自动中断。"
                    th.interrupt()
                    try:
                        await asyncio.wait_for(task, 30)
                    except Exception:  # noqa: BLE001
                        pass
            last = next((it for it in reversed(th.items) if it.get("type") == "turn_end"), None)
            status = (last or {}).get("reason") or "error"
            from ee.headless import _last_text
            text = th.final_text or _last_text(th)
            if status == "error" and not text:
                err = next((it for it in reversed(th.items) if it.get("type") == "error"), None)
                text = (err or {}).get("message") or "出错了"
        except asyncio.CancelledError:
            status, text = "interrupted", text or "ee 退出，运行被中断"
            if th is not None:
                th.interrupt()
            raise
        except Exception as e:  # noqa: BLE001
            log.error("自动化 %s 运行异常", a["id"], exc_info=True)
            status, text = "error", f"内部错误：{type(e).__name__}: {e}"
        finally:
            if th is not None and th.session is not None:
                try:
                    await asyncio.wait_for(th.session.bg.close(), 8)     # 不留后台命令
                except BaseException:  # noqa: BLE001
                    th.session.bg.kill_all_sync()
            self._finish(a, run, status, text, note)

    def _finish(self, a: dict, run: dict, status: str, text: str, note: str) -> None:
        run.update({"finished": self.clock(), "status": status,
                    "summary": _summary(((note + "\n") if note else "") + (text or "")) or "（没有输出）",
                    "unread": True})
        self.running.pop(a["id"], None)
        self.tasks.pop(run["id"], None)
        view: dict = {}

        def fn(d):
            _patch_run(d, run)
            for x in d["automations"]:
                if x["id"] == a["id"]:
                    x["last_status"] = status
                    view.update(x)
            return self._unread(d)
        try:
            unread = self.store.update(fn)
        except Exception:  # noqa: BLE001
            log.error("保存自动化运行结果失败", exc_info=True)
            unread = 0
        log.info("自动化 %s（%s）运行结束：%s", a["name"], a["id"], status)
        if not view:                    # 运行中被删除：记录已随自动化删掉，不再推给桌面端
            return
        self._notify("automation/run_updated", {"run": dict(run)})
        self._notify("automation/updated", {"automation": self.view(view)})
        self._notify("automation/run_done", {"run": dict(run), "unread": unread, "automation": self.view(view)})

    async def wait_idle(self, timeout: float = 30) -> None:
        """等所有正在跑的运行结束（测试用）。"""
        t0 = time.monotonic()
        while self.tasks and time.monotonic() - t0 < timeout:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)


def _patch_run(d: dict, run: dict) -> None:
    for i, r in enumerate(d["runs"]):
        if r["id"] == run["id"]:
            d["runs"][i] = dict(run)
            return
    # 记录不在了：自动化已被删除（运行中删掉）就不再写回，免得留下没有归属的未读记录
    if any(a.get("id") == run.get("automation_id") for a in d["automations"]):
        d["runs"].append(dict(run))


# ------------------------------------------------------------------ RPC
def register(methods: dict, backend: "Backend") -> Automations:
    from ee.server.app import RpcError

    au = Automations(backend)

    def wrap(fn):
        async def call(**kw):
            try:
                return await asyncio.to_thread(fn, **kw)
            except KeyError:
                raise RpcError("自动化不存在（可能已被删除）", 404)
            except ValueError as e:
                raise RpcError(str(e))
        return call

    async def run_now(id: str) -> dict:  # noqa: A002
        try:
            return {"run": au.run_now(id)}
        except KeyError:
            raise RpcError("自动化不存在（可能已被删除）", 404)
        except ValueError as e:
            raise RpcError(str(e))

    def preview(schedule: dict, count: int = 3) -> dict:
        try:
            s = SC.normalize(schedule)
        except SC.ScheduleError as e:
            return {"ok": False, "error": str(e), "next_runs": [], "text": ""}
        now = au.clock()
        return {"ok": True, "error": "", "schedule": s, "text": SC.describe(s),
                "next_runs": SC.next_runs(s, now, max(1, min(10, int(count))), now if s["kind"] == "interval" else None)}

    methods.update({
        "automations/list": wrap(lambda: au.list()),
        "automations/create": wrap(lambda automation: {"automation": au.create(automation)}),
        "automations/update": wrap(lambda id, patch: {"automation": au.update(id, patch)}),  # noqa: A006
        "automations/delete": wrap(lambda id: au.delete(id)),  # noqa: A006
        "automations/run_now": run_now,
        "automations/runs": wrap(lambda automation_id=None, limit=100: au.runs(automation_id, limit)),
        "automations/mark_read": wrap(lambda run_ids=None, automation_id=None, thread_id=None, all=False:  # noqa: A002
                                      au.mark_read(run_ids, automation_id, thread_id, all)),
        "automations/preview": wrap(preview),
    })
    return au
