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
from ee.core.store import SessionStore
from ee.server import gitops
from ee.server import settings as S
from ee.server.threads import Thread


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
            "thread/start": self.thread_start,
            "thread/get": self.thread_get,
            "thread/rename": self.thread_rename,
            "thread/delete": self.thread_delete,
            "thread/undo": self.thread_undo,
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
            "git/diff": self.git_diff,
            "git/revert_file": self.git_revert_file,
        }

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

    def app_info(self) -> dict:
        cfg = load_config(Path.home())
        return {"version": __version__, "home": str(self.home), "pid": os.getpid(), "platform": sys.platform,
                "default_model": cfg.default_model, "modes": [{"id": m, "label": MODE_LABEL[m]} for m in MODES]}

    def thread_list(self, cwd: str | None = None, query: str | None = None, limit: int = 200) -> dict:
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

    def thread_start(self, cwd: str, model: str | None = None, mode: str | None = None) -> dict:
        p = Path(cwd).expanduser()
        if not p.is_dir():
            raise RpcError(f"目录不存在：{cwd}")
        if mode and mode not in MODES:
            raise RpcError(f"未知权限模式：{mode}")
        store = SessionStore(self.home)
        meta = {"cwd": str(p.resolve()), "model": model or "", "mode": mode or "", "title": "新对话",
                "created": time.time()}
        t = Thread(store.id, self.home, self.notify, meta, client_factory=self.client_factory, persist=self.persist)
        try:
            t.ensure_session()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        self.threads[t.id] = t
        t.save_meta()
        self._touch_workspace(meta["cwd"])
        self.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.detail()}

    def thread_get(self, thread_id: str) -> dict:
        t = self._thread(thread_id)
        try:
            t.ensure_session()
        except ValueError as e:
            raise RpcError(f"配置错误：{e}")
        return {"thread": t.detail()}

    def thread_rename(self, thread_id: str, title: str) -> dict:
        t = self._thread(thread_id)
        t.meta["title"] = title.strip() or "新对话"
        t.save_meta()
        self.notify("thread/updated", {"thread": t.summary()})
        return {"thread": t.summary()}

    def thread_delete(self, thread_id: str) -> dict:
        t = self.threads.pop(thread_id, None)
        if t is not None and t.running:
            t.interrupt()
        path = self.home / "sessions" / f"{thread_id}.jsonl"
        if path.is_file():
            path.unlink()
        self.notify("thread/deleted", {"thread_id": thread_id})
        return {"ok": True}

    def thread_undo(self, thread_id: str) -> dict:
        t = self._thread(thread_id)
        s = t.ensure_session()
        if s.running:
            raise RpcError("运行中不能撤销")
        restored = s.undo()
        if restored:
            t._add({"type": "notice", "text": "已撤销上一回合的文件改动：\n" + "\n".join(restored), "level": "info"})
        return {"restored": restored}

    async def turn_start(self, thread_id: str, text: str, model: str | None = None, mode: str | None = None) -> dict:
        if not (text or "").strip():
            raise RpcError("消息不能为空")
        t = self._thread(thread_id)
        try:
            return t.start_turn(text, model=model, mode=mode)
        except ValueError as e:
            raise RpcError(str(e))

    def turn_interrupt(self, thread_id: str) -> dict:
        t = self._thread(thread_id)
        return {"interrupted": t.interrupt()}

    def approval_respond(self, thread_id: str, call_id: str, choice: str, reason: str = "") -> dict:
        t = self._thread(thread_id)
        try:
            return t.respond(call_id, choice, reason)
        except ValueError as e:
            raise RpcError(str(e))

    # ------------------------------------------------------------ 设置 / 模型
    def settings_get(self) -> dict:
        return S.get_settings()

    def settings_set(self, settings: dict) -> dict:
        try:
            res = S.set_settings(settings)
        except ValueError as e:
            raise RpcError(str(e))
        for t in self.threads.values():
            t.reload_config()
        self.notify("settings/changed", res)
        return res

    def models_list(self, cwd: str | None = None) -> dict:
        cfg = load_config(Path(cwd) if cwd and Path(cwd).is_dir() else Path.home())
        out = []
        for name in cfg.model_names():
            try:
                spec = cfg.resolve_model(name)
            except ValueError:
                continue
            out.append({"name": name, "provider": spec.provider, "model": spec.model,
                        "family": spec.profile.family, "context_window": spec.profile.context_window,
                        "reasoning": spec.profile.reasoning, "has_key": bool(spec.api_key()),
                        "price": vars(spec.profile.price)})
        return {"models": out, "default": cfg.default_model}

    # ------------------------------------------------------------ 工作区
    def _touch_workspace(self, path: str) -> None:
        d = self._desktop()
        ws = [w for w in d.get("workspaces", []) if os.path.normcase(w["path"]) != os.path.normcase(path)]
        ws.insert(0, {"path": path, "name": Path(path).name or path, "opened": time.time()})
        d["workspaces"] = ws[:50]
        self._save_desktop(d)

    def workspace_list(self) -> dict:
        ws = self._desktop().get("workspaces", [])
        for w in ws:
            w["exists"] = Path(w["path"]).is_dir()
        return {"workspaces": ws}

    def workspace_open(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise RpcError(f"目录不存在：{path}")
        self._touch_workspace(str(p.resolve()))
        return self.workspace_list()

    def workspace_remove(self, path: str) -> dict:
        d = self._desktop()
        d["workspaces"] = [w for w in d.get("workspaces", [])
                           if os.path.normcase(w["path"]) != os.path.normcase(path)]
        self._save_desktop(d)
        return self.workspace_list()

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

    def broadcast(method: str, params: dict) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False, default=str)
        for q in list(conns.values()):
            q.put_nowait(msg)

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
        try:
            result = await backend.call(req.get("method", ""), req.get("params"))
            out = {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # noqa: BLE001
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if rid is not None:
            q.put_nowait(json.dumps(out, ensure_ascii=False, default=str))

    if exit_on_stdin_eof:
        def watch():
            try:
                while sys.stdin.buffer.read(1024):
                    pass
            except Exception:  # noqa: BLE001
                pass
            loop.call_soon_threadsafe(lambda: stop.done() or stop.set_result(None))
        threading.Thread(target=watch, daemon=True).start()

    async with ws_serve(handler, host, port, process_request=check, max_size=32 * 2**20,
                        ping_interval=None) as server:
        real_port = next(iter(server.sockets)).getsockname()[1]
        if ready:
            ready(real_port)
        else:
            print("EE_SERVE_READY " + json.dumps({"port": real_port, "pid": os.getpid()}), flush=True)
        await stop
    for t in backend.threads.values():
        t.interrupt()


async def _safe_send(ws, msg: str) -> None:
    try:
        await ws.send(msg)
    except Exception:  # noqa: BLE001
        pass
