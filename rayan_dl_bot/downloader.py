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
import logging
import subprocess
import importlib.util

log = logging.getLogger("rayan-dl")

DOWNLOAD_DIR = "/root/hermes-workspace/tmp/downloads"
YTDLP = "/usr/local/bin/yt-dlp"
FFMPEG = "/usr/bin/ffmpeg"
WARP_PROXY = "socks5://127.0.0.1:40000"
TOR_PROXY = "socks5h://127.0.0.1:9050"

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


def is_pornhub(url): return any(s in url for s in PORNHUB_SITES)
def is_tor_site(url): return any(s in url for s in TOR_SITES)
def is_instagram(url): return INSTAGRAM in url
def is_twitter(url): return any(s in url for s in TWITTER)
def is_tiktok(url): return TIKTOK in url


def clean_instagram_url(url: str) -> str:
    """Instagram links with '?igsh=...' (or any query) often fail the embed
    workaround — strip the query string so the reel/post downloads without login."""
    if INSTAGRAM not in url:
        return url
    if "?" in url:
        return url.split("?", 1)[0]
    return url


def detect_site(url: str) -> str:
    if is_pornhub(url):
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
    if is_pornhub(url):
        return []
    if is_tor_site(url):
        return ["--proxy", TOR_PROXY]
    return []


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


def make_caption(info: dict, lang: str = "fa") -> str:
    """Build a rich caption. Uses simple emoji labels per language."""
    title = info.get("title") or "بدون عنوان"
    lines = [f"🎬 **{title[:150]}**\n"]
    if info.get("uploader"):
        lines.append(f"👤 **{'آپلودر' if lang == 'fa' else 'Uploader'}:** {info['uploader']}")
    if info.get("upload_date"):
        lines.append(f"📅 **{'تاریخ' if lang == 'fa' else 'Date'}:** {info['upload_date']}")
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
        desc = info["description"].strip()[:200]
        if desc:
            lines.append(f"\n📝 {desc}")
    lines.append(f"\n🔗 [{'مشاهده در سایت' if lang == 'fa' else 'View source'}]({info.get('webpage_url', '')})")
    return "\n".join(lines)


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
            heights = sorted({int(v.get("height") or 0) for v in []})  # no heights from syndication
            return {
                "title": full.get("text", "")[:120] or "Tweet video",
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
        return {
            "title": full.get("text", "")[:120] or "Tweet",
            "uploader": f"{full.get('user_name')} @{full.get('user_screen')}",
            "webpage_url": url,
            "qualities": {},
            "sizes": {},
            "is_tw": True,
            "tw_type": full.get("type", "text"),
            "tw_full": full,
        }

    proxy = get_proxy_args(url)
    # -N 8: concurrent fragment downloads; --no-check-certificates for speed on some hosts
    cmd = [YTDLP, *proxy, "-J", "--no-warnings", "--skip-download",
           "--no-check-certificates", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout)[-300:]}
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
        }
    except Exception as e:
        return {"error": str(e)}


# ── Download ──────────────────────────────────────────────────
def download_song_mp3(title: str, artist: str, out_mp3: str) -> bool:
        """Download a full song (search YouTube by title+artist) as MP3, with cover embed."""
        import shutil
        d = os.path.dirname(out_mp3)
        base = os.path.basename(out_mp3)  # e.g. song_123_abc.mp3
        stem = base.rsplit(".", 1)[0]       # song_123_abc
        try:
            query = f"ytsearch1:{title} {artist} official audio"
            r = subprocess.run(
                ["/usr/local/bin/yt-dlp", "-f", "bestaudio/best", "--no-playlist",
                 "--no-check-certificates", "--extract-audio", "--audio-format", "mp3",
                 "--audio-quality", "0", "-o", os.path.join(d, stem), query],
                capture_output=True, text=True, timeout=200,
            )
            if r.returncode != 0:
                return False
            # yt-dlp produced <stem>.mp3 (and possibly thumbnail), find it
            cands = [os.path.join(d, f) for f in os.listdir(d)
                     if f.startswith(stem) and f.endswith(".mp3")]
            if not cands:
                return False
            src = cands[0]
            if src != out_mp3:
                shutil.move(src, out_mp3)
            return os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0
        except Exception as e:
            log.error("download_song_mp3: %s", e)
            return False


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
            ["/usr/local/bin/yt-dlp", "-f", "bestaudio/best", "--no-playlist",
             "--no-check-certificates", "-o", tmp, url],
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
        return igtw.download_twitter(url, quality, out_path)

    proxy = get_proxy_args(url)

    if is_audio:
        cmd = [YTDLP, *proxy, "-x", "--audio-format", "mp3", "-f", "bestaudio",
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
        cmd = [YTDLP, *proxy, "-f", fmt,
               "--merge-output-format", "mp4",
               "--no-check-certificates", "-N", "8",
               "--no-playlist",
               "-o", out_path, "--no-warnings", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
        if not ok:
            # yt-dlp may append .mp4/.mp3 if out_path has no proper ext
            for cand in (out_path + ".mp4", out_path + ".mp3", out_path + ".mkv", out_path + ".webm"):
                if os.path.exists(cand) and os.path.getsize(cand) > 0:
                    ok = True
                    break
        return ok
    except Exception as e:
        log.error("download error: %s", e)
        return False


def download_playlist(url: str, out_dir: str) -> int:
    """Download a whole playlist. Returns number of files downloaded."""
    proxy = get_proxy_args(url)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [YTDLP, *proxy, "-f", "best[height<=720]+bestaudio/best[height<=720]",
           "--merge-output-format", "mp4",
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
