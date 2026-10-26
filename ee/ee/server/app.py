"""`ee serve`：桌面端的本地后端（app-server）。

- 只监听 127.0.0.1；连接地址 ws://127.0.0.1:<port>/?token=<token>，token 不对直接 401。
- 协议：JSON-RPC 2.0 风格。请求 {"id", "method", "params"} → 响应 {"id", "result"} 或 {"id", "error": {code, message}}；
  服务端推送 {"method", "params"}（没有 id），广播给所有连接。
- 启动成功后在 stdout 打印一行 `EE_SERVE_READY {"port": N}`，Electron 主进程读这一行拿端口。
- --exit-on-stdin-eof：stdin 关闭（父进程 Electron 退出）时自动退出，避免残留进程。
"""
from __future__ import annotations

import asyncio
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
from ee.config import ee_home, load_config
from ee.core.fileindex import FileIndex
from ee.core.permissions import MODE_LABEL, MODES
from ee.core.secrets import redact
from ee.core.store import SessionStore
from ee.server import files as F
from ee.server import gitops
from ee.server import branch, mcp_rpc, memory_rpc, prompts_rpc, review_rpc, shell_rpc
from ee.server import hooks_rpc
from ee.server import settings as S
from ee.server.threads import Thread

log = logging.getLogger("ee.server")
SLOW_RPC = 5.0     # 秒：超过就记一条日志


class RpcError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


class Backend:
    """与传输无关的后端：方法分发 + 线程管理。测试可以直接用它（不走 WebSocket）。"""

    def __init__(self, home: Path | None = None, client_factory: Callable | None = None, persist: bool = True):
        self.home = home or ee_home()
        self.client_factory = client_factory
        self.persist = persist
        self.threads: dict[str, Thread] = {}
        self.listeners: list[Callable[[str, dict], Any]] = []
        self._indexes: dict[str, tuple[float, FileIndex]] = {}
        self.methods: dict[str, Callable] = {
            "app/info": self.app_info,
            "thread/list": self.thread_list,
            "thread/search": self.thread_search,
            "thread/start": self.thread_start,
            "thread/get": self.thread_get,
            "thread/rename": self.thread_rename,
            "thread/delete": self.thread_delete,
            "thread/undo": self.thread_undo,
            "thread/compact": self.thread_compact,
            "turn/start": self.turn_start,
            "turn/interrupt": self.turn_interrupt,
            "approval/respond": self.approval_respond,
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
        hooks_rpc.register(self.methods, self)         # hooks/list|test（D5-4）

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
        try:
            res = fn(**params)
        except TypeError as e:
            raise RpcError(f"参数错误：{e}", -32602)
        if asyncio.iscoroutine(res):
            res = await res
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
        self._desktop_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------ 线程
    async def _athread(self, thread_id: str) -> Thread:
        """取线程；不在内存里时在工作线程里读会话文件（大文件不卡事件循环）。"""
        t = self.threads.get(thread_id)
        if t is not None:
            return t
        t = await asyncio.to_thread(self._thread, thread_id)
        return self.threads.setdefault(thread_id, t)

    def _thread(self, thread_id: str) -> Thread:
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
                   client_factory=self.client_factory, persist=self.persist)
        self.threads[thread_id] = t
        return t

    async def app_info(self) -> dict:
        from ee import log as eelog
        cfg = await asyncio.to_thread(load_config, Path.home())
        return {"version": __version__, "home": str(self.home), "pid": os.getpid(), "platform": sys.platform,
                "frozen": bool(getattr(sys, "frozen", False)), "log_file": str(eelog.log_dir() / eelog.LOG_NAME),
                "default_model": cfg.default_model, "modes": [{"id": m, "label": MODE_LABEL[m]} for m in MODES]}

    async def thread_list(self, cwd: str | None = None, query: str | None = None, limit: int = 200) -> dict:
        return await asyncio.to_thread(self._thread_list, cwd, query, limit)

    def _thread_list(self, cwd: str | None = None, query: str | None = None, limit: int = 200) -> dict:
        seen: dict[str, dict] = {}
        for t in self.threads.values():
            seen[t.id] = t.summary()
        for p in SessionStore.list_sessions(self.home)[: max(limit * 2, 400)]:
            if p.stem in seen:
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

    async def thread_start(self, cwd: str, model: str | None = None, mode: str | None = None) -> dict:
        if mode and mode not in MODES:
            raise RpcError(f"未知权限模式：{mode}")

        def build() -> Thread:   # 工作线程里：目录检查、git status、shell 探测、写会话文件
            p = Path(cwd).expanduser()
            if not p.is_dir():
                raise RpcError(f"目录不存在：{cwd}")
            store = SessionStore(self.home)
            meta = {"cwd": str(p.resolve()), "model": model or "", "mode": mode or "", "title": "新对话",
                    "created": time.time()}
            t = Thread(store.id, self.home, self.notify, meta, client_factory=self.client_factory,
                       persist=self.persist)
            try:
                t.ensure_session()
            except ValueError as e:
                raise RpcError(f"配置错误：{e}")
            t.save_meta()
            self._touch_workspace(meta["cwd"])
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

    async def thread_rename(self, thread_id: str, title: str) -> dict:
        t = await self._athread(thread_id)
        t.meta["title"] = title.strip() or "新对话"
        t.save_meta()
        self.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.summary()}

    def thread_delete(self, thread_id: str) -> dict:
        t = self.threads.pop(thread_id, None)
        if t is not None and t.running:
            t.interrupt()
        if t is not None and t.session is not None:      # 会话删除：杀掉它的后台命令（D4-6）
            try:
                asyncio.get_running_loop().create_task(t.session.bg.close())
            except RuntimeError:
                t.session.bg.kill_all_sync()
        path = self.home / "sessions" / f"{thread_id}.jsonl"
        if path.is_file():
            path.unlink()
        extra = self.home / "sessions" / thread_id      # 附件等（D3-D）
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

    async def thread_undo(self, thread_id: str) -> dict:
        t = await self._athread(thread_id)
        s = await t.ensure_session_async()
        if s.running:
            raise RpcError("运行中不能撤销")
        restored = await asyncio.to_thread(s.undo)
        if s.last_undo_tree:
            t.mark_undone(s.last_undo_tree)
        if restored:
            t._add({"type": "notice", "text": "已撤销上一回合的文件改动：\n" + "\n".join(restored), "level": "info"})
        return {"restored": restored}

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

    async def turn_start(self, thread_id: str, text: str, model: str | None = None, mode: str | None = None,
                         attachments: list | None = None) -> dict:
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
            return t.start_turn(text or "（见图片）", model=model, mode=mode, images=images, attachments=meta)
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
                        "has_key": bool(spec.api_key()),
                        "price": vars(spec.profile.price)})
        return {"models": out, "default": cfg.default_model}

    # ------------------------------------------------------------ 工作区
    def _touch_workspace(self, path: str) -> None:
        d = self._desktop()
        ws = [w for w in d.get("workspaces", []) if os.path.normcase(w["path"]) != os.path.normcase(path)]
        ws.insert(0, {"path": path, "name": Path(path).name or path, "opened": time.time()})
        d["workspaces"] = ws[:50]
        self._save_desktop(d)

    async def workspace_list(self) -> dict:
        return await asyncio.to_thread(self._workspace_list)

    def _workspace_list(self) -> dict:
        ws = self._desktop().get("workspaces", [])
        for w in ws:
            w["exists"] = Path(w["path"]).is_dir()
        return {"workspaces": ws}

    async def workspace_open(self, path: str) -> dict:
        def run():
            p = Path(path).expanduser()
            if not p.is_dir():
                raise RpcError(f"目录不存在：{path}")
            self._touch_workspace(str(p.resolve()))
            return self._workspace_list()
        return await asyncio.to_thread(run)

    async def workspace_remove(self, path: str) -> dict:
        def run():
            d = self._desktop()
            d["workspaces"] = [w for w in d.get("workspaces", [])
                               if os.path.normcase(w["path"]) != os.path.normcase(path)]
            self._save_desktop(d)
            return self._workspace_list()
        return await asyncio.to_thread(run)

    async def files_search(self, cwd: str, query: str = "", limit: int = 20) -> dict:
        key = os.path.normcase(os.path.abspath(cwd))
        hit = self._indexes.get(key)
        if hit is None or time.time() - hit[0] > 30:
            idx = FileIndex(Path(cwd))
            await asyncio.to_thread(idx.build)
            self._indexes[key] = (time.time(), idx)
        else:
            idx = hit[1]
        return {"files": idx.search(query, n=limit)}

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
    async def git_diff(self, cwd: str) -> dict:
        if not Path(cwd).is_dir():
            raise RpcError(f"目录不存在：{cwd}")
        return await asyncio.to_thread(gitops.review, Path(cwd))

    async def git_revert_file(self, cwd: str, path: str) -> dict:
        try:
            res = await asyncio.to_thread(gitops.revert_file, Path(cwd), path)
        except ValueError as e:
            raise RpcError(str(e))
        for t in self.threads.values():   # 让模型下次知道文件变了
            if t.session is not None and os.path.normcase(str(t.cwd)) == os.path.normcase(cwd):
                t.session.state.files.clear()
        return res


# ---------------------------------------------------------------- WebSocket 传输

async def serve(host: str = "127.0.0.1", port: int = 0, token: str | None = None, exit_on_stdin_eof: bool = False,
                backend: Backend | None = None, ready: Callable[[int], None] | None = None) -> None:
    from websockets.asyncio.server import serve as ws_serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    token = token or os.environ.get("EE_SERVE_TOKEN") or secrets.token_urlsafe(24)
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
            loop.call_soon_threadsafe(lambda: stop.done() or stop.set_result(None))
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
        await stop
    watchdog.stop()
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
