# Changelog

All notable changes to the Rayan Downloader Bot.

## [Unreleased] - production hardening (audit)

### Security
- New `netguard.py` egress layer replaces every `curl`/`urlopen` call that
  consumed remote input: pinned validated IPs, redirect revalidation, timeouts,
  byte caps, blocks loopback/private/link-local/IPv4-mapped IPv6/`file://`.
- TLS verification is the default; `--no-check-certificates` now only appears
  when `RAYAN_TLS_INSECURE=1` is explicitly set.
- Plugin install: netguard fetch, pre-extraction archive inspection, manifest
  size cap, `realpath` entry containment; failed upgrade preserves the old
  plugin.
- Plugins execute one call per subprocess with scrubbed env, rlimits and a
  private TMPDIR (documented as process separation, **not a sandbox**).

### Fixed
- Queue: per-user cap and fair-share scheduling; cancellation/timeout/exception
  paths release permits; no lost `_waiting_total` accounting.
- SQLite: WAL set once at init instead of on every connection; 30 s busy
  timeout; online backups are openable/queryable under concurrent writes;
  retention and failure handling verified.
- Disk cleanup now checks real free space as well as quota, never deletes the
  local Bot API store, and keeps files younger than 10–60 minutes.
- Telegram Markdown captions escape remote titles (`_ * [ ] `` ` ``), so a
  successful download is not lost to an entity parse error.
- `post_init` no longer crashes on an empty admin list.
- IPv6 HTTP `Host` header is a valid authority.
- Plugin site routing no longer falls back to substring matching for domain
  rules (`instagram.com` vs `notinstagram.com`), with a regression test.

### Added
- `config.py` single source of truth with fail-fast validation.
- `tests/test_production_hardening.py`: 44 stdlib tests covering queue
  fairness/cancellation, SQLite integrity/backup/WAL/restore, plugin archive
  attacks/timeout kill/env scrub, SSRF matrix/DNS rebinding/size caps, disk
  cleanup and TLS defaults.

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
