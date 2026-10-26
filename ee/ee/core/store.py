"""会话（线程）存盘：~/.ee/sessions/<id>.jsonl，一行一条记录（只追加）。

记录种类：
  message  发给模型的历史消息（恢复会话用）
  meta     线程元数据（cwd / model / mode / title），后写的覆盖先写的
  item     桌面端显示用的条目（用户消息、助手消息、工具卡片、审批……），同 id 后写的覆盖先写的
  prefix / rewind / reset  内核内部记录
  truncate 编辑重发（D3-E）：丢掉 seq ≥ from_seq 的已有条目（之后新写的条目照常保留）
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class SessionStore:
    def __init__(self, root: Path, session_id: str | None = None, enabled: bool = True):
        self.root = root / "sessions"
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.enabled = enabled
        self.path = self.root / f"{self.id}.jsonl"

    def append(self, kind: str, data: dict) -> None:
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "kind": kind, **data}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def message(self, msg: dict) -> None:
        self.append("message", {"message": msg})

    def meta(self, meta: dict) -> None:
        self.append("meta", {"meta": meta})

    def item(self, item: dict) -> None:
        self.append("item", {"item": item})

    # ---- 读取
    @staticmethod
    def list_sessions(root: Path) -> list[Path]:
        d = root / "sessions"
        return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []

    @staticmethod
    def _records(path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    @staticmethod
    def load_messages(path: Path) -> list[dict]:
        out = []
        for rec in SessionStore._records(path):
            if rec.get("kind") == "message":
                out.append(rec["message"])
            elif rec.get("kind") == "reset":
                out = []
            elif rec.get("kind") == "rewind":
                out = out[: int(rec.get("to", len(out)))]
        return out

    @staticmethod
    def load(path: Path) -> dict:
        """一次读出 messages / items（按首次出现顺序，同 id 取最后一版）/ meta（合并）。"""
        messages: list[dict] = []
        items: dict[str, dict] = {}
        meta: dict = {}
        first_t = last_t = None
        for rec in SessionStore._records(path):
            t = rec.get("t")
            if t is not None:
                first_t = t if first_t is None else first_t
                last_t = t
            kind = rec.get("kind")
            if kind == "message":
                messages.append(rec["message"])
            elif kind == "reset":
                messages = []
            elif kind == "rewind":
                messages = messages[: int(rec.get("to", len(messages)))]
            elif kind == "truncate":
                cut = int(rec.get("from_seq", 0))
                items = {k: v for k, v in items.items() if int(v.get("seq", 0)) < cut}
            elif kind == "meta":
                meta.update(rec.get("meta") or {})
            elif kind == "item":
                it = rec.get("item") or {}
                if it.get("id"):
                    items[it["id"]] = it
        if not items:   # 老会话（终端版）没有 item 记录：从消息粗略还原
            items = {it["id"]: it for it in _items_from_messages(messages)}
        meta.setdefault("created", first_t)
        meta["updated"] = last_t
        if not meta.get("title"):
            first_user = next((it for it in items.values() if it.get("type") == "user"), None)
            meta["title"] = _title(first_user["text"]) if first_user else "新对话"
        ordered = sorted(items.values(), key=lambda it: it.get("seq", 0))
        return {"messages": messages, "items": ordered, "meta": meta}

    @staticmethod
    def summary(path: Path) -> dict:
        d = SessionStore.load(path)
        meta = d["meta"]
        return {"id": path.stem, "title": meta.get("title") or "新对话", "cwd": meta.get("cwd", ""),
                "model": meta.get("model", ""), "mode": meta.get("mode", ""),
                "created": meta.get("created"), "updated": meta.get("updated") or path.stat().st_mtime,
                "parent_id": meta.get("parent_id")}


def strip_context(text: str) -> str:
    return (text or "").split("\n\n<context-update")[0]


def _title(text: str, n: int = 40) -> str:
    t = " ".join(strip_context(text).split())
    return (t[:n] + "…") if len(t) > n else (t or "新对话")


def _items_from_messages(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    tools: dict[str, dict] = {}
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "user":
            text = strip_context(m.get("content") or "")
            if text.startswith("[系统提示]"):
                continue
            items.append({"id": f"m{i}", "type": "user", "text": text})
        elif role == "assistant":
            if m.get("content") or m.get("reasoning"):
                items.append({"id": f"m{i}", "type": "assistant", "text": m.get("content") or "",
                              "reasoning": m.get("reasoning") or ""})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                it = {"id": f"t{tc.get('id')}", "type": "tool", "call_id": tc.get("id"), "name": fn.get("name"),
                      "args": args, "summary": fn.get("name"), "status": "ok", "output": "", "diff": ""}
                tools[tc.get("id")] = it
                items.append(it)
        elif role == "tool":
            it = tools.get(m.get("tool_call_id"))
            if it is not None:
                it["output"] = m.get("content") or ""
    return items
