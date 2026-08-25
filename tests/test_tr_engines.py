#!/usr/bin/env python3
"""Tests for the pluggable translation-engine system (2026-08-25, mission 2).

Covers translation_engines.py + the admin «🌍 موتور ترجمه» panel + the
subtitle_translate rewiring onto the chain executor:

E1  registry: ids/labels/needs, defaults merge with db cfg (tr_cfg_*)
E2  chain persistence: get_chain/save_chain defaults + validation
E3  per-engine translate(): mymemory SEP alignment, libre list handling,
    OpenAI-compatible plain-JSON AND SSE-stream responses, numbered-line
    output parsing, missing-config errors
E4  chain executor: first-engine-wins, fallback on failure, all-fail → None,
    wall-clock deadline stops new attempts
S   panel screens: status chips / chain toggles / per-engine cfg / test,
    trfield input state save+mask, callback routing smoke

Offline only — network and the real db module are monkeypatched.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_tr_engines.py
"""
import asyncio
import json
import sys
import types
import unittest.mock as mock
import urllib.error

sys.path.insert(0, "/root/hermes-workspace/scripts")
BOT_DIR = "/root/hermes-workspace/scripts/rayan_dl_bot"
sys.path.insert(0, BOT_DIR)

# FLAT import: subtitle_translate resolves `import translation_engines` to the
# top-level module — the package-qualified path would be a DIFFERENT object.
import translation_engines as te               # noqa: E402
from rayan_dl_bot import subtitle_translate as st   # noqa: E402
from rayan_dl_bot import bot as rbot                # noqa: E402

_passed = _failed = 0


def _fake_db(chain='["gateway_ox", "mymemory"]', **cfgs):
    """db stub: tr_engine_chain + tr_cfg_* keys; other keys get their default."""
    def get_config(key, default=None):
        if key == "tr_engine_chain":
            return chain
        if key.startswith("tr_cfg_"):
            return cfgs.get(key[7:], None)
        return default

    return types.SimpleNamespace(get_config=get_config, set_config=lambda k, v: None)


class _Resp:
    """urlopen stand-in for JSON bodies."""

    def __init__(self, payload):
        self._p = payload.encode() if isinstance(payload, str) else payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _router(responses):
    """urlopen stand-in dispatching by URL substring → response payload,
    exception, or a urlopen-style callable."""
    table = [(substr, val) for substr, val in reversed(list(responses.items()))]

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for substr, val in table:
            if substr in url:
                if isinstance(val, Exception):
                    raise val
                if callable(val):
                    return val(req, timeout=timeout)
                return _Resp(val)
        raise AssertionError(f"unexpected url: {url}")

    return urlopen


# ==================================================================
# E1 — registry & config merge
# ==================================================================
def test_registry_shape_and_needs():
    assert set(te.ENGINES) == {"gateway_ox", "mymemory", "libre", "custom_openai"}
    assert te.ENGINES["gateway_ox"]["needs"] == ["base_url", "model"]
    assert te.ENGINES["custom_openai"]["needs"] == ["base_url", "api_key", "model"]
    # google gtx must never appear (blocked from this server)
    assert not any("gtx" in e or "google" in e.lower() for e in te.ENGINES)
    for eid, meta in te.ENGINES.items():
        assert te.ENGINES[eid]["label_fa"], f"{eid} needs a fa label"


def test_cfg_merges_db_over_defaults():
    fdb = _fake_db(**{"gateway_ox": '{"base_url": "http://x/y", "model": "m1"}'})
    cfg = te._cfg_for("gateway_ox", db_mod=fdb)
    assert cfg["base_url"] == "http://x/y", "db overrides default"
    assert cfg["model"] == "m1"
    # unknown engine → empty; broken JSON → falls back to defaults
    assert te._cfg_for("nope", db_mod=fdb) == {}
    bad = _fake_db(**{"libre": "{not json"})
    assert te._cfg_for("libre", db_mod=bad)["base_url"] == "https://libretranslate.com"


def test_set_engine_cfg_field_validation():
    saved = {}

    class SaverDb(types.SimpleNamespace):
        pass

    fdb = types.SimpleNamespace(
        get_config=lambda k, d=None: None,
        set_config=lambda k, v: saved.__setitem__(k, v))
    assert te.set_engine_cfg("mymemory", "email", " a@b.c ", db_mod=fdb)
    stored = json.loads(saved["tr_cfg_mymemory"])
    assert stored["email"] == "a@b.c", "value trimmed, merged over defaults"
    # invalid engine / invalid field rejected
    assert not te.set_engine_cfg("mymemory", "api_key", "x", db_mod=fdb), \
        "only `needs` fields are settable"
    assert not te.set_engine_cfg("nope", "email", "x", db_mod=fdb)


# ==================================================================
# E2 — chain persistence
# ==================================================================
def test_get_chain_default_and_validation():
    assert te.get_chain(db_mod=_fake_db()) == ["gateway_ox", "mymemory"]
    assert te.get_chain(db_mod=None) == ["gateway_ox", "mymemory"], \
        "no db at all → built-in default"
    # junk / foreign ids filtered out; empty result → default again
    assert te.get_chain(db_mod=_fake_db(chain='["mymemory","gtx"]')) \
        == ["mymemory"]
    assert te.get_chain(db_mod=_fake_db(chain="not-json")) \
        == ["gateway_ox", "mymemory"]
    assert te.get_chain(db_mod=_fake_db(chain='[]')) \
        == ["gateway_ox", "mymemory"]


def test_save_chain_roundtrip_and_rejects_empty():
    saved = {}

    class Saver:
        get_config = staticmethod(lambda k, d=None: None)

        @staticmethod
        def set_config(k, v):
            saved[k] = v

    assert te.save_chain(["gateway_ox", "bogus", "mymemory"], db_mod=Saver())
    assert json.loads(saved["tr_engine_chain"]) == ["gateway_ox", "mymemory"]
    assert not te.save_chain(["nope"], db_mod=Saver()), "empty after filter → reject"


# ==================================================================
# E3 — individual engines
# ==================================================================
MYMEMORY_BODY = json.dumps({"responseData": {"translatedText":
                                             "سلام ||| دو"}})


def test_mymengine_sep_alignment():
    fdb = _fake_db()
    calls = {}

    def fake_urlopen(req, timeout=None):
        calls["url"] = req.full_url
        return _Resp(MYMEMORY_BODY)

    with mock.patch.object(te.urllib.request, "urlopen", fake_urlopen):
        out = te._translate_mymemory(["hello", "two"], "en", "fa",
                                     {"email": "a@b.c"})
    assert out == ["سلام", "دو"]
    assert "de=a%40b.c" in calls["url"], "admin email reaches the de= param"


def test_mymemory_429_maps_to_engine_error():
    e429 = urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)

    def fake_urlopen(req, timeout=None):
        raise e429

    with mock.patch.object(te.urllib.request, "urlopen", fake_urlopen):
        try:
            te._translate_mymemory(["hello"], "en", "fa", {})
            assert False, "EngineError expected"
        except te.EngineError as e:
            assert "429" in str(e)


def test_mymemory_quota_warning_rejected():
    body = json.dumps({"responseData": {
        "translatedText": "MYMEMORY WARNING: QUERY LENGTH LIMIT hit"}})

    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(body)):
        try:
            te._translate_mymemory(["hello"], "en", "fa", {})
            assert False, "quota warning must NOT count as a translation"
        except te.EngineError:
            pass


def test_libre_list_response_and_key_param():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["url"] = req.full_url
        return _Resp(json.dumps({"translatedText": ["سلام", "دو"]}))

    with mock.patch.object(te.urllib.request, "urlopen", fake_urlopen):
        out = te._translate_libre(["hello", "two"], "en", "fa",
                                  {"base_url": "https://lib.re/", "api_key": "K9"})
    assert out == ["سلام", "دو"]
    assert captured["url"].rstrip("/").endswith("/translate")
    assert captured["body"]["api_key"] == "K9"
    assert captured["body"]["q"] == ["hello", "two"], "list mode keeps cues apart"
    # single-string response shape tolerated
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(
                               json.dumps({"translatedText": "سلام"}))):
        out = te._translate_libre(["one"], "en", "fa", {"base_url": "http://l/"})
    assert out == ["سلام"]
    # missing base_url → clean error, no request
    try:
        te._translate_libre(["x"], "en", "fa", {})
        assert False
    except te.EngineError as e:
        assert "base_url" in str(e)


OPENAI_OK = json.dumps({"choices": [{"message": {
    "content": "1. سلام\n2. دنیا"}}]})


def test_llm_plain_json_numbered_parsing():
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(OPENAI_OK)):
        out = te._llm_translate("gateway_ox", ["hello", "world"], "en", "fa",
                                {"base_url": "http://localhost:20128/v1",
                                 "model": "ox-alpha-combo"})
    assert out == ["سلام", "دنیا"]


def test_llm_sse_stream_parsed_from_deltas():
    sse = ('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
           'data: {"choices":[{"delta":{"content":"سلام"}}]}\n\n'
           'data: {"choices":[{"delta":{"content":"\\nدنیا"}}]}\n\n'
           'data: [DONE]\n')
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(sse)):
        out = te._llm_chat("http://gw/v1/chat/completions", "gemini-fallback-combo",
                           "translate this")
    assert out == "سلام\nدنیا", "deltas reassembled in order"


def test_llm_missing_config_raises_before_network():
    def boom(req, timeout=None):
        raise AssertionError("network reached despite missing config")

    with mock.patch.object(te.urllib.request, "urlopen", boom):
        try:
            te._llm_translate("custom_openai", ["x"], "en", "fa", {})
            assert False
        except te.EngineError as e:
            assert "missing config" in str(e)


def test_llm_reasoning_content_ignored_content_used():
    body = json.dumps({"choices": [{"message": {
        "content": "سلام دنیا.",
        "reasoning_content": "long thinking..."}}]})
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(body)):
        out = te._llm_chat("http://gw/v1/chat/completions", "m", "t")
    assert out == "سلام دنیا."


# ==================================================================
# E4 — chain executor semantics
# ==================================================================
def test_executor_first_ok_wins_no_later_calls():
    fdb = _fake_db()
    mm_called = {"n": 0}

    def mm(req, timeout=None):
        mm_called["n"] += 1
        raise AssertionError("second engine called although first succeeded")

    with mock.patch.object(te.urllib.request, "urlopen",
                           _router({"20128": OPENAI_OK, "mymemory.translated.net": mm})):
        out, used = te.translate_batch_via_chain(
            ["hello", "world"], "en", "fa", db_mod=fdb)
    assert used == "gateway_ox" and out == ["سلام", "دنیا"]
    assert mm_called["n"] == 0


def test_executor_falls_through_to_second_engine():
    fdb = _fake_db()
    gw_called, mm_called = {"n": 0}, {"n": 0}

    def gw(req, timeout=None):
        gw_called["n"] += 1
        raise urllib.error.URLError("connection refused")

    def mm(req, timeout=None):
        mm_called["n"] += 1
        return _Resp(MYMEMORY_BODY)

    with mock.patch.object(te.urllib.request, "urlopen",
                           _router({"20128": gw, "mymemory.translated.net": mm})):
        out, used = te.translate_batch_via_chain(
            ["hello", "two"], "en", "fa", db_mod=fdb)
    assert used == "mymemory" and out == ["سلام", "دو"]
    assert gw_called["n"] == 1 and mm_called["n"] == 1


def test_executor_all_fail_returns_none():
    fdb = _fake_db(chain='["gateway_ox", "mymemory"]')

    def down(req, timeout=None):
        raise urllib.error.URLError("no route")

    with mock.patch.object(te.urllib.request, "urlopen", down):
        out, used = te.translate_batch_via_chain(
            ["hello"], "en", "fa", db_mod=fdb)
    assert out is None and used is None


def test_executor_deadline_blocks_new_engines():
    fdb = _fake_db(chain='["gateway_ox", "mymemory"]')

    def slow(req, timeout=None):
        import time as _t
        _t.sleep(0.05)
        return _Resp(OPENAI_OK)   # misaligned count → engine "fails"

    with mock.patch.object(te.urllib.request, "urlopen", slow):
        out, used = te.translate_batch_via_chain(
            ["a", "b"], "en", "fa", db_mod=fdb, deadline=-1.0)
    assert out is None and used is None, "expired deadline → nothing even tried"


def test_executor_result_shape_guard():
    """A lying engine (wrong count / non-str / empty strings) is REJECTED."""
    fdb = _fake_db(chain='["gateway_ox"]')
    body = json.dumps({"choices": [{"message": {"content": "1. فقط یکی"}}]})

    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(body)):
        out, used = te.translate_batch_via_chain(["a", "b"], "en", "fa",
                                                 db_mod=fdb)
    assert out is None, "misaligned LLM answer must not reach the caller"


def test_test_engine_helper_reports_both_ways():
    fdb = _fake_db()
    one_line = json.dumps({"choices": [{"message": {"content": "سلام دنیا."}}]})
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(one_line)):
        ok, detail = te.test_engine("gateway_ox", db_mod=fdb)
    assert ok and detail == "سلام دنیا.", f"got {ok}, {detail!r}"
    with mock.patch.object(te.urllib.request, "urlopen",
                           lambda req, timeout=None:
                           (_ for _ in ()).throw(urllib.error.URLError("x"))):
        ok, detail = te.test_engine("gateway_ox", db_mod=fdb)
    assert not ok and detail, f"got {ok}, {detail!r}"


# ==================================================================
# Batching / SEP validation
# ==================================================================
def test_make_batches_sizes():
    b = te.make_batches([str(i) for i in range(45)])
    assert [len(x) for x in b] == [20, 20, 5]
    assert te.make_batches([]) == []
    assert len(te.make_batches(["x"])) == 1


# ==================================================================
# subtitle_translate wiring
# ==================================================================
SRT = ("1\n00:00:01,000 --> 00:00:02,000\nhello world\n\n"
       "2\n00:00:02,000 --> 00:00:03,000\nsecond line here\n")


def _srt_files(d=None):
    import os
    import tempfile
    d = d or tempfile.mkdtemp(prefix="treng_")
    src = os.path.join(d, "a.srt")
    dst = os.path.join(d, "b.srt")
    open(src, "w").write(SRT)
    return src, dst


def _budget_db(used=0):
    state = {"n": used}

    def get_config(k, d=None):
        if k.startswith("mymemory_words_used"):
            return str(state["n"])
        return None

    def set_config(k, v):
        state["n"] = int(v)

    return types.SimpleNamespace(get_config=get_config, set_config=set_config), state


def test_file_translation_uses_gateway_without_budget_touch():
    import os
    src, dst = _srt_files()
    fdb, state = _budget_db(used=500)
    ok_body = json.dumps({"choices": [{"message": {
        "content": "1. سلام\n2. دو"}}]})

    with mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(st.te.urllib.request, "urlopen",
                           lambda req, timeout=None: _Resp(ok_body)), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        ok = st.translate_srt_file(src, dst, "fa", "en")
    assert ok and "سلام" in open(dst).read() and "-->" in open(dst).read()
    assert state["n"] == 500, "unlimited gateway must NOT spend word budget"


def test_budgeted_only_chain_over_budget_raises_before_network():
    import os
    src, dst = _srt_files()
    fdb, _state = _budget_db(used=897)

    def boom(req, timeout=None):
        raise AssertionError("network call attempted despite exhausted budget")

    # ONLY the budgeted engine configured → pre-flight skip fires before HTTP
    with mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["mymemory"]), \
         mock.patch.object(te.urllib.request, "urlopen", boom):
        try:
            st.translate_srt_file(src, dst, "fa", "en")
            assert False, "BudgetExhausted expected"
        except st.BudgetExhausted:
            pass
    assert not os.path.exists(dst)


def test_failed_batch_keeps_originals_file_still_valid():
    import os
    src, dst = _srt_files()
    # two batches of 2 cues each? no: 2 cues → ONE batch; make 3 files-worth:
    big = "".join(f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{i % 60:02d},500\ncue {i} text\n\n"
                  for i in range(1, 23))                       # 22 cues → 2 batches
    open(src, "w").write(big)
    fdb, _ = _budget_db()

    def garbage(req, timeout=None):
        return _Resp("unusable-no-sep")                        # always misaligned

    with mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te.urllib.request, "urlopen", garbage), \
         mock.patch.object(st.time, "sleep", lambda s: None):
        try:
            st.translate_srt_file(src, dst, "fa", "en")
            assert False, "all batches failed → TranslationFailed"
        except st.TranslationFailed:
            pass
    assert not os.path.exists(dst), "total failure writes nothing"


def test_wall_clock_cap_keeps_originals_for_rest():
    """Once the executor's deadline has passed, no engine is started for the
    remaining batches — they keep originals and the file still completes."""
    import os
    src, dst = _srt_files()
    big = "".join(f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{i % 60:02d},500\ncue {i} text\n\n"
                  for i in range(1, 23))                       # 22 cues → 2 batches
    open(src, "w").write(big)
    fdb, _ = _budget_db()
    ok_body = json.dumps({"choices": [{"message": {
        "content": "\n".join(f"{i}. ترجمه{i}" for i in range(1, 21))}}]})
    calls = {"n": 0}

    def counting_urlopen(req, timeout=None):
        calls["n"] += 1
        return _Resp(ok_body)

    # First batch runs (deadline not yet passed), then the deadline expires:
    # the SECOND batch must keep originals without any further HTTP.
    real_mono = te.time.monotonic

    def one_batch_then_deadline():
        t = real_mono()
        if calls["n"] >= 1:
            return t + st.WALL_CLOCK_S * 10        # way past any deadline
        return t

    with mock.patch.dict(sys.modules, {"db": fdb}), \
         mock.patch.object(te, "get_chain",
                           lambda db_mod=None: ["gateway_ox"]), \
         mock.patch.object(te.urllib.request, "urlopen", counting_urlopen), \
         mock.patch.object(st.time, "monotonic", lambda: one_batch_then_deadline()):
        ok = st.translate_srt_file(src, dst, "fa", "en")
    assert ok, "file still delivered"
    out = open(dst).read()
    assert out.count("-->") == 22, "every cue present"
    assert "ترجمه" in out, "first batch translated before the cap"
    assert calls["n"] == 1, f"only batch 1 hit the network, got {calls['n']} calls"
    assert "cue 21 text" in out, "post-cap batch kept originals"


# ==================================================================
# Panel screens — callback routing smoke tests
# ==================================================================
class _Msg:
    def __init__(self):
        self.texts = []
        self.markups = []

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def reply_text(self, *a, **k):
        self.texts.append(a[0] if a else k.get("text"))
        self.markups.append(k.get("reply_markup"))


async def _drive_admin_cb(cb_data, user_data=None):
    msg = _Msg()

    class Q:
        data = cb_data
        from_user = type("U", (), {"id": 987654})()
        message = msg

        async def answer(self, *a, **k):
            pass

    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot_data = {}
    with mock.patch.object(rbot, "lang_of", lambda u: "fa"), \
         mock.patch.object(rbot, "is_admin", lambda uid: True):
        await rbot.admin_cb(update, ctx)
    return msg


def _btn_datas(msg):
    kb = msg.markups[-1]
    if kb is None:
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_panel_status_screen_shows_ordered_chips():
    msg = asyncio.run(_drive_admin_cb("adm|trengines"))
    assert any("موتور ترجمه" in t for t in msg.texts)
    datas = _btn_datas(msg)
    assert "adm|trchain" in datas and "adm|trconfig" in datas and "adm|trtest" in datas


def test_panel_chain_screen_lists_all_engines_with_marks():
    fdb = _fake_db(chain='["mymemory"]')

    async def go():
        msg = _Msg()

        class Q(_Msg):
            pass

        class QObj:
            data = "adm|trchain"

            class from_user:
                id = 1

            message = msg

            async def answer(self, *a, **k):
                pass

        update = mock.MagicMock()
        update.callback_query = QObj()
        ctx = mock.MagicMock()
        ctx.user_data = {}
        ctx.bot_data = {}
        with mock.patch.object(rbot, "lang_of", lambda u: "fa"), \
             mock.patch.object(rbot, "is_admin", lambda uid: True), \
             mock.patch.object(te, "get_chain", lambda db_mod=None: ["mymemory"]):
            await rbot.admin_cb(update, ctx)
        return msg

    msg = asyncio.run(go())
    assert any("✅ 1." in t for t in msg.texts), "in-chain engine numbered"
    datas = _btn_datas(msg)
    for eid in ("gateway_ox", "mymemory", "libre", "custom_openai"):
        assert f"adm|trtoggle|{eid}" in datas


def test_panel_toggle_updates_chain_order():
    saved = {}

    class ToggleDb:
        @staticmethod
        def get_config(k, d=None):
            return saved.get(k, '["gateway_ox"]')

        @staticmethod
        def set_config(k, v):
            saved[k] = v

    async def go():
        ud = {}
        msg = await _drive_inner("adm|trtoggle|libre", ud)
        return msg

    async def _drive_inner(cb, ud):
        msg = _Msg()

        class QObj:
            data = cb

            class from_user:
                id = 1

            message = msg

            async def answer(self, *a, **k):
                pass

        update = mock.MagicMock()
        update.callback_query = QObj()
        ctx = mock.MagicMock()
        ctx.user_data = ud
        ctx.bot_data = {}
        with mock.patch.object(rbot, "lang_of", lambda u: "fa"), \
             mock.patch.object(rbot, "is_admin", lambda uid: True), \
             mock.patch.object(te, "get_chain",
                               lambda db_mod=None:
                               json.loads(ToggleDb.get_config("tr_engine_chain"))), \
             mock.patch.object(te, "save_chain", te_save):
            await rbot.admin_cb(update, ctx)
        return msg

    te_save_results = []

    def te_save(chain, db_mod=None):
        saved["tr_engine_chain"] = json.dumps(chain)
        te_save_results.append(list(chain))
        return True

    asyncio.run(go())
    assert te_save_results and te_save_results[-1] == ["gateway_ox", "libre"], \
        "tap appends to the END (latest tap = lowest priority)"


def test_panel_engine_cfg_shows_only_needed_fields():
    msg = asyncio.run(_drive_admin_cb("adm|trcfg|custom_openai"))
    datas = _btn_datas(msg)
    for field in ("base_url", "api_key", "model"):
        assert f"adm|trfield|custom_openai|{field}" in datas, field


def test_panel_trfield_prompt_sets_input_state():
    ud = {}
    msg = asyncio.run(_drive_admin_cb("adm|trfield|mymemory|email", ud))
    assert ud.get("adm_state") == "trfield|mymemory|email", "input state armed"
    assert any("بفرست" in t for t in msg.texts), "prompt shown"


def test_panel_back_navigation_present_on_hubs():
    # main hub → admin menu; config hub → engine-status screen (its parent)
    msg = asyncio.run(_drive_admin_cb("adm|trengines"))
    assert "adm|menu" in _btn_datas(msg), "status hub returns to the admin menu"
    msg = asyncio.run(_drive_admin_cb("adm|trconfig"))
    assert "adm|trengines" in _btn_datas(msg), "config hub returns to status screen"


# ==================================================================
# trfield text-input handler (forced-reply pattern)
# ==================================================================
def test_trfield_input_saves_value_and_masks_key():
    sent = {}
    saved = {}

    class FakeMsg:
        async def reply_text(self, text=None, parse_mode=None, reply_markup=None, **kw):
            sent["text"] = text
            sent["markup"] = reply_markup

    class FakeUpdate:
        effective_user = type("U", (), {"id": 987654})()
        message = FakeMsg()

    ctx = mock.MagicMock()
    ctx.user_data = {"adm_state": "trfield|libre|api_key"}

    class InputDb:
        @staticmethod
        def get_config(k, d=None):
            return saved.get(k)

        @staticmethod
        def set_config(k, v):
            saved[k] = v

    upd = FakeUpdate()
    upd.message.text = "  sk-secret-1234567890  "
    with mock.patch.object(rbot, "lang_of", lambda u: "fa"), \
         mock.patch.object(rbot, "is_admin", lambda uid: True), \
         mock.patch.object(te, "set_engine_cfg",
                           lambda eid, f, v, db_mod=None:
                           saved.__setitem__(f"tr_cfg_{eid}",
                                             json.dumps({f: v})) or True):
        asyncio.run(rbot.admin_input(upd, ctx))
    assert json.loads(saved["tr_cfg_libre"])["api_key"] == "sk-secret-1234567890", \
        "trimmed value saved"
    assert "adm_state" not in ctx.user_data, "state consumed"
    assert "••••" in sent["text"], "key masked in confirmation"
    assert "sk-secret" not in sent["text"], "raw key never echoed"


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
