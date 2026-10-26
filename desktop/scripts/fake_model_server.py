"""演示 / 截图用的假 OpenAI Chat Completions 服务器（SSE 流式），不需要任何真实 key。

用法：python scripts/fake_model_server.py [端口，默认 18080]
然后在 设置 → 模型与供应商 里加一个供应商：base_url = http://127.0.0.1:18080/v1，key 随便填。

脚本化的一次“修 bug”回合：计划 → 读文件/搜索 → 编辑 → 跑测试（需要批准）→ 更新计划 → 总结。
每次收到的请求按“上一条消息是什么”决定下一步，所以可以反复演示。
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(__import__("os").environ.get("FAKE_DELAY", "0.015"))

PLAN1 = [
    {"content": "阅读 calc.py 和测试，定位 bug", "status": "in_progress"},
    {"content": "修复 average() 的除零问题", "status": "pending"},
    {"content": "运行测试确认通过", "status": "pending"},
]
PLAN2 = [
    {"content": "阅读 calc.py 和测试，定位 bug", "status": "completed"},
    {"content": "修复 average() 的除零问题", "status": "completed"},
    {"content": "运行测试确认通过", "status": "completed"},
]

FINAL = """测试已经全部通过 ✅

**问题原因**：`average()` 直接用 `sum(xs) / len(xs)`，传入空列表时会抛出 `ZeroDivisionError`，`test_average_empty` 期望返回 `0.0`。

**修改**（`src/calc.py`）：

```python
def average(xs: list[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)
```

| 用例 | 结果 |
| --- | --- |
| `test_add` | 通过 |
| `test_average` | 通过 |
| `test_average_empty` | 通过（之前失败） |

如果你希望空列表时抛出更明确的异常（比如 `ValueError`），告诉我，我改成那种写法并同步更新测试。"""


def step_for(messages: list[dict]) -> int:
    """根据历史里已有的 tool 结果数量决定走到哪一步。"""
    last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    tools_after = [m for m in messages[last_user:] if m.get("role") == "tool"]
    return {0: 0, 4: 1, 5: 2, 6: 3}.get(len(tools_after), 3 if len(tools_after) > 6 else 0)


def tc(i, cid, name, args):
    return {"index": i, "id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for f in frames:
            self.wfile.write(f"data: {json.dumps(f, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(DELAY)
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "deepseek-flash", "object": "model"}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", "x")}

        def ch(delta, finish=None, usage=None):
            f = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                f["usage"] = usage
            return f

        def text(s, n=4):
            return [ch({"content": s[i:i + n]}) for i in range(0, len(s), n)]

        def think(s, n=6):
            return [ch({"reasoning_content": s[i:i + n]}) for i in range(0, len(s), n)]

        usage = {"prompt_tokens": 9000, "completion_tokens": 240, "prompt_cache_hit_tokens": 8192}
        step = step_for(body.get("messages", []))
        if step == 0:
            frames = think("用户说测试失败。先列个计划，然后读 calc.py 和测试文件，再搜一下 average 的调用点。") + \
                text("我先看一下代码和测试，找出失败的原因。") + [
                ch({"tool_calls": [tc(0, "c_todo", "todo", {"todos": PLAN1})]}),
                ch({"tool_calls": [tc(1, "c_r1", "read", {"path": "src/calc.py"})]}),
                ch({"tool_calls": [tc(2, "c_r2", "read", {"path": "tests/test_calc.py"})]}),
                ch({"tool_calls": [tc(3, "c_g1", "grep", {"pattern": "average\\(", "output_mode": "files"})]}),
                ch({}, "tool_calls", usage)]
        elif step == 1:
            frames = think("average 对空列表会除零。改成先判断空列表返回 0.0。") + \
                text("问题在 `average()`：空列表会除以零。我来修复它。") + [
                ch({"tool_calls": [tc(0, "c_e1", "edit", {
                    "path": "src/calc.py",
                    "old_string": "def average(xs: list[float]) -> float:\n    return sum(xs) / len(xs)",
                    "new_string": "def average(xs: list[float]) -> float:\n    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)"})]}),
                ch({}, "tool_calls", usage)]
        elif step == 2:
            frames = text("改好了。确认一下测试依赖，然后跑测试。") + [
                ch({"tool_calls": [tc(0, "c_s1", "shell", {"command": "pip install -q pytest && python -m pytest -q"})]}),
                ch({}, "tool_calls", usage)]
        else:
            frames = [ch({"tool_calls": [tc(0, "c_todo2", "todo", {"todos": PLAN2})]}),
                      ch({}, "tool_calls", usage)] if not any(
                m.get("tool_call_id") == "c_todo2" for m in body.get("messages", [])) else \
                text(FINAL, 6) + [ch({}, "stop", usage)]
        self._send(frames)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"fake model server on http://127.0.0.1:{port}/v1", flush=True)
    srv.serve_forever()
