"""Download queue for Rayan Downloader bot.

Design:
- ONE global pool of `max_concurrent_downloads` permits, granted to waiters
  in PRIORITY order: skip-lane jobs (referral credits, B1) first, then FIFO.
  Plain asyncio.Semaphore cannot do this — it wakes waiters strictly in
  arrival order, so a "skip the queue" feature would have no effect.
- Waiting counter gives exact queue position for user messages.
- Admin changes max_concurrent_downloads apply on the next grant/release
  (no swap dance needed — the pool is a counter, not an object).
"""
import asyncio
import heapq
import itertools

_free = 0              # permits available (synced to config on first use)
_sem_size = 0          # configured size we last synced to
# NOTE: both start at 0 so the FIRST _resize_if_needed() brings _free up to
# exactly max_concurrent_downloads. Starting _free nonzero here would double-
# count against the first resize and silently raise the real cap.
_waiting_total = 0     # jobs currently waiting on a permit
_heap = []             # (pri, seq, future) — pri 0 = skip lane, 1 = normal FIFO
_seq = itertools.count()
_per_user = {}         # uid -> running count


def _configured() -> int:
    try:
        from . import db
        v = int(db.get_config("max_concurrent_downloads", 2) or 2)
        return max(1, min(v, 6))
    except Exception:
        return 2


def _resize_if_needed():
    """Apply admin-side concurrency changes: shift _free by the delta."""
    global _free, _sem_size
    want = _configured()
    if want != _sem_size:
        _free += want - _sem_size
        _sem_size = want


def _grant_ready():
    """Grant free permits to the best-priority waiters (then oldest first)."""
    global _free
    while _free > 0 and _heap:
        _, _, fut = heapq.heappop(_heap)
        if fut.done():
            continue  # waiter was cancelled while queued — skip stale entry
        _free -= 1
        fut.set_result(None)


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
    granted the next free permit ahead of all normal-waiting jobs."""

    def __init__(self, uid: int, skip: bool = False):
        self.uid = uid
        self.pri = 0 if skip else 1

    async def wait(self) -> int:
        """Acquire a permit; returns queue position we waited at."""
        global _waiting_total
        _resize_if_needed()
        pos_at_arrival = _waiting_total  # position snapshot before we join the wait
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(_heap, (self.pri, next(_seq), fut))
        _waiting_total += 1
        _grant_ready()  # a permit may be free right now (or we jumped the line)
        try:
            await fut
        finally:
            _waiting_total = max(0, _waiting_total - 1)
        _per_user[self.uid] = _per_user.get(self.uid, 0) + 1
        return pos_at_arrival

    def done(self):
        _per_user[self.uid] = max(0, _per_user.get(self.uid, 1) - 1)
        if _per_user.get(self.uid) == 0:
            _per_user.pop(self.uid, None)

    async def __aenter__(self):
        await self.wait()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.done()
        global _free
        _free += 1
        _grant_ready()  # hand the freed permit to the next (highest-priority) waiter
        return False
