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

> The bot reads its token from `TG_BOT_TOKEN`, or falls back to a legacy token file. Tokens are **never** committed.
> **Admins:** initial admins come from `TG_ADMIN_IDS`. More can be added at runtime via `/admin` → «➕ Add admin» and are stored in the local SQLite DB — which is **never committed**.

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
3. Validated (https-only, public hosts, safe paths) and installed automatically

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
├── plugin_manager.py # Plugin loader (sandboxed install, timeouts, module cache)
├── plugin_builder.py # Guided plugin creation from the admin panel
├── i18n.py           # Translations (fa/en full)
└── i18n_extra.py     # tr/ru/de/es packs
```

## 🔒 Security Notes
- Bot token never hardcoded — env vars only
- Plugin installs: https-only, public-host SSRF guard, Zip-Slip protection, size caps, execution timeout
- Site routing by parsed **hostname**, not substring matching (no `notinstagram.com` surprises)
- Files deleted after sending; force-join confirmations expire after 24 h
- DB and tokens are git-ignored

## 🔄 Staying healthy
YouTube frequently changes internals that break old yt-dlp releases. Schedule an update:

```bash
# monthly yt-dlp refresh (crontab)
0 5 1 * * pip install -U yt-dlp && systemctl restart tg-download-bot
```

## 📄 License
MIT — see [LICENSE](LICENSE).
