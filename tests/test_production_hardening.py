#!/usr/bin/env python3
"""Production-hardening regression tests (2026-10-21 audit).

Offline by default: every network test uses either a loopback HTTP fixture or
``example.com`` (skipped when the host has no outbound network). No Telegram
API calls, no production database, no real bot token.

Run with the project venv:
    ./venv/bin/python tests/test_production_hardening.py -v
"""
import asyncio
import inspect
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
BOT_DIR = os.path.join(REPO_ROOT, "rayan_dl_bot")
sys.path.insert(0, BOT_DIR)

os.environ.setdefault("TG_BOT_TOKEN", "123456:TEST-TOKEN-not-a-real-token")
os.environ.setdefault("TG_ADMIN_IDS", "1")
_TEST_DATA = tempfile.mkdtemp(prefix="rayan-hardening-data-")
os.environ.setdefault("RAYAN_DATA_DIR", _TEST_DATA)
os.environ.setdefault("RAYAN_PLUGINS_DIR", os.path.join(_TEST_DATA, "plugins"))

from rayan_dl_bot import config, db, dlqueue, linkcache, netguard, plugin_manager  # noqa: E402


# ─────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────
class QueueTests(unittest.TestCase):
    def setUp(self):
        dlqueue._heap.clear()
        dlqueue._per_user.clear()
        dlqueue._waiting_total = 0
        dlqueue._free = 0
        dlqueue._sem_size = 0

    def _cap(self, total, per_user):
        dlqueue._cfg_cache = (time.monotonic(), total, per_user)

    def test_global_cap_enforced(self):
        self._cap(2, 2)
        state = {"now": 0, "max": 0}
        lock = threading.Lock()

        async def job():
            async with dlqueue.QueueSlot(1):
                with lock:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                await asyncio.sleep(0.05)
                with lock:
                    state["now"] -= 1

        async def run():
            await asyncio.gather(*[job() for _ in range(6)])

        asyncio.run(asyncio.wait_for(run(), 10))
        self.assertEqual(state["max"], 2)
        self.assertEqual(dlqueue.total_running(), 0)
        self.assertEqual(dlqueue._free, 2)

    def test_per_user_cap_stops_album_monopoly(self):
        self._cap(2, 1)
        started = []

        async def job(uid, n):
            async with dlqueue.QueueSlot(uid):
                started.append((uid, n))
                await asyncio.sleep(0.05)

        async def run():
            tasks = [asyncio.create_task(job(1, n)) for n in range(5)]
            tasks.append(asyncio.create_task(job(2, 99)))
            await asyncio.gather(*tasks)

        asyncio.run(asyncio.wait_for(run(), 10))
        self.assertEqual(started[0][0], 1)
        # The second permit must go to the other user, never to a second job
        # of the same user (per_user limit = 1).
        self.assertEqual(started[1], (2, 99))

    def test_skip_lane_priority(self):
        self._cap(1, 1)
        order = []

        async def holder():
            async with dlqueue.QueueSlot(9):
                await asyncio.sleep(0.1)

        async def normal(n):
            async with dlqueue.QueueSlot(1):
                order.append(("normal", n))
                await asyncio.sleep(0.01)

        async def skip():
            async with dlqueue.QueueSlot(2, skip=True):
                order.append(("skip", 0))
                await asyncio.sleep(0.01)

        async def run():
            h = asyncio.create_task(holder())
            await asyncio.sleep(0.01)
            n1 = asyncio.create_task(normal(1))
            n2 = asyncio.create_task(normal(2))
            await asyncio.sleep(0.01)
            s = asyncio.create_task(skip())
            await asyncio.gather(h, n1, n2, s)

        asyncio.run(asyncio.wait_for(run(), 10))
        self.assertEqual(order[0], ("skip", 0))

    def test_fifo_between_normal_jobs(self):
        self._cap(1, 1)
        order = []

        async def holder():
            async with dlqueue.QueueSlot(99):
                await asyncio.sleep(0.1)

        async def job(n):
            async with dlqueue.QueueSlot(1):
                order.append(n)
                await asyncio.sleep(0.01)

        async def run():
            h = asyncio.create_task(holder())
            await asyncio.sleep(0.01)
            tasks = []
            for n in range(4):
                tasks.append(asyncio.create_task(job(n)))
                await asyncio.sleep(0.01)
            await asyncio.gather(h, *tasks)

        asyncio.run(asyncio.wait_for(run(), 10))
        self.assertEqual(order, [0, 1, 2, 3])

    def test_cancelled_waiter_releases_nothing_and_leaves_no_slot(self):
        self._cap(1, 1)

        async def run():
            async with dlqueue.QueueSlot(9):
                waiter = asyncio.create_task(dlqueue.QueueSlot(1).wait())
                await asyncio.sleep(0.01)
                self.assertEqual(dlqueue.queue_position(), 1)
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(0.01)
                self.assertEqual(dlqueue.queue_position(), 0)
                self.assertEqual(dlqueue.total_running(), 1)
            self.assertEqual(dlqueue.total_running(), 0)
            # Cancelling a waiter must not leak or destroy a permit.
            self.assertEqual(dlqueue._free, 1)
            async with dlqueue.QueueSlot(2):
                self.assertEqual(dlqueue.total_running(), 1)

        asyncio.run(asyncio.wait_for(run(), 10))

    def test_exception_inside_slot_releases_capacity(self):
        self._cap(1, 1)

        async def boom():
            async with dlqueue.QueueSlot(1):
                raise RuntimeError("download failed")

        async def ok():
            async with dlqueue.QueueSlot(2):
                return "ok"

        async def run():
            results = await asyncio.gather(boom(), ok(), return_exceptions=True)
            self.assertIsInstance(results[0], RuntimeError)
            self.assertEqual(results[1], "ok")
            self.assertEqual(dlqueue.total_running(), 0)
            self.assertEqual(dlqueue.queue_position(), 0)

        asyncio.run(asyncio.wait_for(run(), 10))

    def test_wait_timeout_does_not_leak_permit(self):
        self._cap(1, 1)

        async def run():
            async with dlqueue.QueueSlot(9):
                try:
                    await asyncio.wait_for(dlqueue.QueueSlot(1).wait(), 0.05)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.01)
                self.assertEqual(dlqueue.queue_position(), 0)
            self.assertEqual(dlqueue.total_running(), 0)
            self.assertEqual(dlqueue._free, 1)

        asyncio.run(asyncio.wait_for(run(), 10))

    def test_no_deadlock_under_burst(self):
        self._cap(2, 2)

        async def job(uid, n):
            async with dlqueue.QueueSlot(uid):
                await asyncio.sleep(0.005)
                return n

        async def run():
            tasks = [job(i % 5, i) for i in range(40)]
            results = await asyncio.gather(*tasks)
            self.assertEqual(sorted(results), list(range(40)))
            self.assertEqual(dlqueue.total_running(), 0)
            self.assertEqual(dlqueue.queue_position(), 0)
            self.assertEqual(dlqueue._free, 2)

        asyncio.run(asyncio.wait_for(run(), 15))

    def test_admin_can_raise_cap_at_runtime(self):
        self._cap(1, 1)

        async def run():
            async with dlqueue.QueueSlot(1):
                dlqueue._cfg_cache = (time.monotonic(), 2, 2)
                dlqueue._sem_size = 1
                dlqueue._free = 0
                async with dlqueue.QueueSlot(2):
                    self.assertEqual(dlqueue.total_running(), 2)

        asyncio.run(asyncio.wait_for(run(), 10))


# ─────────────────────────────────────────────────────────────
# SQLite integrity / backup / restore
# ─────────────────────────────────────────────────────────────
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rayan-db-test-")
        self._old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "rayan.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_integrity_ok_on_fresh_db(self):
        ok, detail = db.check_integrity()
        self.assertTrue(ok, detail)

    def test_backup_is_openable_queryable_and_retained(self):
        db.execute("INSERT INTO links (user_id, url, site, created_at) VALUES (?,?,?,?)",
                   (1, "https://example.com/a", "example", "2026-10-21"))
        path = db.backup_database(keep=3, force=True)
        self.assertTrue(path and os.path.exists(path))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
        finally:
            conn.close()
        # retention: at most `keep` files survive
        bdir = os.path.join(self.tmp, "backups")
        self.assertLessEqual(len([f for f in os.listdir(bdir) if f.endswith(".db")]), 3)

    def test_backup_under_wal_write_load(self):
        stop = threading.Event()
        errors = []

        def writer():
            try:
                for i in range(400):
                    if stop.is_set():
                        break
                    db.execute(
                        "INSERT INTO downloads (user_id, url, success, created_at) "
                        "VALUES (?,?,?,datetime('now'))", (i, f"u{i}", 1))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if db.query_one("SELECT COUNT(*) FROM downloads")[0] > 20:
                break
            time.sleep(0.02)
        try:
            path = db.backup_database(keep=7, force=True)
        finally:
            stop.set()
            thread.join(10)
        self.assertFalse(errors, errors)
        self.assertTrue(path and os.path.exists(path))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 0)
        finally:
            conn.close()

    def test_backup_failure_is_non_fatal(self):
        old = db.DB_PATH
        db.DB_PATH = "/proc/definitely-not-writable/rayan.db"
        try:
            self.assertIsNone(db.backup_database(force=True))
        finally:
            db.DB_PATH = old

    def test_restore_from_backup(self):
        db.execute("INSERT INTO links (user_id, url, site, created_at) VALUES (?,?,?,?)",
                   (7, "https://example.com/keep", "example", "2026-10-21"))
        backup = db.backup_database(keep=7, force=True)
        db.execute("DELETE FROM links")
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM links")[0], 0)
        # Simulate operator restore into a fresh DB path.
        restored = os.path.join(self.tmp, "restored.db")
        shutil.copyfile(backup, restored)
        conn = sqlite3.connect(restored)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
        finally:
            conn.close()

    def test_fileid_cache_ttl_prune_and_case_sensitive_key(self):
        db.ensure_fileid_cache()
        from rayan_dl_bot import cdncache
        cdncache.put("https://youtu.be/AbC", "f1", "video")
        cdncache.put("https://youtu.be/abc", "f2", "video")
        self.assertEqual(cdncache.get("https://youtu.be/AbC")["file_id"], "f1")
        self.assertEqual(cdncache.get("https://youtu.be/abc")["file_id"], "f2")
        db.execute("UPDATE fileid_cache SET created_at = datetime('now','-400 days')")
        cdncache.ensure_table()
        self.assertEqual(db.query_one("SELECT COUNT(*) FROM fileid_cache")[0], 0)

    def test_linkcache_expiry_and_case_sensitivity(self):
        linkcache.clear()
        linkcache.put("https://youtu.be/AbC", {"v": 1})
        self.assertEqual(linkcache.get("https://youtu.be/AbC")["v"], 1)
        self.assertNotEqual(linkcache.get("https://youtu.be/abc"), {"v": 1})
        self.assertIsNone(linkcache.get("https://youtu.be/abc"))
        key = linkcache._norm("https://youtu.be/AbC")
        linkcache._cache[key]["ts"] = time.time() - 10_000
        self.assertIsNone(linkcache.get("https://youtu.be/AbC"))


# ─────────────────────────────────────────────────────────────
# Plugin installation / execution
# ─────────────────────────────────────────────────────────────
PLUGIN_SOURCE = (
    "import os, time\n"
    "class Plugin:\n"
    "    def handle(self, url, config):\n"
    "        return {'type': 'text', 'text': os.environ.get('TG_BOT_TOKEN') or 'NO_TOKEN'}\n"
)


def _zip(path, files, symlink=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rayan-plugins-")
        self._old_dir = plugin_manager.PLUGINS_DIR
        plugin_manager.PLUGINS_DIR = self.tmp
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, plugin_manager, "PLUGINS_DIR", self._old_dir)

    def _manifest(self, **over):
        meta = {"name": "demo", "version": "1.0.0", "description": "test",
                "sites": ["demo.test"], "entry": "plugin", "config": {}}
        meta.update(over)
        return json.dumps(meta)

    def _valid_zip(self, name="demo.zip", source=PLUGIN_SOURCE, **over):
        path = os.path.join(self.tmp, name)
        _zip(path, {"plugin.json": self._manifest(**over), "plugin.py": source})
        return path

    def test_install_discover_list_remove(self):
        res = plugin_manager.install_plugin_from_file(self._valid_zip())
        self.assertTrue(res["ok"], res)
        names = [m["name"] for m in plugin_manager.discover_plugins()]
        self.assertIn("demo", names)
        self.assertIsNotNone(plugin_manager.get_plugin("demo"))
        self.assertTrue(plugin_manager.remove_plugin("demo"))
        self.assertNotIn("demo", [m["name"] for m in plugin_manager.discover_plugins()])

    def test_plugin_runs_isolated_without_bot_secrets(self):
        self.assertTrue(plugin_manager.install_plugin_from_file(self._valid_zip())["ok"])
        meta = plugin_manager.get_plugin("demo")
        result = plugin_manager.call_plugin(meta, "https://demo.test/x")
        self.assertEqual(result.get("text"), "NO_TOKEN", result)

    def test_plugin_cannot_import_bot_config_module(self):
        source = (
            "class Plugin:\n"
            "    def handle(self, url, config):\n"
            "        try:\n"
            "            import config\n"
            "            state = config.BOT_TOKEN or 'EMPTY'\n"
            "        except ImportError:\n"
            "            state = 'BLOCKED'\n"
            "        return {'type': 'text', 'text': state}\n"
        )
        self.assertTrue(plugin_manager.install_plugin_from_file(
            self._valid_zip(source=source))["ok"])
        meta = plugin_manager.get_plugin("demo")
        result = plugin_manager.call_plugin(meta, "https://demo.test/x")
        self.assertEqual(result.get("text"), "BLOCKED", result)

    def test_plugin_timeout_is_enforced_and_child_is_killed(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        source = (
            "import os, time\n"
            "class Plugin:\n"
            "    def handle(self, url, config):\n"
            "        open(config['pidfile'], 'w').write(str(os.getpid()))\n"
            "        time.sleep(60)\n"
            "        return {'type': 'text', 'text': 'late'}\n"
        )
        path = self._valid_zip(source=source, timeout=1,
                               config={"pidfile": pidfile})
        self.assertTrue(plugin_manager.install_plugin_from_file(path)["ok"])
        meta = plugin_manager.get_plugin("demo")
        started = time.monotonic()
        result = plugin_manager.call_plugin(meta, "https://demo.test/x")
        elapsed = time.monotonic() - started
        self.assertIn("timeout", result.get("error", "").lower(), result)
        self.assertLess(elapsed, 10)
        pid = int(open(pidfile).read())
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}"), "timed-out child still alive")

    def test_rejects_path_traversal(self):
        path = os.path.join(self.tmp, "trav.zip")
        _zip(path, {"plugin.json": self._manifest(), "plugin.py": PLUGIN_SOURCE,
                    "../evil.txt": "pwn"})
        res = plugin_manager.install_plugin_from_file(path)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_rejects_symlink_entry(self):
        path = os.path.join(self.tmp, "link.zip")
        _zip(path, {"plugin.json": self._manifest(), "plugin.py": PLUGIN_SOURCE},
             symlink="link")
        self.assertFalse(plugin_manager.install_plugin_from_file(path)["ok"])

    def test_rejects_excessive_entry_count(self):
        path = os.path.join(self.tmp, "many.zip")
        files = {"plugin.json": self._manifest(), "plugin.py": PLUGIN_SOURCE}
        for i in range(plugin_manager.MAX_ZIP_ENTRIES + 1):
            files[f"f{i}.txt"] = "x"
        _zip(path, files)
        self.assertFalse(plugin_manager.install_plugin_from_file(path)["ok"])

    def test_rejects_oversized_archive(self):
        path = os.path.join(self.tmp, "big.zip")
        _zip(path, {"plugin.json": self._manifest(), "plugin.py": PLUGIN_SOURCE})
        old = plugin_manager.MAX_ZIP_BYTES
        plugin_manager.MAX_ZIP_BYTES = 10
        try:
            self.assertFalse(plugin_manager.install_plugin_from_file(path)["ok"])
        finally:
            plugin_manager.MAX_ZIP_BYTES = old

    def test_rejects_invalid_manifest_and_bad_python(self):
        bad = self._valid_zip("badmeta.zip", sites=[])
        self.assertFalse(plugin_manager.install_plugin_from_file(bad)["ok"])
        broken = self._valid_zip("broken.zip", source="class Plugin(:\n")
        self.assertFalse(plugin_manager.install_plugin_from_file(broken)["ok"])

    def test_failed_upgrade_keeps_working_plugin(self):
        self.assertTrue(plugin_manager.install_plugin_from_file(self._valid_zip())["ok"])
        broken = self._valid_zip("broken2.zip", source="class Plugin(:\n")
        self.assertFalse(plugin_manager.install_plugin_from_file(broken)["ok"])
        result = plugin_manager.call_plugin(
            plugin_manager.get_plugin("demo"), "https://demo.test/x")
        self.assertEqual(result.get("text"), "NO_TOKEN", result)

    def test_public_api_signatures_still_compatible(self):
        expected = {
            "discover_plugins": [],
            "get_plugin": ["name"],
            "call_plugin": ["meta", "url", "config"],
            "call_plugin_chapters": ["meta", "url"],
            "install_plugin_from_file": ["zip_path"],
            "remove_plugin": ["name"],
        }
        for name, params in expected.items():
            fn = getattr(plugin_manager, name)
            self.assertEqual(list(inspect.signature(fn).parameters)[:len(params)], params)

    def test_site_routing_uses_hostname_not_substring(self):
        # A domain rule must not route "notinstagram.com" to Instagram.
        self.assertTrue(plugin_manager._site_match("instagram.com", "https://instagram.com/x"))
        self.assertTrue(plugin_manager._site_match("instagram.com", "https://www.instagram.com/x"))
        self.assertFalse(plugin_manager._site_match("instagram.com", "https://notinstagram.com/x"))
        # Legacy non-domain rules keep their documented substring behaviour.
        self.assertTrue(plugin_manager._site_match("tiktok/wildcard", "https://x.test/tiktok/wildcard"))


# ─────────────────────────────────────────────────────────────
# Application behaviour (disk cleanup, escaping, rate limits)
# ─────────────────────────────────────────────────────────────
class ApplicationTests(unittest.TestCase):
    def test_markdown_escaping_prevents_entity_errors(self):
        from rayan_dl_bot import bot as rbot
        raw = "a_b*c[d]`e"
        escaped = rbot._md_escape(raw)
        for ch in "_*[]`":
            self.assertIn("\\" + ch, escaped)
        self.assertEqual(rbot._md_escape(None), "")
        self.assertEqual(rbot._md_escape(123), "123")

    def test_disk_cleanup_spares_active_jobs_and_bot_api_store(self):
        from rayan_dl_bot import bot as rbot
        tmp = tempfile.mkdtemp(prefix="rayan-disk-")
        downloads = os.path.join(tmp, "downloads")
        api = os.path.join(tmp, "bot-api")
        os.makedirs(downloads)
        os.makedirs(api)
        # config.ensure_dirs() creates this at startup; cleanup refuses to
        # touch a directory without it.
        with open(os.path.join(downloads, ".rayan-owned"), "w") as fh:
            fh.write("test\n")
        old = os.path.join(downloads, "old.mp4")
        active = os.path.join(downloads, "active.mp4")
        thumb = os.path.join(downloads, "thumb_stale.jpg")
        bot_blob = os.path.join(api, "file_1.dat")
        for path, age in ((old, 7200), (active, 5), (thumb, 90000), (bot_blob, 7200)):
            with open(path, "wb") as fh:
                fh.write(b"x" * 1024)
            os.utime(path, (time.time() - age, time.time() - age))
        saved = (rbot.DOWNLOAD_DIR, rbot.API_STORAGE, rbot.DISK_LIMIT_GB,
                 rbot.MIN_FREE_BYTES)
        rbot.DOWNLOAD_DIR, rbot.API_STORAGE = downloads, api
        rbot.DISK_LIMIT_GB, rbot.MIN_FREE_BYTES = 0.000001, 10 ** 18
        try:
            rbot.enforce_disk_limit()
        finally:
            (rbot.DOWNLOAD_DIR, rbot.API_STORAGE, rbot.DISK_LIMIT_GB,
             rbot.MIN_FREE_BYTES) = saved
        try:
            self.assertTrue(os.path.exists(active), "active download was deleted")
            self.assertTrue(os.path.exists(bot_blob), "Bot API blob was deleted")
            self.assertFalse(os.path.exists(old))
            self.assertFalse(os.path.exists(thumb))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disk_cleanup_refuses_directory_without_ownership_marker(self):
        from rayan_dl_bot import bot as rbot
        tmp = tempfile.mkdtemp(prefix="rayan-unowned-")
        downloads = os.path.join(tmp, "shared")
        os.makedirs(downloads)
        victim = os.path.join(downloads, "someone-elses.mp4")
        with open(victim, "wb") as fh:
            fh.write(b"x" * 1024)
        os.utime(victim, (time.time() - 7200, time.time() - 7200))
        saved = (rbot.DOWNLOAD_DIR, rbot.API_STORAGE, rbot.DISK_LIMIT_GB,
                 rbot.MIN_FREE_BYTES)
        rbot.DOWNLOAD_DIR, rbot.API_STORAGE = downloads, ""
        rbot.DISK_LIMIT_GB, rbot.MIN_FREE_BYTES = 0.000001, 10 ** 18
        try:
            rbot.enforce_disk_limit()
        finally:
            (rbot.DOWNLOAD_DIR, rbot.API_STORAGE, rbot.DISK_LIMIT_GB,
             rbot.MIN_FREE_BYTES) = saved
        try:
            self.assertTrue(os.path.exists(victim),
                            "cleanup deleted from a directory it does not own")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rate_limit_is_per_user_and_expires(self):
        from rayan_dl_bot import bot as rbot
        rbot._rate.clear()
        self.assertFalse(rbot.rate_limited(1, seconds=60))
        self.assertTrue(rbot.rate_limited(1, seconds=60))
        self.assertFalse(rbot.rate_limited(2, seconds=60))
        rbot._rate[1] = time.time() - 61
        self.assertFalse(rbot.rate_limited(1, seconds=60))
        rbot._rate.clear()

    def test_upload_cap_matches_local_bot_api(self):
        from rayan_dl_bot import bot as rbot
        cap = rbot.upload_cap_mb()
        self.assertIsInstance(cap, int)
        self.assertGreater(cap, 0)
        self.assertLessEqual(cap, 2000)


# ─────────────────────────────────────────────────────────────
# Network guard
# ─────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_hosts = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Handler.seen_hosts.append(self.headers.get("Host"))
        if self.path == "/ok":
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            body = b"x" * 10000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(50):
                chunk = b"x" * 500
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/redirect-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(2)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class NetworkGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_blocked_destinations(self):
        urls = [
            "http://127.0.0.1/", "http://[::1]/", "http://10.1.2.3/",
            "http://192.168.1.1/", "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/",
            "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/",
            "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/",
            "file:///etc/passwd", "gopher://example.com/", "ftp://example.com/",
            "http://example.com:99999/", "http:///missing-host",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(netguard.NetGuardError):
                    netguard.parse_target(url)

    def test_public_urls_allowed(self):
        self.assertEqual(netguard.parse_target("https://example.com/a").port, 443)
        self.assertEqual(netguard.parse_target("http://example.com:8080/a").port, 8080)

    def test_dns_rebinding_mixed_answers_fail_closed(self):
        target = netguard.parse_target("http://rebind.example/")
        real = netguard.socket.getaddrinfo

        def fake(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

        netguard.socket.getaddrinfo = fake
        try:
            with self.assertRaises(netguard.DestinationBlocked):
                netguard.resolve_public(target)
        finally:
            netguard.socket.getaddrinfo = real

    def test_one_resolution_pins_the_connection_and_preserves_host_header(self):
        calls = []
        real = netguard.resolve_public

        def counting(target):
            calls.append(target.host)
            return ["127.0.0.1"]

        netguard.resolve_public = counting
        try:
            resp = netguard.fetch(f"http://example.test:{self.port}/ok",
                                  timeout=5, total_timeout=10)
        finally:
            netguard.resolve_public = real
        self.assertEqual(resp.body, b"hello")
        self.assertEqual(calls, ["example.test"])
        self.assertIn(f"example.test:{self.port}", _Handler.seen_hosts)

    def test_size_cap_content_length_and_streaming(self):
        with self.assertRaises(netguard.ResponseTooLarge):
            netguard.fetch(f"http://127.0.0.1:{self.port}/big", max_bytes=100,
                           allow_private=True, timeout=5, total_timeout=10)
        with self.assertRaises(netguard.ResponseTooLarge):
            netguard.fetch(f"http://127.0.0.1:{self.port}/chunked", max_bytes=1000,
                           allow_private=True, timeout=5, total_timeout=10)

    def test_redirect_to_file_scheme_blocked(self):
        with self.assertRaises(netguard.NetGuardError):
            netguard.fetch(f"http://127.0.0.1:{self.port}/redirect-file",
                           allow_private=True, timeout=5, total_timeout=10)

    def test_redirect_limit(self):
        with self.assertRaises(netguard.TooManyRedirects):
            netguard.fetch(f"http://127.0.0.1:{self.port}/redirect-loop",
                           allow_private=True, timeout=5, total_timeout=10,
                           max_redirects=3)

    def test_timeout_is_enforced(self):
        started = time.monotonic()
        with self.assertRaises(netguard.NetGuardError):
            netguard.fetch(f"http://127.0.0.1:{self.port}/slow", allow_private=True,
                           timeout=0.2, total_timeout=1.5)
        self.assertLess(time.monotonic() - started, 1.6)

    def test_fetch_to_file_cap_and_cleanup(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ng-"), "out.bin")
        with self.assertRaises(netguard.ResponseTooLarge):
            netguard.fetch_to_file(f"http://127.0.0.1:{self.port}/big", path,
                                   max_bytes=100, allow_private=True)
        self.assertFalse(os.path.exists(path), "partial file left behind")

    def test_tls_verification_is_on_by_default(self):
        ctx = netguard.default_ssl_context()
        self.assertEqual(ctx.verify_mode, 2)  # ssl.CERT_REQUIRED
        self.assertTrue(ctx.check_hostname)
        from rayan_dl_bot import downloader, ig_tw
        self.assertEqual(downloader.TLS_OPTS, [])
        self.assertEqual(ig_tw.TLS_OPTS, [])

    def test_legitimate_public_https(self):
        # Try several independent hosts so the test does not hinge on one
        # provider being reachable from this VPS.
        errors = []
        for url in ("https://example.com/", "https://www.iana.org/",
                    "https://raw.githubusercontent.com/"):
            try:
                resp = netguard.fetch(url, timeout=10, total_timeout=20)
            except netguard.NetGuardError as exc:
                errors.append(f"{url}: {exc}")
                continue
            self.assertEqual(resp.status, 200)
            self.assertGreater(len(resp.body), 0)
            return
        self.skipTest("no outbound HTTPS host reachable: " + "; ".join(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
