# 🎬 Rayan Downloader Bot

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.08%2B-green)
![Telegram](https://img.shields.io/badge/telegram-bot-blueviolet)

Multi-language Telegram downloader bot built with **python-telegram-bot** + **yt-dlp**, with a local Bot API server for large uploads (up to 2 GB).

## ✨ Features

### Core downloading
- **Multi-site**: YouTube, TikTok, Instagram (embed workaround), Twitter/X (video / photos / text / full threads / quote tweets), SoundCloud, Pinterest, Aparat/Namasha (bundled plugin), and dozens more
- **Smart unknown-site detection** — two-stage engine:
  1. `yt-dlp` probe (with browser-UA retry) → full quality menu
  2. HTML media analysis → video/photos/comic→PDF/text extraction
- **Download queue** with live position feedback («⏳ در صف: N کار دیگر») and concurrency limits tuned per server
- **Fast concurrent downloads** (`yt-dlp -N 8`)
- **MP3 conversion** + 🎵 **Shazam-style song recognition** (multi-offset fingerprinting, loudness-normalized, AcoustID fallback, WARP-proxied)
- 💬 **YouTube subtitles**: a subtitle button appears *only when subs exist* — sends `.srt` (manual tracks preferred: en/fa/de/es/fr/ar, auto-captions fallback)

### Caching & performance
- ♻️ **Telegram CDN cache** — first send stores the `file_id`; identical requests re-send instantly from Telegram's CDN (zero download/bandwidth/disk)
- 🔁 **Duplicate-link cache** — same link within 30 min served instantly
- Thumbnails on all video sends, IG carousel albums, Twitter GIFs as looping animations

### Groups
- Welcome message on bot add (with admin setup hint)
- Per-group settings: max quality, media filter, link filter, auto-download, silent mode, command-only mode
- Media file-cache works in groups too; per-chat anti-flood pacing

### Administration
- **Admin panel** (`/admin`): stats, user management (info / export-links / ban / unban), links viewer, force-join channels, DB backup download, broadcast
- **Force-join channels**: join buttons + «I joined» check with 24 h expiry
- **Plugin system** with security hardening: hostname-based site matching, install-time SSRF guard (https + public hosts only), Zip-Slip protection, size/count caps, per-plugin timeout, module caching
- Weekly usage report to admin Telegram

## 🚀 Quick Start

```bash
# 1. Clone & install
git clone https://github.com/green1021/rayan-downloader-bot
cd rayan-downloader-bot
pip install -r requirements.txt

# Keep yt-dlp fresh! YouTube breaks old versions regularly:
pip install -U yt-dlp

# 2. Set YOUR bot token + YOUR admin id (never hardcode)
export TG_BOT_TOKEN="YOUR_BOT_TOKEN"        # from @BotFather
export TG_ADMIN_IDS="YOUR_TELEGRAM_USER_ID" # numeric ID from @userinfobot

# 3. Run
python3 rayan_dl_bot_main.py
```

> The bot reads its token from `TG_BOT_TOKEN` (or `BOT_TOKEN`, or the first
> line of `RAYAN_TOKEN_FILE`). The old legacy-script scraping fallback was
> removed. Tokens are **never** committed or logged.
> **Admins:** initial admins come from `TG_ADMIN_IDS`. More can be added at runtime via `/admin` → «➕ Add admin» and are stored in the local SQLite DB — which is **never** committed.

### Configuration

All values are optional; defaults reproduce the historical layout. Invalid
values stop startup with one operator-safe message (never a token).

| Variable | Default | Purpose |
|---|---|---|
| `TG_BOT_TOKEN` / `BOT_TOKEN` | — (required) | BotFather token |
| `RAYAN_TOKEN_FILE` | — | Alternative: file whose first line is the token |
| `TG_ADMIN_IDS` | empty | Comma-separated numeric admin IDs (bootstrap) |
| `RAYAN_DATA_DIR` | `/root/hermes-workspace/data` | Data directory, holds DB + backups |
| `RAYAN_DB_PATH` | `<data>/rayan.db` | SQLite database |
| `RAYAN_DOWNLOAD_DIR` | `/root/hermes-workspace/tmp/downloads` | Scratch downloads (safe to prune) |
| `RAYAN_PLUGINS_DIR` | `<package>/plugins` | Plugin store |
| `TG_API_BASE` | official API | Local Bot API base, e.g. `http://127.0.0.1:8081/bot` |
| `RAYAN_API_STORAGE` | disabled | Local Bot API file store; counted, **never deleted** |
| `RAYAN_DISK_LIMIT_GB` | `2` | Download-area budget |
| `RAYAN_MAX_FILE_SIZE_MB` | `1900` | Per-file cap; also passed to yt-dlp |
| `RAYAN_TLS_INSECURE` | `0` | **Escape hatch only**: `1` disables TLS verification |
| `RAYAN_PLUGIN_ISOLATION` | `1` | `0` runs plugins in-process (less safe) |
| `RAYAN_YTDLP` | auto-detect | yt-dlp executable |

`python3 rayan_dl_bot_main.py` validates configuration and creates the data,
download and plugin directories before starting the bot.

### Optional extras
| Feature | Needs |
|---|---|
| Song recognition | Python 3.11 venv `venv311` with `shazamio aiohttp-socks`, `ffmpeg`, `fpcalc` (libchromaprint-tools), WARP SOCKS5 proxy at `127.0.0.1:40000` |
| >50 MB uploads | Local Bot API server on `127.0.0.1:8081` |
| Subtitles / generic sites | Recent yt-dlp + Node.js ≥ 20 |

## 🔐 Group Usage

Telegram privacy mode = the bot only sees group messages as **group admin**:

1. Group Settings → Administrators → Add Bot
2. Enable «Access messages»

Group commands: `/grouphelp /groupsettings /setquality /setmedia /setfilter /autodl /silent /commandonly /ytdl`

## 🔌 Plugin System

Plugins handle sites the core doesn't (comics → PDF, galleries, custom extractors). Installable straight from the admin panel.

### Install from the admin panel
1. `/admin` → **🔌 Plugins** → **➕ Install Plugin**
2. Send either:
   - a `.zip` containing `plugin.json` + code (≤ 30 MB, ≤ 200 files), or
   - a raw GitHub URL to `plugin.json`
3. Fetched through the egress guard (https-only, public hosts, size cap), the
   archive is inspected **before** extraction (traversal, symlinks, entry
   count, expansion), then installed into a fresh temp directory and moved
   into place. A failed upgrade leaves the previous version working.

### Write your own plugin
```
plugins/<name>/
  plugin.json   { name, version, description, sites[], entry, enabled, config{}, timeout }
  plugin.py     class Plugin: def handle(self, url, config) -> dict
```

`handle()` returns:
```python
{"type": "pdf"|"video"|"photos"|"text",
 "title": "...", "path": "/abs/file.pdf",   # pdf/video
 "photos": ["/abs/1.jpg"],                  # photos
 "text": "caption", "error": "..."}         # on failure
```

See [`rayan_dl_bot/plugins/aparat/plugin.py`](rayan_dl_bot/plugins/aparat/plugin.py) — a complete example using Aparat's public JSON API (all qualities up to 1080p, Namasha redirects included).

Plugin contributions repo: `github.com/green1021/rayan-plugins`

## 🗂 Structure

```
rayan_dl_bot/
├── bot.py            # Handlers, admin panel, group mode, queue feedback
├── db.py             # SQLite (WAL) + schema migrations
├── downloader.py     # Download engine (yt-dlp wrapper, subtitles, playlists)
├── dlqueue.py        # Global download queue w/ positions
├── cdncache.py       # Telegram file_id cache (instant re-sends)
├── linkcache.py      # Duplicate-link cache (30 min TTL)
├── ig_tw.py          # Instagram + Twitter (photos/text/threads/quotes/carousels/GIFs)
├── recognizer.py     # Shazam-style recognition (multi-offset + AcoustID)
├── auto_detect.py    # Unknown-site two-stage detection
├── plugin_manager.py # Plugin install/validation/dispatch (isolated subprocess)
├── plugin_builder.py # Guided plugin creation from the admin panel
├── i18n.py           # Translations (fa/en full)
└── i18n_extra.py     # tr/ru/de/es packs
```

## 🔒 Security Notes

- Bot token never hardcoded, never logged; env/file only.
- **One outbound egress path** (`rayan_dl_bot/netguard.py`): scheme allow-list,
  DNS resolution once, every resolved address validated, connection pinned to
  the validated IP (no validate-then-reconnect TOCTOU), redirects revalidated
  on every hop, connect/total timeouts, and response-size caps. Blocks
  loopback, private, link-local, IPv4-mapped IPv6 and `file://`/`gopher://`
  destinations; fails closed on mixed DNS answers.
- **TLS verification is on by default.** `RAYAN_TLS_INSECURE=1` is a narrowly
  scoped, documented escape hatch and logs a startup warning.
- Plugin installs are treated as untrusted input: fetched through netguard,
  manifest size capped, archive inspected before extraction (traversal,
  symlinks, entry count, expansion), entry path contained via `realpath`.
- Plugin **execution** uses one subprocess per call with a scrubbed
  environment, rlimits and a private `TMPDIR`. This is process separation and
  resource limiting — **not a security sandbox**. A plugin still runs with the
  bot user's privileges and network access. Install only plugins you trust.
- Site routing by parsed **hostname**, not substring matching.
- Files deleted after sending; force-join confirmations expire after 24 h.
- DB, backups and tokens are git-ignored.

## 🧪 Tests

The suite is stdlib `unittest` — no pytest, no extra dependencies:

```bash
./venv/bin/python tests/test_production_hardening.py -v   # queue, SQLite, plugins, SSRF, disk
for f in tests/test_*.py; do echo "== $f"; ./venv/bin/python "$f"; done
```

Tests use temporary data/plugin directories and loopback HTTP fixtures. They
never touch the production database, token or Telegram API. The public-HTTPS
check skips itself when the host has no outbound network.

## 💾 Backup and restore

Backups use SQLite's online backup API, are written to `<data>/backups/`, keep
the newest 7, and never stop the bot on failure. An automatic backup runs at
most once a day; admins can also download one from `/admin`.

```bash
# manual backup (bot stopped or running — both are safe)
sqlite3 /root/hermes-workspace/data/rayan.db ".backup '/root/backups/rayan-manual.db'"
# verify
sqlite3 /root/backups/rayan-manual.db "PRAGMA quick_check; SELECT COUNT(*) FROM users;"

# restore (stop the bot first)
systemctl stop tg-download-bot
cp /root/hermes-workspace/data/rayan.db /root/backups/rayan-before-restore.db
cp <backup.db> /root/hermes-workspace/data/rayan.db
rm -f /root/hermes-workspace/data/rayan.db-wal /root/hermes-workspace/data/rayan.db-shm
systemctl start tg-download-bot
```

## ↩️ Rollback

Code rollback (before or after a controlled restart):

```bash
cd /root/rayan-downloader-bot
git switch audit/production-hardening     # or the previous release branch
git log --oneline -5                      # pick the last known-good commit
git switch --detach <known-good-commit>   # inspect; do not discard user work
systemctl restart tg-download-bot
systemctl is-active tg-download-bot
journalctl -u tg-download-bot -n 50 --no-pager
```

Database rollback uses the verified backup above. Keep the pre-deploy backup
until the new version has served real requests successfully.

### Safe upgrade sequence (documented, not executed by the audit)

1. Back up the tree and DB: `git bundle`/`tar` + `sqlite3 .backup`; verify the
   backup opens and `PRAGMA quick_check` returns `ok`.
2. `git fetch && git switch <release-commit>`; `./venv/bin/pip install -r requirements.txt`
   only if dependencies changed.
3. Run `./venv/bin/python tests/test_production_hardening.py` and the rest of
   `tests/` — every file must exit 0.
4. `systemctl restart tg-download-bot`; poll `systemctl is-active` and
   `journalctl -u tg-download-bot -n 50` for a clean startup line.
5. Send one real download request and confirm delivery.
6. If anything fails: restore the DB backup, check out the previous commit,
   restart, and re-verify.

## 🔄 Staying healthy
YouTube frequently changes internals that break old yt-dlp releases. Schedule an update:

```bash
# monthly yt-dlp refresh (crontab)
0 5 1 * * pip install -U yt-dlp && systemctl restart tg-download-bot
```

## 📄 License
MIT — see [LICENSE](LICENSE).
