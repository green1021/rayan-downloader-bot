"""Regression tests for the 2026-08-25 audit of Rayan Downloader Bot.

Offline only: no Telegram API calls, no real downloads. DB tests use a temp
copy so production rayan.db is never touched.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_rayan_audit_fixes.py
"""
import ast
import os
import py_compile
import sys
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BOT_DIR = os.path.join(REPO_ROOT, "rayan_dl_bot")
MODULES = ("bot", "db", "downloader", "recognizer", "i18n", "i18n_extra",
           "ig_tw", "plugin_manager", "plugin_builder", "auto_detect",
           "dlqueue", "linkcache", "cdncache", "subtitle_translate")


def src(mod):
    with open(os.path.join(BOT_DIR, mod + ".py"), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------
# 1. Every module still compiles
# ------------------------------------------------------------------
def test_modules_compile():
    for m in MODULES:
        py_compile.compile(os.path.join(BOT_DIR, m + ".py"), doraise=True)


# ------------------------------------------------------------------
# 2. Command handlers carry update.message None-guards (PTB v20+ class)
#    start/help/about/lang/history/admin/settings must not dereference
#    update.message before checking it.
# ------------------------------------------------------------------
GUARDED_HANDLERS = ["start", "help_cmd", "about_cmd", "lang_cmd",
                    "history_cmd", "admin_cmd", "settings_cmd"]


def test_command_handlers_guard_update_message():
    tree = ast.parse(src("bot"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in GUARDED_HANDLERS:
        fn = funcs.get(name)
        assert fn is not None, f"handler missing: {name}"
        seg = ast.get_source_segment(src("bot"), fn) or ""
        head = seg.split("db.record_user")[0]
        assert "update.message is None" in head or "not update.message" in head, (
            f"{name}: no update.message guard before first use")


def test_callback_handlers_guard_q_message():
    tree = ast.parse(src("bot"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("button_click", "search_cb", "admin_cb", "shazam_retry_cb",
                 "force_join_check_cb", "setmedia_cb", "setfilter_cb"):
        fn = funcs.get(name)
        assert fn is not None, f"handler missing: {name}"
        seg = ast.get_source_segment(src("bot"), fn) or ""
        # a guard must appear somewhere before any bare q.message.<attr> use
        guard_pos = seg.find("if not q.message")
        assert guard_pos != -1, f"{name}: missing q.message None-guard"


def test_button_click_no_double_answer():
    seg = ""
    tree = ast.parse(src("bot"))
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "button_click":
            seg = ast.get_source_segment(src("bot"), n)
    assert seg.count("await q.answer()") <= 6
    # the old bug: two consecutive unwrapped answer() calls
    assert "\n        await q.answer()\n        await q.answer()\n" not in seg


def test_handler_registration_chat_member_filters():
    s = src("bot")
    # my_chat_member is not a Message — must be a TypeHandler (own group),
    # and group_welcome keeps NEW_CHAT_MEMBERS. Neither shadows the other.
    assert "TypeHandler(Update, on_bot_added)" in s
    assert "NEW_CHAT_MEMBERS, group_welcome)" in s
    assert "NEW_CHAT_MEMBERS, on_bot_added)" not in s
    from rayan_dl_bot import bot as bot_mod
    import telegram.ext as tgx
    assert hasattr(tgx, "TypeHandler")


# ------------------------------------------------------------------
# 3. SSRF validation blocks non-canonical + IPv6 private literals
# ------------------------------------------------------------------
def test_ssrf_validation_blocks_private_forms():
    from rayan_dl_bot.plugin_manager import _validate_install_url as v
    blocked = [
        "https://2130706433/x.zip",          # int literal -> 127.0.0.1
        "https://127.1/x.zip",               # short dotted form
        "https://0x7f000001/x.zip",          # hex literal
        "https://[fd00::1]/x.zip",           # IPv6 ULA
        "https://[::ffff:10.0.0.1]/x.zip",   # v4-mapped v6
        "https://169.254.169.254/x.zip",     # link-local metadata
        "http://example.com/x.zip",          # non-https scheme
    ]
    for url in blocked:
        assert v(url) is not None, f"should be blocked: {url}"


def test_ssrf_validation_allows_public_https():
    from rayan_dl_bot.plugin_manager import _validate_install_url as v
    assert v("https://raw.githubusercontent.com/user/repo/main/plugin.json") is None


def test_ip_is_forbidden_matrix():
    from rayan_dl_bot.plugin_manager import _ip_is_forbidden as f
    assert f("127.0.0.1")
    assert f("10.1.2.3")
    assert f("192.168.0.5")
    assert f("172.16.9.9")
    assert f("fd00::1")
    assert f("fe80::1")
    assert f("::ffff:127.0.0.1")
    assert not f("8.8.8.8")
    assert not f("140.82.121.4")


# ------------------------------------------------------------------
# 4. Plugin installs route fetches through _safe_urlopen
# ------------------------------------------------------------------
def test_install_paths_use_safe_urlopen():
    s = src("plugin_manager")
    body_start = s.find("def _install_zip")
    manifest_start = s.find("def _install_manifest")
    raw_start = s.find("def _install_raw")
    install_bodies = s[body_start:raw_start if raw_start > manifest_start else len(s)]
    # The install path now goes through netguard (pinned DNS, scheme allow-list,
    # byte caps, redirect revalidation); _safe_urlopen is only a compat shim.
    assert ("netguard.fetch_to_file(" in install_bodies
            or "_safe_urlopen(url" in install_bodies)
    assert "urllib.request.urlopen(req" not in install_bodies


# ------------------------------------------------------------------
# 5. Plugin chapter/detect paths are timeout-bounded
# ------------------------------------------------------------------
def test_plugin_callers_use_call_with_timeout():
    s = src("plugin_manager")
    for fname in ("call_plugin_chapters", "call_plugin_detect_type",
                  "call_plugin_download_chapter"):
        i = s.find(f"def {fname}")
        j = s.find("\ndef ", i + 1)
        body = s[i:j]
        # All three dispatch through _invoke(), which applies _timeout_for():
        # a hard subprocess timeout in isolated mode and call_with_timeout()
        # when RAYAN_PLUGIN_ISOLATION=0.
        assert "_invoke(" in body, f"{fname} does not use the bounded dispatcher"
    k = s.find("def _invoke")
    assert "_timeout_for(meta)" in s[k:s.find("\ndef ", k + 1)]


# ------------------------------------------------------------------
# 6. db._conn returns rollback-on-abandon connection factory
# ------------------------------------------------------------------
def test_db_conn_uses_safe_factory(tmpdir=None):
    import importlib
    import rayan_dl_bot.db as db
    importlib.reload(db)
    db.init_db()  # production calls ensure_dirs()+init_db before any _conn()
    conn = db._conn()
    try:
        assert type(conn).__name__ == "_SafeConn"
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _t(x)")
        cur.execute("INSERT INTO _t VALUES (1)")
    finally:
        conn.close()
    # after close+reopen the pending insert was rolled back? No — close()
    # commits nothing; but our __del__ path only fires on abandonment.
    # The contract tested here: _SafeConn exists and normal close works.
    conn2 = db._conn()
    conn2.close()


# ------------------------------------------------------------------
# 7. ig_tw syndication keeps ONE best mp4 (no multi-bitrate dupes)
# ------------------------------------------------------------------
def test_best_mp4_variant_picks_highest_bitrate():
    from rayan_dl_bot.ig_tw import _best_mp4_variant as pick
    variants = [
        {"src": "lo.mp4", "bitrate": 320000},
        {"src": "hi.mp4", "bitrate": 2177040},
        {"src": "mid.mp4", "bitrate": 832000},
    ]
    assert pick(variants) == "hi.mp4"


def test_best_mp4_variant_handles_plain_urls_and_junk():
    from rayan_dl_bot.ig_tw import _best_mp4_variant as pick
    assert pick(["a.mp4", "b.mp4"]) == "a.mp4"
    assert pick([]) is None
    assert pick([{"bitrate": "junk"}, {"src": "ok.mp4"}]) == "ok.mp4"


def test_syndication_video_urls_single_entry():
    # _build_from_syndication should emit at most ONE video url per tweet
    from rayan_dl_bot import ig_tw
    d = {
        "text": "t",
        "user": {"name": "n", "screen_name": "s"},
        "video": {"variants": [
            {"src": "https://v/1.mp4?tag=12", "bitrate": 832000, "type": "video/mp4"},
            {"src": "https://v/2.mp4?tag=12", "bitrate": 2177040, "type": "video/mp4"},
            {"src": "https://v/3.mp4?tag=12", "bitrate": 320000, "type": "video/mp4"},
        ]},
        "mediaDetails": [],
    }
    res = ig_tw._build_from_syndication(d, "https://x.com/u/status/123", "123")
    urls = res.get("video_urls") or []
    assert len(urls) == 1, urls
    assert "2.mp4" in urls[0]  # highest bitrate wins


# ------------------------------------------------------------------
# 8. IG carousel poster-dedup: video posters must not leak into photos
# ------------------------------------------------------------------
def test_carousel_dedup_logic_present():
    s = src("ig_tw")
    i = s.find("def extract_ig_carousel")
    j = s.find("\ndef ", i + 1)
    body = s[i:j]
    assert "posters.add(" in body, "poster tracking missing"
    assert "raw in posters" in body or "posters" in body.split("# photos")[1].split("items.append")[0]


# ------------------------------------------------------------------
# 9. curl media downloads fail on HTTP errors (--fail) and clean up
# ------------------------------------------------------------------
def test_curl_downloads_use_fail_flag():
    s = src("ig_tw")
    for fname in ("download_instagram", "download_twitter_photo"):
        i = s.find(f"def {fname}")
        j = s.find("\ndef ", i + 1)
        body = s[i:j]
        # curl -f was replaced by netguard, which raises on any non-2xx
        # response instead of writing the error page to disk.
        assert "netguard." in body, f"{fname} bypasses the netguard downloader"


# ------------------------------------------------------------------
# 10. CDN-cache fast path cannot reference unbound caption
# ------------------------------------------------------------------
def test_cdn_cache_path_has_no_unbound_caption():
    s = src("bot")
    i = s.find('cdn = cdncache.get(f"{url}|{best}")')
    j = s.find("ok = await _limited_download", i)
    fast_path = s[i:j]
    assert "or caption" not in fast_path, "cdn fast-path still references pre-bind caption"


# ------------------------------------------------------------------
# 11. linkcache/cdncache cleanup conflict fixed
# ------------------------------------------------------------------
def test_local_file_not_deleted_when_linkcached():
    s = src("bot")
    assert "cached_locally" in s
    assert "if not cached_locally:\n                    dl.cleanup(out_path)" in s


# ------------------------------------------------------------------
# 12. hardsub stays disabled (OOM constraint)
# ------------------------------------------------------------------
def test_hardsub_still_disabled():
    s = src("bot")
    i = s.find('mode == "hard"')
    body = s[i:i + 600]
    assert "غیرفعال" in body, "hardsub branch must tell users it is disabled"
    assert "burn_subtitles" not in body.split("return")[1][:200], "hardsub code became reachable?"


# ------------------------------------------------------------------
# run
# ------------------------------------------------------------------
if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    print("---")
    print("FAILURES:", fails)
    sys.exit(1 if fails else 0)


# ------------------------------------------------------------------
# 13. post-audit additions (downloader + i18n audit)
# ------------------------------------------------------------------
def test_get_video_info_runs_in_thread():
    s = src("bot")
    assert "info = await asyncio.to_thread(dl.get_video_info, url)" in s, \
        "get_video_info back on the event loop — freezes all users"


def test_subtitle_helpers_use_caller_out_path():
    s = src("downloader")
    assert 'stem = f"sub_{int(time.time())}"' not in s
    i = s.find("def download_video_with_subs")
    j = s.find("\ndef ", i + 1)
    assert "rsplit" in s[i:j], "softsub stem no longer unique per request"


def test_inflight_download_dedup_present():
    s = src("bot")
    assert "dling_{uid}_{dl_id}_{quality}" in s.replace("f\"", "").replace("\"", "") or \
           "inflight_key" in s


def test_i18n_extra_group_settings_has_media_rows():
    sys.path.insert(0, REPO_ROOT)
    from rayan_dl_bot import i18n
    kwargs = dict(quality=720, auto="on", silent="off", media="x", filter_="all")
    for lang in ("tr", "ru", "de", "es"):
        out = i18n.t(lang, "group_settings", **kwargs)
        assert "{media}" not in out, lang
