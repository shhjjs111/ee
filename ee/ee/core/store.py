"""会话（线程）存盘：~/.ee/sessions/<id>.jsonl，一行一条记录（只追加）。

记录种类：
  message  发给模型的历史消息（恢复会话用）
  meta     线程元数据（cwd / model / mode / title），后写的覆盖先写的
  item     桌面端显示用的条目（用户消息、助手消息、工具卡片、审批……），同 id 后写的覆盖先写的
  prefix   冻结前缀（sha256 + 完整 system/tools + 冻结时的风格），重启恢复会话时逐字节复用
  rewind / reset  内核内部记录
  prune    修剪旧工具输出（D5-10）：把这些下标的 tool 消息换成占位（原 message 记录不动，界面条目另存完整输出）
  truncate 编辑重发（D3-E）：丢掉 seq ≥ from_seq 的已有条目（之后新写的条目照常保留）
  usage    一次模型请求的用量（prompt / 命中 / 未命中归因 / 花费）。只追加、不参与历史重建，
           用于事后算真实的缓存命中率——turn_end 的累计快照看不出「哪一次请求漏了多少」
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable

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
    # 写失败的观测状态也做成类属性默认值：有调用方（含测试）用 `__new__` 绕过 __init__，
    # 只设 enabled/root/path 就写；实例属性默认值必须能在那条路径上也成立。
    failed = 0
    last_error: str | None = None
    on_fail: Callable[[SessionStore, OSError], None] | None = None
    on_write: Callable[[SessionStore], None] | None = None

    # 日志节流：磁盘满时每条记录都会失败，逐条打 error 会把日志撑爆，但也不能只报一次
    # （用户看日志时该知道还在失败）。第 1 次必报，之后每这么多次报一次。
    LOG_EVERY = 50

    def __init__(self, root: Path, session_id: str | None = None, enabled: bool = True):
        self.root = root / "sessions"
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.enabled = enabled
        self.path = self.root / f"{self.id}.jsonl"
        self._tail_checked = False
        # 写失败的可观测状态：failed 是累计次数，last_error 是最后一次的原因。
        # on_fail(store, 异常) 让上层（会话 / 界面）能立刻反应，而不是等有人来查这个属性。
        self.failed = 0
        self.last_error: str | None = None
        self.on_fail: Callable[[SessionStore, OSError], None] | None = None
        # 写成功一次就回调（不是“从失败恢复”）：上层据此把上一次的失败状态清掉。
        # 上层自己做成幂等的（已无失败状态就直接 return），所以这里每次都叫一声。
        self.on_write: Callable[[SessionStore], None] | None = None

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
        except OSError as e:
            # 不能静默：磁盘满 / 杀软占用 / 权限问题时，对话看起来正常但没落盘，
            # 重启即丢，用户完全不知道。记日志 + 记次数，并通知挂上来的上层（on_fail）；
            # threads 会把这件事变成界面上的提示（见 Thread._on_store_fail）。
            self.failed += 1
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            if self.failed == 1 or self.failed % self.LOG_EVERY == 0:
                log.error("会话记录写入失败（第 %d 次）：%s：%s；这条记录没落盘，重启后会丢",
                          self.failed, self.path.name, e)
            cb = self.on_fail
            if cb is not None:
                try:
                    cb(self, e)
                except Exception:  # noqa: BLE001  上报本身出问题不能连累写入方的其他逻辑
                    log.warning("会话写盘失败的上报回调出错", exc_info=True)
        else:
            if self.failed:
                # 之前失败过、这次写成功了（用户清了磁盘 / 关掉了占着文件的程序）：
                # 失败状态必须能自己消失，否则界面上那条「保存不了」会永远挂着。
                self.failed = 0
                self.last_error = None
            cb_ok = self.on_write
            if cb_ok is not None:
                try:
                    cb_ok(self)
                except Exception:  # noqa: BLE001
                    log.warning("会话写盘成功的回调出错", exc_info=True)

    def message(self, msg: dict) -> None:
        self.append("message", {"message": msg})

    def meta(self, meta: dict) -> None:
        self.append("meta", {"meta": meta})

    def item(self, item: dict) -> None:
        self.append("item", {"item": item})

    def usage(self, record: dict) -> None:
        """记一次模型请求的用量（逐请求账本）。

        与 turn_end 的累计快照互补：快照只告诉你「到现在一共花了多少」，回答不了
        「哪一次请求没命中、那一次前面刚发生了什么」。命中率能不能优化，全靠这些原始行。
        只在主会话写：子代理 persist=False，append() 自己就 no-op，它的那一笔由
        subagent._merge_usage 改写成 role=subagent 后随父会话落盘。
        """
        self.append("usage", {"usage": record})

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
    def usage_records(path: Path) -> list[dict]:
        """逐请求用量账本，按写入顺序。老会话没有这类记录时返回空列表（不报错）。

        不做成 `load()` 的字段：`load()` 是恢复会话的热路径（每次打开会话都走），
        而账本只有诊断才会读——诊断时按需扫一遍，比让每次恢复都多解析一整批记录划算。
        """
        out = []
        for rec in SessionStore._records(path):
            if rec.get("kind") == "usage" and isinstance(rec.get("usage"), dict):
                out.append(rec["usage"])
        return out

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
        return _fill_orphan_tool_calls(out)

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
                meta.pop("reasoning_strip_to", None)      # 历史重建：旧游标指向的消息已经不在了
            elif kind == "rewind":
                messages = messages[: _int(rec.get("to"), len(messages))]
                meta.pop("reasoning_strip_to", None)
            elif kind == "prune":
                _apply_prune(messages, rec)
            elif kind == "reasoning_strip":
                # 旧推理清理的游标：历史下标，只前进。重建/截断时上面的分支会清掉它。
                to = _int(rec.get("to"), -1)
                if to >= 0:
                    meta["reasoning_strip_to"] = to
            elif kind == "truncate":
                cut = _int(rec.get("from_seq"), 0)
                items = {k: v for k, v in items.items() if _int(v.get("seq"), 0) < cut}
                meta.pop("reasoning_strip_to", None)
            elif kind == "meta":
                if isinstance(rec.get("meta"), dict):
                    meta.update(rec["meta"])
            elif kind == "item":
                it = rec.get("item")
                if isinstance(it, dict) and it.get("id"):
                    items[it["id"]] = it
        if not items:   # 老会话（终端版）没有 item 记录：从消息粗略还原
            items = {it["id"]: it for it in _items_from_messages(messages)}
        # 孤儿 tool_calls 也要在这里补：这是**生产恢复路径**（app.py:223 / headless.py:349 /
        # cli.py:147 都走 load()），只补 load_messages() 等于只修了测试。
        messages = _fill_orphan_tool_calls(messages)
        meta.setdefault("created", first_t)
        meta["updated"] = last_t
        # 上一个进程中途被杀留下的 running 按 interrupted 呈现（D6-1 U7）：不这么做的话
        # 重启后 terminal_reason 是 None，界面和编排都会把中断的回合当成「空闲且正常结束」。
        _close_stale_runs(meta)
        if not meta.get("title"):
            first_user = next((it for it in items.values() if it.get("type") == "user"), None)
            meta["title"] = _title(first_user["text"]) if first_user else "新对话"
        ordered = sorted(items.values(), key=lambda it: _int(it.get("seq"), 0))
        _close_pending_items(ordered)
        return {"messages": messages, "items": ordered, "meta": meta}

    @staticmethod
    def summary(path: Path) -> dict:
        """只扫 summary 真正用到的字段，不构造 messages / 全部 items。

        `load()` 会逐行解析并把每条消息、每个条目都建成对象；而 thread/list
        最多要对几百个会话文件逐个调 summary()（app.py 的 `_thread_list` /
        `_thread_search`），大仓库里这一步就是几十 MB 的 JSON 解析。
        这里只跟踪：时间戳、meta 记录、用户条目的 seq/文本（标题回退用）。
        结果与 `load()` 派生出的 summary 逐字段一致（见 test_store_summary.py）。
        """
        meta: dict = {}
        first_t = last_t = None
        cut = 0
        item_seqs: dict[str, int] = {}      # 所有条目的 id → seq（只判“有没有条目”）
        users: dict[str, dict] = {}         # 用户条目：同 id 后写的覆盖先写的
        first_msg: tuple[int, str] | None = None   # 老会话（无 item 记录）的标题回退
        n_msg = 0
        for rec in SessionStore._records(path):
            t = rec.get("t")
            if t is not None:
                first_t = t if first_t is None else first_t
                last_t = t
            kind = rec.get("kind")
            if kind == "message":
                m = rec.get("message")
                if isinstance(m, dict):
                    if m.get("role") == "user" and first_msg is None:
                        text = strip_context(m.get("content") or "")
                        if not text.startswith("[系统提示]"):
                            first_msg = (n_msg, text)
                    n_msg += 1
            elif kind == "reset":
                meta.pop("reasoning_strip_to", None)
                first_msg, n_msg = None, 0
            elif kind == "rewind":
                to = _int(rec.get("to"), n_msg)
                meta.pop("reasoning_strip_to", None)
                n_msg = min(n_msg, to)
                if first_msg is not None and first_msg[0] >= to:
                    first_msg = None
            elif kind == "item":
                it = rec.get("item")
                if isinstance(it, dict) and it.get("id"):
                    seq = _int(it.get("seq"), 0)
                    item_seqs[it["id"]] = seq
                    if it.get("type") == "user":
                        users[it["id"]] = it
            elif kind == "truncate":
                cut = _int(rec.get("from_seq"), 0)
                item_seqs = {k: v for k, v in item_seqs.items() if v < cut}
                users = {k: v for k, v in users.items() if _int(v.get("seq"), 0) < cut}
            elif kind == "meta":
                if isinstance(rec.get("meta"), dict):
                    meta.update(rec["meta"])
        meta.setdefault("created", first_t)
        meta["updated"] = last_t
        _close_stale_runs(meta)
        if not meta.get("title"):
            # 与 load() 的标题回退一致：有条目就看首个用户条目，完全没有条目
            # （终端版老会话）才从 messages 还原。
            if item_seqs:
                first_user = min(users.values(), key=lambda it: _int(it.get("seq"), 0), default=None)
                meta["title"] = _title(first_user.get("text") or "") if first_user else "新对话"
            else:
                meta["title"] = _title(first_msg[1]) if first_msg else "新对话"
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


# 回合被打断时补进历史、占住工具结果位置的那句话（与服务端约束对齐：tool_calls 必须跟齐 tool 消息）
ORPHAN_TOOL_CONTENT = "（这一回合被中断，此调用没有结果）"


def _fill_orphan_tool_calls(messages: list[dict]) -> list[dict]:
    """补上带 tool_calls 的 assistant 后面缺失的 tool 消息。

    落盘顺序是「assistant(tool_calls) 先写 → 整个工具执行期 → tool 结果再写」。中间被
    杀进程 / 关窗，历史里就留下孤儿 tool_calls。服务端要求每个 tool_call_id 都有对应的
    tool 消息，缺一条这条会话之后每次请求都 400，用户只能删会话。

    只补缺失的，不重排、不改已有消息；补齐的占位紧跟在该 assistant 之后（与真实顺序一致）。
    """
    if not any(isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
               for m in messages):
        return messages
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        out.append(m)
        i += 1
        calls = m.get("tool_calls") if isinstance(m, dict) and m.get("role") == "assistant" else None
        if not calls:
            continue
        want = [c.get("id") for c in calls if isinstance(c, dict) and c.get("id")]
        # 紧跟其后的连续 tool 消息就是这批调用的结果（容忍缺项、容忍乱序）
        have: set[str] = set()
        while i < n and isinstance(messages[i], dict) and messages[i].get("role") == "tool":
            cid = messages[i].get("tool_call_id")
            if cid:
                have.add(cid)
            out.append(messages[i])
            i += 1
        for cid in want:
            if cid not in have:
                out.append({"role": "tool", "tool_call_id": cid, "content": ORPHAN_TOOL_CONTENT,
                            "_orphan": True})
    return out


def _close_stale_runs(meta: dict) -> None:
    """把「上次进程被杀时还在跑」的回合按 interrupted 呈现（D6-1 U7）。

    进程在回合中途被杀时，磁盘上留着 `status="running"` 的 run，`terminal_reason` 是 None。
    读的时候必须报成 interrupted：`status` 只有 idle/running，单靠它推不出「跑完了」还是
    「被杀」，界面和编排都会把中断的回合当成「空闲」——编排据此判定任务已完成是最坏的结果。

    判定依据是 pid 是否**还活着**，而不是「pid 不是本进程」。后者的前提是「写这条 running
    的不是当前进程，就一定不是正在跑」——桌面端后端与 `ee exec --resume` 共用同一份
    sessions/<id>.jsonl 时这个前提不成立：任何一方 load 时，另一方正在跑的 run 都会被改成
    interrupted，随后任何一次 save_meta 又会把这份被污染的 meta 整份写回磁盘（假的 interrupted
    被持久化，用户在这期间改的会话名/模型/模式也被旧快照覆盖）。编排按 terminal_reason 判断
    子任务是否结束，把还在跑的子任务当已结束并据此收尾/重派，是这条最坏的后果。

    老记录没有 pid 字段，同样只能来自已经不在的进程。

    只改返回值，**不写回磁盘**：查询不该有副作用（I9 的同一条精神），而且每次读到同样结果。
    finished 留 None——不知道确切时刻就不编造。
    """
    runs = meta.get("runs")
    if not isinstance(runs, list):
        return
    me = os.getpid()
    from ee.server.lifeline import pid_alive      # 延迟导入：core 不该在导入期依赖 server
    for r in runs:
        if not (isinstance(r, dict) and r.get("status") == "running"):
            continue
        pid = r.get("pid")
        if pid == me:
            continue                    # 本进程创建的 run：内存里那个任务说了算
        if pid and pid_alive(int(pid)):
            continue                    # 另一个还活着的 ee 进程正在跑它，别替它判死
        r["status"] = "done"
        r["terminal_reason"] = "interrupted"
    meta["runs"] = runs


def _close_pending_items(items: list[dict]) -> None:
    """把上一个进程留下的 pending 审批/提问条目标成 cancelled。

    这两类条目在创建时就把 `status: "pending"` 落盘了（threads.py 里 persist=True），
    进程被杀时必然残留。而重启后 Thread.approvals 是新进程里的空表，respond()/answer()
    一定抛「这个审批已经处理过或已失效」——界面上却还是可点的卡片，看起来有一个
    永远批准不了的阻塞操作。

    与 _close_stale_runs 同一套做法：只改返回值，不写回磁盘。
    """
    for it in items:
        if isinstance(it, dict) and it.get("status") == "pending" \
                and it.get("type") in ("approval", "question"):
            it["status"] = "cancelled"


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
            cid = m.get("tool_call_id")
            it = tools.get(cid) if isinstance(cid, str) else None
            if it is not None:
                it["output"] = m.get("content") or ""
    # 补 seq（显示顺序）。没有它，`detail(tail=N)` 切出来的那一页条目都缺游标，
    # 前端 `loadEarlier` 拿不到 `before_seq` 就只能停在原地——超过一页的老会话翻不上去。
    for n, it in enumerate(items, 1):
        it.setdefault("seq", n)
    return items
