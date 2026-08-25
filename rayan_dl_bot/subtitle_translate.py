"""Translate .srt subtitles to a target language via MyMemory free API.

Strategy:
- Parse the SRT into cue blocks (index / timecode / text)
- Batch cues (~4500 chars per request, MyMemory limit 500) with numbering
  so translations stay aligned; reassemble with original timecodes → valid .srt

Hardening after the 2026-08-25 429 storm (dozens of "mymemory: HTTP Error 429"
in one minute — the old cue-by-cue fallback multiplied the damage):
- DAILY BUDGET: words are estimated BEFORE any call and checked against a
  persisted per-day counter (db config key mymemory_words_used_<date>,
  budget 900). Over budget → skip translation entirely, caller sends the
  original srt with a one-line notice.
- BACKOFF: on the FIRST 429 sleep 30s once and retry that batch once; a
  SECOND 429 aborts the WHOLE translation (BudgetExhausted) — no per-cue storm.
- CACHE: translated srt is cached by (video_id, lang) hash in cache/ for 7
  days — repeat requests are instant and free.

MyMemory: free anon ~5,000 chars/day (with 'de' email param ~50k/day).
"""
import hashlib
import logging
import os
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("rayan-dl")

_CUE_RE = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL)

CONTACT_EMAIL = "rayan.downloader.bot@gmail.com"  # raises MyMemory daily quota

# ── Daily word budget (C1: free tier ≈1000 words/day/IP — stay under it) ──
DAILY_WORD_BUDGET = 900
_BUDGET_KEY_PREFIX = "mymemory_words_used_"

# ── Translation cache ─────────────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_TTL_S = 7 * 24 * 3600


class BudgetExhausted(Exception):
    """Raised when MyMemory is out of budget for today (or hard-429'd).
    Caller must fall back to the ORIGINAL srt + notice, never retry."""


def _today_key() -> str:
    return f"{_BUDGET_KEY_PREFIX}{time.strftime('%Y-%m-%d')}"


def budget_words_used(db_mod=None) -> int:
    """Words already spent today (from the db-backed counter)."""
    try:
        if db_mod is None:
            import db as db_mod  # flat import, same dir on sys.path
        return int(db_mod.get_config(_today_key(), 0) or 0)
    except Exception as e:
        log.warning("budget read: %s", e)
        return 0  # fail open — counter trouble must not crash translation


def budget_add_words(n: int, db_mod=None) -> None:
    """Add n words to today's spent counter (call AFTER each batch request)."""
    try:
        if db_mod is None:
            import db as db_mod
        db_mod.set_config(_today_key(), budget_words_used(db_mod) + max(0, n))
    except Exception as e:
        log.error("budget add: %s", e)


def estimate_words(cues) -> int:
    """Rough word count of cue texts — used to PREDICT spend before calling.
    cues: list of (idx, timecode, [lines])."""
    total = 0
    for _, _, lines in cues or []:
        for line in lines:
            total += len(line.split())
    return total


def budget_has_room(estimated_words: int, db_mod=None, used=None) -> bool:
    """True when estimated_words fits inside what's left of today's budget.
    `used` overrides the stored counter (tests); db_mod injectable too."""
    if used is None:
        used = budget_words_used(db_mod)
    try:
        used = int(used)
    except (TypeError, ValueError):
        used = 0
    return (used + max(0, estimated_words)) <= DAILY_WORD_BUDGET


def cache_path(video_id: str, lang: str) -> str:
    """Stable cache file path for (video_id, target lang)."""
    h = hashlib.sha1(f"{video_id}|{lang}".encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"srt_{h}.srt")


def cache_get(video_id: str, lang: str):
    """Return the cached translated srt path, or None (missing/expired)."""
    p = cache_path(video_id, lang)
    try:
        if os.path.exists(p) and time.time() - os.path.getmtime(p) <= CACHE_TTL_S:
            return p
        if os.path.exists(p):
            os.remove(p)  # expired → drop so the dir doesn't grow forever
    except OSError as e:
        log.debug("srt cache get: %s", e)
    return None


def cache_put(video_id: str, lang: str, src_path: str) -> None:
    """Copy a freshly-translated srt into the cache (best-effort)."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        dst = cache_path(video_id, lang)
        with open(src_path, encoding="utf-8", errors="ignore") as fi, \
                open(dst + ".tmp", "w", encoding="utf-8") as fo:
            fo.write(fi.read())
        os.replace(dst + ".tmp", dst)  # atomic — no half-written cache hits
    except OSError as e:
        log.debug("srt cache put: %s", e)


def parse_srt(text: str):
    """Return list of (index, timecode, [text lines])."""
    cues = []
    for m in _CUE_RE.finditer(text):
        idx, tc, body = m.group(1), m.group(2), m.group(3).strip()
        lines = [l for l in body.splitlines() if l.strip()]
        if lines:
            cues.append((idx, tc, lines))
    return cues


class _RateLimited(Exception):
    """HTTP 429 from MyMemory — caller retries once, then aborts."""


def _is_http_429(e: BaseException) -> bool:
    return isinstance(e, urllib.error.HTTPError) and getattr(e, "code", 0) == 429


def _translate_batch(text: str, sl: str, tl: str) -> str | None:
    """One MyMemory request. Returns None on other failures; raises
    _RateLimited specifically on HTTP 429 (caller decides retry vs abort)."""
    q = urllib.parse.urlencode({"q": text, "langpair": f"{sl}|{tl}",
                                "de": CONTACT_EMAIL})
    u = f"https://api.mymemory.translated.net/get?{q}"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json_loads(r.read().decode())
        out = (d.get("responseData") or {}).get("translatedText")
        if out and "MYMEMORY WARNING" not in out:
            return out
        # quota warning embedded — still may contain usable text
        return out.split("QUERY LENGTH LIMIT")[0].strip() or None
    except Exception as e:
        if _is_http_429(e):
            raise _RateLimited(str(e)) from e
        log.warning("mymemory batch: %s", e)
        return None


def json_loads(b):
    import json
    return json.loads(b)


def translate_srt_file(src_path: str, dst_path: str, target_lang: str = "fa",
                       source_lang: str = "en",
                       video_id: str = "", db_mod=None) -> bool:
    """Translate an .srt file (writes dst_path). True on success.

    Raises BudgetExhausted when today's word budget is gone or MyMemory
    hard-429s twice — callers send the original srt with a short fa notice.
    Timecode preservation is untouched by all of this."""
    try:
        raw = open(src_path, encoding="utf-8", errors="ignore").read()
    except OSError as e:
        log.error("translate_srt read: %s", e)
        return False
    cues = parse_srt(raw)
    if not cues:
        return False

    # C3a: estimate the spend FIRST — over budget → abort before ANY call.
    est = estimate_words(cues)
    if not budget_has_room(est, db_mod=db_mod):
        log.info("mymemory budget exhausted (%s words used, need ~%s) — skipping translation",
                 budget_words_used(db_mod=db_mod), est)
        raise BudgetExhausted(f"need~{est}")

    # Build batches of full cue texts joined by separator markers
    SEP = " ||| "
    texts = [" ".join(lines) for _, _, lines in cues]
    batches, cur, cur_len = [], [], 0
    for t in texts:
        if cur_len + len(t) + len(SEP) > 4400 or len(cur) >= 80:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(t)
        cur_len += len(t) + len(SEP)
    if cur:
        batches.append(cur)

    translated = []
    for bi, batch in enumerate(batches):
        joined = SEP.join(batch)
        n_sep = len(batch) - 1
        res = None
        try:
            res = _translate_batch(joined, source_lang, target_lang)
            if not (res and res.count(SEP) == n_sep):
                # ONE polite retry after 30s (C3b) — covers a transient single
                # 429 / truncation. A SECOND 429 raises _RateLimited and aborts
                # the whole translation immediately: no cue-by-cue storm.
                time.sleep(30)
                res = _translate_batch(joined, source_lang, target_lang)
        except _RateLimited as first_429:
            try:
                time.sleep(30)  # single backoff, then exactly ONE retry
                res = _translate_batch(joined, source_lang, target_lang)
            except _RateLimited:
                raise BudgetExhausted(
                    f"hard 429 at batch {bi + 1}/{len(batches)}") from None
        if res and res.count(SEP) == n_sep:
            parts = [p.strip() for p in res.split(SEP)]
            translated.extend(parts)
        else:
            # misaligned/failed batch → keep ORIGINAL text for these cues;
            # NEVER fan out into per-cue calls (that's what caused C1).
            log.warning("mymemory batch %s/%s unusable — keeping originals", bi + 1, len(batches))
            translated.extend(batch)
        budget_add_words(len(" ".join(batch).split()), db_mod=db_mod)

    # Reassemble — original indices and timecodes, byte-for-byte structure
    out_lines = []
    for (idx, tc, _), tr in zip(cues, translated):
        out_lines.append(idx)
        out_lines.append(tc)
        out_lines.append(tr)
        out_lines.append("")
    try:
        with open(dst_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines))
        return True
    except OSError as e:
        log.error("translate_srt write: %s", e)
        return False
