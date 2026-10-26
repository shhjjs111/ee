"""最小可运行的 AI Agent：一个工具调用循环 + 2 个工具（计算、读文件）。

运行：
    pip install openai
    export DEEPSEEK_API_KEY=你的key
    python agent.py
"""
import ast
import json
import operator
import os
from pathlib import Path

from openai import OpenAI

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
MAX_STEPS = 10  # 单轮最多调用工具的次数，防止死循环

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

# ---------- 工具实现 ----------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("只支持数字和 + - * / // % ** 运算")


def calculate(expression: str) -> str:
    return str(_eval(ast.parse(expression, mode="eval").body))


def read_file(path: str, max_chars: int = 20000) -> str:
    text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] + ("\n...[已截断]" if len(text) > max_chars else "")


TOOLS = {"calculate": calculate, "read_file": read_file}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "calculate",
        "description": "计算数学表达式，例如 '(3+4)*2**10'",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "数学表达式"}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取本地文本文件的内容",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"}},
            "required": ["path"]}}},
]


def run_tool(name: str, args_json: str) -> str:
    """执行工具；出错时把错误信息当结果回喂给模型，让它自己修正。"""
    try:
        args = json.loads(args_json or "{}")
        return TOOLS[name](**args)
    except Exception as e:  # noqa: BLE001
        return f"工具出错: {type(e).__name__}: {e}"


# ---------- 核心循环 ----------
def agent_turn(messages: list) -> str:
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOL_SCHEMAS)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:          # 没有工具调用 = 最终回答
            return msg.content or ""
        for call in msg.tool_calls:     # 执行每个工具，把结果塞回对话
            result = run_tool(call.function.name, call.function.arguments)
            print(f"  [工具] {call.function.name}({call.function.arguments}) -> {result[:200]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "（已达到最大步数，停止。）"


def main():
    messages = [{"role": "system", "content": "你是 ee 的个人助手，用中文简洁回答。需要算数或看文件时调用工具。"}]
    print(f"Agent 已启动（模型 {MODEL}），输入 exit 退出。")
    while True:
        try:
            user = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in {"exit", "quit"}:
            break
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        print("Agent:", agent_turn(messages))


if __name__ == "__main__":
    main()
