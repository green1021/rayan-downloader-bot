"""Telegram CDN file_id cache — instant re-send of previously uploaded files.

When a file is sent once, Telegram returns a file_id. Storing it against the
source URL lets the bot re-send identical media INSTANTLY from Telegram's own
CDN: zero server bandwidth, zero disk, zero download time. Perfect for viral
links many users request.

Schema (created lazily by db.ensure_fileid_cache()):
  CREATE TABLE IF NOT EXISTS fileid_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT,
    file_id TEXT,
    kind TEXT,             -- video|photo|animation|document
    caption TEXT,
    created_at TEXT DEFAULT (datetime('now'))
  );
"""
import hashlib
import logging

log = logging.getLogger("rayan-dl")


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()[:24]


def ensure_table():
    try:
        import db
        db.ensure_fileid_cache()
        return True
    except Exception:
        return False


def get(url: str):
    """Return cached dict {file_id, kind, caption} or None."""
    try:
        import db
        row = db.query_one(
            "SELECT file_id, kind, caption FROM fileid_cache WHERE url_hash = ?",
            (_url_hash(url),))
        if row:
            return {"file_id": row[0], "kind": row[1], "caption": row[2]}
    except Exception as e:
        log.debug("fileid_cache get: %s", e)
    return None


def put(url: str, file_id: str, kind: str, caption: str = None):
    try:
        import db
        db.execute(
            "INSERT OR REPLACE INTO fileid_cache (url_hash, url, file_id, kind, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            (_url_hash(url), url[:500], file_id, kind, caption))
    except Exception as e:
        log.debug("fileid_cache put: %s", e)


# ── db.py helpers (appended there at import time via monkeypatch-free design) ──
def install_db_helpers():
    """Add ensure_fileid_cache/query_one/execute to db module if missing.
    Kept here so all CDN-cache SQL lives in one place."""
    import db as _db

    if not hasattr(_db, "ensure_fileid_cache"):
        def ensure_fileid_cache():
            c = _db._conn()
            c.execute("""CREATE TABLE IF NOT EXISTS fileid_cache (
                url_hash TEXT PRIMARY KEY,
                url TEXT,
                file_id TEXT,
                kind TEXT,
                caption TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
            c.commit()
            c.close()
        _db.ensure_fileid_cache = ensure_fileid_cache

    if not hasattr(_db, "query_one"):
        def query_one(sql, args=()):
            c = _db._conn()
            cur = c.execute(sql, args)
            row = cur.fetchone()
            c.close()
            return row
        _db.query_one = query_one

    if not hasattr(_db, "execute"):
        def execute(sql, args=()):
            c = _db._conn()
            c.execute(sql, args)
            c.commit()
            c.close()
        _db.execute = execute
