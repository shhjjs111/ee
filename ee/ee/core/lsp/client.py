"""LSP 传输层：在子进程上按 Content-Length 收发 JSON-RPC 消息。

只做协议，不懂语义（诊断内容、生命周期、启动策略都在别的模块）。

几个必须做对、做错了又不会立刻报错的地方：

- **Content-Length 是 UTF-8 字节数，不是字符数。** 按字符算，遇到中文就会读少了字节，
  表现为「偶尔卡住」而不是抛异常——这是本模块最容易埋的坑。所以读用 readexactly，
  不用 readline（MCP 那套是按行的，LSP 有帧头，不能照抄）。
- **stderr 必须持续排空。** 不排空的话，子进程写满管道缓冲区会阻塞在 write 上，
  紧接着客户端等响应就超时；症状同样是「偶尔卡住」。
- **pending 不能悬挂。** 取消、关闭、进程退出三种情况都要让所有等待者拿到明确错误。
- **timeout 覆盖发送 + 等响应全过程**（MCP 的 request 里记过这条教训：
  只对 future 用 wait_for 时，阻塞在发送阶段的请求计时根本不生效）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any

log = logging.getLogger("ee.lsp")

IS_WINDOWS = sys.platform == "win32"
MAX_STDERR_LINES = 50
DEFAULT_TIMEOUT = 30.0


class LspError(RuntimeError):
    """协议或进程层面的错误。调用方据此给出 available/unavailable/error 状态。"""


def encode_message(obj: dict) -> bytes:
    """序列化成 LSP 帧：Content-Length 用 UTF-8 字节数。"""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def read_message(reader: asyncio.StreamReader) -> dict | None:
    """读一条 LSP 消息；对端关闭返回 None。

    头按行读（头一定是 ASCII），正文用 readexactly 按**字节**读。
    """
    headers: dict[str, str] = {}
    while True:
        try:
            line = await reader.readline()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            return None
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            k, v = line.decode("ascii", "replace").split(":", 1)
        except ValueError:
            continue
        headers[k.strip().lower()] = v.strip()

    raw = headers.get("content-length")
    if raw is None:
        raise LspError(f"缺 Content-Length 头：{headers}")
    try:
        length = int(raw)
    except ValueError:
        raise LspError(f"Content-Length 不是数字：{raw!r}") from None
    if length <= 0:
        raise LspError(f"Content-Length 非正数：{length}")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as e:
        raise LspError(f"正文只读到 {len(e.partial)}/{length} 字节就断了") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LspError(f"正文不是合法 UTF-8 JSON（{e}）") from None


class LspClient:
    """一个语言服务器进程的传输层。

    用法：
        c = LspClient(["{pyright-langserver}", "--stdio"])
        await c.start()
        res = await c.request("initialize", {...})
        ...
        await c.close()
    """

    def __init__(self, argv: list[str], *, cwd: str | os.PathLike | None = None,
                 env: dict | None = None, name: str = "lsp"):
        self.argv = list(argv)
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.name = name
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._stderr: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self.closed = False
        # 通知回调：method -> callable(params)。诊断走这里进来。
        self.on_notification: dict[str, Any] = {}

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """启动服务器并开始读流。失败抛 LspError（调用方转成 unavailable）。"""
        from ee.proc import child_environ, popen_kwargs

        env = child_environ(self.env)
        # 语言服务器不该被 ee 的机密环境变量污染（和 shell / MCP 同一套清理）。
        # popen_kwargs() 已经带 Windows 的 CREATE_NO_WINDOW / POSIX 的 start_new_session，
        # 不要再自己加 creationflags，否则关键字重复。
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, cwd=self.cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **popen_kwargs())
        except (OSError, ValueError) as e:
            raise LspError(f"启动 {self.name} 失败：{self.argv!r}：{e}") from None
        self._tasks = [asyncio.create_task(self._read_out(), name=f"{self.name}-out"),
                       asyncio.create_task(self._read_err(), name=f"{self.name}-err")]

    async def close(self) -> None:
        """关闭：取消等待中的请求、结束读任务、终止进程。可重复调用。"""
        if self._closing:
            return
        self._closing = True
        self.closed = True
        self._fail_all("客户端已关闭")
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        p = self.proc
        if p is not None and p.returncode is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(p.wait(), 3)
                except asyncio.TimeoutError:
                    log.warning("%s 进程 kill 后仍未退出", self.name)

    # ------------------------------------------------------------ 收发
    async def _send(self, obj: dict) -> None:
        p = self.proc
        if p is None or p.stdin is None or p.stdin.is_closing():
            raise LspError(f"{self.name} 未启动或 stdin 已关闭")
        p.stdin.write(encode_message(obj))
        try:
            await p.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            raise LspError(f"{self.name} 的 stdin 已断开：{e}") from None

    async def request(self, method: str, params: dict | None = None,
                      timeout: float = DEFAULT_TIMEOUT) -> dict:
        """发请求等响应；**timeout 覆盖发送 + 等待全过程**。返回完整响应对象（含 result/error）。"""
        if self.closed:
            raise LspError(f"{self.name} 已关闭")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params

        async def send_then_wait():
            await self._send(msg)
            return await fut

        try:
            return await asyncio.wait_for(send_then_wait(), timeout)
        except asyncio.TimeoutError:
            raise LspError(f"{method} 超时（{timeout:g} 秒）") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        """发通知（不等响应）。失败抛 LspError，调用方决定是否忽略。"""
        if self.closed:
            raise LspError(f"{self.name} 已关闭")
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    def cancel(self, rid: int) -> None:
        """取消一个还没响应的请求（发 $/cancelRequest 并让本地等待者立刻失败）。"""
        fut = self._pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_exception(LspError(f"请求 {rid} 已取消"))
        self._pending.pop(rid, None)
        if self.proc is not None and not self.closed:
            asyncio.create_task(self._notify_quietly("$/cancelRequest", {"id": rid}))

    async def _notify_quietly(self, method: str, params: dict) -> None:
        try:
            await self.notify(method, params)
        except LspError:
            pass

    # ------------------------------------------------------------ 读流
    async def _read_out(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                msg = await read_message(self.proc.stdout)
                if msg is None:
                    break
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except LspError as e:
            log.warning("%s 读流协议错误：%s", self.name, e)
            self._fail_all(f"{self.name} 协议错误：{e}")
        finally:
            if not self._closing:
                self.closed = True
                tail = self.stderr_tail()
                self._fail_all(f"{self.name} 进程已退出" + (f"：\n{tail}" if tail else ""))

    async def _dispatch(self, msg: dict) -> None:
        rid = msg.get("id")
        if rid is not None and msg.get("method") is None:
            fut = self._pending.pop(int(rid), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if rid is not None and msg.get("method"):
            # 服务器反向请求：本项目 MVP 不支持，按协议回 MethodNotFound 而不是静默丢弃
            await self._notify_quietly_reply(rid, msg.get("method"))
            return
        method = msg.get("method")
        if method:
            cb = self.on_notification.get(method)
            if cb is not None:
                try:
                    res = cb(msg.get("params") or {})
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:  # noqa: BLE001  回调出错不能拖垮读流
                    log.warning("%s 通知回调 %s 出错：%s", self.name, method, e)

    async def _notify_quietly_reply(self, rid, method: str) -> None:
        try:
            await self._send({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": f"ee 不支持 {method}"}})
        except LspError:
            pass

    async def _read_err(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self._stderr.append(line.decode("utf-8", "replace").rstrip()[:500])
                if len(self._stderr) > MAX_STDERR_LINES * 2:
                    del self._stderr[:MAX_STDERR_LINES]
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError):
            pass

    def stderr_tail(self, n: int = MAX_STDERR_LINES) -> str:
        return "\n".join(self._stderr[-n:])

    def _fail_all(self, why: str) -> None:
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(LspError(why))
            self._pending.pop(rid, None)
