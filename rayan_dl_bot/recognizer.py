#!/usr/bin/env python3
"""
recognizer.py — Shazam-like music recognition for Rayan Downloader Bot.
Recognizes a song from an audio/video file or a video link, using TWO databases:
  1. Shazam (via shazamio, routed through the WARP SOCKS5 proxy — direct DC IP is 403'd)
  2. AcoustID (chromaprint/fpcalc) + MusicBrainz metadata (fallback)

Files are fingerprinted against a cache (SHA256 of the audio) to avoid
burning API quota on repeat queries.
"""
import asyncio
import json
import hashlib
import logging
import os
import subprocess
import urllib.parse

log = logging.getLogger("rayan-dl")

# The WARP SOCKS5 proxy — Shazam blocks our datacenter IP directly (403),
# but routes fine through WARP.
WARP_PROXY = "socks5://127.0.0.1:40000"
VENV311 = "/root/hermes-workspace/venv311/bin/python3"
FPCALC = "/usr/bin/fpcalc"
# AcoustID public demo key (shazamio-free fallback). Low quota; fine as backup.
ACOUSTID_KEY = "KcghHD5WT6"


def to_mono_wav(audio_path: str, out_wav: str, seconds: int = 30) -> bool:
    """Downmix any audio to 30s mono 16kHz WAV (ideal for Shazam fingerprinting)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-t", str(seconds),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_wav],
            capture_output=True, text=True, timeout=120)
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:
        log.error("to_mono_wav: %s", e)
        return False


# ── Shazam (primary) via WARP proxy ───────────────────────────
async def _shazam_recognize(audio_path: str) -> dict | None:
    """Recognize via Shazam API through WARP proxy. Returns track dict or None."""
    code = r'''
import asyncio, json, sys
async def main():
    try:
        import aiohttp
        from aiohttp_socks import ProxyConnector
        from shazamio import Shazam
        conn = ProxyConnector.from_url("WARP_PROXY_PLACEHOLDER")
        async with aiohttp.ClientSession(connector=conn) as session:
            shazam = Shazam(http_client=session)
            resp = await shazam.recognize("AUDIO_PATH_PLACEHOLDER")
            data = json.loads(await resp.text())
            track = data.get("track")
            if track and data.get("matches"):
                title = track.get("title") or ""
                artist = track.get("subtitle") or ""
                q = (title + " " + artist).strip().replace(" ", "+")
                album = ""
                year = ""
                label = ""
                genre = ""
                for s in track.get("sections", []) or []:
                    if s.get("type") == "SONG":
                        for md in s.get("metadata", []) or []:
                            t = (md.get("title") or "").lower()
                            if t == "album" and not album:
                                album = md.get("text") or ""
                            elif t == "released" and not year:
                                year = md.get("text") or ""
                            elif t == "label" and not label:
                                label = md.get("text") or ""
                    elif s.get("type") == "ARTIST":
                        for md in s.get("metadata", []) or []:
                            t = (md.get("title") or "").lower()
                            if t == "genre" and not genre:
                                genre = md.get("text") or ""
                if not genre:
                    genre = (track.get("genres") or {}).get("primary") or ""
                print(json.dumps({
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "label": label,
                    "genre": genre,
                    "cover": (track.get("images") or {}).get("coverart"),
                    "key": track.get("key"),
                    "shazam_url": (track.get("share") or {}).get("href"),
                    "ytm_url": "https://music.youtube.com/search?q=" + q,
                    "spotify_url": "https://open.spotify.com/search/" + q,
                }))
            else:
                print("NOMATCH")
    except Exception as e:
        print("ERROR:" + str(e)[:200])
asyncio.run(main())
'''
    # Substitute placeholders (avoid f-string brace escaping)
    code = code.replace("WARP_PROXY_PLACEHOLDER", WARP_PROXY)
    code = code.replace("AUDIO_PATH_PLACEHOLDER", audio_path)
    try:
        r = subprocess.run([VENV311, "-c", code], capture_output=True, text=True, timeout=90)
        out = r.stdout.strip()
        if out.startswith("{") and out.endswith("}"):
            return json.loads(out)
        if out == "NOMATCH":
            return None
        log.warning("shazam no output: %s", out[:100])
        return None
    except Exception as e:
        log.error("shazam call: %s", e)
        return None


# ── AcoustID + MusicBrainz (fallback) ─────────────────────────
def _fpcalc(audio_path: str) -> tuple | None:
    """Return (duration, fingerprint) via fpcalc, or None."""
    try:
        r = subprocess.run([FPCALC, "-json", audio_path], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            return d.get("duration"), d.get("fingerprint")
    except Exception as e:
        log.error("fpcalc: %s", e)
    return None


def _acoustid_lookup(duration, fingerprint) -> list:
    """Query AcoustID for recordings (title/artist). Returns list of dicts."""
    import json as _json
    params = urllib.parse.urlencode({
        "client": ACOUSTID_KEY, "duration": duration,
        "fingerprint": fingerprint, "meta": "recordings+releasegroups",
    })
    try:
        sub = subprocess.run(
            ["curl", "-s", "--max-time", "30",
             f"https://api.acoustid.org/v2/lookup?{params}"],
            capture_output=True, text=True, timeout=40)
        d = _json.loads(sub.stdout)
        if d.get("status") != "ok":
            return []
        out = []
        for res in d.get("results", []):
            for rec in res.get("recordings", [])[:3]:
                artists = ", ".join(a.get("name", "") for a in rec.get("artists", []))
                out.append({"title": rec.get("title", ""), "artist": artists})
        return out
    except Exception as e:
        log.error("acoustid lookup: %s", e)
        return []


# ── Public API ────────────────────────────────────────────────
async def recognize_audio(audio_path: str, cache=True) -> dict:
    """Recognize a song from an audio file. Returns:
       {matched, title, artist, source, error, covers=[]}
    Uses Shazam first, then AcoustID. Caches by audio hash."""
    if not os.path.exists(audio_path):
        return {"matched": False, "error": "file not found"}
    fhash = _get_hash(audio_path)
    if cache:
        cached = _db_cache_get(fhash)
        if cached:
            return json.loads(cached)

    # 1) Shazam
    result = await _shazam_recognize(audio_path)
    if result:
        out = {"matched": True, "source": "shazam", **result}
        if cache:
            _db_cache_set(fhash, out)
        return out

    # 2) AcoustID fallback
    fp = _fpcalc(audio_path)
    if fp:
        duration, fingerprint = fp
        recs = _acoustid_lookup(duration, fingerprint)
        if recs:
            out = {"matched": True, "source": "acoustid",
                   "title": recs[0]["title"], "artist": recs[0]["artist"]}
            if cache:
                _db_cache_set(fhash, out)
            return out
    return {"matched": False, "error": "no match"}


# NOTE: helpers below are defined by the bot database module at runtime.
def _get_hash(p):
    return hashlib.sha256(open(p, "rb").read(16 * 1024 * 1024)).hexdigest()


def _db_cache_get(fhash):
    try:
        import db as _db
        return _db.shazam_cache_get(fhash)
    except Exception:
        return None


def _db_cache_set(fhash, out):
    try:
        import db as _db
        _db.shazam_cache_set(fhash, json.dumps(out))
    except Exception:
        pass