#!/usr/bin/env python3
"""
Rayan Downloader Bot — main entrypoint
Multi-language + admin panel + fast concurrent downloads
"""

import os
import re
import sys
import time
import json
import logging
import asyncio
import shutil
import hashlib
import subprocess
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

import i18n
import db
import downloader as dl
import ig_tw as igtw
import recognizer as recog
import plugin_manager as pm
import plugin_builder as pb
import auto_detect as adetect

# ── Config ────────────────────────────────────────────────────
def _load_token() -> str:
    """Load bot token: from env first, else from the legacy bot file."""
    t = os.environ.get("TG_BOT_TOKEN", "")
    if t and "***" not in t:
        return t
    try:
        with open("/root/hermes-workspace/scripts/rayan_dl_bot.py") as f:
            src = f.read()
        m = re.search(r'BOT_TOKEN = os\.environ\.get\("TG_BOT_TOKEN", "([^"]+)"\)', src)
        if m and "***" not in m.group(1):
            return m.group(1)
    except Exception:
        pass
    return ""


BOT_TOKEN = _load_token()
DOWNLOAD_DIR = dl.DOWNLOAD_DIR
API_STORAGE = "/var/lib/tg-bot-api"
DB_PATH = "/root/hermes-workspace/data/rayan.db"
DISK_LIMIT_GB = 2
MAX_FILE_SIZE_MB = 1900  # local Bot API allows up to 2GB

# Admin IDs — initial owners from env (used for first-run bootstrap).
# Additional admins are stored in DB (admins table) so ANY server deployment
# can define its own admins. The DB is gitignored (never committed).
ADMIN_IDS = [int(x) for x in os.environ.get("TG_ADMIN_IDS", "").split(",") if x.strip()]

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("rayan-dl")

# ── Rate limiting (simple per-user) ───────────────────────────
_rate = {}

# Global download concurrency cap: default 2 concurrent downloads across all users.
# Prevents CPU/RAM spikes when several people send links at once.
# Admin can change via /admin → settings (stored in bot_config).
_download_sem = asyncio.Semaphore(2)


def _sem_value() -> int:
    """Current max concurrent downloads from bot_config (default 2)."""
    try:
        v = int(db.get_config("max_concurrent_downloads", 2) or 2)
        return max(1, min(v, 6))  # sane bounds
    except Exception:
        return 2


def rate_limited(user_id: int, seconds: int = 8) -> bool:
    now = time.time()
    last = _rate.get(user_id, 0)
    if now - last < seconds:
        return True
    _rate[user_id] = now
    # light cleanup: drop entries older than 10 minutes to avoid memory growth
    if len(_rate) > 500:
        cutoff = now - 600
        for uid in [u for u, ts in _rate.items() if ts < cutoff]:
            del _rate[uid]
    return False


async def _limited_download(*args, **kwargs):
    """Run dl.download_video under a dynamic concurrency cap (1-6, admin-configurable)."""
    sem = asyncio.Semaphore(_sem_value())
    async with sem:
        return await asyncio.to_thread(dl.download_video, *args, **kwargs)


# ── Disk management ───────────────────────────────────────────
def folder_size(path: str) -> int:
    total = 0
    if os.path.isdir(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
    return total


def enforce_disk_limit():
    try:
        dirs = [DOWNLOAD_DIR, API_STORAGE]
        all_files = []
        for d in dirs:
            if os.path.isdir(d):
                for dirpath, _, filenames in os.walk(d):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        try:
                            mtime = os.path.getmtime(fp)
                            size = os.path.getsize(fp)
                        except Exception:
                            continue
                        if time.time() - mtime < 300:
                            continue
                        all_files.append((mtime, size, fp))

        total = folder_size(DOWNLOAD_DIR) + folder_size(API_STORAGE)
        limit = DISK_LIMIT_GB * 1024 * 1024 * 1024
        if total <= limit:
            return
        all_files.sort()
        for mtime, size, fp in all_files:
            if total <= limit:
                break
            try:
                os.remove(fp)
                total -= size
                log.info("Disk cleanup: removed %s", fp)
            except Exception:
                pass
    except Exception as e:
        log.error("Disk enforce error: %s", e)


def resolve_out_path(out_path: str) -> str:
    """Return the actual file yt-dlp wrote (may have appended .mp4/.mp3)."""
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    for cand in (out_path + ".mp4", out_path + ".mp3", out_path + ".mkv", out_path + ".webm"):
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return out_path


# ── Helper: get user lang ─────────────────────────────────────
def lang_of(update: Update) -> str:
    try:
        return db.get_user_lang(update.effective_user.id)
    except Exception:
        return "fa"


# ── Language keyboard helper ──────────────────────────────────
def lang_keyboard():
    """Full language selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="setlang|fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="setlang|en")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="setlang|tr"),
         InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang|ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="setlang|de"),
         InlineKeyboardButton("🇪🇸 Español", callback_data="setlang|es")],
    ])


# ── Handlers: start / help / about ────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.record_user(update)
    if db.is_banned(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "banned"))
        return

    # First-time users: ask language (English is default if not chosen)
    if not db.user_lang_chosen(update.effective_user.id):
        await update.message.reply_text(
            i18n.t("en", "lang_prompt"),
            reply_markup=lang_keyboard(),
        )
        return

    # Force-join gate (private chats only)
    if not await _ensure_membership(update, context):
        return

    lang = lang_of(update)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "help_btn"), callback_data="help"),
         InlineKeyboardButton(i18n.t(lang, "history_btn"), callback_data="history")],
        [InlineKeyboardButton(i18n.t(lang, "choose_site"), callback_data="lang")],
    ])
    await update.message.reply_text(i18n.t(lang, "start"), parse_mode="Markdown",
                                    disable_web_page_preview=True, reply_markup=kb)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.record_user(update)
    lang = lang_of(update)
    if not _private_only(update):
        # In a group: show a short, group-appropriate help (no personal stuff)
        await update.message.reply_text(
            i18n.t(lang, "help") + "\n\n" + i18n.t(lang, "group_help")[:400],
            parse_mode="Markdown", disable_web_page_preview=True)
        return
    await update.message.reply_text(i18n.t(lang, "help"), parse_mode="Markdown")


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private_only(update):
        return
    db.record_user(update)
    await update.message.reply_text(i18n.t(lang_of(update), "about"), parse_mode="Markdown")


# ── Language selection ────────────────────────────────────────
async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private_only(update):
        return
    db.record_user(update)
    await update.message.reply_text(i18n.t(lang_of(update), "lang_menu"),
                                    reply_markup=lang_keyboard())


async def set_lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split("|")
    log.info("set_lang_cb: data=%s parts=%s", q.data, parts)
    if len(parts) == 2 and parts[0] == "setlang":
        code = parts[1]
        if code not in i18n.LANGUAGES:
            try:
                await q.answer()
            except Exception:
                pass
            try:
                await q.message.edit_text(i18n.t(code, "lang_not_supported"))
            except Exception as e:
                log.warning("set_lang not_supported UI: %s", e)
            return
        # Save FIRST — language change must persist even if UI update fails
        db.set_user_lang(q.from_user.id, code)
        log.info("set_lang_cb: user %s lang -> %s", q.from_user.id, code)
        try:
            await q.answer()
        except Exception:
            pass
        try:
            await q.message.edit_text(i18n.t(code, "lang_set").format(lang=i18n.LANGUAGES.get(code, {}).get("name", code)))
        except Exception as e:
            log.warning("set_lang_cb UI: %s", e)
    else:
        try:
            await q.answer()
        except Exception:
            pass
# ── Song search (text query → 10 buttons, paginated) ─────────
def _fmt_duration(sec):
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def _show_search_page(update, context, uid, page=0):
    """Render 10 results for the given page + prev/next buttons."""
    data = context.user_data.get("srch")
    if not data:
        return
    results = data["results"]
    query = data["query"]
    lang = data["lang"]
    total_pages = (len(results) + 9) // 10
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    data["page"] = page
    start = page * 10
    chunk = results[start:start + 10]
    lines = [i18n.t(lang, "search_title").format(query=query), ""]
    kb = []
    for i, r in enumerate(chunk):
        idx = start + i
        dur = _fmt_duration(r.get("duration"))
        title = r.get("title", "")
        chan = r.get("channel", "")
        lines.append(f"{i+1}. 🎵 {title} — {dur} ({chan})")
        kb.append([InlineKeyboardButton(f"{i+1}. {title[:35]} ({dur})",
                                        callback_data=f"srch|pick|{idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(i18n.t(lang, "search_prev"), callback_data="srch|prev"))
    nav.append(InlineKeyboardButton(i18n.t(lang, "search_page").format(page=page + 1),
                                     callback_data="srch|noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(i18n.t(lang, "search_next"), callback_data="srch|next"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(i18n.t(lang, "search_cancel"), callback_data="srch|close")])
    try:
        await update.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def start_song_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """Text without a link in private chat → search songs, show 10 buttons."""
    db.record_user(update)
    uid = update.effective_user.id
    lang = lang_of(update)
    status = await update.message.reply_text(i18n.t(lang, "shazam_searching"))
    results = await asyncio.to_thread(dl.search_youtube, query, 30)
    if not results:
        await status.edit_text(i18n.t(lang, "search_no_results"))
        return
    context.user_data["srch"] = {"results": results, "query": query, "lang": lang, "page": 0}
    await _show_search_page(update, context, uid, 0)


async def search_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    try:
        await q.answer()
    except Exception:
        pass
    lang = lang_of(update)
    uid = q.from_user.id
    if data == "srch|close":
        try:
            await q.message.edit_text(i18n.t(lang, "cancelled"))
        except Exception:
            pass
        context.user_data.pop("srch", None)
        return
    if data == "srch|noop":
        return
    if data == "srch|next":
        d = context.user_data.get("srch")
        if d:
            await _show_search_page(update, context, uid, d.get("page", 0) + 1)
        return
    if data == "srch|prev":
        d = context.user_data.get("srch")
        if d:
            await _show_search_page(update, context, uid, d.get("page", 0) - 1)
        return
    if data.startswith("srch|pick|"):
        try:
            idx = int(data.split("|")[2])
        except (ValueError, IndexError):
            return
        d = context.user_data.get("srch")
        if not d or idx >= len(d["results"]):
            await q.message.edit_text(i18n.t(lang, "link_expired"))
            return
        r = d["results"][idx]
        try:
            await q.message.edit_text(i18n.t(lang, "search_downloading"))
        except Exception:
            pass
        # force-join gate (private)
        if not await _ensure_membership(update, context):
            return
        if db.is_banned(uid):
            await q.message.reply_text(i18n.t(lang, "banned"))
            return
        try:
            await context.bot.send_chat_action(chat_id=q.message.chat_id, action="upload_audio")
        except Exception:
            pass
        song = os.path.join(DOWNLOAD_DIR, f"srch_{uid}_{int(time.time())}.m4a")
        dl.cleanup(song)
        ok = await asyncio.to_thread(dl.download_url_audio, r["url"], song)
        if ok and os.path.getsize(song) < MAX_FILE_SIZE_MB * 1024 * 1024:
            try:
                await context.bot.send_audio(
                    chat_id=q.message.chat_id,
                    audio=open(song, "rb"),
                    title=r.get("title", ""),
                    performer=r.get("channel", ""),
                    caption=f"🎵 {r.get('title','')}",
                )
                db.record_download(uid, r["url"], "youtube", "search", True)
            except Exception as e:
                log.warning("search send audio: %s", e)
        else:
            await q.message.reply_text(i18n.t(lang, "download_failed"))
        dl.cleanup(song)
        await asyncio.to_thread(enforce_disk_limit)
        context.user_data.pop("srch", None)
        return


# ── History ───────────────────────────────────────────────────
def _private_only(update: Update) -> bool:
    """Return True if the message is in a private chat.
    Group-only commands should be used in groups; private-only (history,
    settings, admin) should NOT clutter a group."""
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # History is a personal feature — only in private chat, never in groups
    if not _private_only(update):
        return
    db.record_user(update)
    lang = lang_of(update)
    rows = db.get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text(i18n.t(lang, "history_empty"))
        return
    lines = [i18n.t(lang, "history_title")]
    site_names = {
        "youtube": i18n.t(lang, "site_youtube"),
        "instagram": i18n.t(lang, "site_instagram"),
        "twitter": i18n.t(lang, "site_twitter"),
        "tiktok": i18n.t(lang, "site_tiktok"),
        "pornhub": i18n.t(lang, "site_pornhub"),
        "other": i18n.t(lang, "site_other"),
    }
    for site, url, created in rows:
        # PLAIN RAW URL — no Markdown, no truncation, no escaping.
        # Telegram auto-links raw URLs; the full text is always shown.
        lines.append(i18n.t(lang, "history_item").format(
            site=site_names.get(site, site), url=url, time=created[:16]))
    text = "\n".join(lines)
    # Telegram limit ~4096 chars: if over, show fewer items with a note
    if len(text) > 4000:
        lines = lines[:1]
        shown = 0
        for line in rows:
            item = i18n.t(lang, "history_item").format(
                site=site_names.get(line[0], line[0]), url=line[1], time=(line[2] or "")[:16])
            if len("\n".join(lines + [item])) > 3900:
                break
            lines.append(item)
            shown += 1
        lines.append(f"\n… و {len(rows) - shown} مورد دیگر")
        text = "\n".join(lines)
    # NO parse_mode: raw text keeps URLs fully visible (Markdown can squint long links)
    await update.message.reply_text(text, disable_web_page_preview=True)


# ── Admin panel ───────────────────────────────────────────────
# Cache admin list in memory (60s TTL) — is_admin is called on many messages;
# avoids a DB query on every single message for non-admin users.
_admin_cache = {}
_admin_cache_ts = 0.0


def _refresh_admin_cache():
    global _admin_cache_ts
    now = time.time()
    if now - _admin_cache_ts > 60:
        try:
            _admin_cache.clear()
            for uid in db.get_db_admins():
                _admin_cache[uid] = True
            _admin_cache_ts = now
        except Exception:
            pass


def is_admin(user_id: int) -> bool:
    """Admin = one of env-bootstrapped owners OR a DB-registered admin."""
    if user_id in ADMIN_IDS:
        return True
    _refresh_admin_cache()
    return user_id in _admin_cache


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Admin panel is private-only — never visible in a group
    if not _private_only(update):
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        return
    lang = lang_of(update)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "adm_stats"), callback_data="adm|stats"),
         InlineKeyboardButton(i18n.t(lang, "adm_users"), callback_data="adm|users")],
        [InlineKeyboardButton(i18n.t(lang, "adm_links"), callback_data="adm|links"),
         InlineKeyboardButton(i18n.t(lang, "adm_channels"), callback_data="adm|channels")],
        [InlineKeyboardButton(i18n.t(lang, "adm_userlinks"), callback_data="adm|userlinks"),
         InlineKeyboardButton(i18n.t(lang, "adm_db"), callback_data="adm|db")],
        [InlineKeyboardButton(i18n.t(lang, "adm_admins"), callback_data="adm|adminlist"),
         InlineKeyboardButton(i18n.t(lang, "adm_addadmin"), callback_data="adm|addadmin")],
        [InlineKeyboardButton(i18n.t(lang, "adm_startmsg"), callback_data="adm|startmsg"),
         InlineKeyboardButton(i18n.t(lang, "adm_resetmsg"), callback_data="adm|resetmsg")],
        [InlineKeyboardButton(i18n.t(lang, "adm_techstats"), callback_data="adm|techstats"),
         InlineKeyboardButton(i18n.t(lang, "adm_logs"), callback_data="adm|logs")],
        [InlineKeyboardButton(i18n.t(lang, "adm_plugins"), callback_data="adm|plugins"),
         InlineKeyboardButton(i18n.t(lang, "adm_botsettings"), callback_data="adm|botsettings")],
        [InlineKeyboardButton(i18n.t(lang, "adm_broadcast"), callback_data="adm|broadcast")],
        [InlineKeyboardButton(i18n.t(lang, "adm_ban"), callback_data="adm|ban")],
        [InlineKeyboardButton(i18n.t(lang, "adm_unban"), callback_data="adm|unban"),
         InlineKeyboardButton(i18n.t(lang, "adm_addchan"), callback_data="adm|addchan")],
    ])
    await update.message.reply_text(i18n.t(lang, "admin_menu"), reply_markup=kb)


def admin_back_keyboard(lang: str, to: str = "adm|menu"):
    """Keyboard with a back button for admin sub-menus.
    'to' lets sub-menus return to their REAL previous menu instead of the main one."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "back"), callback_data=to),
         InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")]
    ])


async def _admin_menu_edit(q, lang: str):
    """Render the admin main menu onto the existing message (edit, not new msg)."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "adm_stats"), callback_data="adm|stats"),
         InlineKeyboardButton(i18n.t(lang, "adm_users"), callback_data="adm|users")],
        [InlineKeyboardButton(i18n.t(lang, "adm_links"), callback_data="adm|links"),
         InlineKeyboardButton(i18n.t(lang, "adm_channels"), callback_data="adm|channels")],
        [InlineKeyboardButton(i18n.t(lang, "adm_userlinks"), callback_data="adm|userlinks"),
         InlineKeyboardButton(i18n.t(lang, "adm_db"), callback_data="adm|db")],
        [InlineKeyboardButton(i18n.t(lang, "adm_admins"), callback_data="adm|adminlist"),
         InlineKeyboardButton(i18n.t(lang, "adm_addadmin"), callback_data="adm|addadmin")],
        [InlineKeyboardButton(i18n.t(lang, "adm_startmsg"), callback_data="adm|startmsg"),
         InlineKeyboardButton(i18n.t(lang, "adm_resetmsg"), callback_data="adm|resetmsg")],
        [InlineKeyboardButton(i18n.t(lang, "adm_techstats"), callback_data="adm|techstats"),
         InlineKeyboardButton(i18n.t(lang, "adm_logs"), callback_data="adm|logs")],
        [InlineKeyboardButton(i18n.t(lang, "adm_plugins"), callback_data="adm|plugins"),
         InlineKeyboardButton(i18n.t(lang, "adm_botsettings"), callback_data="adm|botsettings")],
        [InlineKeyboardButton(i18n.t(lang, "adm_broadcast"), callback_data="adm|broadcast")],
        [InlineKeyboardButton(i18n.t(lang, "adm_ban"), callback_data="adm|ban")],
        [InlineKeyboardButton(i18n.t(lang, "adm_unban"), callback_data="adm|unban"),
         InlineKeyboardButton(i18n.t(lang, "adm_addchan"), callback_data="adm|addchan")],
    ])
    try:
        await q.message.edit_text(i18n.t(lang, "admin_menu"), reply_markup=kb)
    except Exception:
        await q.message.reply_text(i18n.t(lang, "admin_menu"), reply_markup=kb)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if not is_admin(q.from_user.id):
        try:
            await q.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        except Exception:
            pass
        return
    lang = lang_of(update)
    parts = q.data.split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        context.user_data.pop("adm_state", None)
        await _admin_menu_edit(q, lang)
        return

    if action == "stats":
        s = db.get_stats()
        by_site = "\n".join(f"▫️ {k}: {v}" for k, v in list(s["by_site"].items())[:8]) or "—"
        disk = f"{folder_size(DOWNLOAD_DIR)//1024//1024 + folder_size(API_STORAGE)//1024//1024} MB"
        await q.message.edit_text(
            i18n.t(lang, "admin_stats").format(
                users=s["users"], links=s["links"], success=s["success"],
                failed=s["failed"], by_site=by_site, disk=disk),
            parse_mode="Markdown",
            reply_markup=admin_back_keyboard(lang))
    elif action == "users":
        rows = db.get_top_users(10)
        if not rows:
            await q.message.edit_text(i18n.t(lang, "no_users"), reply_markup=admin_back_keyboard(lang))
            return
        lines = [i18n.t(lang, "admin_users")]
        for uid, username, first, last, links in rows:
            name = first or last or username or str(uid)
            lines.append(i18n.t(lang, "admin_user_line").format(id=uid, name=name, links=links))
        # Add per-user action buttons (ban/unban/history) for first 5 users
        btns = []
        for uid, username, first, last, links in rows[:5]:
            btns.append([InlineKeyboardButton(f"👤 {first or last or username or uid} ({links})",
                                              callback_data=f"adm|userinfo|{uid}")])
        btns.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
                     InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")])
        kb = InlineKeyboardMarkup(btns)
        await q.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                  disable_web_page_preview=True,
                                  reply_markup=kb)
    elif action == "userinfo":
        # parts: adm|userinfo|<uid>
        if len(parts) >= 3:
            try:
                uid = int(parts[2])
            except ValueError:
                uid = 0
            info = db.get_user_info(uid)
            if not info:
                await q.message.edit_text("❌ کاربر پیدا نشد.", reply_markup=admin_back_keyboard(lang))
                return
            links_count = info.get("total_links") or 0
            banned = "✅ فعال" if not info.get("banned") else "🚫 مسدود"
            text = (f"👤 **{info['name']}**\n"
                    f"🆔 `{uid}`\n"
                    f"📛 یوزرنیم: @{info['username'] or '—'}\n"
                    f"🔗 لینک‌ها: {links_count}\n"
                    f"🌍 زبان: {info.get('lang') or '—'}\n"
                    f"🛡 وضعیت: {banned}")
            btns = [
                [InlineKeyboardButton("📤 لینک‌های این کاربر (فایل)", callback_data=f"adm|exportlinks|{uid}")],
                [InlineKeyboardButton("🚫 بن", callback_data=f"adm|banuser|{uid}") if not info.get("banned")
                 else InlineKeyboardButton("✅ رفع مسدودی", callback_data=f"adm|unbanuser|{uid}")],
                [InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|users"),
                 InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")],
            ]
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        else:
            await q.message.edit_text(i18n.t(lang, "invalid_id"), reply_markup=admin_back_keyboard(lang))
    elif action == "exportlinks":
        # parts: adm|exportlinks|<uid> — export all links as txt
        if len(parts) >= 3:
            try:
                uid = int(parts[2])
            except ValueError:
                uid = 0
            links = db.get_user_links(uid, 500)
            info = db.get_user_info(uid) or {}
            name = info.get("name") or str(uid)
            if not links:
                await q.message.edit_text("📭 این کاربر لینکی نفرستاده.", reply_markup=admin_back_keyboard(lang))
                return
            lines = [f"🔗 لینک‌های {name} (ID: {uid})",
                     f"تعداد: {len(links)}\n" + "=" * 30 + "\n"]
            for lid, url, site, title, created in links:
                lines.append(f"• [{site}] {title or 'بدون عنوان'}")
                lines.append(f"  {url}")
                lines.append(f"  🕒 {created}\n")
            fpath = f"/tmp/links_{uid}_{int(time.time())}.txt"
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            with open(fpath, "rb") as f:
                await q.message.reply_document(document=f, filename=f"links_{uid}.txt",
                                               caption=f"📄 لینک‌های {name}")
            os.remove(fpath)
    elif action == "banuser":
        if len(parts) >= 3:
            try:
                uid = int(parts[2])
            except ValueError:
                uid = 0
            if uid == q.from_user.id:
                await q.message.edit_text("❌ نمی‌تونی خودت رو مسدود کنی!", reply_markup=admin_back_keyboard(lang))
                return
            db.ban_user(uid)
            await q.message.edit_text(f"✅ کاربر `{uid}` مسدود شد.", parse_mode="Markdown",
                                      reply_markup=admin_back_keyboard(lang))
    elif action == "unbanuser":
        if len(parts) >= 3:
            try:
                uid = int(parts[2])
            except ValueError:
                uid = 0
            db.unban_user(uid)
            await q.message.edit_text(f"✅ مسدودی کاربر `{uid}` برداشته شد.", parse_mode="Markdown",
                                      reply_markup=admin_back_keyboard(lang))
    elif action == "links":
        rows = db.get_all_links(20)
        if not rows:
            await q.message.edit_text(i18n.t(lang, "no_links"), reply_markup=admin_back_keyboard(lang))
            return
        site_names = {
            "youtube": i18n.t(lang, "site_youtube"),
            "instagram": i18n.t(lang, "site_instagram"),
            "twitter": i18n.t(lang, "site_twitter"),
            "tiktok": i18n.t(lang, "site_tiktok"),
            "pornhub": i18n.t(lang, "site_pornhub"),
            "other": i18n.t(lang, "site_other"),
        }
        lines = [i18n.t(lang, "admin_links")]
        for lid, uid, username, first, last, site, title, url, created in rows:
            name = first or last or username or str(uid)
            title_s = (title or "—")[:60]
            # Full clickable link (no truncation of the URL itself)
            esc_url = url.replace("\\", "/").replace(")", "%29").replace("(", "%28").replace("]", "%5D")
            link = f"[{url}]({esc_url})"
            lines.append(i18n.t(lang, "admin_link_line").format(
                user=name, uid=uid, site=site_names.get(site, site),
                title=title_s, url=link, time=(created or "")[:16]))
        # Telegram message limit ~4096 chars — truncate if needed
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3900] + "\n..."
        await q.message.edit_text(text, parse_mode="Markdown",
                                  disable_web_page_preview=True,
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "channels":
        channels = db.get_force_channels()
        if not channels:
            text = i18n.t(lang, "force_join_no_channels")
            kb = admin_back_keyboard(lang)
        else:
            lines = [i18n.t(lang, "force_join_list")]
            for ch in channels:
                lines.append(i18n.t(lang, "force_join_list_item").format(
                    username=ch["username"], title=ch.get("title") or ""))
            text = "\n".join(lines)
            # Add delete buttons for each channel
            btns = [[InlineKeyboardButton(f"🗑 حذف @{ch['username']}", callback_data=f"adm|delchan|{ch['username']}")]
                    for ch in channels]
            btns.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
                         InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")])
            kb = InlineKeyboardMarkup(btns)
        await q.message.edit_text(text, reply_markup=kb)
    elif action == "addchan":
        context.user_data["adm_state"] = "addchan"
        await q.message.edit_text(i18n.t(lang, "force_join_admin_usage"),
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "delchan":
        # parts: adm|delchan|<username>
        if len(parts) >= 3:
            username = _clean_username(parts[2])
            if db.remove_force_channel(username):
                await q.message.edit_text(i18n.t(lang, "force_join_removed").format(ch=username),
                                          reply_markup=admin_back_keyboard(lang))
            else:
                await q.message.edit_text(i18n.t(lang, "force_join_not_found").format(ch=username),
                                          reply_markup=admin_back_keyboard(lang))
        else:
            context.user_data["adm_state"] = "delchan"
            await q.message.edit_text("🎯 یوزرنیم کانالی که می‌خوای حذف کنی رو بفرست (مثل mychannel):",
                                      reply_markup=admin_back_keyboard(lang))
    elif action == "adminlist":
        db_admins = db.get_db_admins()
        env_admins = ADMIN_IDS
        lines = ["👥 **لیست ادمین‌ها:**\n"]
        for a in env_admins:
            lines.append(f"▫️ `{a}` — (env owner)")
        for a in db_admins:
            lines.append(f"▫️ `{a}` — (panel)")
        if not env_admins and not db_admins:
            lines.append("📭 هیچ ادمینی تعریف نشده!")
        btns = []
        for a in db_admins:
            btns.append([InlineKeyboardButton(f"🗑 حذف ادمین {a}", callback_data=f"adm|removeadmin|{a}")])
        btns.append([InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm|addadmin")])
        btns.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
                     InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")])
        await q.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(btns))
    elif action == "addadmin":
        context.user_data["adm_state"] = "addadmin"
        await q.message.edit_text("🎯 آیدی عددی کاربری که می‌خوای ادمین کنه رو بفرست:",
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "removeadmin":
        # parts: adm|removeadmin|<uid>
        if len(parts) >= 3:
            try:
                uid = int(parts[2])
            except ValueError:
                uid = 0
            if uid in ADMIN_IDS:
                await q.message.edit_text("❌ نمی‌تونی ادمین env رو حذف کنی (توی systemd تنظیم شده).",
                                          reply_markup=admin_back_keyboard(lang))
            elif db.remove_admin(uid):
                await q.message.edit_text(f"✅ ادمین `{uid}` حذف شد.", parse_mode="Markdown",
                                          reply_markup=admin_back_keyboard(lang))
            else:
                await q.message.edit_text("❌ این کاربر ادمین نیست.", reply_markup=admin_back_keyboard(lang))
    elif action == "startmsg":
        context.user_data["adm_state"] = "startmsg"
        current = db.get_custom_msg("fa", "start") or i18n.LANGUAGES["fa"].get("start", "")
        preview = current[:200]
        await q.message.edit_text(
            f"📝 **تغییر متن شروع ربات**\n\n"
            f"متن فعلی (فارسی):\n```\n{preview}\n```\n\n"
            f"۱. **متن جدید رو به فارسی بنویس** و بفرست (متن ساده یا با Markdown و ایموجی)\n"
            f"۲. بعدش ازت می‌پرسم ترجمه خودکار به بقیه زبان‌ها باشه یا دستی.\n\n"
            f"✍️ متن جدیدت رو بفرست:",
            parse_mode="Markdown", reply_markup=admin_back_keyboard(lang))
    elif action == "resetmsg":
        # Remove all custom start overrides → back to defaults
        for code in i18n.TARGET_MAP:
            db.remove_custom_msg(code, "start")
        i18n.invalidate_msg_cache()
        await q.message.edit_text("✅ متن شروع به **پیش‌فرض** برگشت (همه زبان‌ها).",
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "techstats":
        import subprocess as _sp
        # system resource stats
        try:
            r = _sp.run(["free", "-m"], capture_output=True, text=True, timeout=10)
            mem_line = r.stdout.splitlines()[1].split()
            used_mb = int(mem_line[2]); total_mb = int(mem_line[1])
        except Exception:
            used_mb, total_mb = 0, 0
        try:
            r = _sp.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
            df_parts = r.stdout.splitlines()[1].split()
            disk_used = df_parts[4] if len(df_parts) > 4 else "?"
        except Exception:
            disk_used = "?"
        try:
            r = _sp.run(["bash", "-c", "cat /proc/loadavg"], capture_output=True, text=True, timeout=10)
            load = r.stdout.split()[:1][0] if r.stdout.strip() else "?"
        except Exception:
            load = "?"
        text = f"🤖 **آمار فنی ربات**\n\n"
        text += f"💾 حافظه: {used_mb}MB / {total_mb}MB ({int(used_mb/total_mb*100) if total_mb else 0}%)\n"
        text += f"💿 دیسک: استفاده {disk_used}\n"
        text += f"⏱ بار سیستم (loadavg): {load}\n"
        text += f"📦 حجم دانلود فعلی: {folder_size(DOWNLOAD_DIR)//1024//1024} MB\n"
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard(lang))
    elif action == "logs":
        # Show recent errors from systemd journal
        try:
            r = subprocess.run(
                ["journalctl", "-u", "tg-download-bot", "--no-pager", "-n", "200", "-o", "short-iso"],
                capture_output=True, text=True, timeout=15)
            lines = [l for l in r.stdout.splitlines()
                     if any(x in l.lower() for x in ("error", "exception", "traceback"))
                     and "HTTP Request" not in l]
            if not lines:
                text = "✅ **هیچ خطایی در سابقه نیست!** ربات سالم کار می‌کنه."
            else:
                tail = lines[-8:]
                text = "🪵 **آخرین خطاها:**\n\n" + "\n".join(f"• `{l[:150]}`" for l in tail)
                text += "\n\n(برای خطای کامل، لاگ سیستم رو ببین)"
        except Exception as e:
            text = f"❌ خوندن لاگ: {e}"
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard(lang))
    elif action == "botsettings":
        current = db.get_config("max_concurrent_downloads", "2")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⬇️ حداکثر دانلود همزمان: {current}", callback_data="adm|setdl")],
            [InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
             InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")],
        ])
        await q.message.edit_text(
            "⚙️ **تنظیمات ربات**\n\n"
            "تنظیم «حداکثر دانلود همزمان» کنترل می‌کنه چند دانلود هم‌زمان انجام شه.\n"
            "• کم (۱) = پایدارتر ولی کندتر\n"
            "• زیاد (۶) = سریع‌تر ولی CPU/رم بیشتر\n\n"
            "برای تغییر، روی دکمه بزن و عدد ۱ تا ۶ رو بفرست.",
            parse_mode="Markdown", reply_markup=kb)
    elif action == "setdl":
        context.user_data["adm_state"] = "setdl"
        await q.message.edit_text("🔢 عدد ۱ تا ۶ رو بفرست (حداکثر دانلود همزمان):",
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "plugins":
        plugins = pm.discover_plugins()
        if not plugins:
            text = "🔌 **پلاگین‌ها**\n\n📭 هیچ پلاگینی نصب نیست.\n\n"
            text += "برای نصب، دکمه «➕ نصب پلاگین» رو بزن و فایل zip یا لینک بفرست.\n"
            text += "یا با «🛠 پلاگین‌ساز» یک پلاگین دستی بساز!"
            kb = [[InlineKeyboardButton(i18n.t(lang, "adm_install_plugin"), callback_data="adm|plugin_install")],
                  [InlineKeyboardButton(i18n.t(lang, "adm_plugin_builder"), callback_data="adm|pbbuild")],
                  [InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
                   InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")]]
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
            return
        lines = ["🔌 **پلاگین‌ها:**\n"]
        kb = []
        for p in plugins:
            status = "🟢" if p.get("enabled") else "🔴"
            lines.append(f"{status} **{p['name']}** v{p.get('version')}")
            lines.append(f"   {p.get('description','')[:60]}")
            kb.append([InlineKeyboardButton(f"🔧 {p['name']} — تنظیمات", callback_data=f"adm|plugin_info|{p['name']}")])
        kb.append([InlineKeyboardButton(i18n.t(lang, "adm_install_plugin"), callback_data="adm|plugin_install")])
        kb.append([InlineKeyboardButton(i18n.t(lang, "adm_plugin_builder"), callback_data="adm|pbbuild")])
        kb.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
                   InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")])
        await q.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))
    elif action == "plugin_install":
        context.user_data["adm_state"] = "plugin_install"
        await q.message.edit_text(
            "➕ **نصب پلاگین**\n\n"
            "دو راه داری:\n"
            "📦 **فایل zip بفرست** (شامل plugin.json + کد) — همین‌جا آپلودش کن\n"
            "🔗 **یا لینک بفرست**:\n"
            "• لینک مستقیم به فایل **zip**\n"
            "• لینک **raw** از GitHub به plugin.json\n\n"
            "مثال لینک: `https://raw.githubusercontent.com/user/repo/main/plugin.json`\n\n"
            f"فایل یا لینک رو بفرست:",
            parse_mode="Markdown", reply_markup=admin_back_keyboard(lang, "adm|plugins"))
    elif action == "plugin_info":
        # parts: adm|plugin_info|<name>
        if len(parts) >= 3:
            pname = parts[2]
            meta = pm.get_plugin(pname)
            if not meta:
                await q.message.edit_text("❌ پلاگین پیدا نشد.", reply_markup=admin_back_keyboard(lang))
                return
            state = "🟢 فعال" if meta.get("enabled") else "🔴 غیرفعال"
            cfg = meta.get("config", {})
            schema = meta.get("config_schema", {})
            cfg_lines = "\n".join(
                f"   {schema.get(k, {}).get('label', k)}: {v}"
                for k, v in cfg.items()) or "   (بدون تنظیمات)"
            lines = [f"🔌 **{pname}** v{meta.get('version')}",
                     f"📝 {meta.get('description','')}",
                     f"🟢 وضعیت: {state}",
                     f"🌐 سایت‌ها: {', '.join(meta.get('sites', []))}",
                     f"\n⚙️ تنظیمات:\n{cfg_lines}\n"]
            kb = []
            kb.append([InlineKeyboardButton("🔄 فعال/غیرفعال", callback_data=f"adm|plugin_toggle|{pname}")])
            kb.append([InlineKeyboardButton("✏️ تنظیم مقدار", callback_data=f"adm|plugin_set|{pname}")])
            kb.append([InlineKeyboardButton("🗑 حذف پلاگین", callback_data=f"adm|plugin_del|{pname}")])
            kb.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|plugins"),
                       InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")])
            await q.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(kb))
    elif action == "plugin_toggle":
        if len(parts) >= 3:
            pname = parts[2]
            meta = pm.get_plugin(pname)
            if meta:
                new_state = not meta.get("enabled")
                if pm.toggle_plugin(pname, new_state):
                    await q.message.edit_text(f"✅ پلاگین `{pname}` → {'فعال' if new_state else 'غیرفعال'} شد.",
                                              parse_mode="Markdown",
                                              reply_markup=admin_back_keyboard(lang, f"adm|plugin_info|{pname}"))
                else:
                    await q.message.edit_text("❌ خطا در تغییر وضعیت.",
                                              reply_markup=admin_back_keyboard(lang, f"adm|plugin_info|{pname}"))
    elif action == "plugin_del":
        if len(parts) >= 3:
            pname = parts[2]
            if pm.remove_plugin(pname):
                await q.message.edit_text(f"🗑 پلاگین `{pname}` حذف شد.", parse_mode="Markdown",
                                          reply_markup=admin_back_keyboard(lang, "adm|plugins"))
            else:
                await q.message.edit_text("❌ حذف ناموفق.",
                                          reply_markup=admin_back_keyboard(lang, "adm|plugins"))
    elif action == "plugin_set":
        if len(parts) >= 3:
            pname = parts[2]
            meta = pm.get_plugin(pname)
            if not meta:
                await q.message.edit_text("❌ پلاگین پیدا نشد.", reply_markup=admin_back_keyboard(lang))
                return
            cfg = meta.get("config", {})
            if not cfg:
                await q.message.edit_text("❌ این پلاگین تنظیماتی نداره.", reply_markup=admin_back_keyboard(lang))
                return
            schema = meta.get("config_schema", {})
            context.user_data["adm_state"] = "plugin_set"
            context.user_data["plugin_set_name"] = pname
            key_btns = []
            for k in cfg.keys():
                sch = schema.get(k, {})
                label = sch.get("label", f"🔧 {k}")
                help_txt = sch.get("help", "")
                if help_txt:
                    label += "  ℹ️"
                key_btns.append([InlineKeyboardButton(label, callback_data=f"adm|plugin_setkey|{pname}|{k}")])
            key_btns.append([InlineKeyboardButton("❓ راهنمای تنظیمات", callback_data=f"adm|plugin_help|{pname}")])
            key_btns.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data=f"adm|plugin_info|{pname}")])
            await q.message.edit_text(
                f"✏️ **تنظیمات {pname}**\n\n"
                f"کدوم کلید رو تغییر بدی؟\n(ℹ️ یعنی توضیح داره — دکمه «راهنما» همه رو توضیح می‌ده)",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(key_btns))
    elif action == "plugin_help":
        if len(parts) >= 3:
            pname = parts[2]
            meta = pm.get_plugin(pname)
            if not meta:
                await q.message.edit_text("❌ پلاگین پیدا نشد.", reply_markup=admin_back_keyboard(lang))
                return
            schema = meta.get("config_schema", {})
            cfg = meta.get("config", {})
            lines = [f"❓ **راهنمای تنظیمات {pname}**:\n"]
            for k, v in cfg.items():
                sch = schema.get(k, {})
                label = sch.get("label", k)
                help_txt = sch.get("help", "—")
                lines.append(f"• **{label}** = `{v}`\n  {help_txt}\n")
            if not cfg:
                lines.append("(تنظیماتی تعریف نشده)")
            kb = [[InlineKeyboardButton(i18n.t(lang, "back"), callback_data=f"adm|plugin_set|{pname}")],
                  [InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")]]
            await q.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(kb))
    elif action == "plugin_setkey":
        # parts: adm|plugin_setkey|<name>|<key>
        if len(parts) >= 4:
            pname, key = parts[2], parts[3]
            context.user_data["adm_state"] = "plugin_setkey"
            context.user_data["plugin_set_name"] = pname
            context.user_data["plugin_set_key"] = key
            await q.message.edit_text(f"🔧 مقدار جدید برای `{key}` رو بفرست کدوم پلاگین {pname}:",
                                      parse_mode="Markdown",
                                      reply_markup=admin_back_keyboard(lang, f"adm|plugin_set|{pname}"))
    elif action == "pbbuild":
        context.user_data["adm_state"] = "pb_name"
        await q.message.edit_text(
            "🛠 **پلاگین‌ساز دستی**\n\n"
            "قدم ۱ از ۴: اسم پلاگین رو بنویس.\n"
            "فقط حروف انگلیسی، عدد، `-` یا `_` (۲ تا ۴۰ کاراکتر).\n\n"
            "مثال: `mysitecomics`",
            parse_mode="Markdown", reply_markup=admin_back_keyboard(lang, "adm|plugins"))
        return
    elif action == "pbauto":
        context.user_data["adm_state"] = "pb_auto_url"
        await q.message.edit_text(
            "🔎 **تشخیص خودکار**\n\n"
            "یک **لینک نمونه** از سایتت بفرست تا خودم صفحه رو تحلیل کنم و\n"
            "نوع محتوا (کامیک/ویدیو/عکس) + الگوی regex رو پیشنهاد بدم.\n\n"
            "لینک رو بفرست:", reply_markup=admin_back_keyboard(lang, "adm|plugins"))
        return
    elif action == "pbtype":
        # parts: adm|pbtype|<ctype>
        if len(parts) >= 3:
            ctype = parts[2]
            context.user_data["pb_type"] = ctype
            context.user_data["adm_state"] = "pb_pattern"
            info = pb.CONTENT_TYPES.get(ctype, {})
            await q.message.edit_text(
                f"🛠 **{info.get('label', ctype)}**\n\n{info.get('prompt', '')}\n\n"
                f"الگوی **regex** رو بفرست:",
                parse_mode="Markdown", reply_markup=admin_back_keyboard(lang, "adm|plugins"))
    elif action == "userlinks":
        # Show all users, ask for an ID to export their links
        rows = db.get_top_users(50)
        if not rows:
            await q.message.edit_text(i18n.t(lang, "no_users"), reply_markup=admin_back_keyboard(lang))
            return
        lines = ["👥 **همه کاربران — آیدی یکی رو بفرست تا لینک‌هاش برات توی فایل بیاد:**\n"]
        for uid, username, first, last, links in rows:
            name = first or last or username or str(uid)
            lines.append(f"▫️ `{uid}` — {name} ({links} لینک)")
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3850] + "\n..."
        context.user_data["adm_state"] = "userlinks"
        await q.message.edit_text(text, parse_mode="Markdown",
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "db":
        try:
            with open(DB_PATH, "rb") as f:
                await q.message.reply_document(document=f, filename="rayan_backup.db",
                                               caption="💾 نسخه پشتیبان دیتابیس")
        except Exception as e:
            log.error("db export: %s", e)
            try:
                await q.message.edit_text("❌ خطا در ارسال دیتابیس", reply_markup=admin_back_keyboard(lang))
            except Exception:
                pass
    elif action == "broadcast":
        context.user_data["adm_state"] = "broadcast"
        await q.message.edit_text(i18n.t(lang, "broadcast_prompt"),
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "ban":
        context.user_data["adm_state"] = "ban"
        await q.message.edit_text(i18n.t(lang, "ban_prompt"),
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "unban":
        context.user_data["adm_state"] = "unban"
        await q.message.edit_text(i18n.t(lang, "unban_prompt"),
                                  reply_markup=admin_back_keyboard(lang))
    elif action == "close":
        await q.message.delete()


async def plugin_file_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an uploaded plugin zip file when the admin is in 'plugin_install' state."""
    if not is_admin(update.effective_user.id):
        return
    state = context.user_data.get("adm_state")
    if state != "plugin_install":
        return
    context.user_data.pop("adm_state", None)
    # Guard: this handler may fire for edited/channel updates without a message/document
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".zip")):
        await update.message.reply_text(
            "❌ فقط فایل **ZIP** قبول می‌شه (شامل plugin.json + کد پلاگین).\n\n"
            "یا می‌تونی لینک مستقیم zip یا raw GitHub بدی.")
        return
    status = await update.message.reply_text("📦 در حال نصب پلاگین از فایل...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        zip_path = os.path.join(DOWNLOAD_DIR, f"plug_{int(time.time())}_{doc.file_name}")
        await tg_file.download_to_drive(zip_path)
        result = await asyncio.to_thread(pm.install_plugin_from_file, zip_path)
        try:
            os.remove(zip_path)
        except OSError:
            pass
    except Exception as e:
        log.error("plugin file download: %s", e)
        result = {"ok": False, "message": f"دانلود فایل ناموفق: {e}"}
    if result.get("ok"):
        await status.edit_text(f"✅ {result['message']}")
    else:
        await status.edit_text(f"❌ نصب ناموفق: {result.get('message')}")


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast/ban/unban text input from admin."""
    # Guard: this handler can be reached from handle_link on edited/channel updates,
    # and from the TEXT&COMMAND MessageHandler — both may lack a real message.
    if not update.message or not update.message.text:
        return
    if not is_admin(update.effective_user.id):
        return
    state = context.user_data.get("adm_state")
    lang = lang_of(update)
    text = update.message.text.strip()
    if not state:
        # No active state — if admin types a channel command, point them to /admin
        if text.lower().startswith(("/addchannel", "/removechannel", "/delchannel", "/channels")):
            await update.message.reply_text(
                "🛡 **مدیریت کانال‌ها از داخل پنل انجام می‌شه:**\n\n"
                "`/admin` بزن → «🔗 کانال‌ها» برای لیست/حذف\n"
                "`/admin` بزن → «➕ کانال» برای اضافه کردن\n\n"
                "یا اگه الان توی حالت افزودن هستی، فقط **یوزرنیم کانال** رو بفرست (مثل `MatinSenPaii`)."
            )
        return

    if text == "/cancel":
        context.user_data.pop("adm_state", None)
        await update.message.reply_text(i18n.t(lang, "broadcast_cancelled"))
        return

    if state == "broadcast":
        context.user_data.pop("adm_state", None)
        user_ids = db.get_all_user_ids()
        sent = failed = 0
        for uid in user_ids:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await update.message.reply_text(i18n.t(lang, "broadcast_sent").format(sent=sent) +
                                        (i18n.t(lang, "broadcast_failed").format(failed=failed) if failed else ""))

    elif state == "ban":
        context.user_data.pop("adm_state", None)
        try:
            uid = int(text)
        except ValueError:
            await update.message.reply_text(i18n.t(lang, "invalid_id"))
            return
        if db.ban_user(uid):
            await update.message.reply_text(i18n.t(lang, "ban_done").format(id=uid))
        else:
            await update.message.reply_text(i18n.t(lang, "not_found"))

    elif state == "unban":
        context.user_data.pop("adm_state", None)
        try:
            uid = int(text)
        except ValueError:
            await update.message.reply_text(i18n.t(lang, "invalid_id"))
            return
        if db.unban_user(uid):
            await update.message.reply_text(i18n.t(lang, "unban_done").format(id=uid))
        else:
            await update.message.reply_text(i18n.t(lang, "not_found"))

    elif state == "addchan":
        context.user_data.pop("adm_state", None)
        username = _clean_username(text)
        if not username:
            await update.message.reply_text(i18n.t(lang, "force_join_admin_usage"))
            return
        # Fetch channel info (title + chat_id) — also tells us if bot can access it
        title, chat_id, bot_access = username, None, False
        bot_is_admin = False
        try:
            ch = await context.bot.get_chat(chat_id=f"@{username}")
            title = ch.title or username
            chat_id = ch.id
            bot_access = True
            # Check if bot is admin in the channel (needed for auto-check)
            try:
                me = await context.bot.get_me()
                mem = await context.bot.get_chat_member(chat_id=chat_id, user_id=me.id)
                bot_is_admin = mem.status in ("administrator", "creator")
            except Exception:
                bot_is_admin = False
        except Exception as e:
            log.warning("addchan get_chat %s: %s", username, e)
        ok, res = db.add_force_channel(username, title, chat_id, update.effective_user.id)
        if ok:
            msg = i18n.t(lang, "force_join_added").format(ch=username)
            if not bot_access:
                msg += ("\n\n⚠️ **مهم:** ربات به این کانال دسترسی نداره! "
                        "ربات رو **ادمین کانال** کن (یا توی کانال عضو کن) تا بتونه عضویت کاربرا رو چک کنه. "
                        "تا اون موقع، کاربرا با دکمه «عضو شدم» تایید می‌شن.")
            elif not bot_is_admin:
                msg += ("\n\nℹ️ ربات ادمین کانال نیست، پس عضویت کاربرا **خودکار** چک نمی‌شه — "
                        "کاربرا با دکمه «✅ عضو شدم» تایید می‌شن. "
                        "برای چک خودکار، ربات رو **ادمین کانال** کن و دوباره کانال رو اضافه کن.")
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(i18n.t(lang, "force_join_exists").format(ch=username))

    elif state == "delchan":
        context.user_data.pop("adm_state", None)
        username = _clean_username(text)
        if db.remove_force_channel(username):
            await update.message.reply_text(i18n.t(lang, "force_join_removed").format(ch=username))
        else:
            await update.message.reply_text(i18n.t(lang, "force_join_not_found").format(ch=username))

    elif state == "addadmin":
        context.user_data.pop("adm_state", None)
        try:
            uid = int(text.strip())
        except ValueError:
            await update.message.reply_text(i18n.t(lang, "invalid_id"))
            return
        if uid == update.effective_user.id:
            await update.message.reply_text("❌ تو خودت ادمینی!")
            return
        if db.add_admin(uid, update.effective_user.id):
            # invalidate admin cache so the new admin works immediately
            _admin_cache_ts = 0.0
            await update.message.reply_text(f"✅ کاربر `{uid}` ادمین شد.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ کاربر `{uid}` از قبل ادمینه.", parse_mode="Markdown")

    elif state == "startmsg":
        # Save the raw text temporarily, ask translate method
        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("❌ متن خالی بود. دوباره بفرست.")
            return
        context.user_data["startmsg_new"] = text
        context.user_data["adm_state"] = "startmsg_translate"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 ترجمه خودکار به همه زبان‌ها", callback_data="smt|auto")],
            [InlineKeyboardButton("✍️ فقط همین (فارسی)", callback_data="smt|manual")],
            [InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu")],
        ])
        await update.message.reply_text(
            f"✅ متن جدید ذخیره شد:\n```\n{text[:150]}\n```\n\n"
            f"حالا چیکار کنم؟",
            parse_mode="Markdown", reply_markup=kb)

    elif state == "startmsg_translate":
        # handled by startmsg_translate_cb callback
        pass

    elif state == "setdl":
        context.user_data.pop("adm_state", None)
        try:
            v = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ عدد نامعتبر. ۱ تا ۶ بفرست.")
            return
        if 1 <= v <= 6:
            db.set_config("max_concurrent_downloads", v)
            await update.message.reply_text(f"✅ حداکثر دانلود همزمان: **{v}** شد.")
        else:
            await update.message.reply_text("❌ فقط ۱ تا ۶ مجازه.")

    elif state == "plugin_install":
        context.user_data.pop("adm_state", None)
        text = (update.message.text or "").strip().strip("@")
        if not (text.startswith("http://") or text.startswith("https://")):
            await update.message.reply_text("❌ لینک معتبر بفرست (با http/https شروع بشه).")
            return
        status = await update.message.reply_text("📦 در حال نصب پلاگین...")
        result = await asyncio.to_thread(pm.install_plugin_from_url, text)
        if result.get("ok"):
            await status.edit_text(f"✅ {result['message']}")
        else:
            await status.edit_text(f"❌ نصب ناموفق: {result.get('message')}")

    elif state == "plugin_setkey":
        context.user_data.pop("adm_state", None)
        pname = context.user_data.pop("plugin_set_name", "")
        key = context.user_data.pop("plugin_set_key", "")
        val = (update.message.text or "").strip()
        if not pname or not key:
            await update.message.reply_text("❌ اطلاعات پلاگین از دست رفته. دوباره تلاش کن.")
            return
        # try convert to number/bool where sensible
        if val.lower() in ("true", "false"):
            val = val.lower() == "true"
        else:
            try:
                val = int(val)
            except ValueError:
                try:
                    val = float(val)
                except ValueError:
                    pass
        if pm.set_plugin_config(pname, key, val):
            await update.message.reply_text(f"✅ تنظیم `{key}` پلاگین `{pname}` = `{val}` شد.",
                                            parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ خطا در ذخیره تنظیمات.")

    elif state == "pb_name":
        context.user_data["adm_state"] = "pb_site"
        context.user_data["pb_name"] = (text or "").strip()
        await update.message.reply_text(
            "✅ اسم ذخیره شد. قدم ۲ از ۴: **دامنه سایت** رو بنویس.\n\n"
            "فقط دامنه کافیه، مثلا:\n`mysite.com` یا `www.mysite.com`\n"
            "(ربات خودش https و www رو هندل می‌کنه)")
    elif state == "pb_site":
        context.user_data["adm_state"] = "pb_type"
        context.user_data["pb_site"] = (text or "").strip()
        kb = []
        for ctype, info in pb.CONTENT_TYPES.items():
            kb.append([InlineKeyboardButton(info["label"], callback_data=f"adm|pbtype|{ctype}")])
        kb.append([InlineKeyboardButton("🔎 تشخیص خودکار (با URL نمونه)", callback_data="adm|pbauto")])
        kb.append([InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|plugins")])
        await update.message.reply_text(
            "✅ دامنه ذخیره شد. قدم ۳ از ۴: **نوع محتوا** رو انتخاب کن،\n"
            "یا بذار خودم با یه URL نمونه تشخیص بدم 👇",
            reply_markup=InlineKeyboardMarkup(kb))
    elif state == "pb_auto_url":
        context.user_data["adm_state"] = "pb_auto_pattern"
        context.user_data["pb_sample_url"] = (text or "").strip()
        await update.message.reply_text(
            "🔎 در حال تحلیل صفحه... (حداکثر ۳۰ ثانیه)")
        import asyncio as _asyncio
        det = await _asyncio.to_thread(adetect.detect, context.user_data.get("pb_sample_url", ""))
        if det.get("type") in ("comic", "photos"):
            # suggest a pattern from the samples
            samples = det.get("samples") or []
            suggested = ""
            if samples:
                s = samples[0]
                import re as _re
                # build a generic pattern: escape the domain part, keep path shape
                m = _re.match(r"https?://([^/]+)(/.*?)(\.[a-z]+)$", s)
                if m:
                    domain, path, ext = m.group(1), m.group(2), m.group(3)
                    domain_esc = _re.escape(domain)
                    path_esc = _re.escape(path).replace(_re.escape("\\"), "\\\\")
                    suggested = f"https?://{domain_esc}{path_esc[:60]}[^\\\"']+\\.(?:jpg|jpeg|png)"
            context.user_data["pb_auto_type"] = det["type"]
            context.user_data["pb_auto_pattern"] = suggested
            await update.message.reply_text(
                f"🧠 **تشخیص خودکار:** {det.get('detail', det['type'])}\n\n"
                f"پیشنهاد الگو:\n`{suggested or '(الگو قابل ساخت نیست — دستی بنویس)'}`\n\n"
                f"الگو رو تأیید کن یا تغییرش بده و بفرست:",
                parse_mode="Markdown")
        elif det.get("type") == "video":
            context.user_data["pb_auto_type"] = "video"
            context.user_data["pb_auto_pattern"] = r"https?://[^\s\"]+\.(?:mp4|m3u8)"
            await update.message.reply_text(
                f"🧠 **تشخیص خودکار:** {det.get('detail', 'video')}\n\n"
                f"الگوی پیشنهادی:\n`https?://[^\\s\\\"]+\\.(?:mp4|m3u8)`\n\n"
                f"تأیید کن یا تغییرش بده و بفرست:", parse_mode="Markdown")
        else:
            context.user_data.pop("pb_auto_type", None)
            context.user_data.pop("pb_auto_pattern", None)
            context.user_data["adm_state"] = "pb_type"
            await update.message.reply_text(
                f"😕 چیزی قابل استخراج پیدا نشد: {det.get('detail', '')}\n\n"
                f"توی این حالت بهتره **نوع رو دستی انتخاب کنی** 👇",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(info["label"], callback_data=f"adm|pbtype|{ctype}")]
                    for ctype, info in pb.CONTENT_TYPES.items()
                ] + [[InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|plugins")]]))
    elif state == "pb_auto_pattern":
        context.user_data.pop("adm_state", None)
        name = context.user_data.pop("pb_name", "").strip()
        site = context.user_data.pop("pb_site", "").strip()
        ctype = context.user_data.get("pb_auto_type", "multi")
        pattern = (text or "").strip()
        context.user_data.pop("pb_auto_type", None)
        context.user_data.pop("pb_auto_pattern", None)
        if not name or not site or not pattern:
            await update.message.reply_text("❌ اطلاعات ناقصه. دوباره از «🛠 پلاگین‌ساز» شروع کن.")
            return
        status = await update.message.reply_text("🔧 در حال ساخت پلاگین...")
        result = pb.build_plugin(name, site, ctype, pattern)
        if not result.get("ok"):
            await status.edit_text(f"❌ {result.get('message')}")
            return
        await status.edit_text(
            f"✅ **{result['message']}**\n\n"
            f"📋 خلاصه:\n• نام: `{name}`\n• دامنه: `{site}`\n• نوع: `{ctype}`\n"
            f"• الگو: `{pattern[:80]}`\n\n"
            f"حالا یه لینک از `{site}` بفرست تا تستش کنی! 🚀")
    elif state == "pb_pattern":
        context.user_data.pop("adm_state", None)
        name = context.user_data.pop("pb_name", "").strip()
        site = context.user_data.pop("pb_site", "").strip()
        ctype = context.user_data.get("pb_type", "")
        pattern = (text or "").strip()
        context.user_data.pop("pb_type", None)
        if not name or not site or not ctype or not pattern:
            await update.message.reply_text("❌ اطلاعات ناقصه. دوباره از «🛠 پلاگین‌ساز» شروع کن.")
            return
        status = await update.message.reply_text("🔧 در حال ساخت پلاگین...")
        result = pb.build_plugin(name, site, ctype, pattern)
        if not result.get("ok"):
            await status.edit_text(f"❌ {result.get('message')}")
            return
        await status.edit_text(
            f"✅ **{result['message']}**\n\n"
            f"📋 خلاصه:\n• نام: `{name}`\n• دامنه: `{site}`\n• نوع: `{ctype}`\n"
            f"• الگو: `{pattern[:80]}`\n\n"
            f"حالا یه لینک از `{site}` بفرست تا تستش کنی! 🚀")
    elif state == "userlinks":
        context.user_data.pop("adm_state", None)
        try:
            uid = int(text.strip())
        except ValueError:
            await update.message.reply_text(i18n.t(lang, "invalid_id"))
            return
        links = db.get_user_links(uid)
        if not links:
            await update.message.reply_text("📭 این کاربر لینکی نفرستاده.")
            return
        # Build a text file with all links + titles
        user_info = db.get_user_info(uid) or {}
        name = user_info.get("name") or str(uid)
        lines = [f"🔗 لینک‌های {name} (ID: {uid})",
                 f"تعداد: {len(links)}\n" + "=" * 30 + "\n"]
        for lid, url, site, title, created in links:
            lines.append(f"• [{site}] {title or 'بدون عنوان'}")
            lines.append(f"  {url}")
            lines.append(f"  🕒 {created}\n")
        fpath = f"/tmp/links_{uid}_{int(time.time())}.txt"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        with open(fpath, "rb") as f:
            await update.message.reply_document(document=f, filename=f"links_{uid}.txt",
                                                caption=f"📄 لینک‌های {name}")
        os.remove(fpath)


# ── Plugin chapter picker (paginated glass buttons) ──────────
PER_PAGE = 10


async def _show_chapters_page(update: Update, context, plugin_meta, chapters, page=0,
                              status_msg=None) -> None:
    """Render a page of chapter buttons (10 per page) with prev/next navigation."""
    lang = lang_of(update)
    total_pages = max(1, (len(chapters) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PER_PAGE
    chunk = chapters[start:start + PER_PAGE]

    lines = [i18n.t(lang, "ch_list").format(name=plugin_meta['name'], total=len(chapters),
                                            page=page + 1, total_pages=total_pages)]
    kb = []
    for i, ch in enumerate(chunk):
        idx = start + i
        lines.append(f"{idx + 1}. {ch['title'][:60]}")
        kb.append([InlineKeyboardButton(i18n.t(lang, "ch_pick").format(num=ch['num']),
                                        callback_data=f"pldl|{plugin_meta['name']}|{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(i18n.t(lang, "ch_prev"), callback_data=f"plpg|{plugin_meta['name']}|{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="plnoop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(i18n.t(lang, "ch_next"), callback_data=f"plpg|{plugin_meta['name']}|{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton(i18n.t(lang, "ch_close"), callback_data="plclose")])

    text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(text, parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(kb))
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, parse_mode="Markdown",
                                              reply_markup=InlineKeyboardMarkup(kb))


async def chapter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle chapter picker callbacks: pldl (download chapter), plpg (page), plnoop, plclose."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    data = q.data or ""
    parts = data.split("|")
    action = parts[0]

    if action == "plclose":
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if action == "plnoop":
        return

    if len(parts) < 3:
        return
    pname = parts[1]
    chapters = context.user_data.get(f"plchaps_{pname}") or []
    if not chapters:
        try:
            await q.message.edit_text(i18n.t(lang_of(update), "ch_expired"))
        except Exception:
            pass
        return

    if action == "plpg":
        page = int(parts[2])
        await _show_chapters_page(update, context, {"name": pname}, chapters, page=page,
                                  status_msg=q.message)
        return

    if action == "pldl":
        idx = int(parts[2])
        if idx < 0 or idx >= len(chapters):
            return
        ch = chapters[idx]
        # Return to the page this chapter lives on (so the user keeps their place)
        resume_page = idx // PER_PAGE
        meta = pm.get_plugin(pname)
        if not meta:
            await q.message.edit_text("❌ پلاگین پیدا نشد.")
            return
        lang = lang_of(update)
        # tell the user download started, keep the picker visible
        try:
            await q.message.edit_text(
                i18n.t(lang, "ch_downloading").format(num=ch['num']),
                parse_mode="Markdown")
        except Exception:
            pass
        try:
            await context.bot.send_chat_action(chat_id=q.message.chat_id, action="upload_document")
        except Exception:
            pass
        result = await asyncio.to_thread(
            pm.call_plugin_download_chapter, meta, ch["url"],
            {"workdir": DOWNLOAD_DIR, "lang": lang})
        if result.get("error"):
            try:
                await q.message.edit_text(
                    i18n.t(lang, "ch_error").format(num=ch['num'], err=result['error'][:150]),
                    reply_markup=None)
            except Exception:
                pass
            await _show_chapters_page(update, context, meta, chapters, page=resume_page)
            return
        # send the PDF, then RE-OPEN the picker (menu stays open for next chapter)
        try:
            path = result.get("path")
            if path and os.path.exists(path):
                await context.bot.send_document(
                    chat_id=q.message.chat_id, document=open(path, "rb"),
                    caption=f"📄 {result.get('title', '')}",
                    filename=os.path.basename(path))
                db.record_download(q.from_user.id, ch["url"], f"plugin:{pname}", "pdf", True)
                dl.cleanup(path)
        except Exception as e:
            log.warning("chapter pdf send: %s", e)
        await _show_chapters_page(update, context, meta, chapters, page=resume_page,
                                  status_msg=q.message)


# ── Plugin result sender ──────────────────────────────────────
async def _send_plugin_result(update: Update, context, result: dict, lang: str,
                              status_msg=None) -> None:
    """Send a plugin's result: PDF / video / photos / text."""
    chat_id = update.effective_chat.id
    typ = result.get("type")
    caption = result.get("text") or result.get("title") or ""
    try:
        if status_msg:
            await status_msg.edit_text(i18n.t(lang, "sending"))
        else:
            status_msg = await update.message.reply_text(i18n.t(lang, "sending"))
    except Exception:
        pass
    try:
        if typ == "pdf" or typ == "document":
            path = result.get("path")
            if path and os.path.exists(path):
                await context.bot.send_document(chat_id=chat_id, document=open(path, "rb"),
                                                caption=caption, filename=os.path.basename(path))
                db.record_download(update.effective_user.id, result.get("source_url", ""),
                                   "plugin", typ, True)
                dl.cleanup(path)
            else:
                await status_msg.edit_text(i18n.t(lang, "download_failed"))
        elif typ == "video":
            path = result.get("path")
            if path and os.path.exists(path):
                await context.bot.send_video(chat_id=chat_id, video=open(path, "rb"),
                                             caption=caption, supports_streaming=True)
                dl.cleanup(path)
            else:
                await status_msg.edit_text(i18n.t(lang, "download_failed"))
        elif typ == "photos":
            photos = result.get("photos") or []
            if photos:
                from telegram import InputMediaPhoto
                media = [InputMediaPhoto(open(p, "rb")) for p in photos]
                await context.bot.send_media_group(chat_id=chat_id, media=media)
                for p in photos:
                    dl.cleanup(p)
            else:
                await status_msg.edit_text(i18n.t(lang, "download_failed"))
        elif typ == "audio":
            path = result.get("path")
            if path and os.path.exists(path):
                await context.bot.send_audio(chat_id=chat_id, audio=open(path, "rb"),
                                             caption=caption)
                dl.cleanup(path)
            else:
                await status_msg.edit_text(i18n.t(lang, "download_failed"))
        elif typ == "text":
            await update.message.reply_text(result.get("text", ""), disable_web_page_preview=True)
        else:
            await status_msg.edit_text(i18n.t(lang, "download_failed"))
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.warning("plugin result send: %s", e)
        try:
            await status_msg.edit_text(i18n.t(lang, "processing_error").format(err=str(e)[:150]))
        except Exception:
            pass


# ── Shazam-like song recognition ─────────────────────────────
async def _send_shazam_result(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              result: dict, lang: str) -> None:
    """Send the full recognition card: cover photo + metadata + links + music file.
    Works from both a message and a callback_query (button click)."""
    chat_id = update.effective_chat.id
    txt = i18n.t(lang, "shazam_match").format(
        title=result.get("title", "?"),
        artist=result.get("artist", "?"),
        album=result.get("album") or "-",
        year=result.get("year") or "-",
        genre=result.get("genre") or "-",
        ytm_url=result.get("ytm_url") or "https://music.youtube.com",
        spotify_url=result.get("spotify_url") or "https://open.spotify.com",
        shazam_url=result.get("shazam_url") or "https://www.shazam.com",
    )
    cover = result.get("cover")
    # Send the card (photo + caption) first — use bot.send_* so it works from callbacks
    if cover:
        try:
            sent = await context.bot.send_photo(chat_id=chat_id, photo=cover, caption=txt,
                                                parse_mode="Markdown")
        except Exception:
            sent = await context.bot.send_message(chat_id=chat_id, text=txt,
                                                  parse_mode="Markdown", disable_web_page_preview=True)
    else:
        sent = await context.bot.send_message(chat_id=chat_id, text=txt,
                                              parse_mode="Markdown", disable_web_page_preview=True)
    # Then download the actual song and send the music file (fast m4a, no re-encode)
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")
    except Exception:
        pass
    song = os.path.join(DOWNLOAD_DIR, f"song_{int(time.time())}_{result.get('key','x')}.m4a")
    ok = await asyncio.to_thread(
        dl.download_song, result.get("title", ""), result.get("artist", ""), song)
    if ok and os.path.getsize(song) < MAX_FILE_SIZE_MB * 1024 * 1024:
        try:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=open(song, "rb"),
                title=result.get("title", ""),
                performer=result.get("artist", ""),
                caption=f"🎵 {result.get('title','')} — {result.get('artist','')}",
            )
        except Exception as e:
            log.warning("shazam send audio: %s", e)
    dl.cleanup(song)
    return sent


async def shazam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Guard: commands can fire on edited updates where update.message is None
    if not update.message:
        return
    db.record_user(update)
    await update.message.reply_text(i18n.t(lang_of(update), "shazam_cmd"))


async def handle_media_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recognize the song from an audio/video file the user sends."""
    # Guard: edited/channel updates can reach this handler without a message
    if not update.message or not update.effective_chat:
        return
    uid = update.effective_user.id
    lang = lang_of(update)
    if db.is_banned(uid):
        await update.message.reply_text(i18n.t(lang, "banned"))
        return
    # GIFs have no music — in groups people send them for fun; ignore animations entirely.
    if update.message.animation:
        return
    # In GROUPS, voice messages are usually casual chat — ignore them entirely
    # (only recognize voice in private chat where it's a deliberate music request).
    if update.message.voice and update.effective_chat.type != "private":
        return
    # In GROUPS, video files/notes are usually casual shares — don't try to recognize
    # music from them (private chat still recognizes video).
    if update.effective_chat.type != "private" and (update.message.video or update.message.video_note):
        return
    # Group media_mode: 0 = nothing, 1 = links only — files are ignored entirely.
    if update.effective_chat.type != "private":
        try:
            gs = db.get_chat_settings(update.effective_chat.id)
            if gs.get("media_mode", 2) < 2:
                return
        except Exception:
            pass
    msg = update.message
    file_obj = (msg.audio or msg.video or msg.voice or msg.document
                or (msg.video_note))
    # Documents might be arbitrary; only treat audio/video docs
    if msg.document and not any(x in (msg.document.mime_type or "").lower()
                                 for x in ("audio", "video")):
        return
    if not file_obj:
        return
    status = await update.message.reply_text(i18n.t(lang, "shazam_searching"))
    local_path = os.path.join(DOWNLOAD_DIR, f"shz_{uid}_{int(time.time())}")
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(local_path)
    except Exception as e:
        log.error("shazam download file: %s", e)
        await status.edit_text(i18n.t(lang, "shazam_error").format(err=str(e)[:100]))
        return
    # Convert to mono wav for fingerprinting
    wav = local_path + ".wav"
    if not await asyncio.to_thread(recog.to_mono_wav, local_path, wav):
        await status.edit_text(i18n.t(lang, "shazam_error").format(err="ffmpeg convert failed"))
        dl.cleanup(local_path)
        return
    result = await recog.recognize_audio(wav)
    dl.cleanup(local_path)
    dl.cleanup(wav)
    try:
        await status.delete()
    except Exception:
        pass
    if result.get("matched"):
        await _send_shazam_result(update, context, result, lang)
    else:
        try:
            await update.message.reply_text(i18n.t(lang, "shazam_no_match"))
        except Exception:
            pass


# ── Group settings handlers ───────────────────────────────────
async def _is_group_admin(update: Update, user_id: int) -> bool:
    """Check if user is a group admin (or bot owner)."""
    if is_admin(user_id):
        return True
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return False
    try:
        member = await chat.get_member(user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def group_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = i18n.t(lang_of(update), "group_help")
    # Add a clear privacy-mode note
    text += i18n.t(lang_of(update), "group_privacy_note")
    await update.message.reply_text(text)


async def group_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    s = db.get_chat_settings(chat.id)
    await update.message.reply_text(i18n.t(lang, "group_settings").format(
        quality=s["max_quality"] if s["max_quality"] else "∞",
        auto=i18n.t(lang, "group_on") if s["auto_mode"] else i18n.t(lang, "group_off"),
        silent=i18n.t(lang, "group_on") if s["silent"] else i18n.t(lang, "group_off"),
        media=MEDIA_MODES.get(s.get("media_mode", 2), MEDIA_MODES[2]),
        filter_=s.get("link_filter", "all") or "all") +
        "\n\n🎛 برای انتخاب رسانه‌ها: /setmedia\n🔍 فیلتر لینک: /setfilter")


async def setquality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    args = context.args
    if not args:
        await update.message.reply_text(i18n.t(lang, "setquality_usage"))
        return
    try:
        q = int(args[0])
    except ValueError:
        await update.message.reply_text(i18n.t(lang, "setquality_usage"))
        return
    if q not in (0, 144, 240, 360, 480, 720, 1080, 1440, 2160):
        await update.message.reply_text(i18n.t(lang, "setquality_usage"))
        return
    db.set_chat_settings(chat.id, max_quality=q, set_by=update.effective_user.id)
    await update.message.reply_text(i18n.t(lang, "setquality_done").format(q=q if q else "∞"))


async def autodl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text(i18n.t(lang, "setquality_usage").replace("/setquality", "/autodl on|off"))
        return
    on = args[0].lower() == "on"
    db.set_chat_settings(chat.id, auto_mode=1 if on else 0, set_by=update.effective_user.id)
    await update.message.reply_text(i18n.t(lang, "autodl_done").format(
        state=i18n.t(lang, "group_on") if on else i18n.t(lang, "group_off")))


async def silent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text(i18n.t(lang, "setquality_usage").replace("/setquality", "/silent on|off"))
        return
    on = args[0].lower() == "on"
    db.set_chat_settings(chat.id, silent=1 if on else 0, set_by=update.effective_user.id)
    await update.message.reply_text(i18n.t(lang, "silent_done").format(
        state=i18n.t(lang, "group_on") if on else i18n.t(lang, "group_off")))


# ── Group media-mode settings ─────────────────────────────────
MEDIA_MODES = {
    0: "🚫 هیچ",
    1: "🔗 فقط لینک",
    2: "🎬 لینک + فایل‌ها",
}

async def setmedia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group admin picks which media the bot handles in this group:
    0 = nothing, 1 = links only, 2 = links + files (default)."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    s = db.get_chat_settings(chat.id)
    current = s.get("media_mode", 2)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅ ' if current == 1 else ''}🔗 فقط لینک",
                              callback_data=f"setmedia|1")],
        [InlineKeyboardButton(f"{'✅ ' if current == 2 else ''}🎬 لینک + فایل",
                              callback_data=f"setmedia|2")],
        [InlineKeyboardButton(f"{'✅ ' if current == 0 else ''}🚫 هیچ",
                              callback_data=f"setmedia|0")],
        [InlineKeyboardButton(i18n.t(lang, "close"), callback_data="setmedia|close")],
    ])
    await update.message.reply_text(
        "🎛 **رسانه‌هایی که ربات در این گروه هندل می‌کنه:**\n\n"
        "🔗 فقط لینک — فقط لینک‌ها دانلود می‌شن (عکس/ویدیو/ویس بی‌صدا رد می‌شن)\n"
        "🎬 لینک + فایل — لینک‌ها دانلود می‌شن و فایل‌های صوتی تشخیص آهنگ می‌شن\n"
        "🚫 هیچ — ربات فقط به کامندها جواب می‌ده\n\n"
        "وضعیت فعلی: **" + MEDIA_MODES.get(current, "🎬 لینک + فایل‌ها") + "**",
        parse_mode="Markdown", reply_markup=kb)


async def setmedia_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if not q.data or not q.data.startswith("setmedia|"):
        return
    val = q.data.split("|", 1)[1]
    if val == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    chat = q.message.chat
    if chat.type == "private":
        return
    try:
        mode = int(val)
    except ValueError:
        return
    db.set_chat_settings(chat.id, media_mode=mode, set_by=q.from_user.id)
    try:
        await q.message.edit_text(
            f"✅ حالت رسانه گروه به **{MEDIA_MODES.get(mode, mode)}** تغییر کرد.",
            parse_mode="Markdown")
    except Exception:
        pass


# ── Link filter (group): all / core-sites / custom media types ──
CORE_SITES = ["youtube", "instagram", "twitter", "tiktok"]
FILTER_TYPES = {
    "video": "🎬 ویدیو",
    "photo": "🖼 عکس",
    "audio": "🎵 صدا",
    "text": "📝 متن",
}


def _filter_to_media(f: str) -> set:
    """Parse link_filter value -> set of allowed media types.
    'all' -> all types; 'core' -> handled by site check, not media; 'custom:...' -> listed types."""
    if not f or f == "all":
        return set(FILTER_TYPES.keys())
    if f.startswith("custom:"):
        parts = f.split(":", 1)[1].split(",")
        return {p for p in parts if p in FILTER_TYPES}
    return set(FILTER_TYPES.keys())


def _media_type_of_site(site: str) -> set:
    """Approximate media types a known site can produce."""
    m = {
        "youtube": {"video", "audio"},
        "instagram": {"video", "photo"},
        "twitter": {"video", "photo", "text"},
        "tiktok": {"video"},
        "pornhub": {"video"},
        "other": {"video", "photo", "audio", "text"},
    }
    return m.get(site, {"video", "photo", "audio", "text"})


async def setfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group admin picks a link filter: all links / core sites / custom media types."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    s = db.get_chat_settings(chat.id)
    cur = s.get("link_filter", "all")
    kb = [
        [InlineKeyboardButton(f"{'✅ ' if cur == 'all' else ''}🌐 همه لینک‌ها",
                              callback_data="setfilter|all")],
        [InlineKeyboardButton(f"{'✅ ' if cur == 'core' else ''}⭐ فقط سایت‌های کامل",
                              callback_data="setfilter|core")],
        [InlineKeyboardButton("🎛 سفارشی (نوع محتوا)", callback_data="setfilter|custom_menu")],
        [InlineKeyboardButton(i18n.t(lang, "close"), callback_data="setfilter|close")],
    ]
    desc = ("🌐 همه لینک‌ها — هر لینکی (یوتیوب، اینستاگرام، سایت‌های ناشناخته، همه)\n"
            "⭐ فقط سایت‌های کامل — فقط یوتیوب/اینستاگرام/توییتر/تیک‌تاک (پشتیبانی ۱۰۰٪)\n"
            "🎛 سفارشی — انتخاب نوع: ویدیو/عکس/صدا/متن")
    await update.message.reply_text(
        f"🔍 **فیلتر لینک‌های گروه**\n\n{desc}\n\nوضعیت فعلی: **{cur}**",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def setfilter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if not q.data or not q.data.startswith("setfilter|"):
        return
    val = q.data.split("|", 1)[1]
    chat = q.message.chat
    if chat.type == "private":
        return
    if val == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if val == "custom_menu":
        s = db.get_chat_settings(chat.id)
        cur = s.get("link_filter", "all")
        allowed = _filter_to_media(cur)
        kb = []
        for t, label in FILTER_TYPES.items():
            mark = "✅" if t in allowed else "⚪️"
            kb.append([InlineKeyboardButton(f"{mark} {label}",
                                            callback_data=f"setfilter|toggle:{t}")])
        kb.append([InlineKeyboardButton(i18n.t(lang_of(update), "back"), callback_data="setfilter|menu"),
                   InlineKeyboardButton(i18n.t(lang_of(update), "close"), callback_data="setfilter|close")])
        try:
            await q.message.edit_text(
                f"🎛 **فیلتر سفارشی — نوع محتواها**\n\n"
                f"انتخاب کن کدوم نوع محتوا گرفته بشه:\n"
                f"{', '.join(f'{l}' for l in FILTER_TYPES.values())}",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return
    if val == "menu":
        # re-show main filter menu
        s = db.get_chat_settings(chat.id)
        cur = s.get("link_filter", "all")
        kb = [
            [InlineKeyboardButton(f"{'✅ ' if cur == 'all' else ''}🌐 همه لینک‌ها",
                                  callback_data="setfilter|all")],
            [InlineKeyboardButton(f"{'✅ ' if cur == 'core' else ''}⭐ فقط سایت‌های کامل",
                                  callback_data="setfilter|core")],
            [InlineKeyboardButton("🎛 سفارشی (نوع محتوا)", callback_data="setfilter|custom_menu")],
            [InlineKeyboardButton(i18n.t(lang_of(update), "close"), callback_data="setfilter|close")],
        ]
        try:
            await q.message.edit_text(
                f"🔍 **فیلتر لینک‌های گروه**\n\nوضعیت فعلی: **{cur}**",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return
    if val.startswith("toggle:"):
        t = val.split(":", 1)[1]
        s = db.get_chat_settings(chat.id)
        cur = s.get("link_filter", "all")
        allowed = _filter_to_media(cur)
        if t in allowed:
            allowed.discard(t)
        else:
            allowed.add(t)
        if not allowed:
            allowed = set(FILTER_TYPES.keys())  # never allow zero types
        db.set_chat_settings(chat.id, link_filter="custom:" + ",".join(sorted(allowed)),
                             set_by=q.from_user.id)
        # re-render custom menu
        kb = []
        for typ, label in FILTER_TYPES.items():
            mark = "✅" if typ in allowed else "⚪️"
            kb.append([InlineKeyboardButton(f"{mark} {label}",
                                            callback_data=f"setfilter|toggle:{typ}")])
        kb.append([InlineKeyboardButton(i18n.t(lang_of(update), "back"), callback_data="setfilter|menu"),
                   InlineKeyboardButton(i18n.t(lang_of(update), "close"), callback_data="setfilter|close")])
        try:
            await q.message.edit_text(
                f"🎛 **فیلتر سفارشی — نوع محتواها**\n\n"
                f"انتخاب کن کدوم نوع محتوا گرفته بشه:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return
    # all / core
    db.set_chat_settings(chat.id, link_filter=val, set_by=q.from_user.id)
    try:
        await q.message.edit_text(
            f"✅ فیلتر لینک گروه به **{'همه لینک‌ها' if val == 'all' else 'فقط سایت‌های کامل'}** تغییر کرد.",
            parse_mode="Markdown")
    except Exception:
        pass


# ── Per-user settings ─────────────────────────────────────────
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Personal settings — private only
    if not _private_only(update):
        return
    db.record_user(update)
    uid = update.effective_user.id
    lang = lang_of(update)
    s = db.get_user_settings(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("360p", callback_data=f"setdefq|360"),
         InlineKeyboardButton("480p", callback_data=f"setdefq|480"),
         InlineKeyboardButton("720p", callback_data=f"setdefq|720")],
        [InlineKeyboardButton("1080p", callback_data=f"setdefq|1080"),
         InlineKeyboardButton("1440p", callback_data=f"setdefq|1440"),
         InlineKeyboardButton("2160p", callback_data=f"setdefq|2160")],
    ])
    fmt = "🎵 MP3" if s["default_audio"] else "🎬 MP4"
    await update.message.reply_text(
        i18n.t(lang, "settings_title").format(quality=s["default_quality"], format=fmt) +
        "\n\n" + i18n.t(lang, "settings_quality_prompt"),
        reply_markup=kb)


async def set_def_q_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split("|")
    if len(parts) == 2 and parts[0] == "setdefq":
        try:
            quality = int(parts[1])
        except ValueError:
            return
        db.set_user_settings(q.from_user.id, default_quality=quality)
        lang = lang_of(update)
        try:
            await q.answer()
        except Exception:
            pass
        try:
            await q.message.edit_text(i18n.t(lang, "settings_done").format(q=quality))
        except Exception as e:
            log.warning("set_def_q UI: %s", e)


async def commandonly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    if not await _is_group_admin(update, update.effective_user.id):
        await update.message.reply_text(i18n.t(lang, "group_admin_only"))
        return
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text(i18n.t(lang, "cmdonly_usage"))
        return
    on = args[0].lower() == "on"
    db.set_chat_settings(chat.id, command_only=1 if on else 0, set_by=update.effective_user.id)
    await update.message.reply_text(i18n.t(lang, "cmdonly_done").format(
        state=i18n.t(lang, "group_on") if on else i18n.t(lang, "group_off")))


async def ytdl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicit /ytdl <url> — U2DLBot pattern for groups."""
    # Guard: edited/channel updates reach handlers without update.message
    if not update.message:
        return
    args = context.args
    if not args:
        await update.message.reply_text(i18n.t(lang_of(update), "ytdl_usage"))
        return
    url = args[0]
    if not url.startswith("http"):
        await update.message.reply_text(i18n.t(lang_of(update), "invalid_url"), parse_mode="Markdown")
        return
    # Reuse the normal link handler by faking a plain-text message
    msg = update.message
    msg.text = url
    await handle_link(update, context)


# ── Force-join channels ───────────────────────────────────────
def _clean_username(raw: str) -> str:
    """Extract a clean channel username from ANY user input format.
    Handles: '/addchannel @Name', 'Name', '@Name', 'https://t.me/Name', '/addchannel Name'."""
    s = (raw or "").strip()
    # Strip leading command words (admin may type the old command)
    for cmd in ("/addchannel", "/removechannel", "/delchannel", "/channels"):
        if s.lower().startswith(cmd):
            s = s[len(cmd):].strip()
            break
    # Strip @ prefix and t.me/ URLs
    s = s.lstrip("@").strip()
    if "t.me/" in s:
        s = s.split("t.me/")[-1].strip()
    # Take only the first token (in case of trailing junk)
    s = s.split()[0] if s.split() else s
    s = s.rstrip("/")
    return s.lower()


async def _check_channel_membership(context, user_id: int) -> tuple:
    """Check user membership across all force channels.
    Returns (ok: bool, missing: list of {id, username, title, chat_id, uncheckable}).

    - Channels where the bot IS an admin: ALWAYS checked via getChatMember, even if
      previously confirmed. If the user leaves the channel days later, they get blocked
      again (no permanent free pass).
    - Channels where the bot CANNOT check (not admin / member list inaccessible):
      flagged 'uncheckable' — a manual "I joined" confirmation is valid for 24h,
      then it expires and the user must confirm again.
    """
    channels = db.get_force_channels()
    if not channels:
        return True, []
    missing = []
    for ch in channels:
        chat_id = ch.get("chat_id")
        if not chat_id:
            # We never resolved this channel (get_chat failed at add time) — uncheckable
            if db.is_channel_joined_ttl(user_id, ch["id"], ttl_hours=24):
                continue
            ch["uncheckable"] = True
            missing.append(ch)
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("member", "administrator", "creator"):
                db.mark_channel_joined(user_id, ch["id"])
                continue
            missing.append(ch)
        except Exception as e:
            msg = str(e).lower()
            # Bot not admin / member list inaccessible / chat not found → cannot verify;
            # fall back to manual confirmation (with expiry)
            if ("inaccessible" in msg or "not a member" in msg or "chat not found" in msg
                    or "bot is not a member" in msg or "not found" in msg):
                if db.is_channel_joined_ttl(user_id, ch["id"], ttl_hours=24):
                    continue
                ch["uncheckable"] = True
            log.warning("membership check %s: %s", ch['username'], e)
            missing.append(ch)
    return len(missing) == 0, missing


def _force_join_markup(lang: str, channels: list = None):
    """Keyboard with join links + 'I joined' button."""
    buttons = []
    if channels:
        for ch in channels:
            buttons.append([InlineKeyboardButton(
                f"🔗 {ch.get('title') or ch['username']}",
                url=f"https://t.me/{ch['username']}")])
    buttons.append([InlineKeyboardButton(i18n.t(lang, "force_join_check"), callback_data="fjcheck")])
    return InlineKeyboardMarkup(buttons)


async def _ensure_membership(update: Update, context) -> bool:
    """If force channels exist and user hasn't joined, prompt them. Returns True if OK to proceed."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        return True
    channels = db.get_force_channels()
    if not channels:
        return True
    # Bot owner is always allowed
    if is_admin(user.id):
        return True
    ok, missing = await _check_channel_membership(context, user.id)
    if ok:
        return True
    lang = lang_of(update)
    lines = []
    has_uncheckable = False
    for ch in missing:
        title = ch.get("title") or ch["username"]
        lines.append(i18n.t(lang, "force_join_channels").format(title=title, username=ch["username"]))
        if ch.get("uncheckable"):
            has_uncheckable = True
    text = i18n.t(lang, "force_join_required").format(channels="\n".join(lines))
    if has_uncheckable:
        text += i18n.t(lang, "force_join_uncheckable_note")
    try:
        await update.message.reply_text(
            text, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=_force_join_markup(lang, missing))
    except Exception as e:
        log.warning("force join prompt: %s", e)
    return False


async def force_join_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    user = q.from_user
    lang = lang_of(update)
    ok, missing = await _check_channel_membership(context, user.id)
    if ok:
        try:
            await q.message.edit_text(i18n.t(lang, "force_join_ok"))
        except Exception as e:
            log.warning("fj ok UI: %s", e)
        return
    # For channels the bot CANNOT check (uncheckable: not admin / no access),
    # pressing the button is a manual confirmation — mark them joined.
    # For channels the bot CAN check but user isn't in, keep them missing.
    still_missing = []
    manual_confirmed = False
    for ch in missing:
        if ch.get("uncheckable"):
            db.mark_channel_joined(user.id, ch["id"])
            manual_confirmed = True
        else:
            still_missing.append(ch)
    if manual_confirmed and not still_missing:
        try:
            await q.message.edit_text(i18n.t(lang, "force_join_ok"))
        except Exception as e:
            log.warning("fj ok manual UI: %s", e)
        return
    if not still_missing:
        # All were manual — treat as OK
        return
    lines = []
    for ch in still_missing:
        title = ch.get("title") or ch["username"]
        lines.append(i18n.t(lang, "force_join_channels").format(title=title, username=ch["username"]))
    try:
        await q.message.edit_text(
            i18n.t(lang, "force_join_still_missing").format(missing="\n".join(lines)),
            parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=_force_join_markup(lang, still_missing))
    except Exception as e:
        log.warning("fj still missing UI: %s", e)


async def addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        return
    lang = lang_of(update)
    args = context.args
    if not args:
        await update.message.reply_text(i18n.t(lang, "force_join_admin_usage"))
        return
    username = _clean_username(args[0])
    if not username:
        await update.message.reply_text(i18n.t(lang, "force_join_admin_usage"))
        return
    # Fetch channel title
    title = ""
    try:
        ch = await context.bot.get_chat(chat_id=f"@{username}")
        title = ch.title or username
    except Exception:
        title = username
    ok, res = db.add_force_channel(username, title, update.effective_user.id)
    if ok:
        await update.message.reply_text(i18n.t(lang, "force_join_added").format(ch=username))
    else:
        await update.message.reply_text(i18n.t(lang, "force_join_exists").format(ch=username))


async def removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        return
    lang = lang_of(update)
    args = context.args
    if not args:
        await update.message.reply_text(i18n.t(lang, "force_join_admin_usage"))
        return
    username = _clean_username(args[0])
    if db.remove_force_channel(username):
        await update.message.reply_text(i18n.t(lang, "force_join_removed").format(ch=username))
    else:
        await update.message.reply_text(i18n.t(lang, "force_join_not_found").format(ch=username))


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        return
    lang = lang_of(update)
    channels = db.get_force_channels()
    if not channels:
        await update.message.reply_text(i18n.t(lang, "force_join_no_channels"))
        return
    lines = [i18n.t(lang, "force_join_list")]
    for ch in channels:
        lines.append(i18n.t(lang, "force_join_list_item").format(
            username=ch["username"], title=ch.get("title") or ""))
    await update.message.reply_text("\n".join(lines))


# ── Main link handler ─────────────────────────────────────────
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Guard: this handler is registered on filters.TEXT which also fires for
    # edited messages / channel posts / service messages (update.message is None
    # there) — ignore them silently instead of crashing with AttributeError.
    if not update.message or not update.message.text:
        return
    db.record_user(update)
    uid = update.effective_user.id
    lang = lang_of(update)
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")

    # Admin in-progress state (broadcast/ban/unban) takes priority over link handling
    if is_admin(uid) and context.user_data.get("adm_state"):
        await admin_input(update, context)
        return

    # Force-join gate (private chats only)
    if not await _ensure_membership(update, context):
        return

    if db.is_banned(uid):
        await update.message.reply_text(i18n.t(lang, "banned"))
        return

    # Extract the first URL from the message (handles "@bot" mentions, extra text, etc.)
    raw_text = update.message.text.strip()
    m = re.search(r'https?://[^\s]+', raw_text)
    if not m:
        # In groups, normal chat messages must be ignored silently (no "invalid link" spam).
        # In private chat: if there's no link, treat it as a song search query!
        if is_group:
            return
        query = raw_text.strip()
        if len(query) < 3:
            await update.message.reply_text(i18n.t(lang, "invalid_url"), parse_mode="Markdown")
            return
        await start_song_search(update, context, query)
        return
    url = m.group(0).rstrip('.,;!?،؛')
    if not url.startswith("http"):
        if is_group:
            return
        await update.message.reply_text(i18n.t(lang, "invalid_url"), parse_mode="Markdown")
        return

    # Rate limit only applies to actual link messages (not normal chat)
    if rate_limited(uid):
        await update.message.reply_text(i18n.t(lang, "rate_limit"))
        return

    site = dl.detect_site(url)

    # ── GROUP LINK FILTER: if the group has a filter, just skip disallowed links ──
    if is_group and site != "other":
        try:
            gs_f = db.get_chat_settings(chat.id)
            lf = gs_f.get("link_filter", "all") or "all"
            if lf == "core" and site not in CORE_SITES:
                return
            if lf.startswith("custom:"):
                allowed_types = _filter_to_media(lf)
                produced = _media_type_of_site(site)
                if not (allowed_types & produced):
                    return
        except Exception:
            pass

    # ── PLUGIN ROUTING: a plugin may handle sites the core doesn't support ──
    plugin_meta = pm.find_plugin_for_url(url)
    if plugin_meta and site in ("other", "twitter", "instagram", "youtube"):
        db.record_link(update, url, f"plugin:{plugin_meta['name']}")
        status_msg = await update.message.reply_text(i18n.t(lang, "checking"))

        # Multi-chapter plugin? Show a chapter picker with paginated glass buttons.
        try:
            chapters = await asyncio.to_thread(pm.call_plugin_chapters, plugin_meta, url)
        except Exception:
            chapters = []
        if chapters:
            context.user_data[f"plchaps_{plugin_meta['name']}"] = chapters
            await _show_chapters_page(update, context, plugin_meta, chapters, page=0, status_msg=status_msg)
            return

        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_document")
        except Exception:
            pass
        result = await asyncio.to_thread(
            pm.call_plugin, plugin_meta, url,
            {"workdir": DOWNLOAD_DIR, "lang": lang})
        if result.get("error"):
            await status_msg.edit_text(i18n.t(lang, "processing_error").format(err=result["error"][:200]),
                                       parse_mode="Markdown")
            return
        await _send_plugin_result(update, context, result, lang, status_msg)
        await asyncio.to_thread(enforce_disk_limit)
        return

    db.record_link(update, url, site)

    # ── AUTO-DETECT: unknown site — two-stage smart detection ──
    auto_info_built = False
    if site == "other":
        status_msg = await update.message.reply_text(i18n.t(lang, "auto_checking"))
        try:
            detection = await asyncio.to_thread(adetect.smart_detect, url)
        except Exception:
            detection = {"type": "error", "error": "analysis failed"}

        # STAGE-1 HIT: yt-dlp knows this site (vimeo/dailymotion/soundcloud/...) —
        # build a proper info dict and use the NORMAL download flow (quality menu,
        # clean caption) instead of the raw auto path.
        if detection.get("source") == "ytdlp" and detection.get("ytdlp"):
            # Respect group custom filter: if the group only allows e.g. photos and
            # this is a video, skip silently.
            if is_group:
                try:
                    gs_f = db.get_chat_settings(chat.id)
                    lf = gs_f.get("link_filter", "all") or "all"
                    if lf.startswith("custom:"):
                        allowed_types = _filter_to_media(lf)
                        if "video" not in allowed_types:
                            try:
                                await status_msg.delete()
                            except Exception:
                                pass
                            return
                except Exception:
                    pass
            yd = detection["ytdlp"]
            try:
                await status_msg.delete()
            except Exception:
                pass
            info = {
                "title": yd.get("title") or "ویدیو",
                "uploader": yd.get("uploader") or "",
                "duration": yd.get("duration"),
                "view_count": None,
                "like_count": None,
                "description": "",
                "webpage_url": yd.get("webpage_url") or url,
                "qualities": {str(h): None for h in yd.get("heights", [])},
                "sizes": yd.get("sizes", {}),
                "extractor": yd.get("extractor", ""),
            }
            # Record as auto-downloaded link, then continue to the normal flow below
            # (quality menu in private / auto-best in groups).
            db.record_download(update.effective_user.id, url, "other", "auto", True)
            status_msg = await update.message.reply_text(i18n.t(lang, "checking"))
            auto_info_built = True
        elif detection.get("type") in ("comic", "video", "photos", "text"):
            # Respect group custom filter for heuristic content types.
            if is_group:
                try:
                    gs_f = db.get_chat_settings(chat.id)
                    lf = gs_f.get("link_filter", "all") or "all"
                    if lf.startswith("custom:"):
                        allowed_types = _filter_to_media(lf)
                        m_type = {"comic": "photo", "video": "video", "photos": "photo", "text": "text"}.get(detection["type"], "text")
                        if m_type not in allowed_types:
                            try:
                                await status_msg.delete()
                            except Exception:
                                pass
                            return
                except Exception:
                    pass
            try:
                await status_msg.edit_text(i18n.t(lang, "auto_support").format(
                    detail=detection.get("detail", detection["type"])))
            except Exception:
                pass
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                                   action="upload_document")
            except Exception:
                pass
            result = await asyncio.to_thread(
                adetect.auto_download, url, detection["type"], DOWNLOAD_DIR,
                detection.get("samples"))
            if result.get("error"):
                # Fallback: image-heavy pages often have interesting text.
                # If media extraction failed, try sending page text instead of erroring.
                fb = await asyncio.to_thread(adetect.extract_text, url)
                if fb:
                    try:
                        await status_msg.edit_text(
                            f"🧠 **پشتیبانی خودکار** — عکس‌ها قابل استخراج نبودن، متن صفحه:\n\n{fb}",
                            parse_mode="Markdown", disable_web_page_preview=True)
                    except Exception:
                        pass
                    db.record_download(update.effective_user.id, url, "other", "auto-text", True)
                    return
                try:
                    await status_msg.edit_text(
                        i18n.t(lang, "auto_failed").format(err=result["error"][:200]),
                        parse_mode="Markdown")
                except Exception:
                    pass
                return
            await _send_plugin_result(update, context, result, lang, status_msg)
            await asyncio.to_thread(enforce_disk_limit)
            return
        if detection.get("type") == "error":
            try:
                await status_msg.edit_text(
                    i18n.t(lang, "auto_cant_detect").format(err=detection.get("error", "")[:120]),
                    disable_web_page_preview=True)
            except Exception:
                pass
            return
        # type == "none": let normal flow try (may give a clear error)
        try:
            await status_msg.delete()
        except Exception:
            pass
        status_msg = await update.message.reply_text(i18n.t(lang, "checking"))
    else:
        # Normal path for known sites (twitter/instagram/youtube/etc.): build the
        # status message here so all later code can use it.
        status_msg = await update.message.reply_text(i18n.t(lang, "checking"))

    if not auto_info_built:
        info = dl.get_video_info(url)
    if info.get("error"):
        err = info["error"][:200]
        # Instagram/Twitter often need login cookies — give a clear message
        if "cookie" in err.lower() or "login" in err.lower() or "empty media" in err.lower():
            hint = ("💡 این ویدیو نیاز به **ورود** داره.\n"
                    "اینستاگرام/توییتر ویدیوهای خصوصی رو بدون سشن اجازه نمی‌دن.\n"
                    "اگه پست **عمومی**ه، لینک رو دوباره کپی کن.")
            if site in ("instagram", "twitter"):
                await status_msg.edit_text(f"❌ نتونستم این لینک رو دانلود کنم.\n\n`{err}`\n\n{hint}", parse_mode="Markdown")
                return
        await status_msg.edit_text(i18n.t(lang, "processing_error").format(err=err),
                                   parse_mode="Markdown")
        return

    # Store title for admin link viewer
    title = info.get("title") or ""
    if title:
        db.update_link_title(url, title)

    # ── TWITTER SPECIAL HANDLING: photos / text / quote / thread ──
    tw_full = info.get("tw_full") or {}
    tw_type = info.get("tw_type")
    is_tw = info.get("is_tw")

    # Priority: quote > thread > photos > text
    # Quote tweet: show both tweets cleanly FIRST (most informative)
    if is_tw and tw_type == "quote" and tw_full.get("quoted"):
        q = tw_full["quoted"]
        text = dl._esc_md((tw_full.get("text") or "")[:400])
        qtext = dl._esc_md((q.get("text") or "")[:400])
        msg_text = i18n.t(lang, "tw_quote_only").format(
            name=dl._esc_md(tw_full.get("user_name") or ""), screen=dl._esc_md(tw_full.get("user_screen") or ""),
            text=text, qname=dl._esc_md(q.get("user_name") or ""), qscreen=dl._esc_md(q.get("user_screen") or ""),
            qtext=qtext)
        await status_msg.delete()
        await update.message.reply_text(msg_text, disable_web_page_preview=True)
        # Send the MAIN tweet photos, then the QUOTED tweet photos (if any)
        all_photo_urls = (tw_full.get("photos") or []) + (q.get("photos") or [])
        sent_q = False
        for photo_url in all_photo_urls[:8]:
            p_path = os.path.join(DOWNLOAD_DIR, f"twq_{uid}_{int(time.time())}.jpg")
            ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
            if ok_p:
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=open(p_path, "rb"))
                sent_q = True
                dl.cleanup(p_path)
        # If the quoted tweet has a video, download and send it too
        for v_url in (q.get("video_urls") or [])[:1]:
            v_path = os.path.join(DOWNLOAD_DIR, f"twqv_{uid}_{int(time.time())}.mp4")
            ok_v = await asyncio.to_thread(igtw.download_twitter, url, "best", v_path, v_url)
            if ok_v and os.path.exists(v_path) and os.path.getsize(v_path) > 0:
                await context.bot.send_video(chat_id=update.effective_chat.id, video=open(v_path, "rb"))
                dl.cleanup(v_path)
                sent_q = True
        db.record_download(uid, url, "twitter", "quote", sent_q)
        await asyncio.to_thread(enforce_disk_limit)
        return

    # Thread detection: if this tweet is a reply (in_reply_to set), try walking the thread
    if is_tw and tw_full.get("in_reply_to"):
        thread = igtw.get_twitter_thread(url)
        if thread.get("tweets") and len(thread["tweets"]) > 1:
            await status_msg.edit_text(i18n.t(lang, "tw_thread_header").format(count=len(thread["tweets"])))
            for i, t in enumerate(thread["tweets"], 1):
                text = dl._esc_md((t.get("text") or "")[:500])
                header = i18n.t(lang, "tw_thread_item").format(n=i, name=dl._esc_md(t.get("user_name") or ""), text=text)
                await update.message.reply_text(header, disable_web_page_preview=True)
                # Send photos for each tweet in the thread
                for photo_url in t.get("photos", [])[:4]:
                    p_path = os.path.join(DOWNLOAD_DIR, f"twth_{uid}_{i}_{int(time.time())}.jpg")
                    ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
                    if ok_p:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=open(p_path, "rb"))
                        dl.cleanup(p_path)
            db.record_download(uid, url, "twitter", "thread", True)
            try:
                await status_msg.delete()
            except Exception:
                pass
            await asyncio.to_thread(enforce_disk_limit)
            return

    if is_tw and tw_type in ("photos", "text", "quote", "thread"):
        # Photos tweet: send all photos with the text
        if tw_type == "photos" and tw_full.get("photos"):
            await status_msg.edit_text(i18n.t(lang, "tw_photos_sending").format(count=len(tw_full["photos"])))
            header = i18n.t(lang, "tw_tweet_header").format(
                name=dl._esc_md(tw_full.get("user_name") or ""), screen=dl._esc_md(tw_full.get("user_screen") or ""),
                text=dl._esc_md((tw_full.get("text") or "")[:400]),
                likes=tw_full.get("favorite_count") or 0, rts=tw_full.get("retweet_count") or 0)
            sent_first = False
            for photo_url in tw_full["photos"][:10]:
                p_path = os.path.join(DOWNLOAD_DIR, f"twp_{uid}_{int(time.time())}.jpg")
                ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
                if ok_p:
                    if not sent_first:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=open(p_path, "rb"), caption=header)
                        sent_first = True
                    else:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=open(p_path, "rb"))
                    dl.cleanup(p_path)
            if not sent_first:
                await status_msg.edit_text(i18n.t(lang, "tw_no_media").format(text=(tw_full.get("text") or "")[:400]))
            else:
                db.record_download(uid, url, "twitter", "photos", True)
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            await asyncio.to_thread(enforce_disk_limit)
            return

        # Plain text tweet — send FULL text (Telegram limit ~4000 chars)
        full_text = dl._esc_md((tw_full.get("text") or "").strip())
        msg_text = i18n.t(lang, "tw_no_media").format(text=full_text[:3800])
        try:
            await status_msg.edit_text(msg_text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("tw text UI: %s", e)
        db.record_download(uid, url, "twitter", "text", True)
        return

    # Playlist support
    if info.get("playlist"):
        await status_msg.edit_text(i18n.t(lang, "playlist_detected").format(count=info.get("count", 0)))
        out_dir = os.path.join(DOWNLOAD_DIR, f"pl_{uid}_{int(time.time())}")
        count = await asyncio.to_thread(dl.download_playlist, url, out_dir)
        if count == 0:
            await status_msg.edit_text(i18n.t(lang, "download_failed"))
            return
        sent_any = False
        for fname in sorted(os.listdir(out_dir)):
            if fname.endswith((".mp4", ".mkv")):
                fpath = os.path.join(out_dir, fname)
                if os.path.getsize(fpath) > MAX_FILE_SIZE_MB * 1024 * 1024:
                    continue
                await context.bot.send_video(chat_id=update.effective_chat.id, video=open(fpath, "rb"), supports_streaming=True)
                sent_any = True
        shutil.rmtree(out_dir, ignore_errors=True)
        if sent_any:
            await status_msg.delete()
        else:
            await status_msg.edit_text(i18n.t(lang, "download_failed"))
        await asyncio.to_thread(enforce_disk_limit)
        return

    qualities = info.get("qualities") or {}
    heights = sorted([int(q) for q in qualities.keys()], reverse=True)
    if not heights:
        await status_msg.edit_text(i18n.t(lang, "no_quality"))
        return

    dl_id = hashlib.md5(url.encode()).hexdigest()[:8]
    context.user_data[f"dl_{dl_id}"] = {"url": url, "info": info, "heights": heights, "lang": lang}
    context.user_data["dl_active"] = dl_id

    # ── GROUP MODE: auto-download best quality <= group setting, no buttons ──
    if is_group:
        gs = db.get_chat_settings(chat.id)
        # If command_only is on, plain links are ignored (only /ytdl works)
        if gs["command_only"]:
            try:
                await status_msg.delete()
            except Exception:
                pass
            return
        if gs["auto_mode"]:
            max_q = gs["max_quality"] or 2160
            # pick the highest available quality that is <= max_q
            candidates = [h for h in heights if h <= max_q]
            chosen = candidates[0] if candidates else max(heights)
            await status_msg.edit_text(i18n.t(lang, "dl_in_group").format(title=dl._esc_md(info.get("title", "")[:80])),
                                       parse_mode="Markdown")
            out_path = os.path.join(DOWNLOAD_DIR, f"grp_{chat.id}_{uuid.uuid4().hex[:8]}")
            dl.cleanup(out_path)
            ok = await _limited_download(url, str(chosen), False, out_path)
            out_path = resolve_out_path(out_path)
            if not ok:
                db.record_download(uid, url, site, str(chosen), False)
                if not gs["silent"]:
                    await status_msg.edit_text(i18n.t(lang, "download_failed"))
                return
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            if size_mb > MAX_FILE_SIZE_MB:
                db.record_download(uid, url, site, str(chosen), False)
                dl.cleanup(out_path)
                if not gs["silent"]:
                    await status_msg.edit_text(i18n.t(lang, "too_big").format(size=int(size_mb)))
                return
            info2 = dict(info)
            info2["quality"] = chosen
            info2["size_str"] = dl.human_size(os.path.getsize(out_path))
            caption = dl.make_caption(info2, lang) if not gs["silent"] else None
            sent = await context.bot.send_video(
                chat_id=chat.id,
                video=open(out_path, "rb"),
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
            )
            db.record_download(uid, url, site, str(chosen), True)
            dl.cleanup(out_path)
            try:
                await status_msg.delete()
            except Exception:
                pass
            await asyncio.to_thread(enforce_disk_limit)
            return

    # ── PRIVATE MODE: quality selection menu ──
    sizes = info.get("sizes", {})
    # Use user's default quality if set (closest lower if unavailable)
    us = db.get_user_settings(uid)
    default_q = us.get("default_quality", 720)
    if default_q and heights:
        avail = [h for h in heights if h <= default_q]
        best = avail[0] if avail else min(heights)
    else:
        best = min(720, max(heights))
    # Prefer a quality whose estimated size fits
    if sizes and best:
        for h in sorted(heights, reverse=True):
            if h <= best:
                est = sizes.get(str(h))
                if est and est < (MAX_FILE_SIZE_MB - 5) * 1024 * 1024:
                    best = h
                    break

    keyboard = []
    for h in heights:
        if h != best:
            est = sizes.get(str(h))
            label = f"🎥 {h}p" + (f" ({dl.human_size(est)})" if est else "")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"dl|{dl_id}|{h}")])
    keyboard.append([InlineKeyboardButton(i18n.t(lang, "mp3_only"), callback_data=f"dl|{dl_id}|audio")])
    keyboard.append([InlineKeyboardButton("🎵 تشخیص آهنگ", callback_data=f"dl|{dl_id}|shazam")])

    await status_msg.edit_text(i18n.t(lang, "downloading").format(title=info.get("title", "")[:80], quality=f"{best}p"),
                               parse_mode="Markdown")

    out_path = os.path.join(DOWNLOAD_DIR, f"dl_{uid}_{dl_id}_{best}.mp4")
    dl.cleanup(out_path)

    ok = await _limited_download(url, str(best), False, out_path)
    out_path = resolve_out_path(out_path)
    if not ok:
        db.record_download(uid, url, site, str(best), False)
        await status_msg.edit_text(i18n.t(lang, "download_failed"))
        return

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        db.record_download(uid, url, site, str(best), False)
        await status_msg.edit_text(i18n.t(lang, "too_big").format(size=int(size_mb)))
        dl.cleanup(out_path)
        return

    info2 = dict(info)
    info2["quality"] = best
    info2["size_str"] = dl.human_size(os.path.getsize(out_path))
    caption = dl.make_caption(info2, lang)

    sent = await context.bot.send_video(
        chat_id=update.effective_chat.id,
        video=open(out_path, "rb"),
        caption=caption,
        parse_mode="Markdown",
        supports_streaming=True,
        reply_markup=InlineKeyboardMarkup(keyboard + [
            [InlineKeyboardButton(i18n.t(lang, "more_qualities"), callback_data=f"more|{dl_id}")]
        ]),
    )
    db.record_download(uid, url, site, str(best), True)
    dl.cleanup(out_path)

    if sent:
        await status_msg.delete()
        context.user_data[f"more_{dl_id}"] = keyboard

    await asyncio.to_thread(enforce_disk_limit)


# ── Quality selection callbacks ───────────────────────────────
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data
    try:
        await q.answer()
    except Exception:
        pass
    uid = q.from_user.id
    lang = lang_of(update)

    if data == "cancel":
        await q.message.edit_text(i18n.t(lang, "cancelled"))
        return

    if data == "help":
        await q.message.edit_text(i18n.t(lang, "help"), parse_mode="Markdown")
        return

    if data == "history":
        rows = db.get_history(uid)
        if not rows:
            await q.message.edit_text(i18n.t(lang, "history_empty"))
            return
        lines = [i18n.t(lang, "history_title")]
        for site, url, created in rows:
            short = url[:50] + ("..." if len(url) > 50 else "")
            lines.append(i18n.t(lang, "history_item").format(site=site, url=short, time=created[:16]))
        await q.message.edit_text("\n".join(lines), disable_web_page_preview=True)
        return

    if data == "lang":
        await q.message.edit_text(i18n.t(lang, "lang_menu"), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇮🇷 فارسی", callback_data="setlang|fa")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="setlang|en")],
            [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="setlang|tr")],
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang|ru")],
            [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="setlang|de")],
            [InlineKeyboardButton("🇪🇸 Español", callback_data="setlang|es")],
        ]))
        return

    parts = data.split("|")
    if len(parts) < 3 or parts[0] != "dl":
        return

    dl_id, quality = parts[1], parts[2]
    dl_entry = context.user_data.get(f"dl_{dl_id}")
    if not dl_entry:
        await q.message.reply_text(i18n.t(lang, "link_expired"))
        return

    url = dl_entry["url"]
    info = dl_entry["info"]
    lang = dl_entry.get("lang", lang)

    is_audio = quality == "audio"
    # Shazam recognition: extract audio from the link's video, identify the song
    if quality == "shazam":
        try:
            await q.answer("🎵 در حال بررسی آهنگ...")
        except Exception:
            pass
        status2 = await q.message.reply_text(i18n.t(lang, "shazam_searching"))
        out_audio = os.path.join(DOWNLOAD_DIR, f"shz_{uid}_{dl_id}.wav")
        dl.cleanup(out_audio)
        # Download just the audio (bestaudio -> wav, 30s) for fingerprinting
        ok_audio = await asyncio.to_thread(dl.download_audio_wav, url, out_audio)
        result = None
        if ok_audio:
            result = await recog.recognize_audio(out_audio)
            dl.cleanup(out_audio)
        if result and result.get("matched"):
            try:
                await status2.delete()
            except Exception:
                pass
            await _send_shazam_result(update, context, result, lang)
        else:
            await status2.edit_text(i18n.t(lang, "shazam_no_match"))
        return

    label = "MP3" if is_audio else f"{quality}p"
    msg = await q.message.reply_text(i18n.t(lang, "download_started"))

    ext = "mp3" if is_audio else "mp4"
    out_path = os.path.join(DOWNLOAD_DIR, f"dl_{uid}_{dl_id}_{quality}.{ext}")
    dl.cleanup(out_path)

    ok = await _limited_download(url, quality, is_audio, out_path)
    out_path = resolve_out_path(out_path)
    if not ok:
        db.record_download(uid, url, dl.detect_site(url), label, False)
        await msg.edit_text(i18n.t(lang, "download_failed"))
        return

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        db.record_download(uid, url, dl.detect_site(url), label, False)
        await msg.edit_text(i18n.t(lang, "too_big").format(size=int(size_mb)))
        dl.cleanup(out_path)
        return

    await msg.edit_text(i18n.t(lang, "sending"))

    if is_audio:
        await q.message.reply_audio(audio=open(out_path, "rb"), title=info.get("title", "audio"))
    else:
        info2 = dict(info)
        info2["quality"] = quality
        info2["size_str"] = dl.human_size(os.path.getsize(out_path))
        caption = dl.make_caption(info2, lang)
        await q.message.reply_video(video=open(out_path, "rb"), caption=caption,
                                    parse_mode="Markdown", supports_streaming=True)

    db.record_download(uid, url, dl.detect_site(url), label, True)
    dl.cleanup(out_path)
    await msg.delete()
    await asyncio.to_thread(enforce_disk_limit)


async def more_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    parts = q.data.split("|") if q.data else []
    if len(parts) < 2 or parts[0] != "more":
        return
    dl_id = parts[1]
    lang = lang_of(update)
    keyboard = context.user_data.get(f"more_{dl_id}", [])
    try:
        if keyboard:
            await q.message.reply_text(i18n.t(lang, "choose_quality"),
                                       reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await q.message.reply_text(i18n.t(lang, "link_expired"))
    except Exception:
        # Message may have been deleted or the bot restarted — don't crash the loop
        try:
            await q.answer(i18n.t(lang, "link_expired"), show_alert=True)
        except Exception:
            pass


# ── Error handler ─────────────────────────────────────────────
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error)
    try:
        if update and update.effective_message:
            # Only notify for real failures, not transient network blips
            err = str(context.error)
            net_errors = ("RemoteProtocolError", "ConnectTimeout", "ReadTimeout",
                          "NetworkError", "TimedOut", "ConnectError", "ReadError",
                          "httpx.TimeoutException", "httpx.ConnectError", "httpx.ReadError")
            if any(k in err for k in net_errors):
                await update.effective_message.reply_text("⚠️ خطای موقتی شبکه. دوباره تلاش کن.")
            else:
                await update.effective_message.reply_text("⚠️ خطای غیرمنتظره‌ای رخ داد. دوباره تلاش کن.")
    except Exception:
        pass


async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When the bot is added to a group/channel, greet with a setup hint.
    Because of Telegram privacy mode, the bot only sees group messages when it's
    a group admin — this message tells the admin exactly that."""
    mc = update.my_chat_member
    if not mc or not mc.new_chat_member:
        return
    status = mc.new_chat_member.status
    chat = mc.chat
    if status not in ("member", "administrator") or chat.type not in ("group", "supergroup"):
        return
    try:
        bot_username = (await context.bot.get_me()).username
    except Exception:
        bot_username = "the bot"
    msg = (
        "👋 **سلام! ربات به گروه اضافه شد.**\n\n"
        "⚠️ **برای اینکه ربات توی گروه کار کنه (لینک‌ها رو ببینه و دانلود کنه)، "
        "ادمین گروه باید ربات رو ادمین کنه:**\n\n"
        "۱. تنظیمات گروه → ادمین‌ها → افزودن ادمین\n"
        "۲. این ربات رو انتخاب کن\n"
        "۳. «دسترسی به پیام‌ها» رو فعال کن\n\n"
        "بعدش می‌تونی با کامندها استفاده کنی:\n"
        "/grouphelp — راهنمای گروه\n"
        f"یا ربات رو با @{bot_username} صدا بزن"
    )
    try:
        await context.bot.send_message(chat_id=chat.id, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.warning("bot_added welcome failed: %s", e)


async def startmsg_translate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for choosing auto-translate vs manual-only after entering new start text."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    lang = lang_of(update)
    uid = q.from_user.id
    new_text = context.user_data.pop("startmsg_new", None)
    context.user_data.pop("adm_state", None)
    if not new_text:
        await q.message.edit_text(i18n.t(lang, "cancelled"))
        return
    choice = (q.data or "").split("|")[-1]
    if choice == "manual":
        db.set_custom_msg("fa", "start", new_text)
        i18n.invalidate_msg_cache()
        await q.message.edit_text("✅ متن شروع فقط برای **فارسی** ذخیره شد.\n"
                                  "بقیه زبان‌ها پیش‌فرض می‌مونن.",
                                  reply_markup=admin_back_keyboard(lang))
        return
    # auto translate to all languages
    status = await q.message.edit_text("🤖 در حال ترجمه به همه زبان‌ها...")
    translations = await asyncio.to_thread(i18n.translate_to_all, new_text, "fa")
    for code, txt in translations.items():
        db.set_custom_msg(code, "start", txt)
    i18n.invalidate_msg_cache()
    done = len(translations)
    await status.edit_text(f"✅ متن شروع **به {done} زبان** ترجمه و ذخیره شد!\n\n"
                           f"📝 اگه یه زبانی رو درست نمی‌خوای، از همین‌جا دوباره تغییر بده.",
                           reply_markup=admin_back_keyboard(lang))


# ── Main ──────────────────────────────────────────────────────
async def _register_commands(app) -> None:
    """Register slash-command menu so Telegram shows them when user types '/'.
    This is what makes commands appear in the group/private command picker."""
    try:
        from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
        private_cmds = [
            BotCommand("start", "🚀 Start / شروع"),
            BotCommand("help", "ℹ️ Help / راهنما"),
            BotCommand("lang", "🌍 Language / زبان"),
            BotCommand("settings", "⚙️ Default quality"),
            BotCommand("history", "📜 Download history"),
            BotCommand("about", "🤖 About"),
        ]
        _all = private_cmds + [BotCommand("admin", "🛡 Admin panel")]
        await app.bot.set_my_commands(_all, scope=BotCommandScopeDefault())
        # Admin sees /admin in their private chat
        await app.bot.set_my_commands(
            _all, scope=BotCommandScopeChat(chat_id=ADMIN_IDS[0]))
        log.info("registered command menu (private + admin)")
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def main():
    db.init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).base_url("http://127.0.0.1:8081/bot").build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    # Group commands
    app.add_handler(CommandHandler("grouphelp", group_help))
    app.add_handler(CommandHandler("groupsettings", group_settings))
    app.add_handler(CommandHandler("setquality", setquality))
    app.add_handler(CommandHandler("setmedia", setmedia))
    app.add_handler(CommandHandler("setfilter", setfilter))
    app.add_handler(CommandHandler("autodl", autodl))
    app.add_handler(CommandHandler("silent", silent))
    app.add_handler(CommandHandler("commandonly", commandonly))
    # Per-user settings
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("ytdl", ytdl_cmd))
    # Shazam-like recognition
    app.add_handler(CommandHandler("shazam", shazam_cmd))
    # Force-join channels are managed INSIDE the /admin panel (adm|channels, adm|addchan, adm|delchan)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, admin_input))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_bot_added))
    # Shazam: recognize song from audio/video files
    app.add_handler(MessageHandler(
        filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE
        | (filters.Document.AUDIO | filters.Document.VIDEO),
        handle_media_file))
    # Plugin zip upload while in install state (must be BEFORE generic file handlers)
    app.add_handler(MessageHandler(filters.Document.ALL, plugin_file_cb))

    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^adm"))
    app.add_handler(CallbackQueryHandler(set_lang_cb, pattern="^setlang"))
    app.add_handler(CallbackQueryHandler(set_def_q_cb, pattern="^setdefq"))
    app.add_handler(CallbackQueryHandler(force_join_check_cb, pattern="^fjcheck$"))
    app.add_handler(CallbackQueryHandler(search_cb, pattern="^srch"))
    app.add_handler(CallbackQueryHandler(setmedia_cb, pattern="^setmedia"))
    app.add_handler(CallbackQueryHandler(setfilter_cb, pattern="^setfilter"))
    app.add_handler(CallbackQueryHandler(startmsg_translate_cb, pattern="^smt"))
    app.add_handler(CallbackQueryHandler(chapter_cb, pattern="^pl(dl|pg|noop|close)"))
    app.add_handler(CallbackQueryHandler(more_qualities, pattern="^more"))
    app.add_handler(CallbackQueryHandler(button_click))

    app.add_error_handler(error_handler)
    app.post_init = _register_commands

    log.info("رایان دانلودر started... (admins: %s)", ADMIN_IDS)
    app.run_polling()


if __name__ == "__main__":
    main()
