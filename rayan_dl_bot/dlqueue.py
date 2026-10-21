"""Download queue for Rayan Downloader bot.

Design:
- ONE global pool of `max_concurrent_downloads` permits, granted to waiters
  in PRIORITY order: skip-lane jobs (referral credits, B1) first, then FIFO.
  Plain asyncio.Semaphore cannot do this — it wakes waiters strictly in
  arrival order, so a "skip the queue" feature would have no effect.
- A per-user cap stops one person (or one forwarded album) from occupying
  every permit and starving everyone else.
- Waiting counter gives exact queue position for user messages.
- Admin changes apply on the next grant/release (the pool is a counter, not an
  object); the value is cached briefly so the event loop never does a SQLite
  read on every acquire/release.
"""
import asyncio
import heapq
import itertools
import time

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_PER_USER = 2
HARD_MAX_CONCURRENT = 6

_free = 0              # permits available (synced to config on first use)
_sem_size = 0          # configured size we last synced to
# NOTE: both start at 0 so the FIRST _resize_if_needed() brings _free up to
# exactly max_concurrent_downloads. Starting _free nonzero here would double-
# count against the first resize and silently raise the real cap.
_waiting_total = 0     # jobs currently waiting on a permit
_heap = []             # (pri, seq, future, uid) — pri 0 = skip lane, 1 = FIFO
_seq = itertools.count()
_per_user = {}         # uid -> running count

_cfg_cache = (0.0, DEFAULT_MAX_CONCURRENT, DEFAULT_PER_USER)
_CFG_TTL = 2.0


def _configured() -> int:
    """Global and per-user caps, cached for a moment (DB hit only on refresh)."""
    global _cfg_cache
    now = time.monotonic()
    if now - _cfg_cache[0] < _CFG_TTL:
        return _cfg_cache[1]
    try:
        from . import db
        v = int(db.get_config("max_concurrent_downloads", DEFAULT_MAX_CONCURRENT)
                or DEFAULT_MAX_CONCURRENT)
        per_user = int(db.get_config("max_concurrent_per_user", DEFAULT_PER_USER)
                       or DEFAULT_PER_USER)
    except Exception:
        v, per_user = DEFAULT_MAX_CONCURRENT, DEFAULT_PER_USER
    v = max(1, min(v, HARD_MAX_CONCURRENT))
    per_user = max(1, min(per_user, v))
    _cfg_cache = (now, v, per_user)
    return v


def _per_user_limit() -> int:
    if time.monotonic() - _cfg_cache[0] >= _CFG_TTL:
        _configured()
    return _cfg_cache[2]


def _resize_if_needed():
    """Apply admin-side concurrency changes: shift _free by the delta."""
    global _free, _sem_size
    want = _configured()
    if want != _sem_size:
        _free += want - _sem_size
        _sem_size = want


def _grant_ready():
    """Grant free permits to the best eligible waiters (priority, then FIFO).

    A waiter whose user is at the per-user cap is held back without losing its
    place, so a single user's album cannot monopolise the pool.
    """
    global _free, _waiting_total
    limit = _per_user_limit()
    pending = []
    while _heap:
        item = heapq.heappop(_heap)
        if not item[2].done():  # drop waiters cancelled while queued
            pending.append(item)
    # Choose by (skip-lane first, fewest running jobs for that user, arrival).
    # Plain heap order would let one user's long batch refill its own permits
    # forever while everyone else waits (head-of-line starvation).
    while _free > 0 and pending:
        eligible = [it for it in pending if _per_user.get(it[3], 0) < limit]
        if not eligible:
            break
        best = min(eligible, key=lambda it: (it[0], _per_user.get(it[3], 0), it[1]))
        pending.remove(best)
        _free -= 1
        _per_user[best[3]] = _per_user.get(best[3], 0) + 1
        _waiting_total = max(0, _waiting_total - 1)
        best[2].set_result(None)
    for item in pending:
        heapq.heappush(_heap, item)


def queue_position() -> int:
    """How many jobs are waiting (not yet running)."""
    return _waiting_total


def running_for(uid: int) -> int:
    return _per_user.get(uid, 0)


def total_running() -> int:
    return sum(_per_user.values())


class QueueSlot:
    """async-with slot: waits for a download permit, tracks per-user usage.

    skip=True (referral credit spent) puts the job in the fast lane: it is
    granted the next free permit ahead of all normal-waiting jobs.
    """

    def __init__(self, uid: int, skip: bool = False):
        self.uid = uid
        self.pri = 0 if skip else 1
        self._acquired = False

    async def wait(self) -> int:
        """Acquire a permit; returns queue position we waited at."""
        global _waiting_total, _free
        _resize_if_needed()
        pos_at_arrival = _waiting_total  # position snapshot before we join the wait
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(_heap, (self.pri, next(_seq), fut, self.uid))
        _waiting_total += 1
        _grant_ready()  # a permit may be free right now (or we jumped the line)
        try:
            await fut
        except BaseException:
            # Cancelled while queued: nothing was taken. Cancelled in the tiny
            # window after the permit was granted: give it back, otherwise the
            # pool would shrink by one on every interrupt.
            if fut.done() and not fut.cancelled():
                if not self._acquired:
                    self._release_permit()
            else:
                # Still queued: stop counting it as waiting. The cancelled
                # future is dropped from the heap on the next grant pass.
                _waiting_total = max(0, _waiting_total - 1)
            raise
        self._acquired = True
        return pos_at_arrival

    def _release_permit(self):
        global _free
        self.done()
        _free += 1
        _grant_ready()

    def done(self):
        _per_user[self.uid] = max(0, _per_user.get(self.uid, 1) - 1)
        if _per_user.get(self.uid) == 0:
            _per_user.pop(self.uid, None)

    async def __aenter__(self):
        await self.wait()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._acquired:
            self._acquired = False
            self._release_permit()
        return False
