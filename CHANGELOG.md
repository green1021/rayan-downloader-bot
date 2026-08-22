# Changelog

All notable changes to the Rayan Downloader Bot.

## [1.1.0] - 2026-08-22

### Added
- **Download queue** with live position feedback and per-server concurrency tuning
- 💬 **YouTube subtitles**: quality-menu button appears only when subs exist; sends `.srt` (manual tracks preferred, auto-captions fallback)
- ♻️ **Telegram CDN cache**: stores `file_id` after first send — identical requests re-send instantly from Telegram's CDN (zero bandwidth/disk)
- 🎵 **Song recognition overhaul**: fixed a critical quoting bug that had silently broken all Shazam lookups; multi-offset fingerprinting (0/30/60s), loudness normalization, score-based AcoustID selection
- **Aparat/Namasha plugin** (bundled): public JSON API, qualities up to 1080p, live-tested downloads
- **Personalized `/start`**: greets users by name + lifetime download stats
- **Group overhaul**: welcome message on bot add, media file-cache in groups, per-chat anti-flood pacing
- `smart_detect`: browser-UA retry pass for stubborn sites, shared yt-dlp binary config

### Security
- Plugin installs: SSRF guard (https-only, public hosts), Zip-Slip protection, 30 MB / 200-entry caps, 90 s per-call timeout, module caching
- Site routing now matches parsed hostnames instead of raw substring tests (`notinstagram.com` no longer misroutes)

### Fixed
- Shazam recognition was completely broken since v1.0.0 (code-generation quoting bug) — every lookup failed silently
- Fake semaphore bug: concurrency limit in the download queue had no effect

## [1.0.0] - 2026-08-05

### Added
- Initial release
- Multi-language support (fa/en default full; tr/ru/de/es via extra packs)
- Multi-site download: YouTube, TikTok, Instagram, Twitter/X
- **Twitter full support**: video, photos, text, thread walking, quote tweets
  - Full text via fxtwitter API (syndication truncates long tweets)
- **Instagram**: embed-page workaround (no login for public reels)
- Admin panel: stats, users (info/export-links/ban/unban), links viewer, broadcast
- Force-join channels: join links + manual confirm (24h expiry), auto-check when bot is admin
- Group mode: /setquality /autodl /silent /commandonly /ytdl /groupsettings /grouphelp
- Per-user default quality (/settings)
- Fast concurrent downloads (yt-dlp `-N 8`), MP3 conversion, playlists
- Local Bot API server support (2GB uploads)
- First-run language prompt (English default)
