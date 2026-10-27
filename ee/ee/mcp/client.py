"""MCP 客户端会话：JSON-RPC 2.0 + 三种传输。

stdio：子进程，每行一条 JSON；stdin 显式 PIPE（不继承后端的 stdin，见 HANDOFF 里的 Windows 卡死根因），
       stderr 持续读空只留最后 50 行用于报错。Windows 上 .cmd/.bat（npx 等）走 shell。
http（streamable HTTP）：每条消息 POST；响应可能是 application/json 或 text/event-stream；
       带 Mcp-Session-Id、MCP-Protocol-Version。
sse（旧式）：GET 拿事件流，先等 endpoint 事件，之后 POST 到该地址，响应从事件流回来。
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import urljoin

from ee.mcp.config import ServerConfig
from ee.proc import child_environ

log = logging.getLogger("ee.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "ee", "version": "0.3"}
IS_WINDOWS = sys.platform == "win32"


class McpError(Exception):
    pass


class _Base:
    """收发与请求配对；子类实现 _send(obj) 和启动/关闭。"""

    def __init__(self, cfg: ServerConfig, roots: list[str] | None = None):
        self.cfg = cfg
        self.roots = roots or []
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.closed = False
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions = ""

    # ---- 子类实现
    async def start(self) -> None: ...
    async def _send(self, obj: dict) -> None: ...
    async def close(self) -> None: ...

    def stderr_tail(self) -> str:
        return ""

    # ---- 公共
    def _fail_all(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError(why))
        self._pending.clear()

    async def _on_message(self, msg: Any) -> None:
        if isinstance(msg, list):
            for m in msg:
                await self._on_message(m)
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(McpError(f"{err.get('message') or err} (code {err.get('code')})"))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        method = msg.get("method")
        if method and "id" in msg:                       # 服务器发来的请求
            if method == "ping":
                await self._reply(msg["id"], {})
            elif method == "roots/list":
                await self._reply(msg["id"], {"roots": [{"uri": _file_uri(r), "name": os.path.basename(r) or r}
                                                        for r in self.roots]})
            else:
                await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {"code": -32601, "message": f"ee 不支持 {method}"}})
        # 通知（notifications/*）忽略

    async def _reply(self, rid, result: dict) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        if self.closed:
            raise McpError("连接已关闭")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise McpError(f"{method} 超时（{timeout:g} 秒）") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def initialize(self, timeout: float) -> None:
        res = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        self.instructions = str(res.get("instructions") or "")
        self.protocol_version = str(res.get("protocolVersion") or PROTOCOL_VERSION)
        await self.notify("notifications/initialized")

    async def list_tools(self, timeout: float) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(50):                               # 分页上限，防止服务器死循环
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            tools += [t for t in (res.get("tools") or []) if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)


def _file_uri(path: str) -> str:
    from pathlib import Path
    try:
        return Path(path).resolve().as_uri()
    except (ValueError, OSError):
        return "file://" + path


# ------------------------------------------------------------------ stdio

class StdioClient(_Base):
    def __init__(self, cfg: ServerConfig, roots: list[str] | None = None, base_cwd: str | None = None):
        super().__init__(cfg, roots)
        self.base_cwd = base_cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=50)
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        cfg = self.cfg
        env = child_environ(extra=cfg.env)       # 后端口令等机密变量不给 MCP 服务器
        # MCP 的 stdio 传输规定是 UTF-8（JSON-RPC 每条一行）。Windows 上子进程的
        # stdin/stdout 默认按本机 ANSI 代码页（中文系统是 GBK）编解码，服务器一发出
        # 中文（工具描述、工具结果都常有）就被写成 GBK 字节，我们按 UTF-8 解析会
        # 撞 "非 JSON" 并一直等到超时。这里给子进程显式指定 UTF-8；用户自己配的
        # env 放最后，显式设置仍然优先。
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        cwd = cfg.cwd or self.base_cwd or None
        exe = shutil.which(cfg.command, path=env.get("PATH")) or cfg.command
        kw: dict = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE, "env": env, "cwd": cwd, "limit": 32 * 2**20}
        if IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        try:
            if IS_WINDOWS and exe.lower().endswith((".cmd", ".bat")):
                self.proc = await asyncio.create_subprocess_shell(subprocess.list2cmdline([exe, *cfg.args]), **kw)
            else:
                self.proc = await asyncio.create_subprocess_exec(exe, *cfg.args, **kw)
        except (OSError, ValueError) as e:
            raise McpError(f"启动失败：{cfg.command}：{e}") from None
        self._tasks = [asyncio.create_task(self._read_out()), asyncio.create_task(self._read_err())]

    async def _read_out(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._stderr.append("[stdout 非 JSON] " + line.decode("utf-8", "replace")[:300])
                    continue
                await self._on_message(msg)
        except (asyncio.CancelledError, ValueError):
            pass
        finally:
            self.closed = True
            tail = self.stderr_tail()
            self._fail_all("MCP 服务器进程已退出" + (f"：\n{tail[-1500:]}" if tail else ""))

    async def _read_err(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self._stderr.append(line.decode("utf-8", "replace").rstrip()[:500])
        except (asyncio.CancelledError, ValueError):
            pass

    async def _send(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin or self.closed:
            raise McpError("MCP 服务器进程未运行")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as e:
                self.closed = True
                raise McpError(f"写入 MCP 服务器失败：{e}") from None

    async def close(self) -> None:
        self.closed = True
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), 1.5)
            except asyncio.TimeoutError:
                from ee.proc import kill_tree
                await asyncio.to_thread(kill_tree, proc.pid)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        for t in self._tasks:
            t.cancel()
        self._fail_all("连接已关闭")


# ------------------------------------------------------------------ HTTP

def _iter_sse(text: str):
    """把一段 SSE 文本拆成 (event, data)。"""
    event, data = "message", []
    for line in text.splitlines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def _new_http(url: str, read):
    """本机地址永远直连（不走系统代理），其余跟随环境变量代理。"""
    import httpx
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    local = host in ("localhost", "::1") or host.startswith("127.")
    return httpx.AsyncClient(timeout=httpx.Timeout(30, read=read), follow_redirects=True, trust_env=not local)


class HttpClient(_Base):
    """streamable HTTP。"""

    def __init__(self, cfg: ServerConfig, roots: list[str] | None = None, http=None):
        super().__init__(cfg, roots)
        self._http = http
        self._own_http = http is None
        self.session_id: str | None = None
        self.protocol_version = PROTOCOL_VERSION

    async def start(self) -> None:
        if self._http is None:
            self._http = _new_http(self.cfg.url, 300)

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "MCP-Protocol-Version": self.protocol_version, **self.cfg.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _send(self, obj: dict) -> None:
        if self.closed:
            raise McpError("连接已关闭")
        try:
            r = await self._http.post(self.cfg.url, content=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                      headers=self._headers())
        except Exception as e:  # noqa: BLE001
            raise McpError(f"请求 {self.cfg.url} 失败：{type(e).__name__}: {e}") from None
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if r.status_code == 202 or not r.content:
            if r.status_code >= 400:
                raise McpError(f"HTTP {r.status_code}")
            return
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}：{r.text[:300]}")
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for _, data in _iter_sse(r.text):
                try:
                    await self._on_message(json.loads(data))
                except ValueError:
                    continue
        else:
            try:
                await self._on_message(r.json())
            except ValueError:
                raise McpError(f"响应不是 JSON：{r.text[:200]}") from None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.session_id and self._http is not None:
            try:
                await self._http.delete(self.cfg.url, headers=self._headers())
            except Exception:  # noqa: BLE001
                pass
        if self._own_http and self._http is not None:
            await self._http.aclose()
        self._fail_all("连接已关闭")


class SseClient(_Base):
    """旧式 HTTP+SSE（2024-11-05 规范）。"""

    def __init__(self, cfg: ServerConfig, roots: list[str] | None = None, http=None):
        super().__init__(cfg, roots)
        self._http = http
        self._own_http = http is None
        self.endpoint: str | None = None
        self._ready: asyncio.Future | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._http is None:
            self._http = _new_http(self.cfg.url, None)
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._listen())
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), 20)
        except asyncio.TimeoutError:
            raise McpError("等待 SSE endpoint 事件超时") from None

    async def _listen(self) -> None:
        try:
            async with self._http.stream("GET", self.cfg.url, headers={"Accept": "text/event-stream",
                                                                       **self.cfg.headers}) as r:
                if r.status_code >= 400:
                    raise McpError(f"HTTP {r.status_code}")
                event, data = "message", []
                async for line in r.aiter_lines():
                    if line == "":
                        if data:
                            await self._on_event(event, "\n".join(data))
                        event, data = "message", []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            if self._ready and not self._ready.done():
                self._ready.set_exception(McpError(f"SSE 连接失败：{e}"))
        finally:
            self.closed = True
            self._fail_all("SSE 连接已断开")

    async def _on_event(self, event: str, data: str) -> None:
        if event == "endpoint":
            self.endpoint = urljoin(self.cfg.url, data.strip())
            if self._ready and not self._ready.done():
                self._ready.set_result(True)
            return
        try:
            await self._on_message(json.loads(data))
        except ValueError:
            pass

    async def _send(self, obj: dict) -> None:
        if self.closed or not self.endpoint:
            raise McpError("SSE 连接未就绪")
        try:
            r = await self._http.post(self.endpoint, json=obj, headers=self.cfg.headers)
        except Exception as e:  # noqa: BLE001
            raise McpError(f"请求失败：{e}") from None
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}：{r.text[:300]}")

    async def close(self) -> None:
        self.closed = True
        if self._task:
            self._task.cancel()
        if self._own_http and self._http is not None:
            await self._http.aclose()
        self._fail_all("连接已关闭")


def make_client(cfg: ServerConfig, roots: list[str] | None = None, base_cwd: str | None = None, http=None) -> _Base:
    if cfg.transport == "stdio":
        return StdioClient(cfg, roots, base_cwd)
    if cfg.transport == "sse":
        return SseClient(cfg, roots, http)
    return HttpClient(cfg, roots, http)
