"""测试用的假 LSP 服务器（stdio，Content-Length 帧）。

刻意用来钉住几个容易做错的协议点：
- Content-Length 计的是 **UTF-8 字节数**，不是字符数（中文内容会暴露这个错误）
- 响应可以乱序返回（客户端必须按 id 匹配）
- 可以狂写 stderr（客户端必须持续排空，否则管道写满死锁）
- 可以延迟响应（用来验证超时与取消）
- 可以压根不响应（验证 pending 清理）

用法（由 test_lsp.py 拉起）：
    python fake_lsp_server.py [--mode normal|slow|silent|crash|stderr-flood|utf8]
"""
from __future__ import annotations

import json
import sys
import threading
import time

OUT = sys.stdout.buffer
_quit = threading.Event()


def read_message(stream) -> dict | None:
    """按 Content-Length 读一条消息；EOF 返回 None。"""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            k, v = line.decode("ascii", "replace").split(":", 1)
        except ValueError:
            continue
        headers[k.strip().lower()] = v.strip()
    try:
        n = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    body = stream.read(n)
    if not body or len(body) < n:
        return None
    return json.loads(body.decode("utf-8"))


def send(obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    OUT.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    OUT.flush()


def send_raw_header(char_len_instead_of_bytes: bool, obj: dict) -> None:
    """故意用字符数冒充字节数——用来证明客户端会因此读错。"""
    text = json.dumps(obj, ensure_ascii=False)
    body = text.encode("utf-8")
    n = len(text) if char_len_instead_of_bytes else len(body)
    OUT.write(b"Content-Length: " + str(n).encode("ascii") + b"\r\n\r\n" + body)
    OUT.flush()


def main() -> int:
    mode = "normal"
    args = sys.argv[1:]
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]

    if mode == "stderr-flood":
        def flood():
            for _ in range(200):
                sys.stderr.write("x" * 100 + "\n")
                sys.stderr.flush()
            sys.stderr.write("FLOOD-DONE\n")
            sys.stderr.flush()
        threading.Thread(target=flood, daemon=True).start()

    docs: dict[str, int] = {}      # uri -> version
    diag_after_change = mode in ("normal", "slow", "utf8")

    while not _quit.is_set():
        msg = read_message(sys.stdin.buffer)
        if msg is None:
            break
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"capabilities": {"textDocumentSync": {"openClose": True, "change": 1},
                                              "diagnosticProvider": {"interFileDependencies": False}},
                             "serverInfo": {"name": "fake-lsp", "version": "1"}}})
        elif method == "initialized":
            pass
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            break
        elif method == "textDocument/didOpen":
            td = params.get("textDocument") or {}
            uri = td.get("uri", "")
            docs[uri] = int(td.get("version") or 1)
            if diag_after_change:
                _publish(uri, docs[uri], mode, bad="type error" in (td.get("text") or ""))
        elif method == "textDocument/didChange":
            td = params.get("textDocument") or {}
            uri = td.get("uri", "")
            docs[uri] = int(td.get("version") or docs.get(uri, 1) + 1)
            text = ""
            for ch in params.get("contentChanges") or []:
                text = ch.get("text") or text
            if diag_after_change:
                _publish(uri, docs[uri], mode, bad="type error" in text)
        elif method == "textDocument/didClose":
            docs.pop((params.get("textDocument") or {}).get("uri", ""), None)
        elif method == "test/echo":
            # 回显参数，用来验证 id 匹配与 UTF-8 字节长度
            if mode == "silent":
                continue
            if mode == "crash":
                sys.exit(3)
            delay = float(params.get("delay") or 0)
            if delay:
                time.sleep(delay)
            if mode == "slow":
                time.sleep(5)
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "echo": params.get("text", ""),
                "chars": len(params.get("text", "")),
                "bytes": len(str(params.get("text", "")).encode("utf-8")),
            }})
        elif method == "test/send-bad-content-length":
            # 故意发一个用字符数当字节数的帧
            send_raw_header(True, {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                                   "params": {"uri": "file:///bad.py", "version": 1,
                                              "diagnostics": [{"message": "类型错误：中文", "severity": 1,
                                                               "range": {"start": {"line": 0, "character": 0},
                                                                         "end": {"line": 0, "character": 5}}}]}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown {method}"}})

    return 0


def _publish(uri: str, version: int, mode: str, bad: bool) -> None:
    diags = []
    if bad:
        diags.append({"message": "类型错误：这里应该是 int，给了 str（中文，用来验证字节长度）",
                      "severity": 1,
                      "source": "fake-lsp",
                      "range": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5}}})
    if mode == "utf8":
        # 故意把中文放进 message，验证客户端计算长度时按字节
        diags.append({"message": "中文诊断消息" * 3, "severity": 2, "source": "fake-lsp",
                      "range": {"start": {"line": 1, "character": 0},
                                "end": {"line": 1, "character": 3}}})

    def go():
        if mode == "slow":
            time.sleep(3)
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": uri, "version": version, "diagnostics": diags}})

    threading.Thread(target=go, daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
