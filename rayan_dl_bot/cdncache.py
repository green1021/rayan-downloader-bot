"""Telegram CDN file_id cache — instant re-send of previously uploaded files.

When a file is sent once, Telegram returns a file_id. Storing it against the
source URL lets the bot re-send identical media INSTANTLY from Telegram's own
CDN: zero server bandwidth, zero disk, zero download time. Perfect for viral
links many users request.

Schema (created by db.ensure_fileid_cache()):
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

# 180 days is long enough that popular links keep their instant re-send, while
# the table cannot grow without bound on a bot that runs for years.
RETENTION_DAYS = 180


def _db():
    """The db module, whether imported as package or flat (bot.py) module."""
    try:
        from . import db as _d
        return _d
    except ImportError:
        import db as _d
        return _d


def _url_hash(url: str) -> str:
    """Hash a normalized URL.

    Only scheme/host casing is normalized: YouTube/Twitter IDs are
    case-sensitive, and lower-casing the whole URL could map two different
    videos to one file_id (the bot would then re-send the wrong media).
    Tracking parameters are removed by ``linkcache._norm``.
    """
    try:
        from . import linkcache as _lc
    except ImportError:
        import linkcache as _lc
    return hashlib.sha256(_lc._norm(url).encode()).hexdigest()[:24]


def ensure_table():
    """Create the table and apply retention; never fatal for the caller."""
    try:
        db = _db()
        db.ensure_fileid_cache()
        try:
            db.execute(
                "DELETE FROM fileid_cache WHERE created_at < "
                "datetime('now', ?)", (f"-{RETENTION_DAYS} days",))
        except Exception as exc:  # pruning is best-effort
            log.debug("fileid_cache prune: %s", exc)
        return True
    except Exception as exc:
        log.debug("fileid_cache ensure: %s", exc)
        return False


def install_db_helpers():
    """Deprecated no-op kept for the legacy root bot.py duplicate.

    db now provides ensure_fileid_cache/query_one/execute natively; the old
    monkeypatch shim was removed.
    """
    _db()


def get(url: str):
    """Return cached dict {file_id, kind, caption} or None."""
    try:
        row = _db().query_one(
            "SELECT file_id, kind, caption FROM fileid_cache WHERE url_hash = ?",
            (_url_hash(url),))
        if row:
            return {"file_id": row[0], "kind": row[1], "caption": row[2]}
    except Exception as e:
        log.debug("fileid_cache get: %s", e)
    return None


def put(url: str, file_id: str, kind: str, caption: str = None):
    try:
        _db().execute(
            "INSERT OR REPLACE INTO fileid_cache (url_hash, url, file_id, kind, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            (_url_hash(url), url[:500], file_id, kind, caption))
    except Exception as e:
        log.debug("fileid_cache put: %s", e)
