#!/usr/bin/env python3
"""Tests for the 2026-08-25 subtitle-UX overhaul (mission: ox-alpha).

T1  sub|{id}|{lang} → subq|{id}|{lang}|{quality} → submode|{id}|{lang}|{quality}|{mode}
    callback chain; each step edits the SAME message.
T2  quality-menu split: main page = 1080/720/480 only + «کیفیت‌های دیگر»
    overflow page (paginated ≤8); none-of-three → all heights on main.
T3  translation hardening: daily word budget (900) with pre-flight skip,
    single-30s-retry then abort on 429, no cue-by-cue storm, (video_id, lang)
    srt cache for 7 days, timecodes preserved.
T4  _edit_retry/_answer_retry: one transient TimedOut never kills the flow.

Offline only — network, Telegram API and the real db module are monkeypatched.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_subs_ux.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest.mock as mock
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
BOT_DIR = os.path.join(REPO_ROOT, "rayan_dl_bot")
sys.path.insert(0, BOT_DIR)

from rayan_dl_bot import bot as rbot          # noqa: E402
from rayan_dl_bot import subtitle_translate as st  # noqa: E402
# FLAT import on purpose: subtitle_translate does `import translation_engines`,
# which resolves to the top-level module — importing it via the package
# namespace would create a SECOND module object and patches would miss.
import translation_engines as te              # noqa: E402

_passed = _failed = 0


# ==================================================================
# T2 — split_qualities pure helper (three cases mandated by user)
# ==================================================================
def test_split_three_main_heights():
    main, pages = rbot.split_qualities([1080, 720, 480, 360, 240])
    assert main == [1080, 720, 480], f"trio expected, got {main}"
    assert pages == [[360, 240]], "rest goes to overflow pages"


def test_split_partial_trio():
    # trio members present stay; everything else overflows (desc order)
    main, pages = rbot.split_qualities([1440, 1080, 768, 480])
    assert main == [1080, 480]
    assert pages == [[1440, 768]]
    # 720-only case: no 480/1080 → just 720 on main
    main3, pages3 = rbot.split_qualities([1440, 768, 720])
    assert main3 == [720] and pages3 == [[1440, 768]]
    # 1080+720 present but no 480 → both on main
    main2, pages2 = rbot.split_qualities([1440, 1080, 720])
    assert main2 == [1080, 720] and pages2 == [[1440]]


def test_split_none_of_three_shows_all_on_main():
    """User mandate: if none of 1080/720/480 exists, ALL heights stay on the
    main page exactly as today (no extra click to see anything)."""
    main, pages = rbot.split_qualities([1440, 768, 360])
    assert main == [1440, 768, 360]
    assert pages == []


def test_split_edge_cases():
    assert rbot.split_qualities([]) == ([], [])
    assert rbot.split_qualities(None) == ([], [])
    assert rbot.split_qualities(["720", 1080]) == ([1080, 720], [])   # str ok, dedup+sort
    assert rbot.split_qualities([720, 720]) == ([720], [])            # dedup
    # junk heights poison the WHOLE set comprehension → degenerate to empty
    # (caller falls back: more_qualities serves the full menu instead)
    assert rbot.split_qualities(["abc"]) == ([], [])


def test_split_pagination_cap_eight():
    heights = [2160, 1440] + [1000 - i for i in range(12)]            # 14 others
    main, pages = rbot.split_qualities(heights)
    flat = [h for p in pages for h in p]
    assert len(flat) == len(heights) - len(main), "no height lost"
    assert all(len(p) <= 8 for p in pages), "≤8 per overflow page"


# ==================================================================
# T1 — sub-quality chain helpers + callback routing
# ==================================================================
def test_sub_quality_main_buttons_use_subq_chain():
    btns, has_others = rbot.sub_quality_main_buttons(
        [1080, 720, 480, 360], {"720": 500 * 1024 * 1024}, "ab12cd34", "fa")
    datas = [cb for _, cb in btns]
    assert datas == ["subq|ab12cd34|fa|1080", "subq|ab12cd34|fa|720",
                     "subq|ab12cd34|fa|480"]
    assert has_others is True
    by_cb = dict((cb, lbl) for lbl, cb in btns)
    assert by_cb["subq|ab12cd34|fa|720"] == "🎥 720p (500.0MB)"
    assert by_cb["subq|ab12cd34|fa|1080"] == "🎥 1080p"          # no size → bare label


def test_sub_quality_other_buttons_paginate():
    heights = [1080, 720, 480] + list(range(400, 391, -1))         # trio + 9 others
    btns, total = rbot.sub_quality_other_buttons(heights, {}, "dl000001", "en", 0)
    assert total == 2 and len(btns) == 8                              # ≤8/page
    assert all(cb.startswith("subq|dl000001|en|") for _, cb in btns)
    btns2, _ = rbot.sub_quality_other_buttons(heights, {}, "dl000001", "en", 1)
    assert len(btns2) == 1                                            # remainder page


async def _drive_button_click(cb_data: str, user_data: dict):
    """Drive button_click with a stubbed query whose message records edits."""
    sent = {}

    class Msg:
        async def edit_text(self, text=None, parse_mode=None,
                            reply_markup=None, **kw):
            sent["text"] = text
            sent["markup"] = reply_markup
            sent.setdefault("edits", []).append(text)

        async def reply_text(self, text=None, parse_mode=None,
                             reply_markup=None, **kw):
            sent["text"] = text
            sent["markup"] = reply_markup

        async def reply_document(self, *a, **k):
            sent.setdefault("docs", []).append(k.get("filename"))

    class Q:
        data = cb_data
        from_user = type("U", (), {"id": 987654})()

        def __init__(self):
            self.message = Msg()

        async def answer(self, *a, **k):
            if a:
                sent["answered"] = a[0]
            else:
                sent.setdefault("answered_plain", True)

    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ctx.user_data = user_data
    ctx.bot_data = {}
    with mock.patch.object(rbot, "lang_of", lambda u: "fa"):
        await rbot.button_click(update, ctx)
    return sent


def test_chain_step1_sub_routes_to_quality_picker_same_message():
    ud = {
        "suburl_ab12cd34": "https://youtu.be/dQw4w9WgXcQ",
        "dl_ab12cd34": {"url": "https://youtu.be/dQw4w9WgXcQ",
                        "info": {}, "heights": [1080, 720, 480]},
    }
    sent = asyncio.run(_drive_button_click("sub|ab12cd34|de", ud))
    kb = sent.get("markup")
    assert kb is not None, "step 2 must show the quality picker"
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "subq|ab12cd34|de|1080" in datas and "subq|ab12cd34|de|720" in datas \
        and "subq|ab12cd34|de|480" in datas
    assert "کیفیت" in (sent.get("text") or ""), "fa quality prompt shown"


def test_chain_step2_subq_routes_to_mode_picker_same_message():
    ud = {"suburl_ab12cd34": "https://youtu.be/x"}
    sent = asyncio.run(_drive_button_click("subq|ab12cd34|de|720", ud))
    kb = sent.get("markup")
    assert kb is not None, "step 3 must show the mode picker"
    modes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "submode|ab12cd34|de|soft" in modes
    assert "submode|ab12cd34|de|srt" in modes
    assert "720p" in (sent.get("text") or ""), "chosen quality echoed"
    hard_disabled = not any("hard" in m for m in modes)
    assert hard_disabled, "hardsub stays disabled"


def test_chain_step3_submode_executes_with_chosen_quality():
    """Full step-4 execution on a stubbed downloader: the quality picked at
    subq| reaches download_video_with_subs, srt + translation delivered."""
    tmp = tempfile.mkdtemp(prefix="subsux_")
    srt_path = os.path.join(tmp, "sub_1_ab.srt")
    # bot derives tr_path as srt.replace(".srt", f"_{target}.srt")
    tr_path = os.path.join(tmp, "sub_1_ab_fa.srt")
    open(srt_path, "w").write(
        "1\n00:00:01,000 --> 00:00:02,000\nhello world\n")
    open(tr_path, "w").write(
        "1\n00:00:01,000 --> 00:00:02,000\nسلام جهان\n")
    mkv_path = os.path.join(tmp, "out.mkv")
    open(mkv_path, "w").write("MKV")

    calls = {}

    class FakeDl:
        human_size = staticmethod(rbot.dl.human_size)

        @staticmethod
        def cleanup(p):
            pass

        @staticmethod
        def download_subtitle(url, lang, out):
            calls["sub"] = (url, lang)
            return srt_path

        @staticmethod
        def download_video_with_subs(url, lang, out, q):
            calls["soft_q"] = q
            return mkv_path

    ud = {
        "suburl_ab12cd34": "https://youtu.be/x",
        "subq_ab12cd34": "480",
        "dl_ab12cd34": {"url": "https://youtu.be/x",
                        "info": {"title": "Test Video"}, "heights": [1080, 720, 480]},
    }
    with mock.patch.object(rbot, "dl", FakeDl), \
         mock.patch.multiple(rbot.subtitle_translate,
                             cache_get=lambda vid, lang: None,
                             cache_put=lambda vid, lang, src: None,
                             translate_srt_file=lambda *a, **k: True,
                             BudgetExhausted=st.BudgetExhausted), \
         mock.patch.object(rbot.db, "record_download"), \
         mock.patch.object(rbot, "_fast_mode_on", lambda uid: False), \
         mock.patch.object(rbot, "lang_of", lambda u: "fa"):
        sent = asyncio.run(_drive_button_click("submode|ab12cd34|de|soft", ud))
    assert calls.get("sub") == ("https://youtu.be/x", "de"), "srt fetched for right url+lang"
    assert calls.get("soft_q") == 480, f"chosen quality must reach softsub dl, got {calls}"
    docs = sent.get("docs") or []
    assert any("سافت ساب" in d for d in docs), "mkv delivered"
    assert any("[de]" in d for d in docs), "original srt delivered"
    assert any("[fa]" in d for d in docs), "translated srt delivered"


def test_chain_expired_link_alerts_and_stops():
    sent = asyncio.run(_drive_button_click("sub|deadbeef|fa", {}))
    assert sent.get("answered") == rbot.i18n.t("fa", "link_expired")


def test_more_callback_opens_others_page():
    def _dl_datas(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row
                if b.callback_data.startswith("dl|")]

    # 7 overflow heights → ONE page (≤8/page): close present, no next button
    ud = {"dl_ab12cd34": {"url": "u", "info": {},
                          "heights": [1080, 720, 480, 432, 410, 405, 400, 396, 392, 388]}}
    sent = asyncio.run(_drive_more_click("more|ab12cd34|0", ud))
    kb = sent.get("markup")
    assert kb is not None
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    dl_datas = _dl_datas(kb)
    assert "dl|ab12cd34|432" in dl_datas, "overflow heights start normal downloads"
    assert "dl|ab12cd34|1080" not in dl_datas, "main trio NOT repeated here"
    assert len(dl_datas) == 7, f"all overflow heights listed, got {dl_datas}"
    assert "cancel" in datas, "close button present"
    assert "more|ab12cd34|1" not in datas, "no next page when ≤8 others"
    # >8 overflow heights → pagination appears
    many = {"dl_ab12cd34": {"url": "u", "info": {},
                            "heights": [1080, 720, 480] + list(range(430, 340, -1))}}
    sent2 = asyncio.run(_drive_more_click("more|ab12cd34|0", many))
    datas2 = [b.callback_data for row in sent2["markup"].inline_keyboard for b in row]
    assert "more|ab12cd34|1" in datas2, "next-page button when >8 others"
    assert len(_dl_datas(sent2["markup"])) == 8, "≤8 per page"


async def _drive_more_click(cb_data: str, user_data: dict):
    """more| has its OWN handler (registered with pattern ^more before
    button_click) — drive more_qualities directly."""
    sent = {}

    class Msg:
        async def edit_text(self, text=None, reply_markup=None, **kw):
            sent["text"] = text
            sent["markup"] = reply_markup

    class Q:
        data = cb_data
        from_user = type("U", (), {"id": 987654})()
        message = Msg()

        async def answer(self, *a, **k):
            pass

    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ctx.user_data = user_data
    ctx.bot_data = {}
    with mock.patch.object(rbot, "lang_of", lambda u: "fa"):
        await rbot.more_qualities(update, ctx)
    return sent


# ==================================================================
# T3 — budget counter, pre-flight skip, 30s backoff/abort, cache
# ==================================================================
def _fake_db(used_start=0):
    state = {"n": used_start}

    def get_config(k, d=None):
        return str(state["n"])

    def set_config(k, v):
        state["n"] = int(v)

    return types.SimpleNamespace(get_config=get_config, set_config=set_config), state


SRT = ("1\n00:00:01,000 --> 00:00:02,000\nhello world\n\n"
       "2\n00:00:02,000 --> 00:00:03,000\nsecond line here\n")


def _srt_files():
    d = tempfile.mkdtemp(prefix="subsux_t3_")
    src = os.path.join(d, "a.srt")
    dst = os.path.join(d, "b.srt")
    open(src, "w").write(SRT)
    return src, dst


class _Resp:
    def __init__(self, payload):
        self._p = json.dumps({"responseData": {"translatedText": payload}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def test_budget_counter_accumulates_in_db_config():
    fdb, state = _fake_db(used_start=0)
    with mock.patch.dict(sys.modules, {"db": fdb}):
        st.budget_add_words(120)
        st.budget_add_words(30)
        assert state["n"] == 150, "counter persists via db config key"
        assert st.budget_words_used() == 150


def test_budget_preflight_skip_no_network():
    src, dst = _srt_files()
    fdb, _ = _fake_db(used_start=897)          # est ~5 words → 902 > 900
    calls = {"n": 0}

    def boom(req, timeout=None):
        calls["n"] += 1
        raise AssertionError("network call attempted despite exhausted budget")

    # chain has ONLY the budgeted engine → pre-flight skip fires; unlimited
    # engines would bypass this (covered in test_tr_engines.py)
    with mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["mymemory"]), \
         mock.patch.object(st.time, "sleep", lambda s: None), \
         mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", boom):
        try:
            st.translate_srt_file(src, dst, "fa", "en")
            assert False, "BudgetExhausted expected"
        except st.BudgetExhausted:
            pass
    assert calls["n"] == 0, "pre-flight check must run BEFORE any HTTP call"
    assert not os.path.exists(dst), "nothing half-written on skip"


def test_unlimited_engine_bypasses_budget_preflight():
    """Gateway engines are unlimited — a spent MyMemory budget must NOT block
    them. The pre-flight skip only applies when no unlimited engine exists."""
    src, dst = _srt_files()
    fdb, _ = _fake_db(used_start=897)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        body = json.dumps({"choices": [{"message": {
            "content": "1. سلام\n2. دنیا"}}]}).encode()
        return _RawResp(body)

    with mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["gateway_ox"]), \
         mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", fake_urlopen), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        ok = st.translate_srt_file(src, dst, "fa", "en")
        assert ok and "سلام" in open(dst).read(), "gateway translates despite budget"
        # budget counter untouched by unlimited engines
        assert st.budget_words_used() == 897, "unlimited engine must not spend budget"


def test_chain_fallback_second_engine_used_when_first_fails():
    src, dst = _srt_files()
    fdb, _ = _fake_db()
    calls = []

    def flaky_gateway(req, timeout=None):
        calls.append("gw")
        raise urllib.error.URLError("gateway down")

    def ok_mymemory(req, timeout=None):
        calls.append("mm")
        return _Resp("سلام ||| دو")

    real_urlopen = te.urllib.request.urlopen

    def router(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else ""
        if "localhost" in url or "20128" in url:
            return flaky_gateway(req, timeout)
        return ok_mymemory(req, timeout)

    with mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["gateway_ox", "mymemory"]), \
         mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", router), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        ok = st.translate_srt_file(src, dst, "fa", "en")
    assert ok and "سلام" in open(dst).read(), "second engine carried the batch"
    assert "gw" in calls and "mm" in calls, "fallback actually happened"
    # budget counted ONLY for the budgeted engine that succeeded
    used = int(fdb.get_config("x"))
    assert used > 0, "mymemory spend recorded"


def test_no_cue_by_cue_fallback_exists():
    """C1 root cause: the per-cue fan-out must be GONE from the source."""
    import inspect
    body = inspect.getsource(st) + inspect.getsource(te)
    assert "for t in batch:" not in body, "cue-by-cue fallback deleted"
    # 2 cues → 1 batch; every engine fails it → exactly ONE attempt total,
    # originals kept, whole file reported as failed (no retry storm).
    src, dst = _srt_files()
    fdb, _ = _fake_db()
    calls = {"n": 0}

    def garbage(req, timeout=None):
        calls["n"] += 1
        return _Resp("garbage-no-separator")

    with mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["mymemory"]), \
         mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", garbage), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        try:
            st.translate_srt_file(src, dst, "fa", "en")
            assert False, "TranslationFailed expected when all engines fail"
        except st.TranslationFailed:
            pass
    assert calls["n"] == 1, f"1 batch × 1 engine × 1 attempt, got {calls['n']}"
    assert not os.path.exists(dst), "nothing written on total failure"


def test_translation_cache_roundtrip_and_ttl():
    d = tempfile.mkdtemp(prefix="subsux_cache_")
    srcf = os.path.join(d, "in.srt")
    open(srcf, "w").write(SRT)
    with mock.patch.object(st, "CACHE_DIR", d):
        assert st.cache_get("vid123", "fa") is None, "cold cache miss"
        st.cache_put("vid123", "fa", srcf)
        hit = st.cache_get("vid123", "fa")
        assert hit and open(hit).read() == SRT, "cache hit returns content"
        assert st.cache_get("vid123", "en") is None, "per-lang keys distinct"
        with mock.patch.object(st, "CACHE_TTL_S", -1):
            assert st.cache_get("vid123", "fa") is None, "expired entry dropped"
    # stable path per (video_id, lang)
    assert st.cache_path("v", "fa") == st.cache_path("v", "fa")
    assert st.cache_path("v", "fa") != st.cache_path("v2", "fa")


class _RawResp:
    """urlopen stand-in returning an arbitrary pre-encoded body (LLM JSON)."""

    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def test_timecodes_preserved_after_translation():
    src, dst = _srt_files()
    fdb, _ = _fake_db()

    def good(req, timeout=None):
        return _Resp("سلام ||| دو")

    with mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["mymemory"]), \
         mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", good), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        ok = st.translate_srt_file(src, dst, "fa", "en")
    out = open(dst).read()
    assert ok
    assert out.count("-->") == 2
    assert "00:00:01,000 --> 00:00:02,000" in out
    assert "سلام" in out and "دو" in out


# ==================================================================
# T4 — edit/answer retry wrappers survive one TimedOut
# ==================================================================
class _FlakyMsg:
    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = 0

    async def edit_text(self, *a, **k):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            import telegram.error as terr
            raise terr.TimedOut()


def test_edit_retry_survives_one_timeout():
    msg = _FlakyMsg(fail_times=1)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def run():
        with mock.patch.object(rbot.asyncio, "sleep", fake_sleep):
            return await rbot._edit_retry(msg, "hi")

    res = asyncio.run(run())
    # edit_text returns None on success anyway — assert NO raise + 2 attempts
    assert res is None and msg.calls == 2, "one TimedOut → retried once → success"
    assert 2.0 in sleeps or 2 in sleeps, "retry happens after the 2s backoff"


def test_edit_retry_gives_up_gracefully_after_two_timeouts():
    msg = _FlakyMsg(fail_times=99)   # always times out
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def run():
        with mock.patch.object(rbot.asyncio, "sleep", fake_sleep):
            return await rbot._edit_retry(msg, "hi")

    res = asyncio.run(run())
    assert res is None and msg.calls == 2, \
        "wrapper logs and returns — a dead edit must not kill the flow"
    assert 2.0 in sleeps


def test_answer_retry_never_raises():
    class Dead:
        calls = 0

        async def answer(self, *a, **k):
            type(self).calls += 1
            raise RuntimeError("TimedOut")

    async def run():
        return await rbot._answer_retry(Dead(), "x")

    res = asyncio.run(run())
    assert res is None, "wrapper swallows after retries — flow continues"
    assert Dead.calls == 2, "attempts=2 (one retry after 2s)"


def test_more_callback_on_media_message_edits_reply_markup():
    """Video messages have text=None; more_qualities must edit reply markup without error."""
    sent = {}

    class MediaMsg:
        text = None

        async def edit_reply_markup(self, reply_markup=None):
            sent["markup"] = reply_markup

        async def edit_text(self, *a, **kw):
            raise RuntimeError("BadRequest: There is no text in the message to edit")

    class Q:
        data = "more|testdl12|0"
        from_user = type("U", (), {"id": 123456})()
        message = MediaMsg()

        async def answer(self, *a, **k):
            pass

    ud = {"dl_testdl12": {"url": "https://example.com/v", "info": {},
                          "heights": [1080, 720, 480, 360, 240, 144]}}
    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ctx.user_data = ud
    ctx.bot_data = {}

    async def run():
        with mock.patch.object(rbot, "lang_of", lambda u: "fa"):
            await rbot.more_qualities(update, ctx)

    asyncio.run(run())
    assert sent.get("markup") is not None
    datas = [b.callback_data for row in sent["markup"].inline_keyboard for b in row]
    assert "dl|testdl12|360" in datas
    assert "dl|testdl12|240" in datas
    assert "dl|testdl12|144" in datas
    assert "backdl|testdl12" in datas


def test_backdl_callback_restores_main_menu():
    """backdl| callback restores the main options under the video."""
    sent = {}

    class MediaMsg:
        text = None

        async def edit_reply_markup(self, reply_markup=None):
            sent["markup"] = reply_markup

    class Q:
        data = "backdl|testdl12"
        from_user = type("U", (), {"id": 123456})()
        message = MediaMsg()

        async def answer(self, *a, **k):
            pass

    dummy_main_kb = [
        [rbot.InlineKeyboardButton("🎥 1080p", callback_data="dl|testdl12|1080")],
        [rbot.InlineKeyboardButton("🎥 کیفیت‌های دیگر", callback_data="more|testdl12|0")],
        [rbot.InlineKeyboardButton("🎵 فقط صدا", callback_data="dl|testdl12|audio")],
    ]
    ud = {
        "more_testdl12": dummy_main_kb,
        "dl_testdl12": {"url": "https://example.com/v", "info": {},
                        "heights": [1080, 720, 480, 360, 240, 144]}
    }
    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ctx.user_data = ud
    ctx.bot_data = {}

    async def run():
        with mock.patch.object(rbot, "lang_of", lambda u: "fa"):
            await rbot.button_click(update, ctx)

    asyncio.run(run())
    assert sent.get("markup") is not None
    datas = [b.callback_data for row in sent["markup"].inline_keyboard for b in row]
    assert "dl|testdl12|1080" in datas
    assert "more|testdl12|0" in datas
    assert "dl|testdl12|audio" in datas



# ------------------------------------------------------------------
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
            _passed += 1
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            _failed += 1
    print(f"\n{_passed}/{len(fns)} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
