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

## 🚀 Quick Start (deploy your own instance)

```bash
# 1. Clone & install
git clone https://github.com/green1021/rayan-downloader-bot
cd rayan-downloader-bot
pip install -r requirements.txt

# 2. Set YOUR bot token + YOUR admin user id (never hardcode)
export TG_BOT_TOKEN="YOUR_BOT_TOKEN"        # from @BotFather
export TG_ADMIN_IDS="YOUR_TELEGRAM_USER_ID" # your numeric ID (get from @userinfobot)

# 3. Run
python3 rayan_dl_bot_main.py
```

> The bot reads its token from `TG_BOT_TOKEN` env var, or falls back to a legacy token file. Tokens are **never** committed.
> **Admins:** the first admin(s) come from `TG_ADMIN_IDS`. Additional admins can be added at runtime via the `/admin` panel ("➕ Add admin") and are stored in the local SQLite DB — which is **never committed** to git. Any server can deploy and define its own admins.

## 🔐 Group Usage (important)
Telegram's privacy mode means the bot only sees group messages when it's **a group admin**.
After adding the bot to a group:
1. Group Settings → Administrators → Add Bot
2. Enable "Access messages"
Then group commands work: `/grouphelp /groupsettings /setquality /autodl /silent /commandonly /ytdl`

## 🔌 Plugin System

The bot supports **installable plugins** that handle sites the core doesn't support (comics → PDF, custom galleries, etc).

### Install from the admin panel
1. `/admin` → **🔌 Plugins** → **➕ Install Plugin**
2. Send a URL to the plugin:
   - a `.zip` containing `plugin.json` + code
   - a raw GitHub URL to `plugin.json` (e.g. `https://raw.githubusercontent.com/green1021/rayan-plugins/main/allporncomic/plugin.json`)
3. Plugin is validated (name, sites, entry path) and installed automatically

### Write your own plugin
```
plugins/<name>/
  plugin.json   { name, version, description, sites[], entry, enabled, config{} }
  plugin.py     class Plugin: def handle(self, url, config) -> dict
```

`handle()` returns:
```python
{"type": "pdf"|"video"|"photos"|"text",
 "title": "...", "path": "/abs/file.pdf",   # pdf/video
 "photos": ["/abs/1.jpg"],                    # photos
 "text": "caption", "error": "..."}           # on failure
```
See `rayan_dl_bot/plugins/allporncomic/plugin.py` as an example (downloads comic pages → single PDF via img2pdf).

Plugin contributions repo: `github.com/green1021/rayan-plugins`

## 🗂 Structure

```
rayan_dl_bot/
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