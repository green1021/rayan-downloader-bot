#!/usr/bin/env python3
"""
ig_tw.py — Instagram + Twitter downloaders for Rayan Downloader Bot
- Instagram: fetch /embed/ page, extract video_url (works logged-out for public posts)
- Twitter: yt-dlp with syndication fallback (works for public video tweets)
"""

import re
import json
import logging
import subprocess

log = logging.getLogger("rayan-igtw")

# Simple UA works best for Instagram embed (long Chrome/mobile UA triggers challenge)
IG_UA = "Mozilla/5.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

YTDLP = "/usr/local/bin/yt-dlp"


# ── Instagram ─────────────────────────────────────────────────
def extract_ig_video_url(post_url: str) -> str | None:
    """Fetch the embed page and extract the direct video URL (logged-out)."""
    embed_url = post_url.rstrip("/") + "/embed/"
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "25", "-A", IG_UA, embed_url],
            capture_output=True, text=True, timeout=30,
        )
        html = r.stdout
        if not html:
            return None

        # Robust: find ".mp4" then extend to the full URL (params end at \") — immune to escape layers
        end = html.find(".mp4")
        if end < 0:
            end = html.find(".mov")
        if end < 0:
            log.warning("ig: no media container in embed for %s (len=%d)", post_url, len(html))
            return None
        # Extend past .mp4 to include query params. HTML has layered escapes:
        # \"video_url\":\"https:\\\\/\\\\/...mp4?...\\u00253D\\u00253D&ccb=...
        # Stop at a bare quote or } but let backslash-escapes pass through.
        scan = end + 4
        while scan < len(html):
            ch = html[scan]
            if ch in ('"', "'", "}", "\n", " "):
                break
            if ch == "\\":
                # backslash starts an escape (\", \\, \/, \uXXXX) — skip it and the next char
                scan += 2
                continue
            scan += 1
        start = html.rfind("https:", 0, end)
        if start < 0:
            log.warning("ig: no https: before media in embed for %s", post_url)
            return None
        url = html[start:scan]
        # Unescape layered escapes: \\\/ -> \/ -> / and \\\\u -> \\u -> \u
        for _ in range(4):
            if "\\\\" in url:
                url = url.replace("\\\\", "\\")
            if "\\/" in url:
                url = url.replace("\\/", "/")
        # Decode unicode escapes like \u00253D -> %3D (repeat for layers)
        for _ in range(3):
            try:
                decoded = url.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
            except Exception:
                break
            if decoded == url:
                break
            url = decoded
        # Trim trailing junk
        url = re.sub(r'[\\"\'}\s]+$', "", url)
        if not url.startswith("https://"):
            return None
        return url
    except Exception as e:
        log.error("ig extract error: %s", e)
        return None


def download_instagram(url: str, out_path: str) -> bool:
    """Download an Instagram video via embed extraction + curl."""
    video_url = extract_ig_video_url(url)
    if not video_url:
        return False
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "300", "-A", UA, "-o", out_path, video_url],
            capture_output=True, text=True, timeout=320,
        )
        return os_path_exists(out_path) and os_path_size(out_path) > 0
    except Exception as e:
        log.error("ig download error: %s", e)
        return False


def os_path_exists(p):
    import os
    return os.path.exists(p)


def os_path_size(p):
    import os
    return os.path.getsize(p)


# ── Twitter ───────────────────────────────────────────────────
def _syndication(twid) -> dict | None:
    """Fetch full tweet JSON from Twitter's public syndication API (no login)."""
    import math
    twid = int(twid)
    token = str(int((twid / 1e15) * math.pi)).translate(str.maketrans('', '', '0.'))
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "15", "-A", "Googlebot",
             f"https://cdn.syndication.twimg.com/tweet-result?id={twid}&token={token}"],
            capture_output=True, text=True, timeout=20)
        if not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception as e:
        log.error("syndication error: %s", e)
        return None


def _fxtwitter(twid) -> dict | None:
    """Fetch tweet via fxtwitter API — gives FULL text (syndication truncates long tweets)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "15", f"https://api.fxtwitter.com/status/{twid}"],
            capture_output=True, text=True, timeout=20)
        if not r.stdout.strip():
            return None
        d = json.loads(r.stdout)
        tw = d.get("tweet")
        if not tw:
            return None
        return tw
    except Exception as e:
        log.error("fxtwitter error: %s", e)
        return None


def _extract_twid(url: str) -> str | None:
    """Extract numeric tweet ID from a twitter/x URL."""
    m = re.search(r'/(\d{15,20})(?:/|\?|$)', url)
    return m.group(1) if m else None


def get_twitter_full(url: str) -> dict:
    """Get COMPLETE tweet data via syndication: text, photos, video, quote, thread info.
    Returns dict with 'type': 'video'|'photos'|'text'|'quote'|'error'."""
    twid = _extract_twid(url)
    if not twid:
        # fallback to yt-dlp info
        return get_twitter_info(url)
    d = _syndication(twid)
    if not d or not d.get("text"):
        # fallback to yt-dlp (may still work for videos)
        return get_twitter_info(url)

    result = {
        "twid": twid,
        "text": d.get("text", ""),
        "user_name": d.get("user", {}).get("name", ""),
        "user_screen": d.get("user", {}).get("screen_name", ""),
        "created_at": d.get("created_at", ""),
        "favorite_count": d.get("favorite_count"),
        "reply_count": d.get("reply_count"),
        "retweet_count": d.get("retweet_count"),
        "lang": d.get("lang"),
        "webpage_url": url,
        "photos": [],
        "video_urls": [],
        "quoted": None,
        "in_reply_to": d.get("in_reply_to_status_id_str"),
        "conversation_id": d.get("conversation_id_str"),
        "is_tw": True,
    }

    # FULL TEXT: syndication truncates long tweets — try fxtwitter for the complete text
    fx = _fxtwitter(twid)
    if fx and fx.get("text"):
        fx_text = fx["text"]
        if len(fx_text) > len(result["text"]):
            result["text"] = fx_text
            result["favorite_count"] = fx.get("likes") or result.get("favorite_count")
            result["retweet_count"] = fx.get("retweets") or result.get("retweet_count")
            # fxtwitter may also carry media/quote info
            fx_media = fx.get("media") or {}
            if fx_media.get("all") and not result["photos"]:
                for m in fx_media["all"]:
                    if m.get("type") == "photo" and m.get("url"):
                        result["photos"].append(m["url"])
            if fx.get("quote") and not result["quoted"]:
                q = fx["quote"]
                result["quoted"] = {
                    "text": q.get("text", ""),
                    "user_name": q.get("author", {}).get("name", ""),
                    "user_screen": q.get("author", {}).get("screen_name", ""),
                }
            if not result["user_name"] or not result["user_screen"]:
                au = fx.get("author") or {}
                result["user_name"] = au.get("name") or result["user_name"]
                result["user_screen"] = au.get("screen_name") or result["user_screen"]

    # Photos
    for m in d.get("mediaDetails", []) or []:
        if m.get("type") == "photo" and m.get("media_url_https"):
            result["photos"].append(m["media_url_https"])

    # Video (prefer best mp4) — syndication uses {type, src} variants
    vid = d.get("video") or {}
    variants = vid.get("variants") or []
    mp4s = [v for v in variants
            if v.get("src") and ("mp4" in str(v.get("type", "")).lower() or "mp4" in v.get("src", ""))]
    if mp4s:
        result["video_urls"] = [v["src"] for v in mp4s]
        result["poster"] = vid.get("poster")
    else:
        # try mediaDetails video_info (yt-dlp style)
        for m in d.get("mediaDetails") or []:
            vi = m.get("video_info") or {}
            for v in vi.get("variants") or []:
                if v.get("content_type") == "video/mp4" and v.get("url"):
                    result["video_urls"].append(v["url"])
            if result["video_urls"]:
                break

    # Quoted tweet
    qt = d.get("quoted_tweet")
    if qt and qt.get("text"):
        result["quoted"] = {
            "text": qt.get("text", ""),
            "user_name": qt.get("user", {}).get("name", ""),
            "user_screen": qt.get("user", {}).get("screen_name", ""),
        }

    # Classify
    if result["video_urls"]:
        result["type"] = "video"
    elif result["photos"]:
        result["type"] = "photos"
    else:
        result["type"] = "text"
    if result["quoted"]:
        result["type"] = "quote"
    return result


def get_twitter_info(url: str) -> dict:
    """Fallback info via yt-dlp (force syndication — works for public video tweets)."""
    cmd = [YTDLP, "--extractor-args", "twitter:api=syndication",
           "-J", "--no-warnings", "--skip-download", "--no-check-certificates", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout)[-300:]}
        d = json.loads(r.stdout)
        heights = sorted({f.get("height") for f in d.get("formats", []) if f.get("height")}, reverse=True)
        heights = [h for h in heights if 144 <= h <= 2160]
        return {
            "title": d.get("title") or d.get("fulltitle") or "",
            "uploader": d.get("uploader") or d.get("channel"),
            "duration": d.get("duration"),
            "view_count": d.get("view_count"),
            "like_count": d.get("like_count"),
            "description": d.get("description"),
            "thumbnail": d.get("thumbnail"),
            "webpage_url": d.get("webpage_url") or url,
            "qualities": {str(h): None for h in heights},
            "sizes": {},
            "is_tw": True,
            "type": "video",
        }
    except Exception as e:
        return {"error": str(e)}


def download_twitter(url: str, quality: str, out_path: str) -> bool:
    """Download a Twitter video with yt-dlp (force syndication API — robust vs rate limits)."""
    fmt = f"bv[height<={quality}]+ba/b[height<={quality}]"
    if quality in ("audio", "best"):
        fmt = "best"
    cmd = [YTDLP, "--extractor-args", "twitter:api=syndication",
           "-f", fmt, "--merge-output-format", "mp4",
           "--no-check-certificates", "-N", "8", "--no-playlist",
           "-o", out_path, "--no-warnings", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = os_path_exists(out_path) and os_path_size(out_path) > 0
        if not ok:
            for cand in (out_path + ".mp4", out_path + ".mkv"):
                if os_path_exists(cand) and os_path_size(cand) > 0:
                    ok = True
                    break
        return ok
    except Exception as e:
        log.error("tw download error: %s", e)
        return False


def download_twitter_photo(url: str, out_path: str) -> bool:
    """Download a single Twitter photo (direct pbs.twimg.com URL)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "60", "-A", UA, "-o", out_path, url],
            capture_output=True, text=True, timeout=70)
        return os_path_exists(out_path) and os_path_size(out_path) > 0
    except Exception as e:
        log.error("tw photo error: %s", e)
        return False


def get_twitter_thread(url: str, max_items: int = 15) -> dict:
    """Fetch a full thread by walking in_reply_to backwards from the given tweet.
    Returns {'type': 'thread', 'tweets': [...], 'error': ...} — tweets are
    {twid, text, user_name, user_screen, photos, video_urls, quoted}."""
    twid = _extract_twid(url)
    if not twid:
        return {"type": "thread", "tweets": [], "error": "no id"}
    tweets = []
    seen = set()
    cur = twid
    for _ in range(max_items):
        if not cur or cur in seen:
            break
        seen.add(cur)
        d = _syndication(cur)
        if not d or not d.get("text"):
            if tweets:
                break  # end of accessible chain
            return {"type": "thread", "tweets": [], "error": "unreachable"}
        item = {
            "twid": cur,
            "text": d.get("text", ""),
            "user_name": d.get("user", {}).get("name", ""),
            "user_screen": d.get("user", {}).get("screen_name", ""),
            "photos": [m.get("media_url_https") for m in (d.get("mediaDetails") or [])
                       if m.get("type") == "photo" and m.get("media_url_https")],
            "video_urls": [],
            "quoted": None,
        }
        vid = d.get("video") or {}
        mp4s = [v for v in (vid.get("variants") or [])
                if v.get("src") and ("mp4" in str(v.get("type", "")).lower() or "mp4" in v.get("src", ""))]
        if mp4s:
            item["video_urls"] = [v["src"] for v in mp4s]
        else:
            for m in d.get("mediaDetails") or []:
                for v in (m.get("video_info") or {}).get("variants") or []:
                    if v.get("content_type") == "video/mp4" and v.get("url"):
                        item["video_urls"].append(v["url"])
                if item["video_urls"]:
                    break
        qt = d.get("quoted_tweet")
        if qt and qt.get("text"):
            item["quoted"] = {
                "text": qt.get("text", ""),
                "user_name": qt.get("user", {}).get("name", ""),
                "user_screen": qt.get("user", {}).get("screen_name", ""),
            }
        tweets.append(item)
        cur = d.get("in_reply_to_status_id_str")
    # Reverse: oldest first (thread order)
    tweets.reverse()
    return {"type": "thread", "tweets": tweets, "error": None}
