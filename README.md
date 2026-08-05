# 🎬 Rayan Downloader Bot

Telegram downloader bot built with **python-telegram-bot** + **yt-dlp**, supporting a local Bot API server for large uploads (up to 2GB).

## ✨ Features

- **Multi-site download**: YouTube, TikTok, Instagram (embed workaround), Twitter/X (video / photos / text / full threads / quote tweets), and more
- **Fast concurrent downloads** (yt-dlp `-N 8`)
- **MP3 conversion** (audio-only mode)
- **Multi-language**: fa / en / tr / ru / de / es (first-run language prompt, English default)
- **Admin panel** (`/admin`): stats, user management (info / export-links / ban / unban), links viewer, force-join channels, DB backup download, broadcast
- **Force-join channels**: join-link buttons + "I joined" confirmation with 24h expiry; auto-check when the bot is channel admin
- **Group mode**: `/setquality /autodl /silent /commandonly /ytdl /groupsettings /grouphelp`
- **Per-user default quality** (`/settings`)
- **Download history** (`/history`), playlists, smart proxy routing

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt   # python-telegram-bot, yt-dlp

# 2. Set your bot token (never hardcode)
export TG_BOT_TOKEN="YOUR_BOT_TOKEN"

# 3. Run
python3 rayan_dl_bot_v3_main.py
```

> The bot reads its token from `TG_BOT_TOKEN` env var, or falls back to a legacy token file. Tokens are **never** committed.

## 🗂 Structure

```
rayan_dl_bot_v3/
├── bot.py          # Handlers + admin panel + group mode
├── db.py           # SQLite + schema migration
├── downloader.py   # Download engine (yt-dlp)
├── ig_tw.py        # Instagram + Twitter (photos/text/threads/quotes)
├── i18n.py         # Translations
└── i18n_extra.py   # Extra language packs
```

## 🔒 Security Notes
- Bot token never hardcoded — read from env
- Files deleted after sending
- Force-join confirmations expire (24h) so leaving a channel re-blocks

## 📄 License
Private. © green1021 / Rayan Downloader