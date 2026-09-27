#!/usr/bin/env python3
"""
downloader.py — Core download engine for Rayan Downloader Bot
- Fast concurrent downloads (yt-dlp -N)
- Smart proxy routing (direct / Tor / WARP)
- Pornhub custom downloader integration
"""

import os
import re
import json
import time
import asyncio
import logging
import subprocess
import importlib.util

log = logging.getLogger("rayan-dl")

DOWNLOAD_DIR = "/root/hermes-workspace/tmp/downloads"
YTDLP = "/usr/local/bin/yt-dlp"
# Node is available — use it as the JS runtime so yt-dlp can extract all
# YouTube formats (without a JS runtime some formats/qualities are missing).
JS_RUNTIME = ["--js-runtimes", "node:/usr/bin/node"]
FFMPEG = "/usr/bin/ffmpeg"
WARP_PROXY = "socks5://127.0.0.1:40000"
TOR_PROXY = "socks5h://127.0.0.1:9050"

# ── Size pre-gate ─────────────────────────────────────────────
# yt-dlp aborts BEFORE transferring when the format reports a size above this.
# bot.py syncs its own MAX_FILE_SIZE_MB here at import; the post-download
# check stays as backstop for hosts that don't report sizes.
MAX_FILESIZE_BYTES = 1900 * 1024 * 1024


def set_max_filesize_bytes(n: int) -> None:
    global MAX_FILESIZE_BYTES
    try:
        MAX_FILESIZE_BYTES = max(0, int(n))
    except (TypeError, ValueError):
        pass


def _size_gate() -> list:
    """[--max-filesize N] args, empty when the gate is disabled (0)."""
    return ["--max-filesize", str(MAX_FILESIZE_BYTES)] if MAX_FILESIZE_BYTES > 0 else []


# ── Transient (network-ish) errors — B7 auto-retry ────────────
class TransientError(Exception):
    """A retryable, network-ish failure from an external process/HTTP call.

    Deliberately NOT raised for: user cancellation (asyncio.CancelledError),
    validation errors (bad URL / bad arguments), or permanent extractor
    failures ("Unsupported URL", login walls) — callers must let those
    propagate/fail fast instead of burning retries on them."""


# stderr markers that mean the failure was the NETWORK, not the input.
# Everything else (unsupported URL, missing format, auth wall...) fails fast.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "temporary failure", "connection reset",
    "connection refused", "connection error", "connection aborted",
    "network", "getaddrinfo", "name or service not known",
    "unable to download", "http error 5", "http error 429",
    "remote end closed", "eof occurred", "ssl:", "chunked",
    "giving up after", "readerror", "connecterror", "proxy",
)


def looks_transient(err_text: str) -> bool:
    """True when an error string smells like a network hiccup (retryable)."""
    t = (err_text or "").lower()
    return any(m in t for m in _TRANSIENT_MARKERS)


def classify_failure(exc: BaseException) -> bool:
    """True if exc is worth retrying. NEVER retries CancelledError
    (user-cancel) — that must propagate immediately."""
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, TransientError):
        return True
    # subprocess timeouts are network-ish by nature
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    msg = str(exc)
    return looks_transient(msg)


def _retry_run(cmd: list, *, timeout: float, attempts: int = 3,
               base_delay: float = 1.0, ok=None):
    """Run cmd with exponential backoff on network-ish failures.

    ok(returncode, proc) lets callers add success conditions beyond rc==0.
    Non-retryable failures raise immediately (validation errors must not
    burn retries); CancelledError always propagates untouched.
    NOTE: process stderr is carried in a PLAIN Exception — classify_failure
    must judge it by its TEXT (markers), not by a pre-decided type."""
    last_exc = None
    for attempt in range(max(1, attempts)):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            if r.returncode == 0 or (ok and ok(r.returncode, r)):
                return r
            last_exc = Exception((r.stderr or r.stdout or "")[-300:])
        except subprocess.TimeoutExpired as e:
            last_exc = e
        except asyncio.CancelledError:
            raise  # user-cancel: never swallowed, never retried
        except Exception as e:
            last_exc = e
        # only sleep+retry when the NEXT attempt exists AND failure is retryable
        if attempt < attempts - 1 and classify_failure(last_exc):
            time.sleep(base_delay * (2 ** attempt))  # 1s, 2s, 4s...
            continue
        break
    if isinstance(last_exc, Exception):
        raise last_exc
    raise TransientError("command failed")

# Load pornhub helper
_SPEC = importlib.util.spec_from_file_location("ph", "/root/hermes-workspace/scripts/ph_downloader.py")
ph = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ph)

# Load Instagram/Twitter helper
_SPEC2 = importlib.util.spec_from_file_location("igtw", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_tw.py"))
igtw = importlib.util.module_from_spec(_SPEC2)
_SPEC2.loader.exec_module(igtw)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── Site detection ────────────────────────────────────────────
PORNHUB_SITES = ["pornhub.com"]
TOR_SITES = [
    "xvideos.com", "xhamster.com", "xnxx.com", "redtube.com",
    "youporn.com", "spankbang.com", "eporner.com", "beeg.com",
    "tube8.com", "motherless.com",
]
INSTAGRAM = "instagram.com"
TWITTER = ["twitter.com", "x.com"]
TIKTOK = "tiktok.com"


def _hostname(url: str) -> str:
    """Extract lowercase hostname from a URL (None-safe)."""
    try:
        from urllib.parse import urlparse
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _host_matches(url: str, domains) -> bool:
    """True if the URL's hostname is exactly one of domains or a subdomain of one.
    Hostname-only matching prevents misclassification like notinstagram.com or
    evil.com/?url=instagram.com hijacking the instagram extractor."""
    h = _hostname(url)
    if not h:
        return False
    for d in domains:
        d = d.lower()
        if h == d or h.endswith("." + d):
            return True
    return False


def is_pornhub(url): return _host_matches(url, PORNHUB_SITES)
def is_tor_site(url): return _host_matches(url, TOR_SITES)
def is_instagram(url): return _host_matches(url, [INSTAGRAM])
def is_twitter(url): return _host_matches(url, TWITTER)
def is_tiktok(url): return _host_matches(url, [TIKTOK])


def clean_instagram_url(url: str) -> str:
    """Instagram links with '?igsh=...' (or any query) often fail the embed
    workaround — strip the query string so the reel/post downloads without login."""
    if not is_instagram(url):
        return url
    if "?" in url:
        return url.split("?", 1)[0]
    return url


def resolve_short_url(url: str) -> str:
    """Follow redirects for short links (vt.tiktok.com, t.co, etc.) so yt-dlp/extractors
    get the REAL URL. TikTok's mobile short links often break its own extractor."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{url_effective}", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20)
        final = (r.stdout or "").strip()
        if final and final != url and ("tiktok.com" in final or "twitter.com" in final or "x.com" in final):
            return fix_tiktok_url(final)
    except Exception:
        pass
    return fix_tiktok_url(url)


def fix_tiktok_url(url: str) -> str:
    """TikTok's yt-dlp extractor ONLY works when the URL carries a 'web app' marker
    (?_r=1 or ?is_from_webapp=1). Plain shared links (full www.tiktok.com/@user/video/ID)
    fail with 'Unable to extract universal data' — add the marker."""
    if "tiktok.com" not in url:
        return url
    if "_r=1" in url or "is_from_webapp" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_r=1"


def detect_site(url: str) -> str:
    if is_pornhub(url):
        return "pornhub"
    if is_tor_site(url):
        return "pornhub"
    if is_instagram(url):
        return "instagram"
    if is_twitter(url):
        return "twitter"
    if is_tiktok(url):
        return "tiktok"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "other"


def get_proxy_args(url: str) -> list:
    """Smart proxy routing: prefer WARP (fast, ~16MB/s) for blocked sites,
    fall back to Tor (slow, ~1MB/s) only when WARP is blocked.
    Pornhub is handled by ph_downloader (already WARP-first internally)."""
    if is_tor_site(url):
        # WARP is ~16x faster than Tor; Tor is the fallback for anti-bot sites
        if _warp_can_reach(url):
            return ["--proxy", WARP_PROXY]
        return ["--proxy", TOR_PROXY]
    return []


_warp_check_cache: dict = {}
import threading
_warp_lock = threading.Lock()


def _warp_can_reach(url: str, timeout: float = 6.0) -> bool:
    """Cheap check: can WARP reach this site? Cached per-domain for 60s."""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc
    now = __import__("time").time()
    with _warp_lock:
        cached = _warp_check_cache.get(domain)
        if cached and now - cached[1] < 60:
            return cached[0]
    ok = False
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(int(timeout)),
             "--socks5-hostname", "127.0.0.1:40000", "-A", "Mozilla/5.0", f"https://{domain}/"],
            capture_output=True, text=True, timeout=int(timeout) + 5)
        ok = r.stdout.strip() not in ("", "000", "403", "407")
    except Exception:
        ok = False
    with _warp_lock:
        _warp_check_cache[domain] = (ok, now)
    return ok


# ── Formatting ────────────────────────────────────────────────
def format_number(n) -> str:
    if isinstance(n, (int, float)):
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(int(n))
    return str(n)


def format_duration(sec) -> str:
    if not sec:
        return "N/A"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(size) -> str:
    if not size:
        return "?"
    size = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


def _esc_md(text: str) -> str:
    """Escape Telegram Markdown special chars in user/tweet content so a tweet
    containing *, _, [, etc. can't break the caption formatting."""
    if text is None:
        return ""
    return (str(text).replace("\\", "\\\\")
            .replace("_", "\\_").replace("*", "\\*")
            .replace("[", "\\[").replace("`", "\\`"))


def make_caption(info: dict, lang: str = "fa") -> str:
    """Build a rich caption. Uses simple emoji labels per language.
    User-provided content (title/uploader/description) is Markdown-escaped so it
    can never break the formatting."""
    title = _esc_md(info.get("title") or "بدون عنوان")
    lines = [f"🎬 **{title[:150]}**\n"]
    if info.get("uploader"):
        lines.append(f"👤 **{'آپلودر' if lang == 'fa' else 'Uploader'}:** {_esc_md(info['uploader'])}")
    if info.get("upload_date"):
        lines.append(f"📅 **{'تاریخ' if lang == 'fa' else 'Date'}:** {_esc_md(info['upload_date'])}")
    if info.get("duration"):
        lines.append(f"⏱ **{'مدت' if lang == 'fa' else 'Duration'}:** {format_duration(info['duration'])}")
    if info.get("view_count") is not None:
        lines.append(f"👀 **{'بازدید' if lang == 'fa' else 'Views'}:** {format_number(info['view_count'])}")
    if info.get("like_count") is not None:
        lines.append(f"👍 **{'لایک' if lang == 'fa' else 'Likes'}:** {format_number(info['like_count'])}")
    if info.get("quality"):
        lines.append(f"📀 **{'کیفیت' if lang == 'fa' else 'Quality'}:** {info['quality']}p")
    if info.get("size_str"):
        lines.append(f"💾 **{'حجم' if lang == 'fa' else 'Size'}:** {info['size_str']}")
    if info.get("description"):
        desc = _esc_md(info["description"].strip()[:500])
        if desc:
            lines.append(f"\n📝 {desc}")
    lines.append(f"\n🔗 [{'مشاهده در سایت' if lang == 'fa' else 'View source'}]({info.get('webpage_url', '')})")
    caption = "\n".join(lines)
    # Telegram hard limit: 4096 chars per caption/message — never exceed it.
    # Cut at a newline boundary so markdown/links are not split mid-token.
    if len(caption) > 4000:
        caption = caption[:4000].rsplit("\n", 1)[0]
    return caption


# ── Info ──────────────────────────────────────────────────────
def get_video_info(url: str) -> dict:
    """Get video info (title, qualities, sizes) — with concurrency for speed."""
    if is_pornhub(url):
        info = ph.get_video_info(url)
        info["sizes"] = {}
        return info

    if is_instagram(url):
        # Instagram: strip ?igsh query (it breaks the embed workaround), then extract direct video URL
        url = clean_instagram_url(url)
        video_url = igtw.extract_ig_video_url(url)
        if not video_url:
            return {"error": "Instagram sent an empty media response. This post may be private or need login."}
        return {
            "title": "Instagram video",
            "uploader": "",
            "duration": None,
            "view_count": None,
            "like_count": None,
            "description": "",
            "webpage_url": url,
            "qualities": {"1080": video_url},
            "sizes": {"1080": None},
            "ig_video_url": video_url,
            "is_ig": True,
        }

    if is_twitter(url):
        full = igtw.get_twitter_full(url)
        if full.get("error"):
            return full
        if full.get("type") == "video":
            # Video tweet: expose qualities for the normal flow
            screen = full.get("user_screen") or ""
            return {
                "title": f"🐦 Tweet by @{screen}" if screen else "Tweet video",
                "uploader": f"{full.get('user_name')} @{full.get('user_screen')}",
                "duration": None,
                "view_count": None,
                "like_count": full.get("favorite_count"),
                "description": full.get("text", ""),
                "webpage_url": url,
                "qualities": {"720": None},
                "sizes": {},
                "is_tw": True,
                "tw_type": "video",
                "tw_full": full,
            }
        # Photos / text / quote tweets
        screen = full.get("user_screen") or ""
        return {
            "title": f"🐦 Tweet by @{screen}" if screen else "Tweet",
            "uploader": f"{full.get('user_name')} @{full.get('user_screen')}",
            "webpage_url": url,
            "qualities": {},
            "sizes": {},
            "is_tw": True,
            "tw_type": full.get("type", "text"),
            "tw_full": full,
        }

    # TikTok short links (vt.tiktok.com) break its extractor — resolve to real URL first
    if is_tiktok(url):
        url = resolve_short_url(url)

    proxy = get_proxy_args(url)
    # -N 8: concurrent fragment downloads; --no-check-certificates for speed on some hosts
    cmd = [YTDLP, *JS_RUNTIME, *proxy, "-J", "--no-warnings", "--skip-download",
           "--no-check-certificates", url]
    # B7: network-ish failures auto-retry with backoff; bad URLs / login walls
    # fail fast. TikTok's API rate-limits aggressively → one extra attempt.
    attempts = 4 if is_tiktok(url) else 3
    try:
        r = _retry_run(cmd, timeout=120, attempts=attempts, base_delay=2.0,
                       # success needs a real JSON object on stdout — some
                       # failures exit 1 yet still print ("null", b"")
                       ok=lambda rc, proc: (proc.stdout or "").lstrip().startswith("{"))
    except Exception as e:
        return {"error": str(e)[-300:]}
    try:
        d = json.loads(r.stdout)

        # Playlist detection
        if d.get("_type") == "playlist" or d.get("entries"):
            entries = d.get("entries") or []
            return {"playlist": True, "count": len(entries), "title": d.get("title", "playlist")}

        heights = sorted({f.get("height") for f in d.get("formats", []) if f.get("height")}, reverse=True)
        heights = [h for h in heights if 144 <= h <= 2160]

        sizes = {}
        for h in heights:
            size = None
            for f in d.get("formats", []):
                if f.get("height") == h and f.get("filesize") and f.get("acodec") != "none":
                    size = f.get("filesize")
                    break
            if size is None:
                vsize = None
                for f in d.get("formats", []):
                    if f.get("height") == h and f.get("filesize") and f.get("acodec") == "none":
                        vsize = f.get("filesize")
                        break
                asize = None
                for f in d.get("formats", []):
                    if f.get("acodec") and f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("filesize"):
                        asize = f.get("filesize")
                        break
                if vsize and asize:
                    size = vsize + asize
            if size:
                sizes[str(h)] = size

        thumb = d.get("thumbnail")
        # subtitle language picker: which AUTO languages exist (manual ones are
        # already in "subtitles"); and which heights carry per-format subtitle
        # tracks (yt-dlp exposes subtitles on individual formats) — display-only
        auto_langs = list((d.get("automatic_captions") or {}).keys())
        sub_heights = sorted(
            {f.get("height") for f in d.get("formats", [])
             if f.get("height") and (f.get("subtitles") or f.get("requested_subtitles"))},
            reverse=True)
        return {
            "title": d.get("title") or d.get("fulltitle") or "",
            "uploader": d.get("uploader") or d.get("channel") or d.get("uploader_id"),
            "duration": d.get("duration"),
            "view_count": d.get("view_count"),
            "like_count": d.get("like_count"),
            "upload_date": d.get("upload_date"),
            "description": d.get("description"),
            "thumbnail": thumb,
            "webpage_url": d.get("webpage_url") or url,
            "qualities": {str(h): None for h in heights},
            "sizes": sizes,
            "is_yt": "youtube.com" in url or "youtu.be" in url,
            # subtitle availability — read once here, bot reuses it (no second probe)
            "subtitles": list((d.get("subtitles") or {}).keys()),
            "auto_captions": bool(d.get("automatic_captions")),
            "auto_caption_langs": auto_langs,
            "format_sub_heights": [int(h) for h in sub_heights],
        }
    except Exception as e:
        return {"error": str(e)}


# ── Download ──────────────────────────────────────────────────
def search_youtube(query: str, limit: int = 30) -> list:
    """Search YouTube, return list of {id, title, channel, duration, url}.
    Filters out channels/non-video entries; includes enough results for paging."""
    try:
        r = subprocess.run(
            ["/usr/local/bin/yt-dlp", *JS_RUNTIME, "--flat-playlist", "-J", "--no-check-certificates",
             f"ytsearch{limit}:{query}"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        out = []
        for e in data.get("entries", []):
            url = e.get("url") or ""
            if not url.startswith("http"):
                continue  # skip channels
            dur = e.get("duration")
            if dur is None:
                continue  # live/no duration
            title = (e.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "id": e.get("id"),
                "title": title[:80],
                "channel": (e.get("channel") or "")[:30],
                "duration": int(dur),
                "url": url,
            })
        return out
    except Exception as e:
        log.error("search_youtube: %s", e)
        return []


def download_url_audio(url: str, out_file: str) -> bool:
    """Download audio (m4a) directly from a video URL, no re-encode (fast)."""
    import shutil
    d = os.path.dirname(out_file)
    stem = os.path.basename(out_file).rsplit(".", 1)[0]
    out = os.path.join(d, stem + ".m4a")
    try:
        r = subprocess.run(
            ["/usr/local/bin/yt-dlp", *JS_RUNTIME, "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist",
             "--no-check-certificates", "--force-overwrites",
             *_size_gate(),
             "-o", out, url],
            capture_output=True, text=True, timeout=240,
        )
        if r.returncode != 0:
            return False
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            return False
        if os.path.abspath(out) != os.path.abspath(out_file):
            shutil.move(out, out_file)
        return os.path.exists(out_file) and os.path.getsize(out_file) > 0
    except Exception as e:
        log.error("download_url_audio: %s", e)
        return False


def download_song(title: str, artist: str, out_file: str, want_mp3: bool = False) -> bool:
        """Download a song from YouTube (search by title+artist).
        By default downloads m4a directly (NO re-encode -> fast, ~4s). If want_mp3,
        converts with ffmpeg (slower). Returns True on success."""
        import shutil
        d = os.path.dirname(out_file)
        stem = os.path.basename(out_file).rsplit(".", 1)[0]
        query = f"ytsearch1:{title} {artist} official audio"
        try:
            if want_mp3:
                r = subprocess.run(
                    ["/usr/local/bin/yt-dlp", *JS_RUNTIME, "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist",
                     "--no-check-certificates", "--force-overwrites",
                     *_size_gate(),
                     "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
                     "-o", os.path.join(d, stem), query],
                    capture_output=True, text=True, timeout=200,
                )
                if r.returncode != 0:
                    return False
                cands = [os.path.join(d, f) for f in os.listdir(d)
                         if f.startswith(stem) and f.endswith(".mp3")]
            else:
                # Fast path: download m4a (aac) directly, no re-encode
                out = os.path.join(d, stem + ".m4a")
                r = subprocess.run(
                    ["/usr/local/bin/yt-dlp", *JS_RUNTIME, "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist",
                     "--no-check-certificates", "--force-overwrites",
                     *_size_gate(),
                     "-o", out, query],
                    capture_output=True, text=True, timeout=200,
                )
                if r.returncode != 0:
                    return False
                cands = [out] if os.path.exists(out) and os.path.getsize(out) > 0 else []
            if not cands:
                return False
            src = cands[0]
            if os.path.abspath(src) != os.path.abspath(out_file):
                shutil.move(src, out_file)
            return os.path.exists(out_file) and os.path.getsize(out_file) > 0
        except Exception as e:
            log.error("download_song: %s", e)
            return False


# keep alias for backwards compatibility
def download_song_mp3(title, artist, out_mp3):
    return download_song(title, artist, out_mp3, want_mp3=True)


def download_audio_wav(url: str, out_wav: str, seconds: int = 30) -> bool:
    """Download just the audio of a video link as mono WAV for fingerprinting.
    Instagram uses the embed workaround (ig_tw); everything else uses yt-dlp."""
    import shutil

    def _ffmpeg_wav(src: str) -> bool:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-t", str(seconds),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_wav],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 0

    # ── Instagram: fetch direct mp4 from embed page, then extract audio ──
    if is_instagram(url):
        try:
            url = clean_instagram_url(url)
            # igtw is loaded at module top (importlib)
            video_url = igtw.extract_ig_video_url(url)
            if not video_url:
                return False
            src = out_wav + ".ig.mp4"
            r = subprocess.run(
                ["curl", "-s", "-L", "--max-time", "120", "-A", igtw.UA, "-o", src, video_url],
                capture_output=True, text=True, timeout=140,
            )
            if not (os.path.exists(src) and os.path.getsize(src) > 0):
                return False
            ok = _ffmpeg_wav(src)
            try:
                os.remove(src)
            except Exception:
                pass
            return ok
        except Exception as e:
            log.error("ig download_audio_wav: %s", e)
            return False

    # ── Other sites: yt-dlp bestaudio ──
    tmp = out_wav + ".raw"
    try:
        r = subprocess.run(
            ["/usr/local/bin/yt-dlp", *JS_RUNTIME, "-f", "bestaudio/best", "--no-playlist",
             "--no-check-certificates",
             *_size_gate(),
             "-N", "8", "-o", tmp, url],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            return False
        # find the actual downloaded file (may have extension appended)
        cands = [os.path.join(os.path.dirname(out_wav), f)
                 for f in os.listdir(os.path.dirname(out_wav)) if f.startswith(os.path.basename(tmp))]
        if not cands:
            return False
        src = cands[0]
        ok = _ffmpeg_wav(src)
        try:
            os.remove(src)
        except Exception:
            pass
        return ok
    except Exception as e:
        log.error("download_audio_wav: %s", e)
        return False


def download_video(url: str, quality: str, is_audio: bool, out_path: str) -> bool:
    """Download video/audio. Uses -N concurrency and fast merge."""
    if is_pornhub(url):
        info = ph.get_video_info(url)
        hls = info.get("qualities", {}).get(quality)
        if not hls:
            return False
        return ph.download_hls(hls, out_path)

    if is_instagram(url):
        # Direct download from the embed-extracted video URL (strip ?igsh first)
        url = clean_instagram_url(url)
        video_url = igtw.extract_ig_video_url(url)
        if not video_url:
            return False
        try:
            r = subprocess.run(
                ["curl", "-s", "-L", "--max-time", "300", "-A", igtw.UA, "-o", out_path, video_url],
                capture_output=True, text=True, timeout=320,
            )
            ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            if not ok:
                for cand in (out_path + ".mp4",):
                    if os.path.exists(cand) and os.path.getsize(cand) > 0:
                        ok = True
                        break
            return ok
        except Exception as e:
            log.error("ig download error: %s", e)
            return False

    if is_twitter(url):
        # Try direct mp4 via fxtwitter metadata first (syndication API is dead)
        direct = None
        try:
            import ig_tw as _tw
            tw = _tw.get_twitter_full(url)
            vids = tw.get("video_urls") or []
            if vids:
                direct = vids[0]
        except Exception:
            pass
        return igtw.download_twitter(url, quality, out_path, direct_url=direct)

    # TikTok short links (vt.tiktok.com) break its extractor — resolve to real URL first
    if is_tiktok(url):
        url = resolve_short_url(url)

    proxy = get_proxy_args(url)

    if is_audio:
        cmd = [YTDLP, *JS_RUNTIME, *proxy, "-x", "--audio-format", "mp3", "-f", "bestaudio",
               *_size_gate(),
               "--no-check-certificates", "-N", "8",
               "-o", out_path, "--no-warnings", url]
    else:
        if quality == "audio":
            fmt = "best"
        elif is_instagram(url) or is_tiktok(url):
            fmt = "best"
        else:
            # bv+ba merge with fallback — fast and robust
            fmt = f"bv[height<={quality}]+ba/b[height<={quality}]"
        cmd = [YTDLP, *JS_RUNTIME, *proxy, "-f", fmt,
               "--merge-output-format", "mp4",
               *_size_gate(),
               "--no-check-certificates", "-N", "8",
               "--no-playlist",
               "-o", out_path, "--no-warnings", url]
    try:
        attempts = 4 if is_tiktok(url) else 3
        last_err = None
        for attempt in range(attempts):
            # clean partial file between attempts
            if attempt > 0:
                dl.cleanup(out_path)
                for cand in (out_path + ".mp4", out_path + ".mp3", out_path + ".mkv", out_path + ".webm"):
                    dl.cleanup(cand)
                # B7: only sleep when the LAST failure was network-ish — a
                # permanent extractor error must fail fast, not wait out backoff
                if last_err is not None and not classify_failure(last_err):
                    break
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
                ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
                if not ok:
                    # yt-dlp may append .mp4/.mp3 if out_path has no proper ext
                    for cand in (out_path + ".mp4", out_path + ".mp3", out_path + ".mkv", out_path + ".webm"):
                        if os.path.exists(cand) and os.path.getsize(cand) > 0:
                            ok = True
                            break
                if ok:
                    return True
                err_text = ((r.stderr or "") + "\n" + (r.stdout or ""))[-400:]
            except subprocess.TimeoutExpired as e:
                err_text = str(e)          # timeout = network-ish → retryable
            except asyncio.CancelledError:
                raise                       # user-cancel propagates untouched
            except Exception as e:
                err_text = f"{type(e).__name__}: {e}"
            last_err = TransientError(err_text)
            if attempt < attempts - 1 and classify_failure(last_err):
                time.sleep(1.0 * (2 ** attempt))   # 1s, 2s, 4s...
        return False
    except Exception as e:
        log.error("download error: %s", e)
        return False


def list_available_subs(url: str) -> dict | None:
    """Check if a video has manual or auto-generated subtitles.
    Returns {"manual": [langs], "auto": bool} or None on failure/timeout."""
    proxy = get_proxy_args(url)
    try:
        r = subprocess.run(
            [YTDLP, *JS_RUNTIME, *proxy, "--list-subs", "--skip-download",
             "--no-warnings", "--no-check-certificates", url],
            capture_output=True, text=True, timeout=45)
        out = r.stdout or ""
        if not out or "has no subtitles" in out.lower():
            return None
        manual, auto = [], False
        section = None
        for ln in out.splitlines():
            l = ln.strip()
            if "Available subtitles" in l:
                section = "manual"
                continue
            if "Available automatic captions" in l:
                section = "auto"
                auto = True
                continue
            if l.startswith("Language") or not l or l.startswith("["):
                continue
            code = l.split()[0] if l.split() else ""
            if not code or "-" not in code and len(code) > 5:
                continue
            if section == "manual" and code:
                manual.append(code)
            elif section == "auto":
                auto = True
        return {"manual": manual[:20], "auto": auto}
    except Exception as e:
        log.warning("list_subs: %s", e)
        return None


def download_video_with_subs(url: str, lang: str, out_path: str,
                             height: int | None = None) -> str | None:
    """Download a video with the subtitle track EMBEDDED (soft subs).

    Uses MKV container so the subtitle stream survives and external players
    (VLC / MX Player / Infuse) can toggle it. Returns path or None."""
    import glob as _glob
    d = os.path.dirname(out_path) or "."
    # unique per request — callers pass uid+dl_id names; two users requesting
    # subtitles in the same second must not collide on one output file
    stem = out_path.rsplit(".", 1)[0] + f"_{int(time.time()*1000)%100000}"
    proxy = get_proxy_args(url)
    vf = (f"bestvideo[height<={height}]+bestaudio/best"
          if height else "bestvideo+bestaudio/best")
    cmd = [YTDLP, *JS_RUNTIME, *proxy,
           "--embed-subs", "--write-subs", "--sub-langs", lang,
           "--merge-output-format", "mkv",
           *_size_gate(),
           "-f", vf, "--no-playlist", "--no-check-certificates",
           "-o", stem, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        for cand in sorted(_glob.glob(stem + "*.mkv")):
            if os.path.getsize(cand) > 10000:
                return cand
        # fallback: whatever got produced
        for ext in ("*.mp4", "*.webm", "*.mkv"):
            g = _glob.glob(os.path.join(d, stem + "*" + ext.lstrip("*")))
            if g and os.path.getsize(g[0]) > 10000:
                return g[0]
        return None
    except Exception as e:
        log.error("download_video_with_subs: %s", e)
        return None


def burn_subtitles(video_path: str, srt_path: str, out_path: str) -> str | None:
    """HARD-SUB: render the subtitle track onto the video (burn-in) via libass.
    Slow (re-encodes video). Returns out_path or None."""
    try:
        safe = srt_path.replace("'", "\\'")
        # 2GB-RAM guard: cap decoder threads so AV1/1080p sources can't OOM-kill us
        c = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-threads", "2", "-i", video_path,
             "-vf", f"subtitles={safe}:force_style='FontName=Vazirmatn,FontSize=22,"
                    f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                    f"Outline=2,MarginV=25'",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-threads", "2",
             "-c:a", "copy", out_path],
            capture_output=True, text=True, timeout=3600)
        if c.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            return out_path
        log.error("burn_subtitles rc=%s %s", c.returncode, (c.stderr or "")[:200])
        return None
    except Exception as e:
        log.error("burn_subtitles: %s", e)
        return None


def download_subtitle(url: str, lang: str, out_path: str) -> str | None:
    """Download one subtitle track as .srt (auto-generated allowed).
    Returns the path of the .srt file or None."""
    import glob
    d = os.path.dirname(out_path) or "."
    # honor the caller's unique path (uid+dl_id) — epoch stems collided across users
    stem = out_path[:-4] if out_path.endswith(".srt") else out_path
    stem = stem + f"_{int(time.time()*1000)%100000}"
    proxy = get_proxy_args(url)
    try:
        r = subprocess.run(
            [YTDLP, *JS_RUNTIME, *proxy, "--write-subs", "--write-auto-subs",
             "--sub-langs", lang, "--sub-format", "srt/vtt/best", "--skip-download",
             "--no-warnings", "--no-check-certificates",
             "-o", os.path.join(d, stem), url],
            capture_output=True, text=True, timeout=90)
        # yt-dlp names it <stem>.<lang>.srt (or .vtt)
        for f in sorted(glob.glob(os.path.join(d, stem + "*.srt"))) or \
                 sorted(glob.glob(os.path.join(d, stem + "*.vtt"))):
            return f
        return None
    except Exception as e:
        log.error("download_subtitle: %s", e)
        return None


def download_playlist(url: str, out_dir: str) -> int:
    """Download a whole playlist. Returns number of files downloaded."""
    proxy = get_proxy_args(url)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [YTDLP, *JS_RUNTIME, *proxy, "-f", "best[height<=720]+bestaudio/best[height<=720]",
           "--merge-output-format", "mp4",
           *_size_gate(),
           "--no-check-certificates", "-N", "8",
           "-o", os.path.join(out_dir, "%(title).80s.%(ext)s"),
           "--no-warnings", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        files = [f for f in os.listdir(out_dir) if f.endswith(".mp4") or f.endswith(".mkv")]
        return len(files)
    except Exception as e:
        log.error("playlist error: %s", e)
        return 0


def cleanup(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
