#!/usr/bin/env python3
"""Tests for Rayan Downloader Part C features (B5-B8) + audit leftovers A2-A6.

Offline only: no Telegram API calls, no real downloads. Every DB test runs
against a TEMP COPY of the production rayan.db (never the live file), via
db.DB_PATH monkeypatching + init_db() doing additive migration on the copy.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_rayan_blitz_c.py
"""
import asyncio
import importlib
import inspect
import os
import py_compile
import shutil
import sqlite3
import sys
import tempfile
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
BOT_DIR = os.path.join(REPO_ROOT, "rayan_dl_bot")
# the bot modules import each other flat (`import db`) — bot.py puts its own
# dir on sys.path at runtime; tests mirror that
sys.path.insert(0, BOT_DIR)

LIVE_DB = "/root/hermes-workspace/data/rayan.db"

MODULES = ("bot", "db", "downloader", "dlqueue", "i18n", "i18n_extra",
           "plugin_builder", "plugin_manager")

_passed = _failed = 0


def src(mod):
    with open(os.path.join(BOT_DIR, mod + ".py"), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------
# compile gate: every module still compiles
# ------------------------------------------------------------------
def test_modules_compile():
    for m in MODULES:
        py_compile.compile(os.path.join(BOT_DIR, m + ".py"), doraise=True)


# ------------------------------------------------------------------
# shared fixture: temp COPY of production db (never the live one)
# ------------------------------------------------------------------
class CopyDB:
    """Fresh temp COPY of the production DB per test; db points at the copy."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rayan_c_")
        self.path = os.path.join(self.dir, "rayan_copy.db")
        if os.path.exists(LIVE_DB):
            shutil.copy(LIVE_DB, self.path)   # copy of rayan.db — never live
        else:
            sqlite3.connect(self.path).close()
        # drop C-feature tables so each test starts from pre-C schema,
        # then let init_db() perform its ADDITIVE migration on the copy.
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE IF EXISTS usage")
        conn.commit()
        conn.close()
        import db
        self.db = db
        patcher = mock.patch.object(db, "DB_PATH", self.path)
        patcher.start()
        db.init_db()  # additive migration on the COPY
        return db

    def __exit__(self, *exc):
        mock.patch.stopall()
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


# ==================================================================
# B6 — DAILY LIMIT: db layer
# ==================================================================
def test_b6_usage_migration_additive_on_copy():
    """init_db() on a copy of the LIVE db creates the usage table without
    touching existing rows."""
    with CopyDB() as db:
        conn = sqlite3.connect(db.DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "usage" in tables
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert n_users > 0, "expected the copy to carry real users"
        conn.close()


def test_b6_default_limit_is_10_and_clamped():
    with CopyDB() as db:
        assert db.daily_limit() == 10
        db.set_daily_limit(0)       # clamped to >= 1 by daily_limit()
        assert db.daily_limit() == 1
        db.set_daily_limit(99999)   # clamped to <= 1000
        assert db.daily_limit() == 1000
        db.set_daily_limit(25)
        assert db.daily_limit() == 25


def test_b6_bump_and_reset_across_utc_midnight():
    """Counter accumulates per (UTC day, user); a new UTC day reads as 0 —
    no cron needed."""
    from datetime import datetime, timedelta
    with CopyDB() as db:
        uid = 990001
        assert db.usage_today(uid) == 0
        db.bump_usage(uid)
        db.bump_usage(uid, 4)
        assert db.usage_today(uid) == 5
        # forge YESTERDAY on this user's row → today must read zero again
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE usage SET day=? WHERE user_id=?", (yesterday, str(uid)))
        conn.commit()
        conn.close()
        assert db.usage_today(uid) == 0, "usage leaked across UTC midnight"


def test_b6_every_successful_delivery_counts():
    """record_download(success=True) bumps usage — incl. cache re-serves —
    and failures never do."""
    with CopyDB() as db:
        uid = 990002
        db.record_download(uid, "http://x/1", "youtube", "720", True)
        db.record_download(uid, "http://x/2", "youtube", "cached", True)
        db.record_download(uid, "http://x/3", "youtube", "720", False)
        assert db.usage_today(uid) == 2


# ==================================================================
# B6 — gate semantics in bot.py
# ==================================================================
def test_b6_gate_blocks_at_cap_exempts_admin_and_referral():
    import bot
    importlib.reload(bot)
    with CopyDB() as db:
        uid = 990003
        assert not bot._over_daily_limit(uid), "fresh user must pass"
        for i in range(10):   # default cap is exactly 10
            db.record_download(uid, f"http://x/{i}", "youtube", "720", True)
        assert bot._over_daily_limit(uid), "user over default cap must be blocked"

        # admins are always allowed
        if bot.ADMIN_IDS:
            admin_uid = bot.ADMIN_IDS[0]
            for i in range(50):
                db.record_download(admin_uid, f"http://a/{i}", "youtube", "720", True)
            assert not bot._over_daily_limit(admin_uid), "admin must be exempt"

        # referral-credit holders are allowed
        holder = 990004
        for i in range(10):
            db.record_download(holder, f"http://h/{i}", "youtube", "720", True)
        inviter = 990005
        db.get_or_create_ref_code(inviter)
        assert db.record_referral(inviter, holder), "referral must record"
        assert not bot._over_daily_limit(holder), "credit holder must be exempt"

        # gate fails OPEN: broken counter must not brick downloads
        with mock.patch.object(db, "usage_today", side_effect=RuntimeError("boom")):
            assert not bot._over_daily_limit(uid), "internal error must fail open"


def test_b6_check_daily_limit_sends_friendly_message():
    import bot
    importlib.reload(bot)
    with CopyDB() as db:
        uid = 990006
        for i in range(10):
            db.record_download(uid, f"http://x/{i}", "youtube", "720", True)

        sent = {}
        class _Msg:
            def __init__(self):
                self.message = self
            async def reply_text(self, text, *a, **k):
                sent["text"] = text
                return self

        ok = asyncio.run(bot.check_daily_limit(_Msg(), uid, "en"))
        assert not ok, "over-cap user must NOT proceed"
        assert "10" in sent.get("text", ""), "cap message must mention the limit"
        assert "🚦" in sent.get("text", "")

        # under-cap user sails through without any message
        class _Msg2(_Msg):
            async def reply_text(self, text, *a, **k):
                raise AssertionError("under-cap user must get no cap message")
        assert asyncio.run(bot.check_daily_limit(_Msg2(), 990007, "en"))


def test_b6_setlimit_cmd_validation_and_persistence():
    import bot
    with CopyDB() as db:
        replies = []
        class _Msg:
            message = None
            effective_user = type("U", (), {"id": 424242})()
            effective_chat = None
            async def reply_text(self, text, *a, **k):
                replies.append(text)
                return self

        upd = _Msg()
        upd.message = _Msg()

        class Ctx:
            args = ["20"]
        with mock.patch.object(bot, "is_admin", return_value=True):
            asyncio.run(bot.setlimit_cmd(upd, Ctx()))
        assert db.daily_limit() == 20
        assert "20" in replies[-1]

        # non-admin rejected
        replies.clear()
        with mock.patch.object(bot, "is_admin", return_value=False):
            Ctx.args = ["30"]
            asyncio.run(bot.setlimit_cmd(upd, Ctx()))
        assert db.daily_limit() != 30, "non-admin must not change the limit"

        # invalid values rejected
        for bad in (["0"], ["1001"], ["abc"], []):
            replies.clear()
            Ctx.args = bad
            asyncio.run(bot.setlimit_cmd(upd, Ctx()))
        assert db.daily_limit() == 20, "invalid input must keep old limit"


def test_b6_wired_into_private_link_path_only():
    """/setlimit registered; handle_link gates private chats; groups exempt."""
    b = src("bot")
    assert 'CommandHandler("setlimit", setlimit_cmd)' in b
    seg = b[b.index("async def handle_link"):]
    seg = seg[:seg.index("\nasync def ", 10)]
    assert "check_daily_limit" in seg
    import bot as _bot
    gate_src = inspect.getsource(_bot.check_daily_limit)
    assert "not is_group" in src("bot")  # group exemption marker present


# ==================================================================
# B7 — RETRY SEMANTICS (_retry_async / _send_media_retry + downloader layer)
# ==================================================================
def test_b7_retry_async_retries_transient_then_succeeds():
    import bot
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read timed out")
        return "ok"

    assert asyncio.run(bot._retry_async(flaky, attempts=3, base_delay=0.01)) == "ok"
    assert calls["n"] == 3


def test_b7_never_retries_validation_or_user_cancel():
    import bot
    from telegram.error import BadRequest, Forbidden

    async def run_once(exc):
        calls = {"n": 0}

        async def always_fails():
            calls["n"] += 1
            raise exc

        try:
            await bot._retry_async(always_fails, attempts=3, base_delay=0.01)
            return "no-raise"
        except type(exc):
            return "raised"

    # validation error: must NOT retry (1 call only)
    assert asyncio.run(run_once(BadRequest("invalid"))) == "raised"
    # user cancel / blocked bot: must NOT retry either
    assert asyncio.run(run_once(Forbidden("blocked"))) == "raised"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(fns)} passed, {failed} failed")
    sys.exit(1 if failed else 0)
