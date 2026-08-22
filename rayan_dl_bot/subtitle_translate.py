"""Translate .srt subtitles to a target language via MyMemory free API.

Strategy:
- Parse the SRT into cue blocks (index / timecode / text)
- Batch cues (~4500 chars per request, MyMemory limit 500) with numbering
  so translations stay aligned; retry single-cue on batch failure
- Reassemble with original timecodes → valid .srt

MyMemory: free anon ~5,000 chars/day (with 'de' email param ~50k/day).
Falls back gracefully: if translation fails, original text is kept.
"""
import logging
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("rayan-dl")

_CUE_RE = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL)

CONTACT_EMAIL = "rayan.downloader.bot@gmail.com"  # raises MyMemory daily quota


def parse_srt(text: str):
    """Return list of (index, timecode, [text lines])."""
    cues = []
    for m in _CUE_RE.finditer(text):
        idx, tc, body = m.group(1), m.group(2), m.group(3).strip()
        lines = [l for l in body.splitlines() if l.strip()]
        if lines:
            cues.append((idx, tc, lines))
    return cues


def _translate_batch(text: str, sl: str, tl: str) -> str | None:
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
        log.warning("mymemory batch: %s", e)
        return None


def json_loads(b):
    import json
    return json.loads(b)


def translate_srt_file(src_path: str, dst_path: str, target_lang: str = "fa",
                       source_lang: str = "en") -> bool:
    """Translate an .srt file in place-ish (writes dst_path). True on success."""
    try:
        raw = open(src_path, encoding="utf-8", errors="ignore").read()
    except OSError as e:
        log.error("translate_srt read: %s", e)
        return False
    cues = parse_srt(raw)
    if not cues:
        return False

    # Build batches of full cue texts joined by newline markers
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
        for attempt in range(3):
            res = _translate_batch(joined, source_lang, target_lang)
            if res and res.count(SEP) == n_sep:
                break
            time.sleep(1.2 * (attempt + 1))
            res = None
        if res and res.count(SEP) == n_sep:
            parts = [p.strip() for p in res.split(SEP)]
            translated.extend(parts)
        else:
            # fall back: translate each cue individually; keep original on fail
            for t in batch:
                one = _translate_batch(t, source_lang, target_lang)
                translated.append(one.strip() if one else t)
        time.sleep(0.6)  # be polite to the free API

    # Reassemble
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
