"""Pluggable translation-engine registry + chain executor (pure stdlib).

The admin panel (bot.py «🌍 موتور ترجمه») picks an ordered CHAIN of engines
and per-engine config (base URL / API key / model / email) stored in db config:

    tr_engine_chain          JSON array, e.g. ["gateway_ox", "mymemory"]
    tr_cfg_gateway_ox        {"base_url": ..., "model": ...}
    tr_cfg_mymemory          {"email": ...}
    tr_cfg_libre             {"base_url": ..., "api_key": ...}
    tr_cfg_custom_openai     {"base_url": ..., "api_key": ..., "model": ...}

translate_via_chain() runs the engines in order; the first engine that
produces a fully-aligned batch wins. On failure it logs and falls through to
the next engine; when ALL fail it returns None and the caller sends the
original srt with a notice.

Engines:
  gateway_ox      OpenAI-compatible LLM on a local gateway (default free)
  mymemory        free web API, ~1000 words/day/IP — budgeted + cached upstream
  libre           LibreTranslate (libretranslate.com needs an API key; any
                  self-hosted base URL works without one)
  custom_openai   ANY OpenAI-compatible endpoint the admin supplies

LLM gateways differ in streaming behaviour: ox-alpha-combo answers with one
clean JSON body, gemini-fallback-combo always answers with SSE chunks — both
are handled by the same parser.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("rayan-dl")

# ── Limits ──────────────────────────────────────────────────────
ENGINE_TIMEOUT_S = 30      # per batch request, per engine
CHAIN_WALL_CLOCK_S = 180   # whole-translation cap; overdue → stop trying engines
BATCH_CUES = 20            # cues per batch (SEP-marker alignment check below)
SEP = " ||| "

# Budgeted engines burn a shared daily quota — subtitle_translate tracks their
# spend. Unlimited engines (gateways) never touch the counter.
BUDGETED_ENGINES = {"mymemory"}

DEFAULT_GATEWAY_BASE = "http://localhost:20128/v1"
DEFAULT_GATEWAY_MODEL = "ox-alpha-combo"

# ── Registry ────────────────────────────────────────────────────
# label_fa shows in the panel; `needs` lists which cfg fields each engine
# requires (the panel only renders those fields).
ENGINES = {
    "gateway_ox": {
        "label_fa": "🧠 هوش مصنوعی لوکال (رایگان)",
        "needs": ["base_url", "model"],
        "defaults": {"base_url": DEFAULT_GATEWAY_BASE, "model": DEFAULT_GATEWAY_MODEL},
    },
    "mymemory": {
        "label_fa": "🌐 MyMemory (رایگان با سقف روزانه)",
        "needs": ["email"],
        "defaults": {"email": "rayan.downloader.bot@gmail.com"},
    },
    "libre": {
        "label_fa": "🕊 LibreTranslate",
        "needs": ["base_url", "api_key"],
        "defaults": {"base_url": "https://libretranslate.com"},
    },
    "custom_openai": {
        "label_fa": "⚙️ سرویس دلخواه (OpenAI-compatible)",
        "needs": ["base_url", "api_key", "model"],
        "defaults": {},
    },
}


class EngineError(Exception):
    """One engine failed — the chain executor tries the next engine."""


def _cfg_for(engine_id: str, db_mod=None) -> dict:
    """Merged cfg for one engine: db tr_cfg_<id> JSON over registry defaults."""
    merged = dict(ENGINES.get(engine_id, {}).get("defaults") or {})
    raw = None
    try:
        if db_mod is None:
            import db as db_mod
        raw = db_mod.get_config(f"tr_cfg_{engine_id}", None)
    except Exception as e:
        log.debug("tr cfg read %s: %s", engine_id, e)
    if raw:
        try:
            merged.update(json.loads(raw))
        except (ValueError, TypeError):
            log.warning("tr_cfg_%s not valid JSON: %r", engine_id, raw[:80])
    return {k: v for k, v in merged.items() if v}


def get_chain(db_mod=None):
    """Active ordered chain of engine ids (validated against the registry)."""
    raw = None
    try:
        if db_mod is None:
            import db as db_mod
        raw = db_mod.get_config("tr_engine_chain", None)
    except Exception as e:
        log.debug("chain read: %s", e)
    chain = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                chain = [e for e in parsed if e in ENGINES]
        except (ValueError, TypeError):
            pass
    return chain or ["gateway_ox", "mymemory"]


def save_chain(chain, db_mod=None) -> bool:
    """Persist an ordered chain; ids outside the registry are dropped."""
    clean = [e for e in (chain or []) if e in ENGINES]
    if not clean:
        return False
    try:
        if db_mod is None:
            import db as db_mod
        db_mod.set_config("tr_engine_chain", json.dumps(clean))
        return True
    except Exception as e:
        log.error("chain save: %s", e)
        return False


def set_engine_cfg(engine_id: str, key: str, value: str, db_mod=None) -> bool:
    """Upsert ONE field of tr_cfg_<engine_id> (panel edits field by field)."""
    if engine_id not in ENGINES or key not in ENGINES[engine_id]["needs"]:
        return False
    cfg = _cfg_for(engine_id, db_mod=db_mod)
    cfg[key] = value.strip()
    try:
        if db_mod is None:
            import db as db_mod
        db_mod.set_config(f"tr_cfg_{engine_id}", json.dumps(cfg))
        return True
    except Exception as e:
        log.error("tr cfg save %s.%s: %s", engine_id, key, e)
        return False


# ── HTTP helper ─────────────────────────────────────────────────
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # translation JSON is small; cap abuse/DoS


def _read_capped(resp) -> bytes:
    try:
        data = resp.read(MAX_RESPONSE_BYTES + 1)
    except TypeError:  # test doubles / file-like objects without a size arg
        data = resp.read()
    if len(data) > MAX_RESPONSE_BYTES:
        raise EngineError("translation response too large")
    return data


def _post_json(url: str, payload: dict | None = None, timeout: int = ENGINE_TIMEOUT_S):
    """POST JSON, return parsed body as dict. Raises EngineError on failure."""
    data = json.dumps(payload).encode() if payload is not None else b""
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(_read_capped(r).decode())
    except urllib.error.HTTPError as e:
        raise EngineError(f"HTTP {e.code}") from e
    except Exception as e:
        raise EngineError(str(e)[:120]) from e


def _get_json(url: str, timeout: int = ENGINE_TIMEOUT_S):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(_read_capped(r).decode())
    except urllib.error.HTTPError as e:
        raise EngineError(f"HTTP {e.code}") from e
    except Exception as e:
        raise EngineError(str(e)[:120]) from e


# ── OpenAI-compatible engines (gateway_ox, custom_openai) ───────
_SSE_LINE = re.compile(r"^data:\s*(\{.*\})")


def _llm_chat(base_url: str, model: str, prompt: str,
              api_key: str = "", timeout: int = ENGINE_TIMEOUT_S) -> str:
    """One chat completion. Handles BOTH response styles: a plain JSON body
    and SSE `data: {...}` chunk streams (gemini-style gateways stream even
    when stream:false is requested — content is reassembled from deltas)."""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = {"model": model, "stream": False, "temperature": 0,
               "messages": [{"role": "user", "content": prompt}]}
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(body, timeout=timeout) as r:
            raw = _read_capped(r).decode()
    except urllib.error.HTTPError as e:
        raise EngineError(f"HTTP {e.code}") from e
    except Exception as e:
        raise EngineError(str(e)[:120]) from e

    text = raw.lstrip()
    if text.startswith("data:"):                      # SSE stream
        parts = []
        for line in text.splitlines():
            m = _SSE_LINE.match(line.strip())
            if not m:
                continue
            try:
                chunk = json.loads(m.group(1))
            except ValueError:
                continue
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content") or (
                    choice.get("message") or {}).get("content")
                if piece:
                    parts.append(piece)
        out = "".join(parts)
        if not out:
            raise EngineError("empty SSE stream")
        return out
    # plain JSON body
    d = json.loads(text)
    choices = d.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content")
               if choices else None)
    if not content:
        raise EngineError("no content in response")
    return content


_PROMPT = (
    "Translate every line of the subtitle text below from {src} to {dst}. "
    "Rules: translate EACH numbered item SEPARATELY, keep the numbering and "
    "the separators exactly, do NOT merge or drop items, output ONLY the "
    "translations.\n\n{text}")


def _llm_translate(engine_id: str, texts: list, src: str, dst: str,
                   cfg: dict) -> list:
    """Shared LLM path: number the lines, ask for the same structure back."""
    base_url = (cfg.get("base_url") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not base_url or not model:
        missing = [k for k in ("base_url", "model") if not cfg.get(k)]
        raise EngineError(f"missing config: {', '.join(missing)}")
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = _PROMPT.format(src=src, dst=dst, text=numbered)
    out = _llm_chat(base_url, model, prompt, api_key=cfg.get("api_key", ""))
    # Accept either "1. ترجمه" lines or SEP-separated output; align by count.
    got = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^\d{1,3}[.)]\s*(.+)$", ln)
        got.append(m.group(1).strip() if m else ln)
    if len(got) != len(texts):
        raise EngineError(f"alignment failed ({len(got)} vs {len(texts)})")
    return got


# ── E1 gateway_ox ───────────────────────────────────────────────
def _translate_gateway_ox(texts, src, dst, cfg):
    return _llm_translate("gateway_ox", texts, src, dst, cfg)


# ── E2 mymemory (moved from subtitle_translate._translate_batch) ─
def _translate_mymemory(texts, src, dst, cfg):
    email = (cfg.get("email") or "").strip()
    joined = SEP.join(texts)
    q = urllib.parse.urlencode({
        "q": joined, "langpair": f"{src}|{dst}",
        **({"de": email} if email else {})})
    u = f"https://api.mymemory.translated.net/get?{q}"
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=ENGINE_TIMEOUT_S) as r:
            d = json.loads(_read_capped(r).decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise EngineError("429 quota") from e
        raise EngineError(f"HTTP {e.code}") from e
    except Exception as e:
        raise EngineError(str(e)[:120]) from e
    out = (d.get("responseData") or {}).get("translatedText")
    if not out or "MYMEMORY WARNING" in out or "QUERY LENGTH LIMIT" in out:
        raise EngineError("quota warning / empty response")
    parts = [p.strip() for p in out.split(SEP)]
    if len(parts) != len(texts) or any(not p for p in parts):
        raise EngineError(f"alignment failed ({len(parts)} vs {len(texts)})")
    return parts


# ── E3 libre ────────────────────────────────────────────────────
def _translate_libre(texts, src, dst, cfg):
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise EngineError("missing config: base_url")
    payload = {"q": texts, "source": src, "target": dst, "format": "text"}
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        payload["api_key"] = api_key
    d = _post_json(f"{base_url}/translate", payload)
    translated = d.get("translatedText")
    if isinstance(translated, str):       # some hosts collapse a list to one str
        translated = [translated]
    if not isinstance(translated, list) or len(translated) != len(texts):
        raise EngineError("bad response shape")
    return [t.strip() for t in translated]


# ── E4 custom_openai ────────────────────────────────────────────
def _translate_custom_openai(texts, src, dst, cfg):
    return _llm_translate("custom_openai", texts, src, dst, cfg)


_TRANSLATORS = {
    "gateway_ox": _translate_gateway_ox,
    "mymemory": _translate_mymemory,
    "libre": _translate_libre,
    "custom_openai": _translate_custom_openai,
}


def engine_label_fa(engine_id: str) -> str:
    return ENGINES.get(engine_id, {}).get("label_fa", engine_id)


# ── Chain executor ──────────────────────────────────────────────
def make_batches(texts: list, batch_cues: int = BATCH_CUES) -> list:
    """Split cue texts into batches of ~batch_cues (never an empty batch)."""
    batches = []
    for i in range(0, len(texts), max(1, batch_cues)):
        batches.append(texts[i:i + max(1, batch_cues)])
    return batches


def translate_batch_via_chain(texts: list, src: str, dst: str,
                              deadline: float | None = None,
                              db_mod=None, chain=None):
    """Translate ONE batch through the chain.

    Returns (translated_list_or_None, used_engine_id_or_None).
    Each engine gets one attempt within its 30s timeout; failures are logged
    and the NEXT engine runs. When the wall-clock deadline has passed, no new
    engine is started. None/None means every engine failed."""
    if deadline is None:
        deadline = time.monotonic() + CHAIN_WALL_CLOCK_S
    if chain is None:
        chain = get_chain(db_mod=db_mod)
    results = []
    for engine_id in chain:
        fn = _TRANSLATORS.get(engine_id)
        if not fn:
            continue
        if time.monotonic() > deadline:
            log.warning("translation chain: wall clock cap hit before %s", engine_id)
            break
        t0 = time.monotonic()
        try:
            out = fn(texts, src, dst, _cfg_for(engine_id, db_mod=db_mod))
            dt = time.monotonic() - t0
            if isinstance(out, list) and len(out) == len(texts) \
                    and all(isinstance(t, str) and t for t in out):
                log.info("engine %s OK (%s cues, %.1fs)", engine_id, len(texts), dt)
                return out, engine_id
            raise EngineError(f"bad result shape from {engine_id}")
        except Exception as e:
            dt = time.monotonic() - t0
            log.warning("engine %s FAILED after %.1fs: %s — trying next",
                        engine_id, dt, str(e)[:100])
            results.append((engine_id, False, str(e)[:60]))
    log.warning("translation chain exhausted: all %s engine(s) failed", len(chain))
    return None, None


def test_engine(engine_id: str, sample: str = "Hello world. Subtitle line.",
                src: str = "en", dst: str = "fa", db_mod=None) -> tuple:
    """Panel «تست اتصال»: run ONE engine on a fixed sample sentence.
    Returns (ok: bool, detail: str) — detail is the translation or the error."""
    fn = _TRANSLATORS.get(engine_id)
    if not fn:
        return False, "unknown engine"
    try:
        out = fn([sample], src, dst, _cfg_for(engine_id, db_mod=db_mod))
        return True, (out[0] if out else "")
    except Exception as e:
        return False, str(e)[:120]
