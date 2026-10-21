"""Link cache module for Rayan Downloader bot.
In-memory TTL cache: url -> (result_key, timestamp). 30 min TTL.
Thread-safe enough for asyncio single-loop usage; guarded with a lock for safety.
"""
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TTL = 1800  # 30 minutes
_MAX_ENTRIES = 500
_cache = {}  # normalized url -> {"ts": float, "data": dict}
_lock = threading.Lock()

# Tracking parameters that never change which media a URL points at.
_TRACKING = {"fbclid", "gclid", "igshid", "igsi", "ref", "ref_src", "ref_url",
             "share", "si", "spm", "s", "_r"}


def _norm(url: str) -> str:
    """Normalize a URL for cache-key purposes.

    Only the scheme and host are lower-cased: paths and query values can be
    case-sensitive (YouTube video IDs are), and lower-casing the whole URL made
    two different videos share one cache entry — i.e. the bot could re-send the
    wrong file.  Tracking parameters are dropped structurally instead of by
    substring search on the raw URL.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(query), ""))


def get(url: str):
    """Return cached data dict or None if absent/expired."""
    key = _norm(url)
    now = time.time()
    with _lock:
        ent = _cache.get(key)
        if not ent:
            return None
        if now - ent["ts"] > _TTL:
            del _cache[key]
            return None
        return ent["data"]


def put(url: str, data: dict, ttl: int = None) -> None:
    """Store data for url. data should be small & JSON-safe (no file handles)."""
    key = _norm(url)
    with _lock:
        # simple size guard
        if len(_cache) > _MAX_ENTRIES:
            cutoff = time.time() - _TTL
            for k in [k for k, v in _cache.items() if v["ts"] < cutoff]:
                del _cache[k]
            if len(_cache) > _MAX_ENTRIES:
                _cache.clear()
        _cache[key] = {"ts": time.time(), "data": data}


def clear() -> None:
    """Drop all entries (tests / admin maintenance)."""
    with _lock:
        _cache.clear()


def stats() -> dict:
    with _lock:
        alive = sum(1 for v in _cache.values() if time.time() - v["ts"] <= _TTL)
        return {"entries": len(_cache), "alive": alive}
