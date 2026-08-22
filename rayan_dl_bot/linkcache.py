"""Link cache module for Rayan Downloader bot.
In-memory TTL cache: url -> (result_key, timestamp). 30 min TTL.
Thread-safe enough for asyncio single-loop usage; guarded with a lock for safety.
"""
import time
import threading

_TTL = 1800  # 30 minutes
_cache = {}  # url_lower -> {"ts": float, "data": dict}
_lock = threading.Lock()


def _norm(url: str) -> str:
    """Normalize a URL for cache-key purposes."""
    u = (url or "").strip().lower()
    # strip common tracking params noise
    for marker in ("?igsi=", "?_r=", "?s=", "?utm_", "&utm_", "?fbclid", "?ref"):
        i = u.find(marker)
        if i > 0:
            u = u[:i]
    return u


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
        if len(_cache) > 500:
            cutoff = time.time() - _TTL
            for k in [k for k, v in _cache.items() if v["ts"] < cutoff]:
                del _cache[k]
            if len(_cache) > 500:
                _cache.clear()
        _cache[key] = {"ts": time.time(), "data": data}


def stats() -> dict:
    with _lock:
        alive = sum(1 for v in _cache.values() if time.time() - v["ts"] <= _TTL)
        return {"entries": len(_cache), "alive": alive}
