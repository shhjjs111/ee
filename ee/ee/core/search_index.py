"""会话全文搜索（D4-4）：~/.ee/index/search.db，SQLite FTS5（trigram 分词，中文可用）。

- 索引粒度：一条显示条目（用户消息 / 助手回复 / 工具卡片的摘要与输出）一行。
- 增量同步：files 表记每个会话文件的 mtime + 已索引到的字节偏移。会话文件只追加
  （SessionStore 只有 `"ab"`），所以搜索前**只读尾巴**，不再把整份 JSONL 重新解析、
  也不再把该会话的 FTS 行全部删了重插。编辑重发 / 截断通过 `truncate` 记录处理。
- 删除靠 docmap（(session_id,item_id) → FTS rowid）定位：FTS5 表没有二级索引，
  `DELETE ... WHERE session_id=?` 只能全表扫（实测 39k 行的库删一个 3.8k 条的会话要 3.8s）。
- 查询：≥3 个字符走 FTS5 MATCH（bm25 排序）；更短（如两个汉字）退回 LIKE 扫描。
- FTS5 不可用时（极少见的 sqlite 编译）整体退回 LIKE 扫描，不报错。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from ee.core.store import strip_context

MAX_TEXT = 20000          # 单条目最多索引的字符数（工具输出可能很长）
SNIPPET_RADIUS = 36

_lock = threading.Lock()


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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


def _is_missing_fts5(exc: Exception) -> bool:
    """建虚拟表失败的原因是不是「这个 sqlite 没有编 FTS5」。

    并发首次建库时 sqlite 抛的也是 OperationalError（database is locked）。
    把那种瞬时错误也当成「不支持 FTS」，会让这个进程此后的每条搜索都退化成全表扫，
    而且真正的锁错误被伪装成「不支持 FTS」，排查时看不到任何异常。
    """
    msg = str(exc).lower()
    return "fts5" in msg or "no such module" in msg


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
        c = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        try:
            # WAL：桌面端后端与 ee exec 共用同一个 search.db。默认的 rollback journal 下
            # 读者的 SHARED 锁会顶住对方的 COMMIT，一路顶到 timeout 才报 database is locked。
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass                    # 网络盘等不支持 WAL 的存储：退回默认，不影响功能
        c.execute("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
        # off：已经并进索引的字节数（= 已索引字节数，也就是下一次从哪读）。
        # 会话文件只追加（SessionStore 只有 "ab"），所以搜索前只读尾巴。
        # 老库没这一列：新增后为 NULL，第一次同步时「(mtime,size) 没变」的会话直接回填。
        if "off" not in {r[1] for r in c.execute("PRAGMA table_info(files)")}:
            try:
                c.execute("ALTER TABLE files ADD COLUMN off INTEGER")
            except sqlite3.OperationalError as e:
                # 两个进程同时首次打开（桌面端 + ee exec）时会双双看到“没有 off”，
                # 后到的那个 ALTER 报 duplicate column。这不是错误，是竞态已被人代劳。
                if "duplicate column" not in str(e).lower():
                    raise
        # docmap：(session_id,item_id) → FTS rowid。FTS5 表没有二级索引，
        # `DELETE ... WHERE session_id=?` 只能全表扫——实测 39k 行的库里删一个 3.8k 条的
        # 会话要 3.8 秒，几乎就是整次重建的全部成本。有了映射，删除是索引查找 + 按 rowid 删。
        c.execute("CREATE TABLE IF NOT EXISTS docmap (doc_rowid INTEGER PRIMARY KEY, "
                  "session_id TEXT NOT NULL, item_id TEXT NOT NULL, seq INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS docmap_session ON docmap(session_id)")
        # 复合索引单独建：索引建立在 rebuild 时每条 item 都要查一次「同 id 的旧行」，
        # 只有 session_id 索引时每次都要扫完该会话的全部行（3841 条 × 3841 次 = 45s）。
        c.execute("CREATE INDEX IF NOT EXISTS docmap_item ON docmap(session_id, item_id)")
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='docs'").fetchone()
        if row and "fts5" not in (row[0] or "").lower():
            # 上一次降级留下的普通 docs 表。不删掉的话，下面的 IF NOT EXISTS 会因同名表已存在
            # 而**静默跳过**，本进程带着 fts=True 去跑 `docs MATCH ?`，查询时才抛错。
            # 索引是纯缓存（能从 sessions/ 重建），删表重建比让用户手动删 search.db 合适。
            # IF EXISTS：并发首次打开时两个进程可能都看到旧表，后到的那个不能报错。
            # 映射表也要清：旧 rowid 会被新表重用，留着会让删除打到别人的行上。
            c.execute("DROP TABLE IF EXISTS docs")
            c.execute("DELETE FROM docmap")
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
                      "text, session_id UNINDEXED, item_id UNINDEXED, kind UNINDEXED, seq UNINDEXED, "
                      "tokenize='trigram')")
        except sqlite3.OperationalError as e:
            # 只有「这个 sqlite 没有 FTS5」才该降级。并发首次建库时 sqlite 抛的恰恰也是
            # OperationalError（database is locked）；一次偶发锁不能让这个进程此后的每条搜索
            # 都变成 LIKE 全表扫——实例被 get_index() 永久缓存，进程内不会自行恢复，
            # 而真正的锁错误还被伪装成「不支持 FTS」，排查时看不到任何异常。
            if not _is_missing_fts5(e):
                c.close()
                raise
            self.fts = False
            c.execute("CREATE TABLE IF NOT EXISTS docs (text TEXT, session_id TEXT, item_id TEXT, kind TEXT, seq INTEGER)")
        # 老库升级：用 docs 里现成的列回填映射，不重建索引、不重扫会话文件。
        # 只在映射为空时做，并与下面的 commit 同事务：写到一半崩掉会整体回滚，
        # 不会留下半张映射表（分不清哪些行有映射，删除就会漏）。
        if not c.execute("SELECT 1 FROM docmap LIMIT 1").fetchone():
            # OR IGNORE：两个进程同时迁移时，先到的那个先填完，后到的这条不报主键冲突。
            c.execute("INSERT OR IGNORE INTO docmap(doc_rowid, session_id, item_id, seq) "
                      "SELECT rowid, session_id, item_id, seq FROM docs")
        c.commit()
        self._conn = c
        return c

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- 同步
    def sync(self) -> int:
        """把索引和 sessions/ 目录对齐，返回更新过的会话数。

        没变过的会话只看 stat；变过的会话**从上次的字节偏移往下接**，不再整份重建。
        这个前提是「会话文件只追加」（`SessionStore` 只有 `"ab"`，全仓库没有改写
        会话文件的路径）。将来若加入重写/压缩，这里必须退回 `off=0` 的整份重建。
        """
        with _lock:
            c = self._db()
            known = {r[0]: (r[1], r[2], r[3])
                     for r in c.execute("SELECT id, mtime, size, off FROM files")}
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
                prev = known.get(sid)
                if prev is not None:
                    pmtime, psize, poff = prev
                    if (pmtime, psize) == (mtime, size):
                        if poff is None:          # 老库升级：它已经按整份索引过
                            c.execute("UPDATE files SET off = ? WHERE id = ?", (size, sid))
                        continue
                    if poff is not None and 0 <= poff <= size:
                        self._index_file(c, sid, p, off=poff)
                        changed += 1
                        continue
                self._index_file(c, sid, p, off=0)   # 新文件 / 文件变小 / 老库首次：整份
                changed += 1
            c.commit()
            return changed

    def _drop(self, c: sqlite3.Connection, sid: str) -> None:
        for (rid,) in c.execute("SELECT doc_rowid FROM docmap WHERE session_id = ?", (sid,)).fetchall():
            c.execute("DELETE FROM docs WHERE rowid = ?", (rid,))
        c.execute("DELETE FROM docmap WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM files WHERE id = ?", (sid,))

    def _index_file(self, c: sqlite3.Connection, sid: str, p: Path, off: int = 0) -> None:
        """把 [off, EOF) 里的新记录并进索引；off=0 表示整份重建。

        只消费到最后一个换行：崩溃留下的半行下次还会读到（SessionStore 下次追加前会先
        补一个换行），不能在 off 里把它跳过去。
        """
        try:
            st = p.stat()
            with open(p, "rb") as f:
                f.seek(off)
                raw = f.read()
        except OSError:
            return
        end = raw.rfind(b"\n") + 1
        body = raw[:end]
        if off == 0:
            self._drop(c, sid)
            if body.startswith(b"\xef\xbb\xbf"):
                body = body[3:]
        for line in body.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue                    # 坏行（崩溃写了一半）：跳过，不影响其它行
            if isinstance(rec, dict):
                self._apply_record(c, sid, rec)
        # size 存的是**已索引字节数**（不是文件大小）：读完到落库之间文件又长了，
        # 下一条 stat 就会不等，于是下一次 sync 从 off 接着读，不会漏行。
        c.execute("INSERT OR REPLACE INTO files (id, mtime, size, off) VALUES (?, ?, ?, ?)",
                  (sid, st.st_mtime, off + end, off + end))

    def _apply_record(self, c: sqlite3.Connection, sid: str, rec: dict) -> None:
        """一条会话记录进索引。语义必须和「整份重读」一致：

        - item：同 id 后写的覆盖先写的（工具卡片会从 running 更新成 ok）
        - meta：标题变了要换掉旧标题行（它单独占一行，item_id 为空）
        - truncate：seq ≥ from_seq 的条目整批删掉（编辑重发 / 回退）
        """
        kind = rec.get("kind")
        if kind == "item":
            it = rec.get("item")
            if not isinstance(it, dict) or not it.get("id"):
                return
            item_id = str(it.get("id"))
            text = _item_text(it).strip()
            self._forget_item(c, sid, item_id)
            if text:
                self._insert_doc(c, sid, item_id, str(it.get("type") or ""), _int(it.get("seq")),
                                 text[:MAX_TEXT])
        elif kind == "meta":
            meta = rec.get("meta")
            if not isinstance(meta, dict) or "title" not in meta:
                return
            title = str(meta.get("title") or "").strip()
            self._forget_item(c, sid, "")
            if title:
                self._insert_doc(c, sid, "", "title", -1, title[:MAX_TEXT])
        elif kind == "truncate":
            cut = _int(rec.get("from_seq"))
            doomed = [r[0] for r in c.execute("SELECT doc_rowid, seq FROM docmap WHERE session_id = ?", (sid,))
                      if _int(r[1]) >= cut]
            for rid in doomed:
                self._delete_doc(c, rid)

    @staticmethod
    def _insert_doc(c: sqlite3.Connection, sid: str, item_id: str, kind: str, seq: int, text: str) -> None:
        cur = c.execute("INSERT INTO docs (text, session_id, item_id, kind, seq) VALUES (?, ?, ?, ?, ?)",
                        (text, sid, item_id, kind, seq))
        c.execute("INSERT OR REPLACE INTO docmap (doc_rowid, session_id, item_id, seq) VALUES (?, ?, ?, ?)",
                  (cur.lastrowid, sid, item_id, seq))

    def _forget_item(self, c: sqlite3.Connection, sid: str, item_id: str) -> None:
        for (rid,) in c.execute("SELECT doc_rowid FROM docmap WHERE session_id = ? AND item_id = ?",
                                (sid, item_id)).fetchall():
            self._delete_doc(c, rid)

    @staticmethod
    def _delete_doc(c: sqlite3.Connection, rid: int) -> None:
        c.execute("DELETE FROM docs WHERE rowid = ?", (rid,))
        c.execute("DELETE FROM docmap WHERE doc_rowid = ?", (rid,))

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
