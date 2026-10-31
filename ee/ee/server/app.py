"""`ee serve`：桌面端的本地后端（app-server）。

- 只监听 127.0.0.1；连接地址 ws://127.0.0.1:<port>/?token=<token>，token 不对直接 401。
- 协议：JSON-RPC 2.0 风格。请求 {"id", "method", "params"} → 响应 {"id", "result"} 或 {"id", "error": {code, message}}；
  服务端推送 {"method", "params"}（没有 id），广播给所有连接。
- 启动成功后在 stdout 打印一行 `EE_SERVE_READY {"port": N}`，Electron 主进程读这一行拿端口。
- --exit-on-stdin-eof：stdin 关闭（父进程 Electron 退出）时自动退出，避免残留进程。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ee import __version__
from ee.core import reasoning as RZ
from ee.config import ROLES
from ee.config import ee_home, load_config
from ee.core.fileindex import FileIndex
from ee.core.ids import is_thread_id
from ee.core.permissions import MODE_LABEL, MODES
from ee.core.secrets import redact
from ee.core.store import SessionStore
from ee.server import files as F
from ee.server import gitops
from ee.server import branch, mcp_rpc, memory_rpc, prompts_rpc, review_rpc, rules_rpc, shell_rpc, worktree_rpc
from ee.server import checkpoint_rpc, hooks_rpc, skills_rpc
from ee.server import settings as S
from ee.server.threads import Thread

log = logging.getLogger("ee.server")
SLOW_RPC = 5.0     # 秒：超过就记一条日志
# 这些方法的 thread_id 是可选参数；显式传 None 表示“没有当前会话”。其余方法必须给合法 id。
OPTIONAL_THREAD_ID = {"budget/status", "automations/mark_read", "loop/list"}
# 同步做磁盘/索引 IO 的 RPC：放到工作线程，别卡事件循环（对应方法本身仍是同步函数，测试可直接调用）。
BLOCKING_RPCS = {"thread/delete", "thread/export"}


def _thread_id_optional(fn) -> bool:
    """处理函数自己把 thread_id 声明成可选（默认 None）时，null 就是“没有当前对话”，不算非法。"""
    try:
        p = inspect.signature(fn).parameters.get("thread_id")
    except (TypeError, ValueError):
        return False
    return p is not None and p.default is None


class RpcError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


class Backend:
    """与传输无关的后端：方法分发 + 线程管理。测试可以直接用它（不走 WebSocket）。"""

    def __init__(self, home: Path | None = None, client_factory: Callable | None = None, persist: bool = True,
                 user_home: Path | None = None):
        self.home = home or ee_home()
        # 真实主目录（技能兼容目录 ~/.agents/skills、~/.claude/skills 从这里找）。
        # 测试注入隔离目录，避免开发机上装的技能影响会话开头注入的消息数量。
        self.user_home = Path(user_home) if user_home is not None else Path.home()
        self.client_factory = client_factory
        self.persist = persist
        self.threads: dict[str, Thread] = {}
        # 指挥会话（D6-1 U6）：名额控制器首次派发时按父会话生效配置构造，此前为 None。
        self.slots = None
        from ee.server.orchestrator import Orchestrator
        self.orchestrator = Orchestrator(self)
        self.listeners: list[Callable[[str, dict], Any]] = []
        self._indexes: dict[str, tuple[float, FileIndex]] = {}
        self.methods: dict[str, Callable] = {
            "app/info": self.app_info,
            "thread/list": self.thread_list,
            "thread/search": self.thread_search,
            "thread/export": self.thread_export,
            "thread/start": self.thread_start,
            "thread/get": self.thread_get,
            "thread/rename": self.thread_rename,
            "thread/delete": self.thread_delete,
            # 编排（D6-1 U9）：服务面入口。工具那五个只给模型用，界面与端到端脚本
            # 拿不到子会话列表和真实改动数。
            "thread/children": self.thread_children,
            "thread/status": self.thread_status,
            "thread/wait": self.thread_wait,
            "thread/create_child": self.thread_create_child,
            "thread/undo": self.thread_undo,
            "thread/compact": self.thread_compact,
            "thread/context_breakdown": self.thread_context_breakdown,
            "turn/start": self.turn_start,
            "turn/interrupt": self.turn_interrupt,
            "turn/withdraw": self.turn_withdraw,
            "approval/respond": self.approval_respond,
            "question/respond": self.question_respond,
            "settings/get": self.settings_get,
            "settings/set": self.settings_set,
            "models/list": self.models_list,
            "workspace/list": self.workspace_list,
            "workspace/open": self.workspace_open,
            "workspace/remove": self.workspace_remove,
            "files/search": self.files_search,
            "files/tree": self.files_tree,
            "files/read": self.files_read,
            "terminal/shell": self.terminal_shell,
            "git/diff": self.git_diff,
            "git/revert_file": self.git_revert_file,
        }
        memory_rpc.register(self.methods, self.home)   # memory/get|add|remove|save
        prompts_rpc.register(self.methods, self.home)  # prompts/list|expand
        branch.register(self.methods, self)            # thread/fork|edit_resend
        mcp_rpc.register(self.methods, self)           # mcp/list|test
        review_rpc.register(self.methods, self)        # review/targets|start, git/commit_message|commit
        shell_rpc.register(self.methods, self)         # shell/list|read|kill（后台命令，D4-6）
        rules_rpc.register(self.methods, self)         # permissions/list|remove（永久允许规则，D4-8）
        worktree_rpc.register(self.methods, self)      # worktree/list|create|diff|merge|remove（D5-1）
        from ee.server import handoff_rpc
        handoff_rpc.register(self.methods, self)        # handoff/manifest|verify（T10-e）
        from ee.server import bestof_rpc
        bestof_rpc.register(self.methods, self)         # bestof/pick（T10-d）
        from ee.server import bestof_verify
        bestof_verify.register(self.methods, self)      # bestof/verify（T10-d 判据编排）
        checkpoint_rpc.register(self.methods, self)    # checkpoint/list|diff|restore|gc、review/hunk（D5-7）
        skills_rpc.register(self.methods, self.home)   # skills/list|read|dir（技能，D5-5）
        hooks_rpc.register(self.methods, self)         # hooks/list|test（D5-4）
        from ee.server import sysprompt_rpc
        sysprompt_rpc.register(self.methods, self)     # sysprompt/info|preview|trust|untrust|set（SYSTEM.md）
        from ee.server import answer_rpc
        answer_rpc.register(self.methods, self)        # thread/options、styles/list、budget/status、reasoning/*（D5-9）
        from ee.server import automations as automations_rpc
        self.automations = automations_rpc.register(self.methods, self)   # automations/*（自动化 D4-10）
        from ee.server import loops as loops_rpc
        self.loops = loops_rpc.register(self.methods, self)               # loop/*（会话内 /loop，D5-3）

    # ------------------------------------------------------------ 通知
    def notify(self, method: str, params: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(method, params)
            except Exception:  # noqa: BLE001
                pass

    async def call(self, method: str, params: dict | None) -> Any:
        fn = self.methods.get(method)
        if fn is None:
            raise RpcError(f"未知方法：{method}", -32601)
        params = params or {}
        if not isinstance(params, dict):
            raise RpcError("params 必须是对象", -32602)
        if "thread_id" in params:
            tid = params["thread_id"]
            if not (tid is None and (method in OPTIONAL_THREAD_ID or _thread_id_optional(fn))) and not is_thread_id(tid):
                raise RpcError("参数错误：thread_id 不合法", -32602)
        try:
            if method in BLOCKING_RPCS:
                res = await asyncio.to_thread(fn, **params)
            else:
                res = fn(**params)
                if asyncio.iscoroutine(res):
                    res = await res
        except TypeError as e:
            raise RpcError(f"参数错误：{e}", -32602)
        return res

    # ------------------------------------------------------------ 持久化的小状态（工作区列表）
    @property
    def _desktop_file(self) -> Path:
        return self.home / "desktop.json"

    def _desktop(self) -> dict:
        try:
            return json.loads(self._desktop_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_desktop(self, d: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        from ee.core.atomic import write_text as atomic_write
        atomic_write(self._desktop_file, json.dumps(d, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------ 线程
    def _check_thread_id(self, thread_id: object) -> None:
        if not is_thread_id(thread_id):
            raise RpcError("参数错误：thread_id 不合法", -32602)

    async def _athread(self, thread_id: str) -> Thread:
        """取线程；不在内存里时在工作线程里读会话文件（大文件不卡事件循环）。

        同一个会话的加载是单飞的：两个请求同时进来时，后来者等第一个读完再用同一份 Thread。
        否则会各自读一次文件、各建一个 Thread/Session（后写入的覆盖先写入的），两边就分叉了。
        """
        self._check_thread_id(thread_id)
        t = self.threads.get(thread_id)
        if t is not None:
            return t
        locks = getattr(self, "_load_locks", None)
        if locks is None:
            locks = self._load_locks = {}
        lock = locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            t = self.threads.get(thread_id)          # 等锁期间可能已被前一个请求建好
            if t is not None:
                return t
            t = await asyncio.to_thread(self._thread, thread_id)
            return self.threads.setdefault(thread_id, t)

    def _thread(self, thread_id: str) -> Thread:
        self._check_thread_id(thread_id)
        t = self.threads.get(thread_id)
        if t is not None:
            return t
        path = self.home / "sessions" / f"{thread_id}.jsonl"
        if not path.is_file():
            raise RpcError(f"会话不存在：{thread_id}", 404)
        d = SessionStore.load(path)
        meta = d["meta"]
        if not meta.get("cwd") or not Path(meta["cwd"]).is_dir():
            meta["cwd"] = meta.get("cwd") or str(Path.home())
        t = Thread(thread_id, self.home, self.notify, meta, messages=d["messages"], items=d["items"],
                   client_factory=self.client_factory, persist=self.persist, user_home=self.user_home,
                   orchestrator=self.orchestrator)
        self.threads[thread_id] = t
        return t

    async def app_info(self) -> dict:
        from ee import log as eelog
        cfg = await asyncio.to_thread(load_config, Path.home())
        return {"version": __version__, "home": str(self.home), "pid": os.getpid(), "platform": sys.platform,
                "frozen": bool(getattr(sys, "frozen", False)), "log_file": str(eelog.log_dir() / eelog.LOG_NAME),
                "default_model": cfg.default_model, "modes": [{"id": m, "label": MODE_LABEL[m]} for m in MODES]}

    async def thread_list(self, cwd: str | None = None, query: str | None = None, limit: int = 200) -> dict:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise RpcError("参数错误：limit 必须是非负整数", -32602)
        if cwd is not None and not isinstance(cwd, (str, os.PathLike)):
            raise RpcError("参数错误：cwd 必须是字符串", -32602)
        if query is not None and not isinstance(query, str):
            raise RpcError("参数错误：query 必须是字符串", -32602)
        return await asyncio.to_thread(self._thread_list, cwd, query, limit)

    def _thread_list(self, cwd: str | None = None, query: str | None = None, limit: int = 200) -> dict:
        seen: dict[str, dict] = {}
        for t in self.threads.values():
            seen[t.id] = t.summary()
        for p in SessionStore.list_sessions(self.home)[: max(limit * 2, 400)]:
            if p.stem in seen or not is_thread_id(p.stem):
                continue
            try:
                seen[p.stem] = {**SessionStore.summary(p), "status": "idle"}
            except Exception:  # noqa: BLE001
                continue
        out = list(seen.values())
        if cwd:
            norm = os.path.normcase(os.path.abspath(cwd))
            out = [t for t in out if t.get("cwd") and os.path.normcase(os.path.abspath(t["cwd"])) == norm]
        if query:
            q = query.lower()
            out = [t for t in out if q in (t.get("title") or "").lower()]
        out.sort(key=lambda t: t.get("updated") or 0, reverse=True)
        return {"threads": out[:limit]}

    async def thread_search(self, query: str, cwd: str | None = None, limit: int = 50) -> dict:
        return await asyncio.to_thread(self._thread_search, query, cwd, limit)

    def _thread_search(self, query: str, cwd: str | None = None, limit: int = 50) -> dict:
        """全文搜索会话内容（D4-4）：返回命中的会话摘要 + 每个会话最多 3 条命中片段。"""
        from ee.core.search_index import get_index
        groups = get_index(self.home).search(query, limit=limit * 2)
        norm = os.path.normcase(os.path.abspath(cwd)) if cwd else None
        out = []
        for g in groups:
            sid = g["thread_id"]
            if not is_thread_id(sid):
                continue
            t = self.threads.get(sid)
            try:
                summ = t.summary() if t is not None else {
                    **SessionStore.summary(self.home / "sessions" / f"{sid}.jsonl"), "status": "idle"}
            except Exception:  # noqa: BLE001
                continue
            if norm and not (summ.get("cwd") and os.path.normcase(os.path.abspath(summ["cwd"])) == norm):
                continue
            out.append({"thread": summ, "hits": g["hits"]})
            if len(out) >= limit:
                break
        return {"results": out}

    async def thread_start(self, cwd: str, model: str | None = None, mode: str | None = None,
                           worktree: bool | dict | None = None) -> dict:
        if not isinstance(cwd, (str, os.PathLike)):
            raise RpcError("参数错误：cwd 必须是字符串", -32602)
        if mode and mode not in MODES:
            raise RpcError(f"未知权限模式：{mode}")

        def build() -> Thread:   # 工作线程里：目录检查、git status、shell 探测、写会话文件
            p = Path(cwd).expanduser()
            if not p.is_dir():
                raise RpcError(f"目录不存在：{cwd}")
            wt = None
            if worktree:          # 在新 worktree 里开对话（D5-1）
                from ee.core import worktree as W
                opts = worktree if isinstance(worktree, dict) else {}
                try:
                    wt = W.create(p, opts.get("name"), opts.get("base"), self.home)
                except W.WorktreeError as e:
                    raise RpcError(str(e))
                p = Path(wt["path"])
            else:                 # 直接在已有的 ee worktree 目录里开对话，也记上 worktree 信息
                try:
                    from ee.core import worktree as W
                    wt = W.info_for_dir(p, self.home)
                except Exception:  # noqa: BLE001
                    wt = None
            store = SessionStore(self.home)
            meta = {"cwd": str(p.resolve()), "model": model or "", "mode": mode or "", "title": "新对话",
                    "created": time.time()}
            if wt:
                meta["worktree"] = {"path": wt["path"], "branch": wt["branch"], "repo": wt["repo"], "name": wt["name"]}
            t = Thread(store.id, self.home, self.notify, meta, client_factory=self.client_factory,
                       persist=self.persist, user_home=self.user_home, orchestrator=self.orchestrator)
            try:
                t.ensure_session()
            except ValueError as e:
                raise RpcError(f"配置错误：{e}")
            t.save_meta()
            self._touch_workspace(meta["worktree"]["repo"] if meta.get("worktree") else meta["cwd"])
            return t

        t = await asyncio.to_thread(build)
        self.threads[t.id] = t
        log.info("新会话 %s cwd=%s 模型 %s", t.id, t.meta.get("cwd"), t.meta.get("model"))
        self.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.detail()}

    async def thread_get(self, thread_id: str) -> dict:
        t = await self._athread(thread_id)
        try:
            await t.ensure_session_async()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        return {"thread": t.detail()}

    # ------------------------------------------------------------ 导出（T10-a U2）
    async def thread_export(self, thread_id: str, format: str = "markdown",
                            path: str | None = None, overwrite: bool = False) -> dict:
        """把一个会话导出成 Markdown / JSON。

        `path` 缺省时**不落盘**，只返回内容（前端可预览/复制）——导入导出这类动作
        不该在用户没指定位置时偷偷往磁盘写东西。给了 path 才写。
        """
        return await asyncio.to_thread(self._thread_export, thread_id, format, path, overwrite)

    def _thread_export(self, thread_id: str, fmt: str, path: str | None,
                       overwrite: bool) -> dict:
        from ee.core.export import FORMATS, export

        self._check_thread_id(thread_id)
        if fmt not in FORMATS:
            raise RpcError(f"参数错误：未知导出格式 {fmt!r}（可选：{', '.join(FORMATS)}）", -32602)

        sess = self.home / "sessions" / f"{thread_id}.jsonl"
        if not sess.is_file():
            raise RpcError(f"会话不存在：{thread_id}", 404)

        # 直接从文件读：导出是只读动作，不该为此把会话加载进内存线程池，
        # 也不该让一个正在运行的回合影响导出内容的一致性。
        from ee.core.store import SessionStore
        content = export(SessionStore.load(sess), fmt)

        out_path = None
        if path:
            target = Path(path)
            if not target.is_absolute():
                raise RpcError("参数错误：导出路径必须是绝对路径", -32602)
            if target.is_dir():
                raise RpcError("参数错误：导出路径是目录，不是文件", -32602)
            if target.exists() and not overwrite:
                # 不静默覆盖：用户的东西不能被一次导出抹掉
                raise RpcError(f"目标文件已存在：{target}", -32602)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, content)
            out_path = str(target)

        return {"format": fmt, "content": content, "path": out_path}

    async def thread_rename(self, thread_id: str, title: str) -> dict:
        t = await self._athread(thread_id)
        t.meta["title"] = title.strip() or "新对话"
        t.save_meta()
        self.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.summary()}

    # ------------------------------------------------------------ 编排 RPC（D6-1 U9）
    async def thread_children(self, parent_id: str) -> dict:
        """列出某个会话派发的子会话。

        父会话不存在时报错而不是返回空列表：空列表会被读成"没有子会话"，
        与"父会话写错了"混为一谈。

        async + to_thread：list_children 会逐个 SessionStore.summary() 解析会话文件。
        同步跑会把整个事件循环卡住（所有窗口的流式 token 一起停），而前端对每个
        子会话并发调用，等于 N 次串行等待（报告 P1-9）。
        """
        from ee.server.orchestrator import OrchestratorError
        self._check_thread_id(parent_id)
        if self.threads.get(parent_id) is None and not (self.home / "sessions" / f"{parent_id}.jsonl").is_file():
            raise RpcError(f"会话不存在：{parent_id}", 404)
        try:
            rows = await asyncio.to_thread(self.orchestrator.list_children, parent_id)
            return {"threads": rows}
        except OrchestratorError as e:
            raise RpcError(str(e), -32602) from None

    async def thread_status(self, parent_id: str, child_id: str) -> dict:
        """单个子会话的详细状态（含 worktree 真实改动数，供界面卡片用）。

        async + to_thread：状态里有 git status 子进程（worktree 或 cwd 两条路径），
        同步跑会卡住事件循环（报告 P1-9）。
        """
        from ee.server.orchestrator import OrchestratorError
        self._check_thread_id(parent_id)
        self._check_thread_id(child_id)
        try:
            row = await asyncio.to_thread(self.orchestrator.status, parent_id, child_id)
            return {"thread": row}
        except OrchestratorError as e:
            raise RpcError(str(e), -32602) from None

    async def thread_wait(self, parent_id: str, child_ids: list[str],
                          timeout: float | None = None) -> dict:
        """等这些子会话都不在运行（或超时）。取消等待不等于停止子任务。"""
        from ee.server.orchestrator import OrchestratorError
        self._check_thread_id(parent_id)
        if not isinstance(child_ids, list) or not child_ids:
            raise RpcError("参数错误：child_ids 必须是非空数组", -32602)
        for cid in child_ids:
            self._check_thread_id(cid)
        try:
            return await self.orchestrator.wait(parent_id, child_ids, timeout)
        except OrchestratorError as e:
            raise RpcError(str(e), -32602) from None

    async def thread_create_child(self, parent_id: str, title: str = "", prompt: str | None = None,
                                  mode: str | None = None, worktree: bool = True) -> dict:
        """从服务面派发子会话（D6-1 U9）。

        与模型用的 `thread_create` 工具走同一个 `create_child`，四条不变量一条不少：
        父关系由服务端写、共享 cwd 只能只读、名额在发起运行时预留、子会话不能再建孙。
        这里只是多一个入口，不放宽任何判定。
        """
        from ee.server.orchestrator import OrchestratorError
        self._check_thread_id(parent_id)
        if mode and mode not in MODES:
            raise RpcError(f"未知权限模式：{mode}")
        try:
            return await self.orchestrator.create_child(parent_id, title=title, prompt=prompt,
                                                        mode=mode, worktree=bool(worktree))
        except OrchestratorError as e:
            raise RpcError(str(e), -32602) from None

    def _detach_children(self, thread_id: str) -> None:
        """删除父会话时把子会话的父关系解开，**不级联删子会话、也不碰它的 worktree**（D6-1 U7）。

        三条理由：
        * 级联删会连带毁掉子会话 worktree 里还没合并的改动，而用户删的可能只是"这个话题"。
        * 留着 `orchestrator_parent_id` 就会留下一个指向已删会话的悬空子级：它既不能再被
          派发（父会话已经不在了，`_own_parent` 会拒绝），也不再出现在任何父会话的列表里。
        * 子会话本身仍然是一个正常会话，用户还能打开它、续跑、自己合并 worktree。

        内存里和磁盘上都要处理：重启后没点开过的子会话只在磁盘上，只改内存会留下永久孤儿。
        磁盘部分直接追加一条 meta 记录（会话是只追加的 jsonl，后写的覆盖先写的）。
        """
        for t in list(self.threads.values()):
            if t.meta.get("orchestrator_parent_id") != thread_id:
                continue
            t.meta.pop("orchestrator_parent_id", None)
            t.meta.pop("orchestrator_role", None)
            t.save_meta()
            self.notify("thread/updated", {"thread": t.summary()})
        for p in SessionStore.list_sessions(self.home):
            if not is_thread_id(p.stem) or p.stem == thread_id or p.stem in self.threads:
                continue                       # 已删的那个、以及上面处理过的内存会话
            try:
                s = SessionStore.summary(p)
            except Exception:  # noqa: BLE001  单个坏文件不影响删除
                continue
            if s.get("orchestrator_parent_id") != thread_id:
                continue
            # 直接追加一条 meta：会话是只追加的 jsonl，后写的覆盖先写的，其余元数据不受影响
            SessionStore(self.home, session_id=p.stem).meta({"orchestrator_parent_id": None,
                                                              "orchestrator_role": None})

    def thread_delete(self, thread_id: str) -> dict:
        self._check_thread_id(thread_id)
        sessions = (self.home / "sessions").resolve()
        path = sessions / f"{thread_id}.jsonl"
        extra = sessions / thread_id
        # 双保险：即使上面的白名单被绕过，也绝不允许删 sessions 目录本身或它的父目录
        for p in (path, extra):
            try:
                if p.resolve().parent != sessions:
                    raise RpcError("参数错误：thread_id 不合法", -32602)
            except OSError:
                raise RpcError("参数错误：thread_id 不合法", -32602) from None
        if extra.resolve() == sessions:
            raise RpcError("参数错误：thread_id 不合法", -32602)
        self.loops.remove_thread(thread_id)              # 会话内 /loop 一并停止
        self.orchestrator.forget(thread_id)              # 子会话被删：名额占用一起回收，否则 16 个后建不出新的
        self._detach_children(thread_id)                 # 子会话留下（含 worktree），只解开父关系（D6-1 U7）
        t = self.threads.pop(thread_id, None)
        if t is not None:
            # 先标记：还在跑的回合收尾时不能再把 jsonl 写回来（否则删除后“复活”成残记录）
            t.deleted = True
        if t is not None and t.running:
            t.interrupt()
        if t is not None and t.session is not None:      # 会话删除：杀掉它的后台命令（D4-6）
            try:
                asyncio.get_running_loop().create_task(t.session.bg.close())
            except RuntimeError:
                t.session.bg.kill_all_sync()
            # 连带释放 HTTP 连接池，否则删掉的会话会一直占着连接
            aclose = getattr(t.session, "aclose", None)
            if aclose is not None:
                try:
                    asyncio.get_running_loop().create_task(aclose())
                except RuntimeError:
                    pass                             # 没有事件循环：随进程退出释放
        if path.is_file():
            path.unlink()
        if extra.is_dir():
            import shutil
            shutil.rmtree(extra, ignore_errors=True)
        try:
            from ee.core.search_index import get_index
            get_index(self.home).forget(thread_id)
        except Exception:  # noqa: BLE001
            pass
        self.notify("thread/deleted", {"thread_id": thread_id})
        return {"ok": True}

    async def thread_undo(self, thread_id: str, force: bool = False) -> dict:
        """/undo。要恢复的文件在回合后被用户改过时返回 {needs_confirm, conflicts}、不动文件；
        前端确认后带 force=true 再调：先把当前工作区存成恢复点（时间线里可反悔）再恢复。"""
        from ee.core.session import RestoreConflict
        from ee.server.checkpoint_rpc import add_restore_point, restore_point_view
        t = await self._athread(thread_id)
        s = await t.ensure_session_async()
        if s.running:
            raise RpcError("运行中不能撤销")
        # 同一 cwd 的其它会话正在跑时也不能撤销：undo 会按文件内容把工作区整体回滚，
        # 对方正在写的东西会被一起盖掉，而双方都不知道（多标签 / ee exec 并行）。
        cwd = str(t.meta.get("cwd") or "")
        if cwd:
            busy = [k for k, o in self.threads.items()
                    if k != thread_id and o.running and str(o.meta.get("cwd") or "") == cwd]
            if busy:
                raise RpcError(f"同一个工作区还有 {len(busy)} 个会话正在运行，撤销会覆盖它们的改动。"
                               f"等它们结束再试。")
        box: dict = {}

        def keep(tree: str) -> None:
            box["rp"] = add_restore_point(t, tree, "执行 /undo 前的文件（含你手改的内容）")

        from ee.core.snapshot import RestoreBlocked, RestorePartial
        try:
            restored = await asyncio.to_thread(s.undo, bool(force), keep)
        except RestoreConflict as e:
            return {"restored": [], "needs_confirm": True, "conflicts": e.conflicts}
        except RestoreBlocked as e:          # 预检没过：一个文件都没动
            raise RpcError(f"撤销失败：{e}\n关掉占用这些文件的程序后再撤销一次。")
        except RestorePartial as e:          # 恢复到一半：如实报告，这一回合不标已撤销，可再撤一次
            t._add({"type": "notice", "level": "warn",
                    "text": f"撤销只完成了一部分：{e}\n已恢复：\n" + "\n".join(e.restored)
                            + "\n关掉占用这些文件的程序后再撤销一次即可补上。"})
            self.notify("git/changed", {"cwd": t.meta.get("cwd")})
            return {"restored": e.restored, "failed": [{"path": p, "reason": why} for p, why in e.failed],
                    "partial": True, "restore_point": restore_point_view(box.get("rp"))}
        if s.last_undo_tree:
            t.mark_undone(s.last_undo_tree)
        if restored:
            t._add({"type": "notice", "text": "已撤销上一回合的文件改动：\n" + "\n".join(restored), "level": "info"})
            self.notify("git/changed", {"cwd": t.meta.get("cwd")})
        return {"restored": restored, "restore_point": restore_point_view(box.get("rp"))}

    async def thread_compact(self, thread_id: str) -> dict:
        t = await self._athread(thread_id)
        if t.running or t.compacting:
            raise RpcError("正在运行中，等这一回合结束再压缩")
        try:
            s = await t.ensure_session_async()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        if s.running:
            raise RpcError("正在运行中，等这一回合结束再压缩")
        t.compacting = True
        self.notify("thread/updated", {"thread": t.summary()})
        try:
            res = await s.compact(emit=t._on_event)
        finally:
            t.compacting = False
        t.context_left = round(s.context_left(), 4)
        if res.get("ok"):
            t.save_meta()
        self.notify("thread/usage", {"thread_id": t.id, "usage": t.usage, "context_left": t.context_left})
        self.notify("thread/updated", {"thread": t.summary()})
        return {"ok": bool(res.get("ok")), "message": res.get("message", ""),
                "tokens_before": res.get("tokens_before", 0), "tokens_after": res.get("tokens_after", 0),
                "context_left": t.context_left}

    async def thread_context_breakdown(self, thread_id: str) -> dict:
        """上下文构成（A12）：各部分 token 与占比、修剪/压缩节省。只读，不冻结前缀、不动历史。"""
        from ee.core import breakdown
        t = await self._athread(thread_id)
        try:
            s = await t.ensure_session_async()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        return await asyncio.to_thread(breakdown.for_session, s)

    async def turn_start(self, thread_id: str, text: str, model: str | None = None, mode: str | None = None,
                         attachments: list | None = None, effort: str | None = None, style: str | None = None,
                         queue: str | None = None, goal: dict | None = None) -> dict:
        if text is not None and not isinstance(text, str):
            raise RpcError("参数错误：text 必须是字符串", -32602)
        if not (text or "").strip() and not attachments:
            raise RpcError("消息不能为空")
        text = text or ""
        t = await self._athread(thread_id)
        if t.compacting:
            raise RpcError("正在压缩上下文，稍等片刻再发送")
        try:
            await t.ensure_session_async()   # 首次建会话要跑 git 等子进程：放到工作线程
            images, meta = [], []
            if attachments:
                if t.running:
                    raise RpcError("ee 正在工作，等这一回合结束再发图片")
                from ee.core.attachments import save_all
                images, meta = await asyncio.to_thread(save_all, self.home, thread_id, list(attachments))
            log.info("turn/start 会话 %s：%d 字，%d 张图片", thread_id, len(text), len(images))
            return t.start_turn(text or "（见图片）", model=model, mode=mode, images=images, attachments=meta,
                                effort=effort, style=style, queue=queue, goal=goal)
        except ValueError as e:
            raise RpcError(str(e))

    async def turn_withdraw(self, thread_id: str, item_id: str) -> dict:
        """撤回还没送入的排队消息（插话 / 待追加），返回 {text, queue}。"""
        t = await self._athread(thread_id)
        try:
            return t.withdraw(item_id)
        except ValueError as e:
            raise RpcError(str(e))

    async def turn_interrupt(self, thread_id: str) -> dict:
        t = await self._athread(thread_id)
        return {"interrupted": t.interrupt()}

    async def approval_respond(self, thread_id: str, call_id: str, choice: str, reason: str = "") -> dict:
        t = await self._athread(thread_id)
        try:
            return t.respond(call_id, choice, reason)
        except ValueError as e:
            raise RpcError(str(e))

    async def question_respond(self, thread_id: str, call_id: str, answer: str) -> dict:
        t = await self._athread(thread_id)
        try:
            return t.answer(call_id, answer)
        except ValueError as e:
            raise RpcError(str(e))

    # ------------------------------------------------------------ 设置 / 模型
    async def settings_get(self) -> dict:
        return await asyncio.to_thread(S.get_settings)

    async def settings_set(self, settings: dict) -> dict:
        try:
            res = await asyncio.to_thread(S.set_settings, settings)
        except ValueError as e:
            raise RpcError(str(e))
        log.info("设置已保存：默认模型 %s", (res.get("settings") or {}).get("default_model"))
        for t in self.threads.values():
            t.reload_config()
        self.notify("settings/changed", res)
        return res

    async def models_list(self, cwd: str | None = None) -> dict:
        return await asyncio.to_thread(self._models_list, cwd)

    def _models_list(self, cwd: str | None = None) -> dict:
        cfg = load_config(Path(cwd) if cwd and Path(cwd).is_dir() else Path.home())
        out = []
        for name in cfg.model_names():
            try:
                spec = cfg.resolve_model(name)
            except ValueError:
                continue
            out.append({"name": name, "provider": spec.provider, "model": spec.model,
                        "family": spec.profile.family, "context_window": spec.profile.context_window,
                        "reasoning": spec.profile.reasoning, "vision": spec.profile.vision,
                        "effort_support": RZ.support(spec.profile),           # D5-9：{effort, off, levels, labels}
                        "effort_values": dict(spec.profile.effort_values or {}),
                        "has_key": bool(spec.api_key()),
                        "price": vars(spec.profile.price)})
        return {"models": out, "default": cfg.default_model,
                "roles": {r: (cfg.data.get("roles") or {}).get(r, "") for r in ROLES}}

    # ------------------------------------------------------------ 工作区
    def _touch_workspace(self, path: str) -> None:
        d = self._desktop()
        # 重新打开 = 明确要它，撤掉之前的隐藏标记
        target = os.path.normcase(os.path.abspath(path))
        d["hidden_workspaces"] = [h for h in d.get("hidden_workspaces", [])
                                  if os.path.normcase(os.path.abspath(h)) != target]
        ws = [w for w in d.get("workspaces", []) if os.path.normcase(os.path.abspath(w["path"])) != target]
        ws.insert(0, {"path": path, "name": Path(path).name or path, "opened": time.time()})
        d["workspaces"] = ws[:50]
        self._save_desktop(d)

    async def workspace_list(self) -> dict:
        return await asyncio.to_thread(self._workspace_list)

    def _workspace_list(self) -> dict:
        # 项目列表要把「历史会话里出现过的目录」一并算进来。
        #
        # 为什么：侧栏的项目分组是从 thread 的 cwd 推导的（Sidebar 的 groups），
        # 而 desktop.json 里的 workspaces 只记"用户显式加过的"。两者不一致时会出现
        # 「界面看得到、但移除不掉、也打不开」的幽灵项目——移除删的是 desktop.json
        # （里面本来没有），openPath 的白名单也只读 desktop.json（于是被拒）。
        # 这里把两端对齐：凡是能看见的目录，就出现在 workspace/list 里。
        d = self._desktop()
        seen: dict[str, dict] = {}
        for w in d.get("workspaces", []):
            seen[os.path.normcase(os.path.abspath(w["path"]))] = dict(w)

        # 从容器的会话 + 磁盘上的历史会话收集 cwd
        cwds: set[str] = set()
        for t in self.threads.values():
            try:
                if t.meta.get("cwd"):
                    cwds.add(str(t.cwd))
            except Exception:  # noqa: BLE001
                continue
        for p in SessionStore.list_sessions(self.home)[:400]:
            if not is_thread_id(p.stem):
                continue
            try:
                c = (SessionStore.summary(p) or {}).get("cwd")
                if c:
                    cwds.add(str(c))
            except Exception:  # noqa: BLE001
                continue

        for c in cwds:
            key = os.path.normcase(os.path.abspath(c))
            if key in seen:
                continue
            seen[key] = {"path": c, "name": Path(c).name or c, "opened": 0, "from_threads": True}

        ws = list(seen.values())
        # 用户点过「从列表移除」的目录不再出现（含从会话推导出来的那些）
        hidden = {os.path.normcase(os.path.abspath(h)) for h in d.get("hidden_workspaces", []) if h}
        if hidden:
            ws = [w for w in ws if os.path.normcase(os.path.abspath(w["path"])) not in hidden]
        for w in ws:
            w["exists"] = Path(w["path"]).is_dir()
        return {"workspaces": ws}

    async def workspace_open(self, path: str) -> dict:
        if not isinstance(path, (str, os.PathLike)):
            raise RpcError("参数错误：path 必须是字符串", -32602)
        def run():
            p = Path(path).expanduser()
            if not p.is_dir():
                raise RpcError(f"目录不存在：{path}")
            self._touch_workspace(str(p.resolve()))
            return self._workspace_list()
        return await asyncio.to_thread(run)

    async def workspace_remove(self, path: str) -> dict:
        def run():
            # 「从列表移除」= 记进 hidden：不再出现在 workspace/list。
            # 为什么要 hidden 而不是直接删：项目列表现在把历史会话目录也合并进来了，
            # 光删 desktop.json 里的条目，下次 list 又会从会话里把它捞回来，
            # 用户看到的就是"点了移除没反应"。
            d = self._desktop()
            target = os.path.normcase(os.path.abspath(path))
            d["workspaces"] = [w for w in d.get("workspaces", [])
                               if os.path.normcase(os.path.abspath(w["path"])) != target]
            hidden = [h for h in d.get("hidden_workspaces", [])
                      if os.path.normcase(os.path.abspath(h)) != target]
            hidden.append(path)
            d["hidden_workspaces"] = hidden[-200:]
            self._save_desktop(d)
            return self._workspace_list()
        return await asyncio.to_thread(run)

    async def files_search(self, cwd: str, query: str = "", limit: int = 20) -> dict:
        if not isinstance(cwd, (str, os.PathLike)):
            raise RpcError("参数错误：cwd 必须是字符串", -32602)
        if not isinstance(query, str):
            raise RpcError("参数错误：query 必须是字符串", -32602)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise RpcError("参数错误：limit 必须是非负整数", -32602)
        key = os.path.normcase(os.path.abspath(cwd))
        hit = self._indexes.get(key)
        if hit is None or time.time() - hit[0] > 30:
            idx = FileIndex(Path(cwd))
            await asyncio.to_thread(idx.build)
            self._indexes[key] = (time.time(), idx)
        else:
            idx = hit[1]
        return {"files": await asyncio.to_thread(idx.search, query, n=limit)}

    async def files_tree(self, cwd: str, path: str = "", show_ignored: bool = False) -> dict:
        try:
            return await asyncio.to_thread(F.tree, cwd, path, show_ignored)
        except F.FilesError as e:
            raise RpcError(str(e))

    async def files_read(self, cwd: str, path: str, max_bytes: int = F.MAX_TEXT) -> dict:
        try:
            return await asyncio.to_thread(F.read, cwd, path, max_bytes)
        except F.FilesError as e:
            raise RpcError(str(e))

    async def terminal_shell(self, cwd: str | None = None) -> dict:
        return await asyncio.to_thread(self._terminal_shell, cwd)

    def _terminal_shell(self, cwd: str | None = None) -> dict:
        """内置终端用哪个 shell：和 agent 的 shell 设置一致（auto / pwsh / powershell / git-bash / cmd / bash）。"""
        from ee.tools.shellenv import ShellNotFound, resolve_shell
        cfg = load_config(Path(cwd) if cwd and Path(cwd).is_dir() else Path.home())
        name = cfg.data.get("shell", "auto")
        notice = None
        try:
            sh = resolve_shell(name)
        except ShellNotFound as e:
            notice = str(e)
            try:
                sh = resolve_shell("auto")
            except ShellNotFound as e2:
                return {"setting": name, "kind": None, "exe": None, "display": None, "notice": str(e2)}
        return {"setting": name, "kind": sh.kind, "exe": sh.exe, "display": sh.display, "notice": notice}

    # ------------------------------------------------------------ git
    async def git_diff(self, cwd: str, thread_id: str | None = None) -> dict:
        """改动审阅。git 项目：相对 HEAD；非 git 项目且给了 thread_id：会话开始的磁盘快照 → 当前（D5-7）。"""
        if not Path(cwd).is_dir():
            raise RpcError(f"目录不存在：{cwd}")
        res = await asyncio.to_thread(gitops.review, Path(cwd), self.home)
        if res["is_repo"] or not thread_id or not res["git_available"]:
            return res
        t, s, d, base = await self.snapshot_review_ctx(thread_id)
        if d is None:
            ds = s._disk_obj
            res["snapshot"] = {"available": False, "reason": ds.disabled if ds is not None and ds.disabled else "磁盘快照已关闭"}
            return res
        if not base:
            res["snapshot"] = {"available": True, "empty": True}
            return res
        try:
            snap = await asyncio.to_thread(gitops.snapshot_review, d, base, self.home)
        except Exception as e:  # noqa: BLE001
            raise RpcError(f"快照对比失败：{e}")
        first = next((it for it in t.items if it.get("type") == "user" and it.get("tree") == base), {})
        res.update(files=snap["files"], additions=snap["additions"], deletions=snap["deletions"],
                   snapshot={"available": True, "base": base, "since": first.get("ts")})
        return res

    async def git_revert_file(self, cwd: str, path: str, thread_id: str | None = None) -> dict:
        from ee.core import gitinfo
        is_repo = (await asyncio.to_thread(gitinfo.status, Path(cwd))).is_repo if Path(cwd).is_dir() else False
        try:
            if is_repo or not thread_id:
                res = await asyncio.to_thread(gitops.revert_file, Path(cwd), path)
            else:                          # 非 git 项目：恢复成会话开始时快照里的版本（D5-7）
                t, s, d, base = await self.snapshot_review_ctx(thread_id)
                if d is None or not base:
                    raise RpcError("这个会话还没有快照，无法撤销")
                if t.running:
                    raise RpcError("运行中不能改文件，先停止这一回合")
                done = await asyncio.to_thread(d.restore, base, [path])
                if not done:
                    raise RpcError(f"路径不在工作区内：{path}")
                res = {"path": path, "action": "restored" if Path(done[0]).exists() else "deleted"}
        except ValueError as e:
            raise RpcError(str(e))
        self.forget_reads(cwd)
        return res

    def forget_reads(self, cwd: str) -> None:
        """工作区文件被界面改了：清掉同一目录会话的已读缓存，让模型下次重新 read。"""
        for t in self.threads.values():
            if t.session is not None and os.path.normcase(str(t.cwd)) == os.path.normcase(str(cwd)):
                t.session.state.files.clear()


# ---------------------------------------------------------------- WebSocket 传输

EXIT_GRACE = 10.0   # 秒：桌面端没了以后，正常清理最多等这么久，超时就强制结束子进程并退出


def _atomic_write(target: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 rename。

    直接 open(w) 中途失败会留下半截文件——导出场景下用户看到的是一个
    「能打开但内容缺了」的会话记录，比没有文件更糟。同名临时文件 + os.replace
    在 Windows 上也是原子的（同卷）。
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def take_serve_token(token: str | None = None) -> str | None:
    """取连接口令：显式传入的优先，否则读环境变量 EE_SERVE_TOKEN。

    无论用哪个，都把 EE_SERVE_TOKEN 从 os.environ 删掉：子进程（shell、后台命令、MCP、hooks）默认
    继承本进程环境，模型跑的命令拿到口令就能连 WebSocket 自己批准审批。
    """
    env_tok = os.environ.pop("EE_SERVE_TOKEN", None)
    return token or env_tok


async def serve(host: str = "127.0.0.1", port: int = 0, token: str | None = None, exit_on_stdin_eof: bool = False,
                backend: Backend | None = None, ready: Callable[[int], None] | None = None,
                parent_pid: int | None = None) -> None:
    from websockets.asyncio.server import serve as ws_serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    token = take_serve_token(token) or secrets.token_urlsafe(24)
    backend = backend or Backend()
    conns: dict = {}          # ws -> 发送队列（单写者，保证通知顺序）
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    loop_thread = threading.get_ident()

    def _put_all(msg: str) -> None:
        for q in list(conns.values()):
            q.put_nowait(msg)

    def broadcast(method: str, params: dict) -> None:
        # 线程安全：有的 RPC 在工作线程里跑（asyncio.to_thread），通知要切回事件循环再入队
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False, default=str)
        if threading.get_ident() == loop_thread:
            _put_all(msg)
        else:
            loop.call_soon_threadsafe(_put_all, msg)

    backend.listeners.append(broadcast)

    def check(connection, request):
        q = parse_qs(urlparse(request.path).query)
        got = (q.get("token") or [""])[0] or request.headers.get("X-EE-Token", "")
        if not secrets.compare_digest(got, token):
            return Response(401, "Unauthorized", Headers([("Content-Type", "text/plain")]), b"bad token\n")
        return None

    async def writer(ws, q: asyncio.Queue):
        while True:
            msg = await q.get()
            if msg is None:
                return
            await _safe_send(ws, msg)

    async def handler(ws):
        q: asyncio.Queue = asyncio.Queue()
        conns[ws] = q
        w = asyncio.ensure_future(writer(ws, q))
        try:
            async for raw in ws:
                asyncio.ensure_future(_handle(q, raw))
        except Exception:  # noqa: BLE001
            pass
        finally:
            conns.pop(ws, None)
            q.put_nowait(None)
            await w

    async def _handle(q: asyncio.Queue, raw):
        try:
            req = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "JSON 解析失败"}}))
            return
        rid = req.get("id")
        method = req.get("method", "")
        t0 = time.monotonic()
        try:
            result = await backend.call(method, req.get("params"))
            out = {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            log.warning("RPC %s 失败：%s", method, e)
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": redact(str(e))}}
        except Exception as e:  # noqa: BLE001
            log.error("RPC %s 内部错误", method, exc_info=True)
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": redact(f"{type(e).__name__}: {e}")}}
        dt = time.monotonic() - t0
        if dt >= SLOW_RPC:
            log.warning("RPC %s 用时 %.1fs（慢）", method, dt)
        if rid is not None:
            q.put_nowait(json.dumps(out, ensure_ascii=False, default=str))

    from ee.server import lifeline

    def request_exit(reason: str) -> None:     # 任意线程可调：通知事件循环退出，并兜底硬退出
        lifeline.hard_exit_after(EXIT_GRACE, reason=reason)
        loop.call_soon_threadsafe(lambda: stop.done() or stop.set_result(None))

    if parent_pid:
        lifeline.ParentWatch(parent_pid, lambda: request_exit("桌面端进程已退出")).start()

    if exit_on_stdin_eof:
        # 先把 fd 0 换成 NUL，再在私有副本上阻塞读：子进程（git 等）就不会继承这根正被读着的管道。
        # 实测 Windows 上不这样做，git 子进程会在启动时卡死，事件循环跟着卡死（界面全部转圈）。
        fd = _detach_stdin()

        def watch():
            try:
                if fd is None:
                    while sys.stdin.buffer.read(1024):
                        pass
                else:
                    while os.read(fd, 1024):
                        pass
            except Exception:  # noqa: BLE001
                pass
            log.info("stdin 已关闭（桌面端退出），后端退出")
            request_exit("stdin 已关闭")
        threading.Thread(target=watch, name="ee-stdin-eof", daemon=True).start()

    from ee import log as eelog
    from ee.core.providers import net
    watchdog = eelog.LoopWatchdog(loop)
    watchdog.start()
    try:
        np = await asyncio.wait_for(asyncio.to_thread(
            lambda: net.plan("https://api.deepseek.com", load_config(Path.home()).data.get("network"))), 5)
        log.info("ee serve %s 启动：pid=%s python=%s frozen=%s home=%s；网络：%s", __version__, os.getpid(),
                 sys.version.split()[0], bool(getattr(sys, "frozen", False)), backend.home, np.describe)
    except Exception:  # noqa: BLE001
        log.warning("启动信息收集失败", exc_info=True)

    async with ws_serve(handler, host, port, process_request=check, max_size=32 * 2**20,
                        ping_interval=None) as server:
        real_port = next(iter(server.sockets)).getsockname()[1]
        if ready:
            ready(real_port)
        else:
            print("EE_SERVE_READY " + json.dumps({"port": real_port, "pid": os.getpid()}), flush=True)
        backend.automations.start()                    # 自动化调度（D4-10）：错过的启动后补跑一次
        backend.loops.start()                          # 会话内 /loop（D5-3，只在内存里）
        gc_task = asyncio.create_task(_snapshot_gc_later(backend), name="ee-snapshot-gc")
        await stop
    gc_task.cancel()
    watchdog.stop()
    await backend.automations.stop()
    await backend.loops.stop()
    for t in backend.threads.values():
        t.interrupt()
    try:
        from ee.core.bgproc import close_all as bg_close_all
        await asyncio.wait_for(bg_close_all(), 8)       # 结束所有后台命令（D4-6）
    except Exception:  # noqa: BLE001
        pass
    try:
        from ee.mcp.manager import shutdown as mcp_shutdown
        await asyncio.wait_for(mcp_shutdown(), 8)       # 关掉 MCP 子进程
    except Exception:  # noqa: BLE001
        pass
    # 释放各会话的 HTTP 连接池（换模型/换 key 会堆出多个 AsyncOpenAI）
    for t in backend.threads.values():
        s = t.session
        if s is not None and getattr(s, "aclose", None) is not None:
            try:
                await asyncio.wait_for(s.aclose(), 5)
            except Exception:  # noqa: BLE001
                pass
    if exit_on_stdin_eof or parent_pid:                 # 作为桌面端内核运行：残留的子孙进程一并结束
        try:
            await asyncio.wait_for(asyncio.to_thread(lifeline.kill_descendants), 10)
        except Exception:  # noqa: BLE001
            pass


async def _snapshot_gc_later(backend: Backend, delay: float = 120.0) -> None:
    """启动两分钟后清理一次影子仓库（一天最多一次；正在运行的会话所在的工作区跳过）。"""
    from ee.core import checkpoints, snapshot
    try:
        await asyncio.sleep(delay)
        cfg = await asyncio.to_thread(load_config, Path.home())
        days, turns = checkpoints.keep_settings(cfg.data)
        busy = {snapshot.repo_key(t.meta.get("cwd") or ".") for t in backend.threads.values() if t.running}
        r = await asyncio.to_thread(checkpoints.gc_all, backend.home, days, turns, False, None, busy)
        if not r.get("skipped"):
            log.info("影子仓库清理：%s", r)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.warning("影子仓库清理失败", exc_info=True)


def _detach_stdin() -> int | None:
    """返回原 stdin 的不可继承副本，并把 fd 0（Windows 上连同 STD_INPUT_HANDLE）指向 NUL。失败返回 None。"""
    try:
        fd = os.dup(0)              # Python 3.4+：dup 出来的 fd 默认不可继承
    except OSError:
        return None
    try:
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, 0)
        os.close(nul)
        if sys.platform == "win32":
            import ctypes
            import msvcrt
            ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))   # STD_INPUT_HANDLE
    except Exception:  # noqa: BLE001
        log.warning("替换 stdin 失败（子进程仍有 stdin=DEVNULL 兜底）", exc_info=True)
    return fd


async def _safe_send(ws, msg: str) -> None:
    try:
        await ws.send(msg)
    except Exception:  # noqa: BLE001
        pass
