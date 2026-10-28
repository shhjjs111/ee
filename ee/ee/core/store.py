"""会话（线程）存盘：~/.ee/sessions/<id>.jsonl，一行一条记录（只追加）。

记录种类：
  message  发给模型的历史消息（恢复会话用）
  meta     线程元数据（cwd / model / mode / title），后写的覆盖先写的
  item     桌面端显示用的条目（用户消息、助手消息、工具卡片、审批……），同 id 后写的覆盖先写的
  prefix   冻结前缀（sha256 + 完整 system/tools + 冻结时的风格），重启恢复会话时逐字节复用
  rewind / reset  内核内部记录
  prune    修剪旧工具输出（D5-10）：把这些下标的 tool 消息换成占位（原 message 记录不动，界面条目另存完整输出）
  truncate 编辑重发（D3-E）：丢掉 seq ≥ from_seq 的已有条目（之后新写的条目照常保留）
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger("ee.store")


def _ends_with_newline(path: Path) -> bool:
    """文件不存在、为空或以换行结尾返回 True。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return True
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except OSError:
        return True


class SessionStore:
    _tail_checked = False          # 本进程第一次追加前检查过文件末尾是否有没写完的半行

    def __init__(self, root: Path, session_id: str | None = None, enabled: bool = True):
        self.root = root / "sessions"
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.enabled = enabled
        self.path = self.root / f"{self.id}.jsonl"
        self._tail_checked = False

    def append(self, kind: str, data: dict) -> None:
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            from ee.core.secrets import redact
            line = (redact(json.dumps({"t": time.time(), "kind": kind, **data}, ensure_ascii=False)) + "\n").encode("utf-8")
            if not self._tail_checked:
                # 上次崩溃可能留下没写完的半行：先补一个换行，新记录另起一行，不和半行粘成一条坏行
                self._tail_checked = True
                if not _ends_with_newline(self.path):
                    line = b"\n" + line
            with open(self.path, "ab") as f:
                f.write(line)
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
        """逐行读出记录。坏行（崩溃时写了一半、不是合法 UTF-8 / JSON、不是对象）跳过并记日志，不影响其他行。"""
        try:
            raw = path.read_bytes()
        except OSError:
            return
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        bad = 0
        for no, line in enumerate(raw.split(b"\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                rec, why = None, e
            else:
                why = "不是 JSON 对象"
            if not isinstance(rec, dict):
                bad += 1
                if bad <= 5:
                    log.warning("会话文件 %s 第 %d 行损坏（可能是崩溃时没写完），已跳过：%s", path.name, no, str(why)[:120])
                continue
            yield rec
        if bad > 5:
            log.warning("会话文件 %s 共跳过 %d 行坏记录", path.name, bad)

    @staticmethod
    def load_messages(path: Path) -> list[dict]:
        out = []
        for rec in SessionStore._records(path):
            if rec.get("kind") == "message":
                if isinstance(rec.get("message"), dict):
                    out.append(rec["message"])
            elif rec.get("kind") == "reset":
                out = []
            elif rec.get("kind") == "rewind":
                out = out[: _int(rec.get("to"), len(out))]
            elif rec.get("kind") == "prune":
                _apply_prune(out, rec)
        return out

    @staticmethod
    def last_prefix(path: Path, compat: str) -> dict | None:
        """最后一条带完整 system/tools、且 compat 相同的 prefix 记录（恢复会话时复用，保住前缀缓存）。"""
        found = None
        for rec in SessionStore._records(path):
            if rec.get("kind") == "prefix" and rec.get("compat") == compat and "system" in rec and "tools" in rec:
                found = rec
        return found

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
                if isinstance(rec.get("message"), dict):
                    messages.append(rec["message"])
            elif kind == "reset":
                messages = []
            elif kind == "rewind":
                messages = messages[: _int(rec.get("to"), len(messages))]
            elif kind == "prune":
                _apply_prune(messages, rec)
            elif kind == "truncate":
                cut = _int(rec.get("from_seq"), 0)
                items = {k: v for k, v in items.items() if _int(v.get("seq"), 0) < cut}
            elif kind == "meta":
                if isinstance(rec.get("meta"), dict):
                    meta.update(rec["meta"])
            elif kind == "item":
                it = rec.get("item")
                if isinstance(it, dict) and it.get("id"):
                    items[it["id"]] = it
        if not items:   # 老会话（终端版）没有 item 记录：从消息粗略还原
            items = {it["id"]: it for it in _items_from_messages(messages)}
        meta.setdefault("created", first_t)
        meta["updated"] = last_t
        # 上一个进程中途被杀留下的 running 按 interrupted 呈现（D6-1 U7）：不这么做的话
        # 重启后 terminal_reason 是 None，界面和编排都会把中断的回合当成「空闲且正常结束」。
        _close_stale_runs(meta)
        if not meta.get("title"):
            first_user = next((it for it in items.values() if it.get("type") == "user"), None)
            meta["title"] = _title(first_user["text"]) if first_user else "新对话"
        ordered = sorted(items.values(), key=lambda it: _int(it.get("seq"), 0))
        return {"messages": messages, "items": ordered, "meta": meta}

    @staticmethod
    def summary(path: Path) -> dict:
        d = SessionStore.load(path)
        meta = d["meta"]
        return {"id": path.stem, "title": meta.get("title") or "新对话", "cwd": meta.get("cwd", ""),
                "model": meta.get("model", ""), "mode": meta.get("mode", ""),
                "created": meta.get("created"), "updated": meta.get("updated") or path.stat().st_mtime,
                "parent_id": meta.get("parent_id"), "source": meta.get("source"),
                "orchestrator_parent_id": meta.get("orchestrator_parent_id"),
                "orchestrator_role": meta.get("orchestrator_role"),
                "runs": list(meta.get("runs") or []),
                "worktree": meta.get("worktree"),
                "workspace": (meta.get("worktree") or {}).get("repo") or meta.get("cwd", "")}


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _close_stale_runs(meta: dict) -> None:
    """把「上次进程被杀时还在跑」的回合按 interrupted 呈现（D6-1 U7）。

    进程在回合中途被杀时，磁盘上留着 `status="running"` 的 run，`terminal_reason` 是 None。
    读的时候必须报成 interrupted：`status` 只有 idle/running，单靠它推不出「跑完了」还是
    「被杀」，界面和编排都会把中断的回合当成「空闲」——编排据此判定任务已完成是最坏的结果。

    判定依据是 pid：写这条 running 的不是当前进程，就一定不是"正在跑"（本进程创建的 run
    在内存里，不会走读路径）。老记录没有 pid 字段，同样只能来自已经不在的进程。

    只改返回值，**不写回磁盘**：查询不该有副作用（I9 的同一条精神），而且每次读到同样结果。
    finished 留 None——不知道确切时刻就不编造。
    """
    runs = meta.get("runs")
    if not isinstance(runs, list):
        return
    me = os.getpid()
    for r in runs:
        if isinstance(r, dict) and r.get("status") == "running" and r.get("pid") != me:
            r["status"] = "done"
            r["terminal_reason"] = "interrupted"
    meta["runs"] = runs


def _apply_prune(messages: list[dict], rec: dict) -> None:
    from ee.core.prune import apply
    try:
        apply(messages, list(rec.get("items") or []))
    except (TypeError, ValueError, AttributeError) as e:   # 坏的 prune 记录：跳过，不影响打开会话
        log.warning("跳过一条无法解析的修剪记录：%s", e)


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
