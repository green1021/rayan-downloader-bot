#!/usr/bin/env python3
"""
db.py — Extended SQLite database for Rayan Downloader Bot
Tables: users (lang, banned), links (status), downloads (per-attempt stats)
"""

import os
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("rayan-dl-db")

DB_PATH = "/root/hermes-workspace/data/rayan.db"

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
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
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
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(force_channels)")}
        if "chat_id" not in fcols:
            conn.execute("ALTER TABLE force_channels ADD COLUMN chat_id INTEGER")
    except Exception as e:
        log.error("DB migrate: %s", e)
    conn.commit()
    conn.close()


def _conn():
    c = sqlite3.connect(DB_PATH)
    # Speed: WAL + synchronous=NORMAL dramatically improves write throughput
    # (batches many small writes instead of fsync per transaction).
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


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
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            conn.close()
            return False
        conn.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error("ban_user: %s", e)
        return False


def unban_user(user_id: int) -> bool:
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            conn.close()
            return False
        conn.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error("unban_user: %s", e)
        return False


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
        cur.execute("SELECT max_quality, auto_mode, silent, command_only FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"max_quality": row[0], "auto_mode": row[1], "silent": row[2], "command_only": row[3]}
    except Exception as e:
        log.error("get_chat_settings: %s", e)
    return {"max_quality": 1080, "auto_mode": 1, "silent": 0, "command_only": 0}


def set_chat_settings(chat_id: int, max_quality=None, auto_mode=None, silent=None, command_only=None, set_by=0):
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
            sets.append("set_by=?"); vals.append(set_by)
            vals.append(chat_id)
            conn.execute(f"UPDATE chat_settings SET {', '.join(sets)} WHERE chat_id=?", vals)
        else:
            conn.execute(
                "INSERT INTO chat_settings (chat_id, max_quality, auto_mode, silent, command_only, set_by) VALUES (?,?,?,?,?,?)",
                (chat_id, max_quality if max_quality is not None else 1080,
                 auto_mode if auto_mode is not None else 1,
                 silent if silent is not None else 0,
                 command_only if command_only is not None else 0, set_by))
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


def record_download(user_id: int, url: str, site: str, quality: str, success: bool):
    try:
        now = datetime.utcnow().isoformat()
        conn = _conn()
        conn.execute("INSERT INTO downloads (user_id, url, site, quality, success, created_at) VALUES (?,?,?,?,?,?)",
                     (user_id, url, site, str(quality), 1 if success else 0, now))
        conn.commit()
        conn.close()
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
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM force_channels WHERE channel_username = ?", (username,))
        if cur.fetchone():
            conn.close()
            return False, "exists"
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO force_channels (channel_username, channel_title, chat_id, added_by, created_at) VALUES (?,?,?,?,?)",
            (username, title, chat_id, added_by, now))
        conn.commit()
        conn.close()
        return True, "added"
    except Exception as e:
        log.error("add_force_channel: %s", e)
        return False, str(e)


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
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        if cur.fetchone():
            conn.close()
            return False
        now = datetime.utcnow().isoformat()
        cur.execute("INSERT INTO admins (user_id, added_by, created_at) VALUES (?,?,?)", (user_id, added_by, now))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error("add_admin: %s", e)
        return False


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
def shazam_cache_get(file_hash: str):
    """Return cached shazam result JSON or None."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT result_json FROM shazam_cache WHERE file_hash = ?", (file_hash,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
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
