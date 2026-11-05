"""搜索索引的并发与降级（2026-11 审计修复）。

`_db()` 用裸 `except sqlite3.OperationalError` 把建虚拟表的**所有**失败都解释成
「sqlite 没编 FTS5」，然后 `self.fts = False` 永久降级成 LIKE 全表扫（实例被 get_index()
缓存，进程内不会恢复）。而并发首次建库时 sqlite 抛的恰恰也是 OperationalError：
`database is locked` —— 一次偶发锁就能让这个进程此后的每条搜索都慢成全表扫，
而且真正的锁错误被伪装成「不支持 FTS」，排查时看不到任何异常。

另外连接没有 WAL：读者与写者互相顶，一个进程在写事务里时另一个的 COMMIT 会一直等到
默认 5 秒超时才报错。
"""
import sqlite3

from ee.core.search_index import SearchIndex


def _plain_docs_db(home):
    """造一个已经存在普通 docs 表（不是 FTS5 虚拟表）的库。"""
    (home / "index").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(home / "index" / "search.db"))
    c.execute("CREATE TABLE docs (text TEXT, session_id TEXT, item_id TEXT, kind TEXT, seq INTEGER)")
    c.commit()
    c.close()


def test_lock_error_is_not_mistaken_for_missing_fts5():
    """并发锁（database is locked）不是「没有 FTS5」：不能因此永久降级。

    sqlite3.Connection 是不可变类型、没法打补丁，所以这里测的是判据本身
    （_db 的降级分支直接由它决定）。
    """
    from ee.core.search_index import _is_missing_fts5

    assert _is_missing_fts5(sqlite3.OperationalError("database is locked")) is False
    assert _is_missing_fts5(sqlite3.OperationalError("database table is locked")) is False
    assert _is_missing_fts5(sqlite3.OperationalError("no such module: fts5")) is True
    assert _is_missing_fts5(sqlite3.OperationalError("error in fts5 module")) is True


def test_plain_docs_table_left_by_degradation_is_rebuilt(tmp_path):
    """上次降级留下的普通 docs 表要换成 FTS5 虚拟表。

    否则 `CREATE VIRTUAL TABLE IF NOT EXISTS` 会因同名表已存在而静默跳过，
    这个进程带着 fts=True 去跑 `docs MATCH ?`，查询时直接抛 OperationalError。
    索引是纯缓存（能从 sessions/ 重建），所以删表重建比报错更合适。
    """
    _plain_docs_db(tmp_path)
    idx = SearchIndex(tmp_path)

    c = idx._db()

    assert idx.fts is True
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()[0]
    assert "fts5" in sql.lower(), "残留的普通 docs 表没有被换成 FTS5 虚拟表"


def test_db_uses_wal_journal(tmp_path):
    """WAL：读者不再顶住写者的 COMMIT（桌面端后端与 ee exec 共用同一个 search.db）。"""
    idx = SearchIndex(tmp_path)
    mode = idx._db().execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_index_and_search_still_work(tmp_path):
    """守住正常路径：建库、写入、查询。"""
    from ee.core.store import SessionStore

    idx = SearchIndex(tmp_path)
    idx._db().close()
    idx.close()

    s = SessionStore(tmp_path, session_id="20260101-000000-abcdef")
    s.item({"type": "user", "text": "配置 webpack 打包", "id": "u1", "seq": 1})
    s.meta({"title": "构建"})

    idx2 = SearchIndex(tmp_path)
    idx2.sync()
    assert idx2.search("webpack"), "索引后应当能搜到"
    idx2.close()
