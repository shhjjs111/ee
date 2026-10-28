"""测试用的假 MCP 服务器（stdio，一行一条 JSON）。也可 import handle() 给 HTTP 测试用。"""
import json
import os
import sys

TOOLS_P1 = [
    {"name": "echo", "description": "原样返回文字", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": ["string", "null"]}}, "required": ["text"],
                     "additionalProperties": False, "$schema": "http://json-schema.org/draft-07/schema#"}},
]
TOOLS_P2 = [
    {"name": "add", "description": "两数相加",
     "inputSchema": {"type": "object", "$defs": {"N": {"type": "number", "format": "double"}},
                     "properties": {"a": {"$ref": "#/$defs/N"}, "b": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/N"}]}}}},
    {"name": "fail", "description": "总是报错", "inputSchema": {"type": "object"}},
    {"name": "pic", "description": "返回一张图", "inputSchema": {"type": "object"}},
]
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


def handle(msg):
    """返回要回给客户端的消息（或 None）。"""
    m, mid = msg.get("method"), msg.get("id")
    if m is None:
        return None
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "fake", "version": "1"}}}
    if m == "tools/list":
        cur = (msg.get("params") or {}).get("cursor")
        if cur == "p2":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS_P2}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS_P1, "nextCursor": "p2"}}
    if m == "tools/call":
        p = msg.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        if name == "echo":
            if args.get("text") == "__cwd__":
                # 让测试能分辨"这次调用落在哪个工作区的连接上"
                return {"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text", "text": "cwd:" + os.getcwd()}]}}
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"echo:{args.get('text')}"}]}}
        if name == "add":
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(args.get("a", 0) + (args.get("b") or 0))}]}}
        if name == "fail":
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "坏了"}], "isError": True}}
        if name == "pic":
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "image", "data": PNG_B64, "mimeType": "image/png"}]}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}}
    return None


def main():
    if "--crash" in sys.argv:
        print("boom: missing config", file=sys.stderr, flush=True)
        sys.exit(3)
    pinged = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "notifications/initialized" and not pinged:
            pinged = True     # 服务器反向 ping 一次，客户端应回应
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"}) + "\n")
            sys.stdout.flush()
            continue
        if msg.get("id") == "srv-1":
            print("got pong", file=sys.stderr, flush=True)
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
