"""Download queue for Rayan Downloader bot.

Simple, correct design:
- ONE global asyncio.Semaphore shared by every download (the old code made a
  fresh one per call, so the cap never actually applied — fixed here).
- Waiting counter gives exact queue position for user messages.
- Per-user concurrency cap (default 2) so a single user can't hog the queue.
- Admin changes max_concurrent_downloads → next acquire picks the new size.
"""
import asyncio

_sem: asyncio.Semaphore | None = None
_sem_size = 0
_waiting = 0                      # jobs currently waiting on the semaphore
_per_user = {}                    # uid -> running count
USER_CAP = 2


def _configured() -> int:
    try:
        from . import db
        v = int(db.get_config("max_concurrent_downloads", 2) or 2)
        return max(1, min(v, 6))
    except Exception:
        return 2


def _get_sem() -> asyncio.Semaphore:
    global _sem, _sem_size
    want = _configured()
    if _sem is None or want != _sem_size:
        # Swap in a new semaphore at the new size. Jobs holding permits from an
        # old semaphore release to that old one; steady state converges quickly.
        _sem = asyncio.Semaphore(want)
        _sem_size = want
    return _sem


def queue_position() -> int:
    """How many jobs are waiting (not yet running)."""
    return _waiting


def running_for(uid: int) -> int:
    return _per_user.get(uid, 0)


class QueueSlot:
    """async-with slot: waits for a download permit, tracks per-user usage."""

    def __init__(self, uid: int):
        self.uid = uid

    async def wait(self) -> int:
        """Acquire a permit; returns queue position we waited at (0 = ran immediately)."""
        global _waiting
        pos_at_arrival = _waiting  # position snapshot before we join the wait
        _waiting += 1
        try:
            await _get_sem().acquire()
            return pos_at_arrival
        finally:
            _waiting -= 1
            _per_user[self.uid] = _per_user.get(self.uid, 0) + 1

    def done(self):
        _per_user[self.uid] = max(0, _per_user.get(self.uid, 1) - 1)
        if _per_user.get(self.uid) == 0:
            _per_user.pop(self.uid, None)

    async def __aenter__(self):
        await self.wait()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.done()
        try:
            _get_sem().release()
        except Exception:
            pass
        return False


def total_running() -> int:
    return sum(_per_user.values())
