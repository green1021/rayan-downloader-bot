#!/usr/bin/env python3
"""
recognizer.py — Shazam-like music recognition for Rayan Downloader Bot.
Recognizes a song from an audio/video file or a video link, using TWO databases:
  1. Shazam (via shazamio, routed through the WARP SOCKS5 proxy — direct DC IP is 403'd)
  2. AcoustID (chromaprint/fpcalc) + MusicBrainz metadata (fallback)

Reliability strategy (why this module looks the way it does):
  - Transient network errors (Host unreachable / DNS / timeout) are RETRIED with
    backoff — before this, a single WARP hiccup meant the user got nothing.
  - Two fingerprint windows are queried CONCURRENTLY (intro + post-intro), so a
    spoken intro or a slow song start doesn't cost a second sequential pass.
  - ffprobe runs first: a file with no audio stream fails in <1s instead of
    burning ~40s of retries.
Results are cached by audio hash (SHA256 prefix) to save API quota.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.parse

try:  # package import (production) vs flat import (tests/scripts)
    from . import netguard
except ImportError:  # pragma: no cover
    import netguard

log = logging.getLogger("rayan-dl")

# The WARP SOCKS5 proxy — Shazam blocks our datacenter IP directly (403),
# but routes fine through WARP.
WARP_PROXY = "socks5://127.0.0.1:40000"
VENV311 = "/root/hermes-workspace/venv311/bin/python3"
FPCALC = "/usr/bin/fpcalc"
# AcoustID public demo key (shazamio-free fallback). Low quota; fine as backup.
ACOUSTID_KEY = "KcghHD5WT6"

# ── Recognition budgets (2GB-RAM box + impatient users) ───────
MAX_ANALYZE_BYTES = 90 * 1024 * 1024   # only analyze the first 90MB of a file
MAX_ANALYZE_SECONDS = 600              # ...and only its first 10 minutes
WINDOW_A = (0, 20)                     # intro window (seconds)
WINDOW_B = (25, 45)                    # post-intro window — queried in PARALLEL
LONG_FILE_SECONDS = 120                # beyond this, add a mid-file rescue window
SHAZAM_TIMEOUT = 55                    # per subprocess attempt
SHAZAM_RETRIES = 2                     # extra attempts after the first
SHAZAM_BACKOFF = (2, 5)                # seconds between retries
SHAZAM_BUDGET = 80                     # hard wall-clock ceiling for the Shazam phase
ACOUSTID_MIN_SCORE = 0.6               # below this AcoustID is noise, not an answer

# Errors worth retrying vs. a clean "this song isn't in the DB"
_TRANSIENT_RE = re.compile(
    r"unreachable|timeout|timed?.?(out|out)|temporar|connect|connection|resolve|"
    r"resolv|dns|ssl|reset|refused|broken|closed|eof|proxy|429|5\d\d|"
    r"rate.?limit|too many|blocked|bad gateway|service unavailable",
    re.I)


def to_mono_wav(audio_path: str, out_wav: str, seconds: int = 30, offset: int = 0,
                normalize: bool = True) -> bool:
    """Downmix audio to mono 16kHz WAV for fingerprinting.

    Improvements over the original:
    - offset: start N seconds in (intros/speech often kill recognition at 0:00)
    - normalize: loudnorm volume leveling (quiet/phone-recorded clips fail otherwise)
    """
    try:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if offset > 0:
            cmd += ["-ss", str(offset)]
        cmd += ["-i", audio_path, "-t", str(seconds),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
        if normalize:
            # Single-pass loudnorm to a sane level; cheap and helps a lot
            cmd[cmd.index("-c:a"):cmd.index("-c:a")] = [
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            ]
        cmd.append(out_wav)
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 0
    except Exception as e:
        log.error("to_mono_wav(%s): %s", audio_path, e)
        return False


def probe_audio(audio_path: str) -> dict:
    """ffprobe pre-check: does this file have an audio stream, and how long is it?

    Failing here costs ~0.2s instead of ~40s of doomed recognition attempts.
    Returns {"has_audio": bool, "duration": float}."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", audio_path],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return {"has_audio": False, "duration": 0}
        d = json.loads(r.stdout or "{}")
        has_audio = bool(d.get("streams"))
        try:
            duration = float((d.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return {"has_audio": has_audio, "duration": duration}
    except Exception as e:
        log.error("probe_audio(%s): %s", audio_path, e)
        # Unknown state — let the normal pipeline try rather than block the user
        return {"has_audio": True, "duration": 0}


# ── Shazam (primary) via WARP proxy ───────────────────────────
async def _shazam_attempt(audio_path: str) -> str:
    """ONE Shazam subprocess attempt. Returns raw stdout:
    '{json}' | 'NOMATCH' | 'ERROR:<msg>' | '' (crash/timeout)."""
    code = r'''
import asyncio, json, sys
async def main():
    try:
        import aiohttp
        from aiohttp_socks import ProxyConnector
        from shazamio import Shazam
        conn = ProxyConnector.from_url("WARP_PROXY_PLACEHOLDER")
        # Hard client-side timeout so a dead proxy can't hang past our budget
        timeout = aiohttp.ClientTimeout(total=45, connect=15)
        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
            shazam = Shazam(http_client=session)
            resp = await shazam.recognize(AUDIO_PATH_PLACEHOLDER)
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
    # Substitute placeholders safely (json.dumps escapes quotes/newlines → no code injection)
    # WARP_PROXY is a trusted module constant → raw substitution.
    # audio_path IS user-influenced → json.dumps escapes quotes/newlines safely.
    code = code.replace("WARP_PROXY_PLACEHOLDER", WARP_PROXY)
    code = code.replace("AUDIO_PATH_PLACEHOLDER", json.dumps(audio_path))

    proc = None
    try:
        # create_subprocess_exec (not to_thread+run) so a cancelled task KILLS the
        # child — needed because the losing window of a parallel pair must stop.
        proc = await asyncio.create_subprocess_exec(
            VENV311, "-c", code, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=SHAZAM_TIMEOUT)
        return (out or b"").decode("utf-8", "replace").strip()
    except asyncio.TimeoutError:
        log.warning("shazam attempt timed out after %ss (%s)", SHAZAM_TIMEOUT,
                    os.path.basename(audio_path))
        return ""
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("shazam attempt spawn (%s): %s", os.path.basename(audio_path), e)
        return ""
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def _shazam_recognize(audio_path: str, deadline: float | None = None) -> dict | None:
    """Recognize via Shazam with retry+backoff on TRANSIENT failures.

    A clean NOMATCH returns immediately (the song genuinely isn't there — no
    point hammering the API). Network/DNS/timeout errors retry up to
    SHAZAM_RETRIES times. `deadline` (loop.time()) caps total wall time."""
    attempts = SHAZAM_RETRIES + 1
    loop = asyncio.get_running_loop()
    last_reason = ""
    for attempt in range(1, attempts + 1):
        out = await _shazam_attempt(audio_path)
        kind, payload = _classify_shazam_out(out)
        if kind == "match":
            return payload
        if kind == "nomatch":
            return None
        # transient / permanent-error
        last_reason = payload or "empty output"
        if kind == "error":
            log.warning("shazam permanent error (%s): %s",
                        os.path.basename(audio_path), last_reason)
            return None
        if attempt < attempts and (deadline is None or loop.time() < deadline):
            delay = SHAZAM_BACKOFF[min(attempt - 1, len(SHAZAM_BACKOFF) - 1)]
            log.warning("shazam transient (%s), retry %d/%d in %ss: %s",
                        os.path.basename(audio_path), attempt, SHAZAM_RETRIES,
                        delay, last_reason)
            await asyncio.sleep(delay)
            continue
        break
    log.warning("shazam gave up on %s after %d attempts: %s",
                os.path.basename(audio_path), attempts, last_reason)
    return None


def _classify_shazam_out(out: str) -> tuple:
    """Map raw subprocess stdout to ('match', dict) | ('nomatch', None) |
    ('transient', reason) | ('error', reason)."""
    s = (out or "").strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            return "match", json.loads(s)
        except ValueError:
            return "transient", "malformed json"
    if s == "NOMATCH":
        return "nomatch", None
    if not s:
        # empty stdout = crash, kill or timeout → network/proxy trouble, retry
        return "transient", "no output"
    if s.startswith("ERROR"):
        msg = s.split(":", 1)[1].strip() if ":" in s else s
        if _TRANSIENT_RE.search(msg):
            return "transient", msg
        return "error", msg
    return "transient", s[:100]


async def _shazam_first_match(paths: list, deadline: float | None = None) -> dict | None:
    """Query several fingerprint windows CONCURRENTLY; first match wins and the
    losers are cancelled. Returns None only if every window came back empty."""
    if not paths:
        return None
    tasks = [asyncio.ensure_future(_shazam_recognize(p, deadline)) for p in paths]
    pending = set(tasks)
    winner = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending,
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    r = t.result()
                except Exception as e:
                    log.warning("shazam window task: %s", e)
                    r = None
                if r and not winner:
                    winner = r
            if winner:
                break
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return winner


# ── AcoustID + MusicBrainz (fallback) ─────────────────────────
def _fpcalc(audio_path: str) -> tuple | None:
    """Return (duration, fingerprint) via fpcalc, or None."""
    try:
        r = subprocess.run([FPCALC, "-json", audio_path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            return d.get("duration"), d.get("fingerprint")
    except Exception as e:
        log.error("fpcalc(%s): %s", os.path.basename(audio_path), e)
    return None


def _acoustid_lookup(duration, fingerprint) -> list:
    """Query AcoustID for recordings (title/artist/releasegroup). Returns list."""
    params = urllib.parse.urlencode({
        "client": ACOUSTID_KEY, "duration": duration,
        "fingerprint": fingerprint, "meta": "recordings+releasegroups",
    })
    try:
        d = netguard.fetch_json(
            f"https://api.acoustid.org/v2/lookup?{params}",
            max_bytes=4 * 1024 * 1024, timeout=30, total_timeout=40)
        if d.get("status") != "ok":
            log.warning("acoustid status=%s", d.get("status"))
            return []
        out = []
        for res in d.get("results", []):
            score = float(res.get("score", 0))
            for rec in res.get("recordings", [])[:3]:
                artists = ", ".join(a.get("name", "") for a in rec.get("artists", []))
                rg_id = ""
                for rg in rec.get("releasegroups", []) or []:
                    if rg.get("id"):
                        rg_id = rg["id"]
                        break
                out.append({"title": rec.get("title", ""), "artist": artists,
                            "score": score, "rg_id": rg_id})
        # best-score first, dedup identical recordings
        out.sort(key=lambda r: r["score"], reverse=True)
        seen, uniq = set(), []
        for r in out:
            k = (r["title"].lower(), r["artist"].lower())
            if k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        return uniq
    except Exception as e:
        log.error("acoustid lookup: %s", e)
        return []


def _coverart_url(rg_id: str) -> str:
    """Cover Art Archive front image for a releasegroup, verified reachable.
    Empty string when unavailable (never blocks recognition for long)."""
    # rg_id comes from a remote API response — only a MusicBrainz UUID shape
    # is allowed, so it cannot smuggle a different host/path into the URL.
    rg_id = re.sub(r"[^0-9a-fA-F-]", "", str(rg_id or ""))[:64]
    if not rg_id:
        return ""
    url = f"https://coverartarchive.org/release-group/{rg_id}/front-250"
    try:
        res = netguard.probe(url, method="GET", timeout=8, max_redirects=3)
        return url if 200 <= res.status < 300 else ""
    except netguard.NetGuardError as e:
        log.warning("coverart probe: %s", e)
        return ""
    except Exception as e:
        log.warning("coverart probe: %s", e)
        return ""


async def _acoustid_best(window_paths: list) -> dict | None:
    """Fingerprint every window and merge AcoustID results. Returns a result
    dict (matched/score/alternates) or None when below ACOUSTID_MIN_SCORE."""
    if not window_paths:
        return None
    fps = await asyncio.gather(*[asyncio.to_thread(_fpcalc, p) for p in window_paths])
    recs = []
    for fp in fps:
        if not fp or not fp[1]:
            continue
        recs.extend(await asyncio.to_thread(_acoustid_lookup, fp[0], fp[1]))
    if not recs:
        return None
    recs.sort(key=lambda r: r.get("score", 0), reverse=True)
    best = recs[0]
    if best.get("score", 0) < ACOUSTID_MIN_SCORE:
        log.info("acoustid best score %.2f < %.2f → ignoring",
                 best.get("score", 0), ACOUSTID_MIN_SCORE)
        return None
    # Candidates within 0.1 of the best are plausible alternatives
    alternates = [{"title": r["title"], "artist": r["artist"],
                   "score": round(r.get("score", 0), 2)}
                  for r in recs[1:]
                  if r.get("score", 0) >= best.get("score", 0) - 0.1][:2]
    q = urllib.parse.quote(f"{best['title']} {best['artist']}".strip())
    cover = ""
    if best.get("rg_id"):
        cover = await asyncio.to_thread(_coverart_url, best["rg_id"])
    return {
        "matched": True, "source": "acoustid",
        "title": best["title"], "artist": best["artist"],
        "album": "", "year": "", "label": "", "genre": "",
        "cover": cover, "key": "",
        "shazam_url": "https://www.shazam.com/search/" + q,
        "ytm_url": "https://music.youtube.com/search?q=" + q,
        "spotify_url": "https://open.spotify.com/search/" + q,
        "score": round(best.get("score", 0), 2),
        "alternates": alternates,
    }


# ── Window preparation ────────────────────────────────────────
async def _prepare_windows(src: str, duration: float) -> list:
    """Extract fingerprint windows concurrently. Long files get TWO parallel
    windows (intro + post-intro) so a slow/spoken start doesn't cost another
    sequential pass; short files get one."""
    jobs = []          # (tag, offset, seconds)
    dur = int(duration or 0)
    if dur > 30:
        jobs.append(("a", WINDOW_A[0], WINDOW_A[1]))
        b_off = min(WINDOW_B[0], max(1, dur - WINDOW_B[1] - 1))
        b_len = min(WINDOW_B[1] - WINDOW_B[0], max(0, dur - b_off))
        if b_off > 0 and b_len >= 5:
            jobs.append(("b", b_off, b_len))
    else:
        jobs.append(("a", 0, 30))

    async def _one(tag, offset, seconds):
        out = f"{src}.win{tag}.wav"
        ok = await asyncio.to_thread(to_mono_wav, src, out, seconds, offset, True)
        return (out, ok)

    made = await asyncio.gather(*[_one(t, o, s) for t, o, s in jobs])
    paths = []
    for out, ok in made:
        if ok:
            paths.append(out)
        else:
            log.warning("window extract failed: %s", out)
    return paths


async def _safe_progress(progress, stage: str) -> None:
    """Call the bot-supplied progress hook; never let UI feedback break recognition."""
    if not progress:
        return
    try:
        res = progress(stage)
        if asyncio.iscoroutine(res):
            await res
    except Exception as e:
        log.debug("progress hook: %s", e)


# ── Public API ────────────────────────────────────────────────
async def recognize_audio(audio_path: str, cache=True, progress=None) -> dict:
    """Recognize a song from an audio/video file. Returns:
       {matched, title, artist, source, error, alternates=[], ...}

    Strategy:
      0. ffprobe pre-check (no audio stream → fail in <1s with a clear error)
      1. Shazam on TWO windows in parallel (0-20s + 25-45s); first match wins,
         transient network errors retried with backoff
      2. Very long files: one extra mid-file window, raced against step 3
      3. AcoustID fallback on the SAME windows (score ≥ 0.6 counts)
    Caches by audio hash. `progress(stage)` gets called with coarse stage names
    so the bot can show live feedback."""
    if not os.path.exists(audio_path):
        return {"matched": False, "error": "file not found"}

    fhash = await asyncio.to_thread(_get_hash, audio_path)
    if cache:
        cached = await asyncio.to_thread(_db_cache_get, fhash)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                log.warning("corrupt shazam cache entry, re-recognizing")

    await _safe_progress(progress, "probe")
    probe = await asyncio.to_thread(probe_audio, audio_path)
    if not probe.get("has_audio"):
        log.warning("recognize_audio: no audio stream in %s", audio_path)
        return {"matched": False, "error": "no_audio_stream"}
    duration = float(probe.get("duration") or 0)
    try:
        size = os.path.getsize(audio_path)
    except OSError:
        size = 0
    if size > MAX_ANALYZE_BYTES or duration > MAX_ANALYZE_SECONDS:
        log.warning("recognize_audio: big file (%.1fMB / %.0fs) — analyzing "
                    "first %ds only", size / 1048576, duration, MAX_ANALYZE_SECONDS)

    await _safe_progress(progress, "listen")
    windows = await _prepare_windows(audio_path, duration)
    if not windows:
        return {"matched": False, "error": "audio convert failed"}

    loop = asyncio.get_running_loop()
    deadline = loop.time() + SHAZAM_BUDGET
    result = await _shazam_first_match(windows, deadline)
    if result:
        # _shazam_first_match returns the raw track payload — tag it here so the
        # caller always gets the same shape regardless of which source matched.
        result = {"matched": True, "source": "shazam", **result}

    if not result and duration > LONG_FILE_SECONDS:
        # Long file, both standard windows missed → race a mid-file window
        # against the AcoustID fallback so neither waits for the other.
        await _safe_progress(progress, "deep")
        mid = min(int(duration * 0.5), MAX_ANALYZE_SECONDS - 25)
        c_wav = f"{audio_path}.winc.wav"
        ok_c = await asyncio.to_thread(to_mono_wav, audio_path, c_wav, 20, mid, True)
        coros = [_acoustid_best(windows)]
        if ok_c:
            coros.append(_shazam_recognize(c_wav, deadline))
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        acoustid_res = outcomes[0] if isinstance(outcomes[0], dict) else None
        deep_res = outcomes[1] if len(outcomes) > 1 and isinstance(outcomes[1], dict) else None
        if isinstance(acoustid_res, BaseException):
            log.warning("acoustid fallback: %s", acoustid_res)
            acoustid_res = None
        if isinstance(deep_res, BaseException):
            log.warning("mid-file shazam: %s", deep_res)
            deep_res = None
        result = deep_res or acoustid_res
    elif not result:
        await _safe_progress(progress, "fallback")
        try:
            result = await _acoustid_best(windows)
        except Exception as e:
            log.warning("acoustid fallback: %s", e)
            result = None

    for p in windows:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.remove(f"{audio_path}.winc.wav")
    except OSError:
        pass

    if result:
        result.setdefault("alternates", [])
        if cache:
            await asyncio.to_thread(_db_cache_set, fhash, result)
        return result
    return {"matched": False, "error": "no match"}


# NOTE: helpers below are defined by the bot database module at runtime.
def _get_hash(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read(16 * 1024 * 1024)).hexdigest()


def _db_cache_get(fhash):
    try:
        import db as _db
        return _db.shazam_cache_get(fhash)
    except Exception as e:
        log.warning("shazam cache read: %s", e)
        return None


def _db_cache_set(fhash, out):
    try:
        import db as _db
        _db.shazam_cache_set(fhash, json.dumps(out))
    except Exception as e:
        log.warning("shazam cache write: %s", e)
