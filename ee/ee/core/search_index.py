"""会话全文搜索（D4-4）：~/.ee/index/search.db，SQLite FTS5（trigram 分词，中文可用）。

- 索引粒度：一条显示条目（用户消息 / 助手回复 / 工具卡片的摘要与输出）一行。
- 增量同步：files 表记每个会话文件的 mtime+size；搜索前只重建变化了的会话、删掉已消失的会话。
  这样不需要在热路径（每次 append）里写数据库，编辑重发 / 截断 / 覆盖等情况也天然正确。
- 查询：≥3 个字符走 FTS5 MATCH（bm25 排序）；更短（如两个汉字）退回 LIKE 扫描。
- FTS5 不可用时（极少见的 sqlite 编译）整体退回逐文件扫描，不报错。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from ee.core.store import SessionStore, strip_context

MAX_TEXT = 20000          # 单条目最多索引的字符数（工具输出可能很长）
SNIPPET_RADIUS = 36

_lock = threading.Lock()


def _item_text(it: dict) -> str:
    typ = it.get("type")
    if typ == "user":
        return strip_context(it.get("text") or "")
    if typ == "assistant":
        return it.get("text") or ""
    if typ == "tool":
        parts = [str(it.get("summary") or it.get("name") or ""), str(it.get("output") or "")]
        return "\n".join(p for p in parts if p)
    if typ in ("plan", "review", "notice", "error"):
        return str(it.get("text") or it.get("markdown") or "")
    return ""


def snippet(text: str, query: str, radius: int = SNIPPET_RADIUS) -> str:
    flat = " ".join(text.split())
    i = flat.lower().find(query.lower())
    if i < 0:
        terms = [t for t in query.split() if t]
        i = next((flat.lower().find(t.lower()) for t in terms if flat.lower().find(t.lower()) >= 0), -1)
    if i < 0:
        return flat[: radius * 2] + ("…" if len(flat) > radius * 2 else "")
    a, b = max(0, i - radius), min(len(flat), i + len(query) + radius)
    return ("…" if a else "") + flat[a:b] + ("…" if b < len(flat) else "")


class SearchIndex:
    def __init__(self, home: Path):
        self.home = home
        self.sessions = home / "sessions"
        self.db_path = home / "index" / "search.db"
        self.fts = True
        self._conn: sqlite3.Connection | None = None

    # ---- 数据库
    def _db(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.execute("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
                      "text, session_id UNINDEXED, item_id UNINDEXED, kind UNINDEXED, seq UNINDEXED, "
                      "tokenize='trigram')")
        except sqlite3.OperationalError:
            self.fts = False
            c.execute("CREATE TABLE IF NOT EXISTS docs (text TEXT, session_id TEXT, item_id TEXT, kind TEXT, seq INTEGER)")
        c.commit()
        self._conn = c
        return c

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- 同步
    def sync(self) -> int:
        """把索引和 sessions/ 目录对齐，返回重建的会话数。"""
        with _lock:
            c = self._db()
            known = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, mtime, size FROM files")}
            present: dict[str, tuple[float, int, Path]] = {}
            if self.sessions.is_dir():
                for p in self.sessions.glob("*.jsonl"):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    present[p.stem] = (st.st_mtime, st.st_size, p)
            changed = 0
            for sid in set(known) - set(present):
                self._drop(c, sid)
            for sid, (mtime, size, p) in present.items():
                if known.get(sid) == (mtime, size):
                    continue
                self._index_file(c, sid, p, mtime, size)
                changed += 1
            c.commit()
            return changed

    def _drop(self, c: sqlite3.Connection, sid: str) -> None:
        c.execute("DELETE FROM docs WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM files WHERE id = ?", (sid,))

    def _index_file(self, c: sqlite3.Connection, sid: str, p: Path, mtime: float, size: int) -> None:
        c.execute("DELETE FROM docs WHERE session_id = ?", (sid,))
        try:
            d = SessionStore.load(p)
        except Exception:  # noqa: BLE001
            d = {"items": [], "meta": {}}
        rows = []
        title = (d.get("meta") or {}).get("title") or ""
        if title:
            rows.append((title, sid, "", "title", -1))
        for it in d.get("items") or []:
            text = _item_text(it).strip()
            if text:
                rows.append((text[:MAX_TEXT], sid, str(it.get("id") or ""), str(it.get("type") or ""),
                             int(it.get("seq") or 0)))
        c.executemany("INSERT INTO docs (text, session_id, item_id, kind, seq) VALUES (?, ?, ?, ?, ?)", rows)
        c.execute("INSERT OR REPLACE INTO files (id, mtime, size) VALUES (?, ?, ?)", (sid, mtime, size))

    def forget(self, sid: str) -> None:
        with _lock:
            c = self._db()
            self._drop(c, sid)
            c.commit()

    # ---- 查询
    # 单次查询的行数上限：**必须先取够再聚合**，不能拿 limit 当 SQL 上限。
    # 原来用 `LIMIT limit*per_thread*4` 取行、再按会话聚合、最后 `[:limit]`，是全局截断：
    # 一个高频词（wait_for 实测命中 62 条 / 11 个会话）在取到 5 个会话的上限后就把剩下的
    # 会话（包括用户正待着的那个）挤掉了。命中集中在一个会话时尤其荒谬——那个会话自己
    # 就有 6 条命中，却因为别处更相关而整条查不到。
    MAX_ROWS = 2000

    def search(self, query: str, limit: int = 50, per_thread: int = 3,
               session_id: str | None = None) -> list[dict]:
        """返回按会话聚合的命中：[{thread_id, hits: [{item_id, kind, seq, snippet}]}]，最相关的会话在前。

        session_id：只看这一个会话（会话内召回用）。给了它就不会被别的会话挤掉，
        且聚合后按 bm25 顺序返回该会话的全部命中（不受 limit 限制，只有 per_thread 封顶）。
        """
        q = " ".join((query or "").split())
        if not q:
            return []
        self.sync()
        where, params = "", []
        if session_id:
            where = " AND session_id = ?"
            params.append(session_id)
        with _lock:
            c = self._db()
            if self.fts and len(q) >= 3:
                phrase = '"' + q.replace('"', '""') + '"'
                sql = ("SELECT session_id, item_id, kind, seq, text FROM docs WHERE docs MATCH ?"
                       + where + " ORDER BY bm25(docs) LIMIT ?")
                rows = c.execute(sql, (phrase, *params, self.MAX_ROWS)).fetchall()
            else:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                sql = ("SELECT session_id, item_id, kind, seq, text FROM docs "
                       "WHERE text LIKE ? ESCAPE '\\'" + where + " LIMIT ?")
                rows = c.execute(sql, (like, *params, self.MAX_ROWS)).fetchall()
        out: dict[str, dict] = {}
        cap = per_thread if not session_id else max(1, limit)
        for sid, item_id, kind, seq, text in rows:
            g = out.setdefault(sid, {"thread_id": sid, "hits": []})
            if len(g["hits"]) < cap:
                g["hits"].append({"item_id": item_id, "kind": kind, "seq": seq, "snippet": snippet(text, q)})
        return list(out.values())[:1] if session_id else list(out.values())[:limit]


_indexes: dict[str, SearchIndex] = {}


def get_index(home: Path) -> SearchIndex:
    key = os.path.normcase(str(home))
    if key not in _indexes:
        _indexes[key] = SearchIndex(home)
    return _indexes[key]
