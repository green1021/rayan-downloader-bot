# Changelog

All notable changes to the Rayan Downloader Bot.

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

### Fixed
- Twitter long-tweet truncation (fxtwitter full-text fallback)
- Force-join "permanent pass" bug (confirmation expiry + always re-check checkable channels)
- Admin panel back-button navigation error