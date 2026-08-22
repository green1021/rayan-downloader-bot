#!/usr/bin/env python3
"""Weekly Rayan Downloader bot report — run by cron, output goes to Telegram via cron delivery."""
import sqlite3
import subprocess
import json
from datetime import datetime, timedelta, timezone

DB = "/root/hermes-workspace/data/rayan.db"
now = datetime.now(timezone.utc)
week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")


def q(sql, args=()):
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


lines = ["📊 **گزارش هفتگی رایان دانلودر**",
         f"🗓 {week_ago} تا {now.strftime('%Y-%m-%d')}", ""]

# Users
total_users = q("SELECT COUNT(*) FROM users")[0][0]
new_users = q("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (week_ago,))[0][0]
lines.append(f"👥 کاربران: {total_users} کل (+{new_users} جدید)")

# Downloads this week
rows = q("SELECT COUNT(*), SUM(success=1) FROM downloads WHERE created_at >= ?", (week_ago,))
total_dl = rows[0][0] or 0
ok_dl = rows[0][1] or 0
fail = total_dl - ok_dl
rate = int(ok_dl / total_dl * 100) if total_dl else 0
lines.append(f"📥 دانلودها: {total_dl} ({ok_dl} موفق، {fail} خطا — {rate}% موفقیت)")

# By site
site_rows = q("SELECT site, COUNT(*) FROM downloads WHERE created_at >= ? GROUP BY site ORDER BY 2 DESC LIMIT 5", (week_ago,))
if site_rows:
    lines.append("🏆 پلتفرم‌های برتر:")
    for s, c in site_rows:
        lines.append(f"   • {s}: {c}")

# Top users this week
user_rows = q("""SELECT u.user_id, COUNT(*) c FROM downloads d JOIN users u ON u.rowid = d.rowid
              WHERE d.created_at >= ? GROUP BY u.user_id ORDER BY c DESC LIMIT 3""", (week_ago,))
top_users = q("SELECT user_id, COUNT(*) FROM downloads WHERE created_at >= ? GROUP BY user_id ORDER BY 2 DESC LIMIT 3", (week_ago,))
if top_users:
    lines.append("🥇 کاربران فعال:")
    for uid_, c in top_users:
        uname = q("SELECT username FROM users WHERE user_id = ?", (uid_,))
        name = f"@{uname[0][0]}" if uname and uname[0][0] else str(uid_)
        lines.append(f"   • {name}: {c} دانلود")

# Failed downloads detail
fails = q("SELECT site, quality, COUNT(*) FROM downloads WHERE success=0 AND created_at >= ? GROUP BY site, quality", (week_ago,))
if fails:
    lines.append("⚠️ خطاها: " + ", ".join(f"{s}/{q_}×{c}" for s, q_, c in fails))

# Service health
try:
    r = subprocess.run(["systemctl", "is-active", "tg-download-bot"], capture_output=True, text=True)
    svc = r.stdout.strip()
except Exception:
    svc = "?"
mem = q("SELECT COUNT(*) FROM shazam_cache")[0][0]
lines += ["", f"🩺 سرویس: `{svc}` | کش شزم: {mem}", "",
          "_گزارش خودکار هفتگی — هر شنبه 09:00 UTC_"]

print("\n".join(lines))
