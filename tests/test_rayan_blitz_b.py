#!/usr/bin/env python3
"""Tests for Rayan Downloader Part B features (B1-B4).

Offline only: no Telegram API calls, no real downloads. Every DB test runs
against a TEMP COPY of the production rayan.db (never the live file), via
db.DB_PATH monkeypatching + init_db() doing additive migration on the copy.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_rayan_blitz_b.py
"""
import ast
import base64
import importlib
import os
import py_compile
import shutil
import sqlite3
import struct
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

MODULES = ("bot", "db", "downloader", "id3tag", "dlqueue", "i18n", "i18n_extra")

_passed = _failed = 0


def src(mod):
    with open(os.path.join(BOT_DIR, mod + ".py"), encoding="utf-8") as f:
        return f.read()


def bot_funcs():
    tree = ast.parse(src("bot"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def func_src(name):
    fns = bot_funcs()
    assert name in fns, f"bot.{name} missing"
    return ast.get_source_segment(src("bot"), fns[name]) or ""


# ------------------------------------------------------------------
# compile gate: every module still compiles
# ------------------------------------------------------------------
def test_modules_compile():
    for m in MODULES:
        py_compile.compile(os.path.join(BOT_DIR, m + ".py"), doraise=True)


# ==================================================================
# B1 — REFERRAL SYSTEM
# ==================================================================

class RefDB:
    """Fresh temp COPY of the production DB per test; db points at the copy."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rayan_b1_")
        self.path = os.path.join(self.dir, "rayan_copy.db")
        if os.path.exists(LIVE_DB):
            shutil.copy(LIVE_DB, self.path)   # copy of rayan.db — never the live one
        else:
            sqlite3.connect(self.path).close()
        # drop any B-feature tables so each test starts from pre-B schema,
        # then let init_db() perform the ADDITIVE migration on the copy.
        conn = sqlite3.connect(self.path)
        for t in ("referrals", "referral_credits", "ref_codes", "library"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
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


def test_b1_migration_additive_on_copy():
    """init_db() on a copy of the LIVE db creates referral tables without
    touching existing rows."""
    with RefDB() as db:
        conn = sqlite3.connect(db.DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"referrals", "referral_credits", "ref_codes"} <= tables
        n_users_before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert n_users_before > 0, "expected the copy to carry real users"
        # legacy columns survived migration
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        assert "lang" in cols and "banned" in cols
        conn.close()


def test_b1_ref_code_unique_and_stable():
    with RefDB() as db:
        c1 = db.get_or_create_ref_code(900001)
        c2 = db.get_or_create_ref_code(900001)
        assert c1 == c2 and c1, "code must be stable per user"
        assert len(c1) >= 4 and all(ch.isalnum() for ch in c1), "base62"
        c3 = db.get_or_create_ref_code(900002)
        assert c3 != c1
        assert db.user_by_ref_code(c1) == 900001
        assert db.user_by_ref_code(c3) == 900002
        assert db.user_by_ref_code("no-such-code!!") is None  # injection-ish junk
        assert db.user_by_ref_code("") is None


def test_b1_record_referral_and_credit_flow():
    with RefDB() as db:
        inviter, invitee = 910001, 910002
        assert db.record_referral(inviter, invitee) is True
        # duplicate invite of same person → ignored
        assert db.record_referral(inviter, invitee) is False
        # no self-invite
        assert db.record_referral(invitee, invitee) is False
        # inviter earned 1 (cap 5), invitee starts with 1
        assert db.earned_referral_credits(inviter) == 1
        assert db.get_referral_credits(inviter) == 1
        assert db.get_referral_credits(invitee) == 1
        assert db.count_referrals(inviter) == 1
        # consume works, and stops at zero
        assert db.consume_referral_credit(invitee) is True
        assert db.get_referral_credits(invitee) == 0
        assert db.consume_referral_credit(invitee) is False
        # unrelated user has nothing
        assert db.get_referral_credits(999999) == 0


def test_b1_cap_five():
    with RefDB() as db:
        inviter = 920000
        for i in range(1, 8):  # invite 7 people
            assert db.record_referral(inviter, 920100 + i) is True
        assert db.count_referrals(inviter) == 7          # all recorded
        assert db.earned_referral_credits(inviter) == 5  # but capped at 5
        assert db.get_referral_credits(inviter) == 5
        for _ in range(5):
            assert db.consume_referral_credit(inviter) is True
        assert db.get_referral_credits(inviter) == 0
        assert db.consume_referral_credit(inviter) is False  # cap holds at spend time


def test_b1_invitee_bonus_survives_used_overflow():
    """A capped inviter who was once invited keeps exactly their bonus credits;
    used can never drive the balance below 0."""
    with RefDB() as db:
        root, mid = 930001, 930002
        db.record_referral(root, mid)      # mid invited by root → +1 bonus
        for i in range(6):
            db.record_referral(mid, 931000 + i)  # mid hits the cap (5 earned)
        assert db.get_referral_credits(mid) == 1 + 5
        for _ in range(6):
            assert db.consume_referral_credit(mid) is True
        assert db.get_referral_credits(mid) == 0
        assert db.consume_referral_credit(mid) is False


def test_b1_start_parsing_and_handlers_exist():
    s = func_src("start")
    # /start <code>: resolves code, blocks self-invite, records referral
    assert "context.args" in s and "user_by_ref_code" in s
    assert "inviter != uid" in s or "inviter == uid" in s
    assert "record_referral" in s
    for fn in ("ref_cmd", "stats_cmd"):
        assert fn in bot_funcs(), f"{fn} handler missing"
    # registered in main()
    m = func_src("main")
    for cmd in ('"ref"', '"stats"', '"downloads"', '"fast"'):
        assert f"CommandHandler({cmd}" in m, f"{cmd} not registered"


def test_b1_stats_shows_referrals():
    st = func_src("stats_cmd")
    assert "count_referrals" in st and "get_referral_credits" in st
    adm = func_src("admin_cb")
    assert "FROM referrals" in adm or "total_refs" in adm  # admin stats line


def test_b1_queue_skip_consumes_when_busy():
    """Drive the REAL bot._limited_download against a stubbed downloader:
    a credit burns only while the queue is busy, and the skip lane genuinely
    overtakes the normal waiter."""
    import asyncio
    import dlqueue

    async def scenario():
        dlqueue._heap.clear()
        dlqueue._free = 0          # ALL capacity busy
        dlqueue._sem_size = 2
        dlqueue._waiting_total = 0
        order = []

        with RefDB() as db:
            import rayan_dl_bot.bot as rbot
            db.record_referral(940001, 940002)
            db.record_referral(940001, 940003)   # skipper has 2 credits
            assert db.get_referral_credits(940001) == 2

            orig = rbot.dl.download_video

            def stub(*a, **k):
                order.append(a[3] if len(a) > 3 else "?")
                return True

            rbot.dl.download_video = stub
            try:
                # a NORMAL job already waiting in line
                async def normal_job():
                    async with dlqueue.QueueSlot(777):
                        order.append("normal-fifo")
                nt = asyncio.create_task(normal_job())
                await asyncio.sleep(0.05)
                assert dlqueue.queue_position() == 1, "normal job must be waiting"

                # credited user requests a download WHILE the queue is busy
                async def sk():
                    await rbot._limited_download("url", "720", False,
                                                 "/tmp/x_skip.mp4", uid=940001)
                st = asyncio.create_task(sk())
                await asyncio.sleep(0.05)
                # credit burned exactly once, at request time
                assert db.get_referral_credits(940001) == 1, \
                    "one credit must burn while the queue is busy"

                # free ONE slot -> the skip lane must take it before FIFO
                dlqueue._free += 1
                dlqueue._grant_ready()
                await asyncio.wait([st], timeout=5)
                assert st.done(), "skipped job must complete first"
                assert order[:1] == ["/tmp/x_skip.mp4"], \
                    f"skip lane did not overtake: {order}"

                # release the rest and drain
                dlqueue._free += 1
                dlqueue._grant_ready()
                await asyncio.wait([nt], timeout=5)
                assert "normal-fifo" in order
            finally:
                rbot.dl.download_video = orig
        dlqueue._heap.clear()
        dlqueue._waiting_total = 0
        dlqueue._free = 2

    asyncio.run(scenario())


def test_b1_no_credit_when_queue_idle():
    """Idle queue -> download starts immediately, zero credits spent."""
    import asyncio
    import dlqueue

    async def idle():
        dlqueue._heap.clear()
        dlqueue._free = 2
        dlqueue._sem_size = 2
        dlqueue._waiting_total = 0
        with RefDB() as db:
            import rayan_dl_bot.bot as rbot
            orig = rbot.dl.download_video
            rbot.dl.download_video = lambda *a, **k: True
            try:
                db.record_referral(950001, 950002)
                assert db.get_referral_credits(950001) == 1
                await rbot._limited_download("url", "720", False,
                                             "/tmp/y.mp4", uid=950001)
                assert db.get_referral_credits(950001) == 1, \
                    "credits must NOT burn when the queue is idle"
            finally:
                rbot.dl.download_video = orig
        dlqueue._heap.clear()
        dlqueue._waiting_total = 0

    asyncio.run(idle())


# ==================================================================
# B2 — ID3v2.3 TAGS
# ==================================================================

# A tiny valid MP3: MPEG-1 Layer III, 128kbps 44.1kHz frames of 0xFF 0xFB silence
_MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413


def make_mp3(path, frames=50):
    with open(path, "wb") as f:
        for _ in range(frames):
            f.write(_MP3_FRAME)
    return path


def test_b2_write_and_read_roundtrip():
    import id3tag
    d = tempfile.mkdtemp(prefix="rayan_b2_")
    try:
        p = make_mp3(os.path.join(d, "song.mp3"))
        size_before = os.path.getsize(p)
        # 1x1 red JPEG (real JPEG magic, tiny but valid-looking container)
        cover = os.path.join(d, "cover.jpg")
        with open(cover, "wb") as f:
            f.write(base64.b64decode(
                "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDAkZ"
                "GxkVFxcXFxcXHx4gHx4eJyIlJSUmKCgpKCgtMTExNDc5OTk5Pz8/Pz8/Pz8/P//A"
                "ABEIAAEAAQMBIgACEQEDEQH/xAAfAAABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJ"
                "Cgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFEiExQQYTUWEHInEUMoGRoQgj"
                "QrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpj"
                "ZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4"
                "ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/aAAwDAQAC"
                "EQMRAD8A9/ooooA//9k="))
        ok = id3tag.write_tags(p, title="آهنگ تست Test Song", artist="Channel X",
                               album="youtube", cover_jpeg=cover)
        assert ok, "write_tags failed"
        size_after = os.path.getsize(p)
        assert size_after > size_before, "tagged file must be larger than raw audio"
        tags = id3tag.read_tags(p)
        assert tags.get("version") == "2.3", f"wanted ID3v2.3, got {tags.get('version')}"
        assert tags.get("title") == "آهنگ تست Test Song"
        assert tags.get("artist") == "Channel X"
        assert tags.get("album") == "youtube"
        assert tags.get("has_cover") is True
        assert tags.get("mpeg_found") is True, "audio stream lost after tagging"
        assert size_after - size_before < 1024 * 1024, "sane tag size"


        # header bytes sanity: v2.3, no experimental/footer flags, syncsafe size
        with open(p, "rb") as f:
            head = f.read(10)
        assert head[:3] == b"ID3" and head[3] == 3 and head[4] == 0
        assert head[5] == 0
        declared = ((head[6] & 127) << 21) | ((head[7] & 127) << 14) | \
                   ((head[8] & 127) << 7) | (head[9] & 127)
        assert declared + 10 <= size_after
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_replaces_existing_tag_without_duplicating():
    import id3tag
    d = tempfile.mkdtemp(prefix="rayan_b2b_")
    try:
        p = make_mp3(os.path.join(d, "twice.mp3"))
        assert id3tag.write_tags(p, title="first", artist="a", album="b")
        assert id3tag.write_tags(p, title="second", artist="c", album="d")
        tags = id3tag.read_tags(p)
        assert tags.get("title") == "second", "old tag must be replaced, not nested"
        assert tags.get("mpeg_found") is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_unicode_and_empty_cover_paths():
    import id3tag
    d = tempfile.mkdtemp(prefix="rayan_b2c_")
    try:
        p = make_mp3(os.path.join(d, "fa.mp3"))
        # cover path that does not exist must not crash or fail the write
        assert id3tag.write_tags(p, title="فارسی 🎵", artist="خواننده",
                                 album="", cover_jpeg="/nonexistent/x.jpg")
        tags = id3tag.read_tags(p)
        assert tags.get("title") == "فارسی 🎵"
        assert "has_cover" not in tags
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_wired_into_audio_send_path():
    run = func_src("_run_download")
    assert "id3tag.write_tags" in run, "MP3 send path must embed ID3 tags"
    assert 'TIT2' not in run  # writer used, not inline byte-poking
    # artist comes from uploader/channel, album from platform/site
    assert "uploader" in run and "detect_site(url)" in run


def test_b2_mutagen_absent_is_fine():
    """The stdlib writer means mutagen absence doesn't break anything."""
    try:
        import mutagen  # noqa: F401
        has_mutagen = True
    except ImportError:
        has_mutagen = False
    assert has_mutagen is False  # this box: hand-rolled path is THE path
    sys.path.insert(0, BOT_DIR)
    import id3tag  # noqa: F401 — imports cleanly without mutagen


# ==================================================================
# B3 — /DOWNLOADS LIBRARY
# ==================================================================

def test_b3_library_table_and_crud_on_copy():
    with RefDB() as db:
        uid = 960001
        assert db.library_add(uid, "FILEID_AAA", "🎵 song one")
        assert db.library_add(uid, "FILEID_BBB", "🎬 video two")
        rows = db.get_library(uid)
        assert [r[1] for r in rows] == ["FILEID_BBB", "FILEID_AAA"], "newest first"
        assert rows[0][2] == "🎬 video two"
        # re-send fetch: ownership-checked single row
        row = db.get_library_entry(uid, rows[0][0])
        assert row and row[1] == "FILEID_BBB"
        # another user cannot see/delete it
        assert db.get_library_entry(666666, rows[0][0]) is None
        assert db.delete_library_entry(666666, rows[0][0]) is False
        # delete-for-me works
        assert db.delete_library_entry(uid, rows[0][0]) is True
        assert len(db.get_library(uid)) == 1


def test_b3_keep_last_ten_per_user():
    with RefDB() as db:
        uid = 970001
        for i in range(15):
            db.library_add(uid, f"FID_{i:02d}", f"f{i}")
        rows = db.get_library(uid)
        assert len(rows) == 10, f"keep-10 violated: {len(rows)}"
        ids = [r[1] for r in rows]
        assert ids[0] == "FID_14" and ids[-1] == "FID_05", "newest 10 kept"


def test_b3_seven_day_expiry_lazy_cleanup():
    with RefDB() as db:
        uid = 980001
        db.library_add(uid, "FID_OLD", "ancient")
        db.library_add(uid, "FID_NEW", "fresh")
        # backdate one row beyond the TTL
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE library SET created_at = ? WHERE file_id = 'FID_OLD'",
                     ("2026-07-01T00:00:00",))  # >7 days before 2026-08-25
        conn.commit()
        conn.close()
        rows = db.get_library(uid)          # access triggers lazy cleanup
        assert [r[1] for r in rows] == ["FID_NEW"]
        gone = sqlite3.connect(db.DB_PATH).execute(
            "SELECT COUNT(*) FROM library WHERE file_id='FID_OLD'").fetchone()[0]
        assert gone == 0, "expired row must be physically deleted on access"


def test_b3_handlers_and_buttons():
    assert "downloads_cmd" in bot_funcs() and "downloads_cb" in bot_funcs()
    m = func_src("main")
    assert 'CallbackQueryHandler(downloads_cb, pattern="^lib")' in m
    kb = func_src("_lib_keyboard")
    assert '"lib|send|"' .replace('"', "") in kb.replace('"', "") or "lib|send|" in kb
    assert "lib|del|" in kb
    cb = func_src("downloads_cb")
    assert "get_library_entry" in cb, "re-send must be ownership-checked"
    assert "delete_library_entry" in cb


# ==================================================================
# B4 — /FAST MODE
# ==================================================================

def test_b4_flag_persists_in_own_column():
    with RefDB() as db:
        uid = 990001
        assert db.fast_mode_enabled(uid) is False
        db.set_fast_mode(uid, True)
        assert db.fast_mode_enabled(uid) is True
        db.set_fast_mode(uid, False)
        assert db.fast_mode_enabled(uid) is False
        # toggling fast NEVER touches default_audio (the old sentinel bug)
        db.set_user_settings(uid, default_quality=1080, default_audio=1)
        db.set_fast_mode(uid, True)
        s = db.get_user_settings(uid)
        assert s["default_audio"] == 1 and s["default_quality"] == 1080
        db.set_fast_mode(uid, False)
        s = db.get_user_settings(uid)
        assert s["default_audio"] == 1 and s["default_quality"] == 1080
        # brand-new user (no settings row yet) can flip it too
        db.set_fast_mode(990002, True)
        assert db.fast_mode_enabled(990002) is True


def test_b4_skips_thumb_and_translation_when_on():
    run = func_src("_run_download")
    seg = run[run.find("cover = None"):run.find("sent = await q.message.reply_audio")]
    assert "_fast_mode_on" in seg, "audio path: fast mode must skip cover fetch"
    hl = func_src("handle_link")
    # both thumbnail sites guarded
    assert hl.count("thumb_path = (None if _fast_mode_on(uid)") == 2, \
        "both video send paths must skip thumbnails in fast mode"
    sub = func_src("button_click")
    tr_seg = sub[sub.find("target = lang if sublang != lang"):sub.find("if target:")]
    assert "_fast_mode_on" in tr_seg, "subtitle translation must be skipped in fast mode"


def test_b4_settings_shows_state():
    st = func_src("settings_cmd")
    assert "fast_mode_enabled" in st, "/settings must show current /fast state"
    assert "fast_line" in st
    m = func_src("main")
    assert 'CommandHandler("fast"' in m


# ==================================================================
# regression guards (pre-existing behaviour must survive)
# ==================================================================

def test_regression_default_audio_setting_still_works():
    with RefDB() as db:
        db.set_user_settings(995000, default_quality=480, default_audio=1)
        s = db.get_user_settings(995000)
        assert s["default_quality"] == 480 and s["default_audio"] == 1


def test_regression_dlqueue_counters_consistent():
    import asyncio
    import dlqueue

    async def burst():
        dlqueue._heap.clear(); dlqueue._free = 2; dlqueue._sem_size = 2
        dlqueue._waiting_total = 0
        ran = []

        async def job(i):
            async with dlqueue.QueueSlot(i):
                ran.append(i)
                await asyncio.sleep(0.01)

        await asyncio.gather(*[job(i) for i in range(6)])
        assert sorted(ran) == list(range(6)), "every waiter eventually runs"
        assert dlqueue.queue_position() == 0
        assert dlqueue.total_running() == 0
        assert dlqueue._free == 2, "permits fully released after burst"

    asyncio.run(burst())
    dlqueue._heap.clear(); dlqueue._waiting_total = 0


def test_regression_i18n_keys_resolve_all_langs():
    from rayan_dl_bot import i18n
    for key in ("ref_menu", "ref_self", "lib_empty"):
        for lang in ("en", "fa"):
            out = i18n.t(lang, key, link="x", invited=1, earned=1, credits=2, cap=5)
            assert out and out != key, f"{lang}/{key} unresolved"


# ------------------------------------------------------------------
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
