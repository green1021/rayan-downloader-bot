#!/usr/bin/env python3
"""
db.py — Extended SQLite database for Rayan Downloader Bot
Tables: users (lang, banned), links (status), downloads (per-attempt stats)
"""

import os
import re
import secrets
import sqlite3
import logging
import string
from datetime import datetime

try:  # package import
    from . import config
except ImportError:  # flat import (production sys.path contains the package dir)
    import config

log = logging.getLogger("rayan-dl-db")

# Single source of truth; the default in config.py is the historical path.
DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT,
    lang TEXT DEFAULT 'en',
    lang_chosen INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    total_links INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT,
    site TEXT,
    title TEXT,
    status TEXT DEFAULT 'received',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT,
    site TEXT,
    quality TEXT,
    success INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    max_quality INTEGER DEFAULT 1080,
    auto_mode INTEGER DEFAULT 1,
    silent INTEGER DEFAULT 0,
    command_only INTEGER DEFAULT 0,
    media_mode INTEGER DEFAULT 2,
    link_filter TEXT DEFAULT 'all',
    set_by INTEGER
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    default_quality INTEGER DEFAULT 720,
    default_audio INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS force_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_username TEXT UNIQUE,
    channel_title TEXT,
    chat_id INTEGER,
    added_by INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_members (
    user_id INTEGER,
    channel_id INTEGER,
    joined_at TEXT,
    PRIMARY KEY (user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS shazam_cache (
    file_hash TEXT PRIMARY KEY,
    result_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_messages (
    lang TEXT,
    msg_key TEXT,
    value TEXT,
    PRIMARY KEY (lang, msg_key)
);
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    inviter_id INTEGER NOT NULL,
    invited_id INTEGER NOT NULL UNIQUE,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS referral_credits (
    user_id INTEGER PRIMARY KEY,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ref_codes (
    user_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT,
    user_id TEXT,
    count INT DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
CREATE TABLE IF NOT EXISTS reservations (
    day TEXT,
    user_id TEXT,
    reserved INT DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
-- Indexes for hot query paths (avoid full-table scans as tables grow)
CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id);
CREATE INDEX IF NOT EXISTS idx_links_url ON links(url);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at);
CREATE INDEX IF NOT EXISTS idx_referrals_inviter ON referrals(inviter_id);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # Journal mode is a database-wide property: set it ONCE here instead of on
    # every short-lived connection (re-issuing it needs a lock and could make
    # concurrent writers fail with "database is locked" during an online
    # backup, which holds a read transaction).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # Migrate legacy tables (add missing columns if they exist without them)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'en'")
        if "lang_chosen" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang_chosen INTEGER DEFAULT 0")
        if "banned" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(links)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE links ADD COLUMN status TEXT DEFAULT 'received'")
        if "title" not in cols:
            conn.execute("ALTER TABLE links ADD COLUMN title TEXT")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_settings)")}
        if "command_only" not in cols:
            conn.execute("ALTER TABLE chat_settings ADD COLUMN command_only INTEGER DEFAULT 0")
        if "media_mode" not in cols:
            conn.execute("ALTER TABLE chat_settings ADD COLUMN media_mode INTEGER DEFAULT 2")
        if "link_filter" not in cols:
            conn.execute("ALTER TABLE chat_settings ADD COLUMN link_filter TEXT DEFAULT 'all'")
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(force_channels)")}
        if "chat_id" not in fcols:
            conn.execute("ALTER TABLE force_channels ADD COLUMN chat_id INTEGER")
        # B3: downloads-library table lives beside the stats table (same name
        # would collide) — dedicated table with file_id so entries can re-send.
        conn.execute("""CREATE TABLE IF NOT EXISTS library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_id TEXT NOT NULL,
            caption TEXT,
            created_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_library_user ON library(user_id)")
        # B4: fast mode is its own flag, NOT a sentinel smuggled into default_audio
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(user_settings)")}
        if "fast_mode" not in ucols:
            conn.execute("ALTER TABLE user_settings ADD COLUMN fast_mode INTEGER DEFAULT 0")
        _seed_translation_config(conn)
    except Exception as e:
        log.error("DB migrate: %s", e)
    conn.commit()
    conn.close()


def _seed_translation_config(conn) -> None:
    """Pluggable translation engines (2026-08-25): seed ONLY missing keys so
    admin edits are never overwritten on restart. Additive — no schema change."""
    import json as _json
    seeds = {
        "tr_engine_chain": _json.dumps(["gateway_ox", "mymemory"]),
        "tr_cfg_gateway_ox": _json.dumps({
            "base_url": "http://localhost:20128/v1",
            "model": "ox-alpha-combo"}),
        "tr_cfg_mymemory": _json.dumps({"email": "rayan.downloader.bot@gmail.com"}),
        "tr_cfg_libre": _json.dumps({"base_url": "https://libretranslate.com"}),
        "tr_cfg_custom_openai": _json.dumps({}),
    }
    now = datetime.utcnow().isoformat()
    for key, value in seeds.items():
        row = conn.execute("SELECT key FROM bot_config WHERE key=?", (key,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO bot_config (key, value, updated_at) VALUES (?,?,?)",
                (key, str(value), now))


class _SafeConn(sqlite3.Connection):
    """Guarantees rollback+close even when a caller hits an exception path that
    skips its conn.close(). CPython refcounting usually destroys the abandoned
    connection anyway, but this makes cleanup explicit and independent of GC
    timing (and rolls the pending transaction back deterministically)."""

    def __del__(self):
        try:
            self.rollback()   # no-op when idle; raises if already closed — swallowed
        except Exception:
            pass
        # base dealloc then closes the underlying sqlite handle


def _conn():
    # ``timeout`` is SQLite's busy handler; WAL is enabled once in init_db().
    # ``synchronous`` is per-connection and does not need a write lock.
    c = sqlite3.connect(DB_PATH, timeout=30, factory=_SafeConn)
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def execute(sql: str, args=()) -> int:
    """Run one write statement on a short-lived connection; return rowcount."""
    conn = _conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def query_one(sql: str, args=()):
    """Run one SELECT and return the first row (or ``None``)."""
    conn = _conn()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def ensure_fileid_cache() -> None:
    """Create the Telegram file_id cache table if it does not exist."""
    conn = _conn()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS fileid_cache (
            url_hash TEXT PRIMARY KEY,
            url TEXT,
            file_id TEXT,
            kind TEXT,
            caption TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
    finally:
        conn.close()


def check_integrity() -> tuple[bool, str]:
    """Cheap structural check (``quick_check``) before the bot starts writing."""
    try:
        conn = _conn()
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        result = (row[0] if row else "no result") or ""
        return result.lower() == "ok", result
    except sqlite3.Error as exc:
        return False, str(exc)


def backup_database(keep: int = 7, force: bool = False) -> str | None:
    """Consistent online backup via SQLite's backup API (safe while WAL is hot).

    Runs at most once per day unless ``force``; keeps the newest ``keep``
    copies.  A failed backup is logged and must never stop the bot.
    """
    import datetime as _dt

    backup_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "backups")
    try:
        os.makedirs(backup_dir, exist_ok=True)
        today = _dt.date.today().isoformat()
        existing = sorted(f for f in os.listdir(backup_dir)
                          if f.startswith("rayan-") and f.endswith(".db"))
        if not force and any(f.startswith(f"rayan-{today}") for f in existing):
            return None
        dest = os.path.join(backup_dir, f"rayan-{today}.db")
        if os.path.exists(dest):
            dest = os.path.join(backup_dir,
                                f"rayan-{today}-{int(_dt.datetime.now().timestamp())}.db")
        src = sqlite3.connect(DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(dest, timeout=30)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        for name in existing[:max(0, len(existing) - keep + 1)]:
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass
        log.info("database backup written: %s", dest)
        return dest
    except Exception as exc:  # noqa: BLE001 - backups must not be fatal
        log.error("database backup failed: %s", exc)
        return None


def record_user(update) -> int:
    """Upsert user, return 1 if new user was created, else 0.
    New users get lang='en' and lang_chosen=0 (prompt on first /start)."""
    try:
        u = update.effective_user
        now = datetime.utcnow().isoformat()
        conn = _conn()
        cur = conn.cursor()
        # Single upsert (INSERT OR IGNORE + UPDATE) — avoids the SELECT round-trip
        cur.execute("""
            INSERT OR IGNORE INTO users
              (user_id, username, first_name, last_name, language_code, lang, lang_chosen, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 'en', 0, ?, ?)
        """, (u.id, u.username, u.first_name, u.last_name, u.language_code, now, now))
        created = cur.rowcount > 0
        cur.execute("""
            UPDATE users SET username=?, first_name=?, last_name=?,
            language_code=?, last_seen=? WHERE user_id=?
        """, (u.username, u.first_name, u.last_name, u.language_code, now, u.id))
        conn.commit()
        conn.close()
        return 1 if created else 0
    except Exception as e:
        log.error("record_user error: %s", e)
        return 0


def get_user_lang(user_id: int) -> str:
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] else "en"
    except Exception:
        return "en"


def user_lang_chosen(user_id: int) -> bool:
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT lang_chosen FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def set_user_lang(user_id: int, lang: str):
    try:
        conn = _conn()
        conn.execute("UPDATE users SET lang = ?, lang_chosen = 1 WHERE user_id = ?", (lang, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("set_user_lang: %s", e)


def is_banned(user_id: int) -> bool:
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def ban_user(user_id: int) -> bool:
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            return False
        conn.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        log.error("ban_user: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def unban_user(user_id: int) -> bool:
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            return False
        conn.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        log.error("unban_user: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def record_link(update, url: str, site: str, title: str = ""):
    try:
        u = update.effective_user
        now = datetime.utcnow().isoformat()
        conn = _conn()
        conn.execute("INSERT INTO links (user_id, url, site, title, status, created_at) VALUES (?,?,?,?,?,?)",
                     (u.id, url, site, title, "received", now))
        conn.execute("UPDATE users SET total_links = total_links + 1, last_seen = ? WHERE user_id = ?", (now, u.id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("record_link: %s", e)


def update_link_title(url: str, title: str):
    """Update the title (subject) of the most recent link with this URL."""
    try:
        conn = _conn()
        conn.execute("UPDATE links SET title = ? WHERE url = ? AND (title IS NULL OR title = '')",
                     (title[:200], url))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("update_link_title: %s", e)


def get_all_links(limit: int = 30):
    """Return recent links joined with user names, newest first."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT l.id, l.user_id, u.username, u.first_name, u.last_name,
                   l.site, l.title, l.url, l.created_at
            FROM links l LEFT JOIN users u ON u.user_id = l.user_id
            ORDER BY l.id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("get_all_links: %s", e)
        return []


# ── Chat (group) settings ─────────────────────────────────────
def get_chat_settings(chat_id: int):
    """Return dict of chat settings with defaults."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT max_quality, auto_mode, silent, command_only, media_mode, link_filter FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"max_quality": row[0], "auto_mode": row[1], "silent": row[2], "command_only": row[3], "media_mode": row[4], "link_filter": row[5]}
    except Exception as e:
        log.error("get_chat_settings: %s", e)
    return {"max_quality": 1080, "auto_mode": 1, "silent": 0, "command_only": 0, "media_mode": 2, "link_filter": "all"}


def set_chat_settings(chat_id: int, max_quality=None, auto_mode=None, silent=None, command_only=None, media_mode=None, link_filter=None, set_by=0):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
        exists = cur.fetchone()
        if exists:
            sets, vals = [], []
            if max_quality is not None:
                sets.append("max_quality=?"); vals.append(max_quality)
            if auto_mode is not None:
                sets.append("auto_mode=?"); vals.append(auto_mode)
            if silent is not None:
                sets.append("silent=?"); vals.append(silent)
            if command_only is not None:
                sets.append("command_only=?"); vals.append(command_only)
            if media_mode is not None:
                sets.append("media_mode=?"); vals.append(media_mode)
            if link_filter is not None:
                sets.append("link_filter=?"); vals.append(link_filter)
            sets.append("set_by=?"); vals.append(set_by)
            vals.append(chat_id)
            # safety: column names are built only from literals above
            assert all(s.split("=")[0] in {"max_quality", "auto_mode", "silent", "command_only", "media_mode", "link_filter", "set_by"} for s in sets)
            conn.execute(f"UPDATE chat_settings SET {', '.join(sets)} WHERE chat_id=?", vals)
        else:
            conn.execute(
                "INSERT INTO chat_settings (chat_id, max_quality, auto_mode, silent, command_only, media_mode, link_filter, set_by) VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, max_quality if max_quality is not None else 1080,
                 auto_mode if auto_mode is not None else 1,
                 silent if silent is not None else 0,
                 command_only if command_only is not None else 0,
                 media_mode if media_mode is not None else 2,
                 link_filter if link_filter is not None else 'all', set_by))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("set_chat_settings: %s", e)


# ── Per-user settings ─────────────────────────────────────────
def get_user_settings(user_id: int):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT default_quality, default_audio FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"default_quality": row[0], "default_audio": row[1]}
    except Exception as e:
        log.error("get_user_settings: %s", e)
    return {"default_quality": 720, "default_audio": 0}


def set_user_settings(user_id: int, default_quality=None, default_audio=None):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,))
        exists = cur.fetchone()
        if exists:
            sets, vals = [], []
            if default_quality is not None:
                sets.append("default_quality=?"); vals.append(default_quality)
            if default_audio is not None:
                sets.append("default_audio=?"); vals.append(default_audio)
            vals.append(user_id)
            conn.execute(f"UPDATE user_settings SET {', '.join(sets)} WHERE user_id=?", vals)
        else:
            conn.execute(
                "INSERT INTO user_settings (user_id, default_quality, default_audio) VALUES (?,?,?)",
                (user_id, default_quality if default_quality is not None else 720,
                 default_audio if default_audio is not None else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("set_user_settings: %s", e)


def count_user_downloads(user_id: int) -> int:
    """Total successful downloads for a user."""
    try:
        c = _conn()
        row = c.execute(
            "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND success = 1",
            (user_id,)).fetchone()
        c.close()
        return row[0] if row else 0
    except Exception:
        return 0


def record_download(user_id: int, url: str, site: str, quality: str, success: bool):
    try:
        now = datetime.utcnow().isoformat()
        conn = _conn()
        conn.execute("INSERT INTO downloads (user_id, url, site, quality, success, created_at) VALUES (?,?,?,?,?,?)",
                     (user_id, url, site, str(quality), 1 if success else 0, now))
        conn.commit()
        conn.close()
        if success:
            # C/B6 bookkeeping: EVERY successful delivery counts toward the
            # user's daily total (incl. cache re-serves — a file sent is a
            # file sent). WHO is actually limited is decided by the gate
            # (bot._over_daily_limit): admins / referral holders / groups
            # are exempt, so their counted usage is harmless.
            bump_usage(user_id)
    except Exception as e:
        log.error("record_download: %s", e)


def get_stats():
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM links")
        links = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM downloads WHERE success = 1")
        success = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM downloads WHERE success = 0")
        failed = cur.fetchone()[0]
        cur.execute("SELECT site, COUNT(*) FROM links GROUP BY site ORDER BY COUNT(*) DESC")
        by_site = dict(cur.fetchall())
        conn.close()
        return {"users": users, "links": links, "success": success, "failed": failed, "by_site": by_site}
    except Exception as e:
        log.error("get_stats: %s", e)
        return {"users": 0, "links": 0, "success": 0, "failed": 0, "by_site": {}}


def get_top_users(limit: int = 10):
    """Top users by link count, with username/first/last."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, username, first_name, last_name, total_links
            FROM users ORDER BY total_links DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("get_top_users: %s", e)
        return []


def get_user_info(user_id: int):
    """Return user info dict or None."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, first_name, last_name, lang, banned, total_links FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "id": row[0], "username": row[1], "first": row[2], "last": row[3],
                "lang": row[4], "banned": row[5], "total_links": row[6],
                "name": row[2] or row[3] or row[1] or str(row[0]),
            }
    except Exception as e:
        log.error("get_user_info: %s", e)
    return None


def get_user_links(user_id: int, limit: int = 200):
    """Return all links sent by a user: (id, url, site, title, created_at)."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url, site, title, created_at FROM links WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("get_user_links: %s", e)
        return []


def get_history(user_id: int, limit: int = 10):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT site, url, created_at FROM links
            WHERE user_id = ? ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("get_history: %s", e)
        return []


def get_all_user_ids():
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE banned = 0")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("get_all_user_ids: %s", e)
        return []


# ── Force-join channels ───────────────────────────────────────
def get_force_channels():
    """Return all force channels: {id, username, title, chat_id}."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id, channel_username, channel_title, chat_id FROM force_channels ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        return [{"id": r[0], "username": r[1], "title": r[2], "chat_id": r[3]} for r in rows]
    except Exception as e:
        log.error("get_force_channels: %s", e)
        return []


def add_force_channel(username: str, title: str = "", chat_id=None, added_by: int = 0):
    """Add a force channel. Returns (ok, reason)."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM force_channels WHERE channel_username = ?", (username,))
        if cur.fetchone():
            return False, "exists"
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO force_channels (channel_username, channel_title, chat_id, added_by, created_at) VALUES (?,?,?,?,?)",
            (username, title, chat_id, added_by, now))
        conn.commit()
        return True, "added"
    except Exception as e:
        log.error("add_force_channel: %s", e)
        return False, str(e)
    finally:
        if conn:
            conn.close()


def remove_force_channel(username: str):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM force_channels WHERE channel_username = ?", (username,))
        conn.commit()
        removed = cur.rowcount > 0
        conn.close()
        return removed
    except Exception as e:
        log.error("remove_force_channel: %s", e)
        return False


def mark_channel_joined(user_id: int, channel_id: int):
    try:
        now = datetime.utcnow().isoformat()
        conn = _conn()
        conn.execute("INSERT OR REPLACE INTO channel_members (user_id, channel_id, joined_at) VALUES (?,?,?)",
                     (user_id, channel_id, now))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("mark_channel_joined: %s", e)


def is_channel_joined(user_id: int, channel_id: int) -> bool:
    """True if user has a recent (non-expired) manual confirmation for this channel."""
    return is_channel_joined_ttl(user_id, channel_id, ttl_hours=24)


def is_channel_joined_ttl(user_id: int, channel_id: int, ttl_hours: int = 24) -> bool:
    """Check a stored join confirmation with expiry. A confirmation older than
    ttl_hours is NOT valid — the user must confirm again (covers the case where
    they left the channel days later, or channels changed)."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT joined_at FROM channel_members WHERE user_id = ? AND channel_id = ?",
                    (user_id, channel_id))
        row = cur.fetchone()
        conn.close()
        if not row:
            return False
        try:
            from datetime import datetime, timezone
            joined = datetime.fromisoformat(row[0])
            if joined.tzinfo is None:
                joined = joined.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - joined).total_seconds() / 3600
            return age_hours <= ttl_hours
        except Exception:
            return True  # unparseable → assume valid (legacy row)
    except Exception:
        return False


# ── Admin management (DB-backed, so it works on ANY server) ───
def get_db_admins():
    """Return admin user ids from DB."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins ORDER BY user_id")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("get_db_admins: %s", e)
        return []


def add_admin(user_id: int, added_by: int) -> bool:
    """Add an admin. Returns True if newly added."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        if cur.fetchone():
            return False
        now = datetime.utcnow().isoformat()
        cur.execute("INSERT INTO admins (user_id, added_by, created_at) VALUES (?,?,?)", (user_id, added_by, now))
        conn.commit()
        return True
    except Exception as e:
        log.error("add_admin: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def remove_admin(user_id: int) -> bool:
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        removed = cur.rowcount > 0
        conn.close()
        return removed
    except Exception as e:
        log.error("remove_admin: %s", e)
        return False


def is_db_admin(user_id: int) -> bool:
    return user_id in get_db_admins()


# ── Shazam cache ──────────────────────────────────────────────
def shazam_cache_get(file_hash: str, ttl_days: int = 30):
    """Return cached shazam result JSON if fresh (within TTL), else None."""
    try:
        from datetime import datetime, timedelta
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT result_json, created_at FROM shazam_cache WHERE file_hash = ?",
                    (file_hash,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        try:
            created = datetime.fromisoformat(row[1])
        except (ValueError, TypeError):
            created = datetime.min
        if datetime.utcnow() - created > timedelta(days=ttl_days):
            # stale — remove and treat as miss
            try:
                conn = _conn()
                conn.execute("DELETE FROM shazam_cache WHERE file_hash = ?", (file_hash,))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return None
        return row[0]
    except Exception as e:
        log.error("shazam_cache_get: %s", e)
        return None


def shazam_cache_set(file_hash: str, result_json: str, ttl_days: int = 30):
    """Cache a shazam result with expiry (clean old rows)."""
    try:
        from datetime import datetime, timedelta
        conn = _conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM shazam_cache WHERE created_at < ?",
                    ((datetime.utcnow() - timedelta(days=ttl_days)).isoformat(),))
        now = datetime.utcnow().isoformat()
        conn.execute("INSERT OR REPLACE INTO shazam_cache (file_hash, result_json, created_at) VALUES (?,?,?)",
                     (file_hash, result_json, now))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("shazam_cache_set: %s", e)


# ── Custom bot messages (admin-editable texts, e.g. start) ────
def get_custom_msg(lang: str, msg_key: str):
    """Return custom override for (lang, msg_key) or None."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_messages WHERE lang=? AND msg_key=?", (lang, msg_key))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log.error("get_custom_msg: %s", e)
        return None


def set_custom_msg(lang: str, msg_key: str, value: str):
    try:
        conn = _conn()
        conn.execute("INSERT OR REPLACE INTO bot_messages (lang, msg_key, value) VALUES (?,?,?)",
                     (lang, msg_key, value))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("set_custom_msg: %s", e)


def remove_custom_msg(lang: str, msg_key: str):
    try:
        conn = _conn()
        conn.execute("DELETE FROM bot_messages WHERE lang=? AND msg_key=?", (lang, msg_key))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("remove_custom_msg: %s", e)


def all_custom_msgs():
    """Return {lang: {msg_key: value}} for all overrides."""
    try:
        conn = _conn()
        rows = conn.execute("SELECT lang, msg_key, value FROM bot_messages").fetchall()
        conn.close()
        out = {}
        for lang, key, val in rows:
            out.setdefault(lang, {})[key] = val
        return out
    except Exception as e:
        log.error("all_custom_msgs: %s", e)
        return {}


# ── Bot config (admin-editable settings) ──────────────────────
def get_config(key: str, default=None):
    try:
        conn = _conn()
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception as e:
        log.error("get_config: %s", e)
        return default


def set_config(key: str, value):
    try:
        conn = _conn()
        now = datetime.utcnow().isoformat()
        conn.execute("INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?,?,?)",
                     (key, str(value), now))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("set_config: %s", e)


def all_config():
    try:
        conn = _conn()
        rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
        conn.close()
        return dict(rows)
    except Exception as e:
        log.error("all_config: %s", e)
        return {}


# ── Referral system (B1) ─────────────────────────────────────
REFERRAL_CAP = 5          # max skip-the-queue credits an inviter can earn
INVITEE_START_CREDIT = 1  # every invited user starts with one credit

_B62 = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _b62(n: int) -> str:
    """int → base62 string (>=1 char)."""
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(reversed(out)) or "0"


def get_or_create_ref_code(user_id: int) -> str:
    """Stable unique base62 referral code for a user (created on first call).
    Collision-safe: retries with fresh random codes until UNIQUE accepts one."""
    conn = _conn()
    try:
        row = conn.execute("SELECT code FROM ref_codes WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row[0]
        while True:
            code = _b62(user_id) + "".join(secrets.choice(_B62) for _ in range(4))
            try:
                conn.execute("INSERT INTO ref_codes (user_id, code) VALUES (?, ?)",
                             (user_id, code))
                conn.commit()
                return code
            except sqlite3.IntegrityError:
                continue  # code collision — draw again
    except Exception as e:
        log.error("get_or_create_ref_code: %s", e)
        return ""
    finally:
        conn.close()


def user_by_ref_code(code: str):
    """Resolve a referral code to its owner's user_id (or None)."""
    if not code or not re.fullmatch(r"[0-9A-Za-z]{2,16}", code):
        return None
    try:
        conn = _conn()
        row = conn.execute("SELECT user_id FROM ref_codes WHERE code = ?", (code,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log.error("user_by_ref_code: %s", e)
        return None


def record_referral(inviter_id: int, invited_id: int) -> bool:
    """Register inviter→invited exactly once (UNIQUE invited_id).
    Returns True if newly recorded. The invitee always lands; the INVITER's
    earnable credits are capped by REFERRAL_CAP in earned_referral_credits()."""
    if not inviter_id or not invited_id or inviter_id == invited_id:
        return False  # no self-invite / bogus ids
    conn = None
    try:
        conn = _conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO referrals (inviter_id, invited_id, created_at) VALUES (?,?,?)",
            (inviter_id, invited_id, datetime.utcnow().isoformat()))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        log.error("record_referral: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def earned_referral_credits(user_id: int) -> int:
    """Credits EARNED from inviting others — capped at REFERRAL_CAP.
    (Cap applied here rather than at insert time so raising the cap later,
    or pre-cap rows, never grants more than the current policy allows.)"""
    try:
        conn = _conn()
        row = conn.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?",
                           (user_id,)).fetchone()
        conn.close()
        return min(int(row[0] or 0), REFERRAL_CAP)
    except Exception as e:
        log.error("earned_referral_credits: %s", e)
        return 0


def get_referral_credits(user_id: int) -> int:
    """Skip-the-queue credits still available: earned (capped) + invitee bonus − used."""
    try:
        conn = _conn()
        row = conn.execute("""
            SELECT COALESCE((
                SELECT MIN((SELECT COUNT(*) FROM referrals WHERE inviter_id = :u), :cap)
                     + CASE WHEN EXISTS(SELECT 1 FROM referrals WHERE invited_id = :u)
                            THEN :start ELSE 0 END
                     - COALESCE((SELECT used FROM referral_credits WHERE user_id = :u), 0)
            ), 0)
        """, {"u": user_id, "cap": REFERRAL_CAP, "start": INVITEE_START_CREDIT}).fetchone()
        conn.close()
        return max(0, int(row[0] or 0)) if row else 0
    except Exception as e:
        log.error("get_referral_credits: %s", e)
        return 0


def consume_referral_credit(user_id: int) -> bool:
    """Atomically spend one credit. The ledger row is ensured lazily, then a
    single conditional UPDATE does the spend — two concurrent downloads racing
    for the last credit cannot both win (SQLite serializes the writes and the
    availability predicate is re-checked against committed state)."""
    conn = None
    try:
        conn = _conn()
        conn.execute("INSERT OR IGNORE INTO referral_credits (user_id, used) VALUES (?, 0)",
                     (user_id,))
        cur = conn.execute("""
            UPDATE referral_credits SET used = used + 1
            WHERE user_id = :u
              AND used + 1 <= MIN((SELECT COUNT(*) FROM referrals WHERE inviter_id = :u), :cap)
                        + CASE WHEN EXISTS(SELECT 1 FROM referrals WHERE invited_id = :u)
                               THEN :start ELSE 0 END
        """, {"u": user_id, "cap": REFERRAL_CAP, "start": INVITEE_START_CREDIT})
        ok = cur.rowcount > 0
        if ok:
            conn.commit()
        else:
            conn.rollback()  # release the no-op transaction cleanly
        conn.close()
        return ok
    except Exception as e:
        log.error("consume_referral_credit: %s", e)
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def count_referrals(user_id: int) -> int:
    """How many friends this user has successfully invited."""
    try:
        conn = _conn()
        row = conn.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?",
                           (user_id,)).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        log.error("count_referrals: %s", e)
        return 0


# ── Daily download limit (B6) ────────────────────────────────
DAILY_LIMIT_KEY = "daily_download_limit"


def reserve_quota(user_id: int, n: int = 1) -> int:
    """Atomic reservation: Return number of slots actually reserved (<= n)."""
    try:
        conn = _conn()
        with conn: # Atomic transaction
            # Cleanup stale reservations
            conn.execute("DELETE FROM reservations WHERE day != ?", (_usage_day(),))
            
            usage = usage_today(user_id)
            row = conn.execute("SELECT reserved FROM reservations WHERE day = ? AND user_id = ?",
                               (_usage_day(), str(user_id))).fetchone()
            reserved = row[0] if row else 0
            
            available = max(0, daily_limit() - (usage + reserved))
            to_reserve = min(n, available)
            
            if to_reserve > 0:
                conn.execute(
                    "INSERT INTO reservations (day, user_id, reserved) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, user_id) DO UPDATE SET reserved = reserved + excluded.reserved",
                    (_usage_day(), str(user_id), to_reserve))
                return to_reserve
            return 0
    except Exception as e:
        log.error("reserve_quota: %s", e)
        return 0

def settle_reservation(user_id: int, n: int, success: bool):
    """Settle: either bump usage and release reservation (success), or just release (failure)."""
    try:
        conn = _conn()
        with conn:
            # Always release reservation
            conn.execute("UPDATE reservations SET reserved = reserved - ? WHERE day = ? AND user_id = ?",
                         (n, _usage_day(), str(user_id)))
            
            if success:
                # Actual usage bump, same connection
                conn.execute(
                    "INSERT INTO usage (day, user_id, count) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, user_id) DO UPDATE SET count = count + excluded.count",
                    (_usage_day(), str(user_id), int(n)))
    except Exception as e:
        log.error("settle_reservation: %s", e)

def daily_limit() -> int:
    """Per-day download cap for non-allowed users (default 10)."""
    try:
        v = int(get_config(DAILY_LIMIT_KEY, 10) or 10)
        return max(1, min(v, 1000))
    except Exception:
        return 10


def set_daily_limit(n: int):
    set_config(DAILY_LIMIT_KEY, int(n))


def _usage_day() -> str:
    """UTC day string — the counter resets naturally when this changes."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def usage_today(user_id: int) -> int:
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT count FROM usage WHERE day = ? AND user_id = ?",
            (_usage_day(), str(user_id))).fetchone()
        conn.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        log.error("usage_today: %s", e)
        return 0


def bump_usage(user_id: int, n: int = 1):
    """Add n to today's counter (upsert on the (day,user_id) key).
    A new UTC day writes a fresh row → counter reset without any cron."""
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO usage (day, user_id, count) VALUES (?, ?, ?) "
            "ON CONFLICT(day, user_id) DO UPDATE SET count = count + excluded.count",
            (_usage_day(), str(user_id), int(n)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("bump_usage: %s", e)


def count_downloads_today(user_id: int) -> int:
    """Successful downloads since midnight UTC (from the downloads log — used by /stats)."""
    try:
        today = datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
        conn = _conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND success = 1 AND created_at >= ?",
            (user_id, today)).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        log.error("count_downloads_today: %s", e)
        return 0


# ── Speed mode / fast mode toggle (B4) ───────────────────────
def fast_mode_enabled(user_id: int) -> bool:
    """True when the user turned /fast ON (own column — never touches default_audio)."""
    try:
        conn = _conn()
        row = conn.execute("SELECT fast_mode FROM user_settings WHERE user_id = ?",
                           (user_id,)).fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        log.error("fast_mode_enabled: %s", e)
        return False


def set_fast_mode(user_id: int, enabled: bool):
    """Persist the per-user fast-mode flag. Upsert so a user with no settings
    row yet still gets one."""
    conn = None
    try:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO user_settings (user_id, default_quality, default_audio, fast_mode) "
            "VALUES (?, 720, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET fast_mode = excluded.fast_mode",
            (user_id, 1 if enabled else 0))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        log.error("set_fast_mode: %s", e)
        return False
    finally:
        if conn:
            conn.close()


# ── Downloads library (B3) ───────────────────────────────────
LIBRARY_TTL_DAYS = 7   # /downloads entries expire after this many days
LIBRARY_KEEP = 10      # and only this many are kept per user


def library_add(user_id: int, file_id: str, caption: str = "") -> bool:
    """Remember a DELIVERED file by its Telegram file_id so the user can
    re-send or delete it later via /downloads. Keeps at most the newest
    LIBRARY_KEEP entries per user."""
    if not file_id:
        return False
    try:
        now = datetime.utcnow().isoformat()
        conn = _conn()
        conn.execute("INSERT INTO library (user_id, file_id, caption, created_at) VALUES (?,?,?,?)",
                     (user_id, file_id, (caption or "")[:200], now))
        # keep only the newest N for this user (lazy trim on every add)
        conn.execute("""
            DELETE FROM library WHERE user_id = ? AND id NOT IN (
                SELECT id FROM library WHERE user_id = ? ORDER BY id DESC LIMIT ?
            )
        """, (user_id, user_id, LIBRARY_KEEP))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error("library_add: %s", e)
        return False


def _library_cleanup_expired(conn) -> None:
    """Lazy expiry: drop rows older than LIBRARY_TTL_DAYS. Runs inside the
    caller's connection/transaction — no separate commit here."""
    try:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=LIBRARY_TTL_DAYS)).isoformat()
        conn.execute("DELETE FROM library WHERE created_at < ?", (cutoff,))
    except Exception as e:
        log.error("library cleanup: %s", e)


def get_library(user_id: int, limit: int = 10, days: int = LIBRARY_TTL_DAYS):
    """Newest `limit` unexpired entries for a user. Expired rows are deleted
    lazily on every access (no cron needed). Returns [(id, file_id, caption, created_at)]."""
    try:
        from datetime import timedelta
        conn = _conn()
        _library_cleanup_expired(conn)
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT id, file_id, caption, created_at FROM library
            WHERE user_id = ? AND created_at >= ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, cutoff, limit)).fetchall()
        conn.commit()
        conn.close()
        return rows
    except Exception as e:
        log.error("get_library: %s", e)
        return []


def get_library_entry(user_id: int, entry_id: int):
    """One owned, unexpired entry → (id, file_id, caption, created_at) or None.
    Used by the re-send button (ownership-checked)."""
    try:
        from datetime import timedelta
        conn = _conn()
        _library_cleanup_expired(conn)
        cutoff = (datetime.utcnow() - timedelta(days=LIBRARY_TTL_DAYS)).isoformat()
        row = conn.execute("""
            SELECT id, file_id, caption, created_at FROM library
            WHERE id = ? AND user_id = ? AND created_at >= ?
        """, (entry_id, user_id, cutoff)).fetchone()
        conn.commit()
        conn.close()
        return row
    except Exception as e:
        log.error("get_library_entry: %s", e)
        return None


def delete_library_entry(user_id: int, entry_id: int) -> bool:
    """Remove one of THIS user's library entries ('delete-for-me').
    The ownership check is in the WHERE clause — users can never delete
    someone else's row."""
    try:
        conn = _conn()
        cur = conn.execute("DELETE FROM library WHERE id = ? AND user_id = ?",
                           (entry_id, user_id))
        removed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return removed
    except Exception as e:
        log.error("delete_library_entry: %s", e)
        return False
