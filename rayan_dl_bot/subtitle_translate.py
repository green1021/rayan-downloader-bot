"""Translate .srt subtitles through the PLUGGABLE ENGINE CHAIN.

History & strategy:
- Parse the SRT into cue blocks (index / timecode / text)
- Batch cues (~20 per request) and run each batch through the admin-chosen
  engine chain (translation_engines.translate_batch_via_chain): first engine
  that returns an aligned translation wins; failures fall through to the
  next engine; a batch whose chain fully fails keeps its ORIGINAL text.
- Reassemble with original indices/timecodes → valid .srt

Hardening after the 2026-08-25 429 storm (dozens of "mymemory: HTTP Error 429"
in one minute — the old cue-by-cue fallback multiplied the damage):
- DAILY BUDGET (budgeted engines only — MyMemory): words are estimated
  BEFORE any call; a budgeted engine is SKIPPED when the day's counter
  (db config key mymemory_words_used_<date>, budget 900) has no room.
  When NO engine in the chain is eligible → BudgetExhausted, caller sends
  the original srt with a one-line notice. Unlimited engines (gateways)
  never touch the counter and are never blocked by it.
- NO RETRY STORM: each engine gets ONE attempt per batch (30s timeout);
  there is deliberately no cue-by-cue or per-engine retry loop.
- CACHE: translated srt is cached by (video_id, lang) hash in cache/ for 7
  days — repeat requests are instant and free.
- WALL CLOCK: the whole file translation stops starting NEW engine attempts
  after 180s; remaining batches keep originals instead of hanging.
"""
import hashlib
import logging
import os
import re
import time

import translation_engines as te

log = logging.getLogger("rayan-dl")

_CUE_RE = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL)

# ── Daily word budget (C1: free tier ≈1000 words/day/IP — stay under it) ──
DAILY_WORD_BUDGET = 900
_BUDGET_KEY_PREFIX = "mymemory_words_used_"

WALL_CLOCK_S = te.CHAIN_WALL_CLOCK_S  # 180s cap for the WHOLE file

# ── Translation cache ─────────────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_TTL_S = 7 * 24 * 3600


class BudgetExhausted(Exception):
    """No engine in the chain may translate today (budgeted engines are out
    of budget and no unlimited engine is configured).
    Caller must fall back to the ORIGINAL srt + notice, never retry."""


class TranslationFailed(Exception):
    """Every eligible engine failed on EVERY batch — nothing was translated.
    Caller must fall back to the ORIGINAL srt + notice."""


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


def _eligible_engines(est_words: int, db_mod=None) -> list:
    """Chain entries that MAY run this file now: unlimited engines always;
    budgeted engines only when today's remaining budget fits the estimate."""
    chain = te.get_chain(db_mod=db_mod)
    eligible = []
    for eid in chain:
        if eid in te.BUDGETED_ENGINES and \
                not budget_has_room(est_words, db_mod=db_mod):
            log.info("skipping budgeted engine %s — daily budget can't fit ~%s words",
                     eid, est_words)
            continue
        eligible.append(eid)
    return eligible


def translate_srt_file(src_path: str, dst_path: str, target_lang: str = "fa",
                       source_lang: str = "en",
                       video_id: str = "", db_mod=None,
                       chain=None) -> bool:
    """Translate an .srt file (writes dst_path). True on success.

    Runs every batch through the admin-configured engine chain. Raises
    BudgetExhausted when only budgeted engines are configured and today's
    budget is gone; TranslationFailed when engines ran but produced nothing
    usable. Callers send the original srt with a short fa notice either way.
    Timecode preservation is untouched by all of this."""
    try:
        raw = open(src_path, encoding="utf-8", errors="ignore").read()
    except OSError as e:
        log.error("translate_srt read: %s", e)
        return False
    cues = parse_srt(raw)
    if not cues:
        return False

    # C3a (carried over): with ONLY budgeted engines in play, predict spend
    # BEFORE any call — over budget → abort without touching the network.
    est = estimate_words(cues)
    eligible = _eligible_engines(est, db_mod=db_mod) if chain is None \
        else [e for e in chain
              if e not in te.BUDGETED_ENGINES or budget_has_room(est, db_mod=db_mod)]
    if not eligible:
        log.info("no eligible engine (%s used today, need ~%s) — skipping translation",
                 budget_words_used(db_mod=db_mod), est)
        raise BudgetExhausted(f"need~{est}")

    deadline = time.monotonic() + WALL_CLOCK_S
    texts = [" ".join(lines) for _, _, lines in cues]
    batches = te.make_batches(texts)

    translated = []
    any_engine_ok = False
    for bi, batch in enumerate(batches):
        if time.monotonic() > deadline:
            log.warning("wall clock cap after %s/%s batches — keeping originals",
                        bi, len(batches))
            translated.extend(batch)
            continue
        out, used = te.translate_batch_via_chain(
            batch, source_lang, target_lang,
            deadline=deadline, db_mod=db_mod, chain=eligible)
        if out is None:
            # whole chain failed for THIS batch → keep originals, never fan
            # out per-cue (the C1 storm rule still holds).
            translated.extend(batch)
        else:
            any_engine_ok = True
            translated.extend(out)
            if used in te.BUDGETED_ENGINES:
                budget_add_words(len(" ".join(batch).split()), db_mod=db_mod)

    if not any_engine_ok:
        log.warning("all engines failed on every batch")
        raise TranslationFailed("chain produced nothing")

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
