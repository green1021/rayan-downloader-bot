#!/usr/bin/env python3
"""
Rayan Downloader Bot — main entrypoint
Multi-language + admin panel + fast concurrent downloads
"""

import os
import re
import sys
import time
import logging
import asyncio
import shutil
import hashlib

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

# Global download concurrency cap: max 2 concurrent downloads across all users.
# Prevents CPU/RAM spikes when several people send links at once.
_download_sem = asyncio.Semaphore(2)


def rate_limited(user_id: int, seconds: int = 8) -> bool:
    now = time.time()
    last = _rate.get(user_id, 0)
    if now - last < seconds:
        return True
    _rate[user_id] = now
    return False


async def _limited_download(*args, **kwargs):
    """Run dl.download_video under the global semaphore (max 2 concurrent)."""
    async with _download_sem:
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
        [InlineKeyboardButton(i18n.t(lang, "help_btn") + " راهنما", callback_data="help"),
         InlineKeyboardButton(i18n.t(lang, "history_btn") + " تاریخچه", callback_data="history")],
        [InlineKeyboardButton("🌍 " + i18n.t(lang, "choose_site"), callback_data="lang")],
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
        short = url[:50] + ("..." if len(url) > 50 else "")
        lines.append(i18n.t(lang, "history_item").format(
            site=site_names.get(site, site), url=short, time=created[:16]))
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


# ── Admin panel ───────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    """Admin = one of env-bootstrapped owners OR a DB-registered admin."""
    if user_id in ADMIN_IDS:
        return True
    try:
        return db.is_db_admin(user_id)
    except Exception:
        return False


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Admin panel is private-only — never visible in a group
    if not _private_only(update):
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(i18n.t(lang_of(update), "not_admin"))
        return
    lang = lang_of(update)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "stats") + " آمار", callback_data="adm|stats"),
         InlineKeyboardButton(i18n.t(lang, "users_btn") + " کاربران", callback_data="adm|users")],
        [InlineKeyboardButton(i18n.t(lang, "links_btn") + " لینک‌ها", callback_data="adm|links"),
         InlineKeyboardButton("🔗 کانال‌ها", callback_data="adm|channels")],
        [InlineKeyboardButton("📤 خروجی کاربر", callback_data="adm|userlinks"),
         InlineKeyboardButton("💾 دیتابیس", callback_data="adm|db")],
        [InlineKeyboardButton("👥 ادمین‌ها", callback_data="adm|adminlist"),
         InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm|addadmin")],
        [InlineKeyboardButton(i18n.t(lang, "broadcast_btn") + " پیام گروهی", callback_data="adm|broadcast"),
         InlineKeyboardButton(i18n.t(lang, "ban_btn") + " مسدود", callback_data="adm|ban")],
        [InlineKeyboardButton(i18n.t(lang, "unban_btn") + " رفع مسدودی", callback_data="adm|unban"),
         InlineKeyboardButton("➕ کانال", callback_data="adm|addchan")],
    ])
    await update.message.reply_text(i18n.t(lang, "admin_menu"), reply_markup=kb)


def admin_back_keyboard(lang: str):
    """Keyboard with a back button for admin sub-menus."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "back"), callback_data="adm|menu"),
         InlineKeyboardButton(i18n.t(lang, "close"), callback_data="adm|close")]
    ])


async def _admin_menu_edit(q, lang: str):
    """Render the admin main menu onto the existing message (edit, not new msg)."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t(lang, "stats") + " آمار", callback_data="adm|stats"),
         InlineKeyboardButton(i18n.t(lang, "users_btn") + " کاربران", callback_data="adm|users")],
        [InlineKeyboardButton(i18n.t(lang, "links_btn") + " لینک‌ها", callback_data="adm|links"),
         InlineKeyboardButton("🔗 کانال‌ها", callback_data="adm|channels")],
        [InlineKeyboardButton("📤 خروجی کاربر", callback_data="adm|userlinks"),
         InlineKeyboardButton("💾 دیتابیس", callback_data="adm|db")],
        [InlineKeyboardButton("👥 ادمین‌ها", callback_data="adm|adminlist"),
         InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm|addadmin")],
        [InlineKeyboardButton(i18n.t(lang, "broadcast_btn") + " پیام گروهی", callback_data="adm|broadcast"),
         InlineKeyboardButton(i18n.t(lang, "ban_btn") + " مسدود", callback_data="adm|ban")],
        [InlineKeyboardButton(i18n.t(lang, "unban_btn") + " رفع مسدودی", callback_data="adm|unban"),
         InlineKeyboardButton("➕ کانال", callback_data="adm|addchan")],
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
            url_s = (url[:45] + ("..." if len(url) > 45 else ""))
            lines.append(i18n.t(lang, "admin_link_line").format(
                user=name, uid=uid, site=site_names.get(site, site),
                title=title_s, url=url_s, time=(created or "")[:16]))
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


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast/ban/unban text input from admin."""
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
            await update.message.reply_text(f"✅ کاربر `{uid}` ادمین شد.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ کاربر `{uid}` از قبل ادمینه.", parse_mode="Markdown")

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
    text = i18n.t(lang_of(update), "group_help")
    # Add a clear privacy-mode note
    text += i18n.t(lang_of(update), "group_privacy_note")
    await update.message.reply_text(text)


async def group_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text(i18n.t(lang_of(update), "only_groups"))
        return
    lang = lang_of(update)
    s = db.get_chat_settings(chat.id)
    await update.message.reply_text(i18n.t(lang, "group_settings").format(
        quality=s["max_quality"] if s["max_quality"] else "∞",
        auto=i18n.t(lang, "group_on") if s["auto_mode"] else i18n.t(lang, "group_off"),
        silent=i18n.t(lang, "group_on") if s["silent"] else i18n.t(lang, "group_off")))


async def setquality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    if rate_limited(uid):
        await update.message.reply_text(i18n.t(lang, "rate_limit"))
        return

    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text(i18n.t(lang, "invalid_url"), parse_mode="Markdown")
        return

    site = dl.detect_site(url)
    db.record_link(update, url, site)

    status_msg = await update.message.reply_text(i18n.t(lang, "checking"))

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
        text = (tw_full.get("text") or "")[:400]
        qtext = (q.get("text") or "")[:400]
        msg_text = i18n.t(lang, "tw_quote_only").format(
            name=tw_full.get("user_name") or "", screen=tw_full.get("user_screen") or "",
            text=text, qname=q.get("user_name") or "", qscreen=q.get("user_screen") or "",
            qtext=qtext)
        await status_msg.delete()
        await update.message.reply_text(msg_text, disable_web_page_preview=True)
        for photo_url in tw_full.get("photos", [])[:4]:
            p_path = os.path.join(DOWNLOAD_DIR, f"twq_{uid}_{int(time.time())}.jpg")
            ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
            if ok_p:
                await context.bot.send_photo(chat_id=uid, photo=open(p_path, "rb"))
                dl.cleanup(p_path)
        db.record_download(uid, url, "twitter", "quote", True)
        await asyncio.to_thread(enforce_disk_limit)
        return

    # Thread detection: if this tweet is a reply (in_reply_to set), try walking the thread
    if is_tw and tw_full.get("in_reply_to"):
        thread = igtw.get_twitter_thread(url)
        if thread.get("tweets") and len(thread["tweets"]) > 1:
            await status_msg.edit_text(i18n.t(lang, "tw_thread_header").format(count=len(thread["tweets"])))
            for i, t in enumerate(thread["tweets"], 1):
                text = (t.get("text") or "")[:500]
                header = i18n.t(lang, "tw_thread_item").format(n=i, name=t.get("user_name") or "", text=text)
                await update.message.reply_text(header, disable_web_page_preview=True)
                # Send photos for each tweet in the thread
                for photo_url in t.get("photos", [])[:4]:
                    p_path = os.path.join(DOWNLOAD_DIR, f"twth_{uid}_{i}_{int(time.time())}.jpg")
                    ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
                    if ok_p:
                        await context.bot.send_photo(chat_id=uid, photo=open(p_path, "rb"))
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
                name=tw_full.get("user_name") or "", screen=tw_full.get("user_screen") or "",
                text=(tw_full.get("text") or "")[:400],
                likes=tw_full.get("favorite_count") or 0, rts=tw_full.get("retweet_count") or 0)
            sent_first = False
            for photo_url in tw_full["photos"][:10]:
                p_path = os.path.join(DOWNLOAD_DIR, f"twp_{uid}_{int(time.time())}.jpg")
                ok_p = await asyncio.to_thread(igtw.download_twitter_photo, photo_url, p_path)
                if ok_p:
                    if not sent_first:
                        await context.bot.send_photo(chat_id=uid, photo=open(p_path, "rb"), caption=header)
                        sent_first = True
                    else:
                        await context.bot.send_photo(chat_id=uid, photo=open(p_path, "rb"))
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
        full_text = (tw_full.get("text") or "").strip()
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
                await context.bot.send_video(chat_id=uid, video=open(fpath, "rb"), supports_streaming=True)
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
            await status_msg.edit_text(i18n.t(lang, "dl_in_group").format(title=info.get("title", "")[:80]),
                                       parse_mode="Markdown")
            out_path = os.path.join(DOWNLOAD_DIR, f"grp_{chat.id}_{int(time.time())}")
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

    await status_msg.edit_text(i18n.t(lang, "downloading").format(title=info.get("title", "")[:80], quality=f"{best}p"),
                               parse_mode="Markdown")

    out_path = os.path.join(DOWNLOAD_DIR, f"dl_{uid}_{best}.mp4")
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
    parts = q.data.split("|")
    if len(parts) < 2 or parts[0] != "more":
        return
    dl_id = parts[1]
    lang = lang_of(update)
    keyboard = context.user_data.get(f"more_{dl_id}", [])
    if keyboard:
        await q.message.reply_text(i18n.t(lang, "choose_quality"),
                                   reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await q.message.reply_text(i18n.t(lang, "link_expired"))


# ── Error handler ─────────────────────────────────────────────
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error)
    try:
        if update and update.effective_message:
            # Only notify for real failures, not transient network blips
            err = str(context.error)
            if "RemoteProtocolError" in err or "ConnectTimeout" in err or "ReadTimeout" in err:
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
    app.add_handler(CommandHandler("autodl", autodl))
    app.add_handler(CommandHandler("silent", silent))
    app.add_handler(CommandHandler("commandonly", commandonly))
    # Per-user settings
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("ytdl", ytdl_cmd))
    # Force-join channels are managed INSIDE the /admin panel (adm|channels, adm|addchan, adm|delchan)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, admin_input))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_bot_added))

    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^adm"))
    app.add_handler(CallbackQueryHandler(set_lang_cb, pattern="^setlang"))
    app.add_handler(CallbackQueryHandler(set_def_q_cb, pattern="^setdefq"))
    app.add_handler(CallbackQueryHandler(force_join_check_cb, pattern="^fjcheck$"))
    app.add_handler(CallbackQueryHandler(more_qualities, pattern="^more"))
    app.add_handler(CallbackQueryHandler(button_click))

    app.add_error_handler(error_handler)
    app.post_init = _register_commands

    log.info("رایان دانلودر started... (admins: %s)", ADMIN_IDS)
    app.run_polling()


if __name__ == "__main__":
    main()
