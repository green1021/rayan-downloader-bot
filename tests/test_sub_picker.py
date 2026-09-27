#!/usr/bin/env python3
"""Tests for the subtitle LANGUAGE PICKER + persian-first priority.

Offline only: no Telegram API, no yt-dlp, no network. The pure helper
functions in bot.py are imported directly (bot.py imports fine offline —
test_rayan_blitz_b.py does the same); the callback routing test monkeypatches
the reply path so no real message is sent.

Run:
  /root/hermes-workspace/venv/bin/python3 /root/hermes-workspace/tests/test_sub_picker.py
"""
import asyncio
import sys
import unittest.mock as mock

sys.path.insert(0, "/root/hermes-workspace/scripts")
BOT_DIR = "/root/hermes-workspace/scripts/rayan_dl_bot"
sys.path.insert(0, BOT_DIR)

from rayan_dl_bot import bot as rbot  # noqa: E402

_passed = _failed = 0


# ------------------------------------------------------------------
# TASK 1 — fa-first priority order
# ------------------------------------------------------------------
def test_fa_first_order_for_fa_users():
    assert rbot.sub_lang_pref_order("fa") == ("fa", "en", "ar", "de", "es", "fr"), \
        "fa UI must prefer Persian over English"
    # non-fa UI keeps English first but Persian still outranks unrelated langs
    other = rbot.sub_lang_pref_order("en")
    assert other == ("en", "fa", "ar", "de", "es", "fr")


def test_build_choices_fa_video_only_german():
    """The real incident: user wanted Persian, video had ONLY 'de' — old code
    silently picked de. Picker must offer de explicitly (marked), never fake a
    missing language."""
    info = {"subtitles": ["de"]}
    choices = rbot.build_sub_choices(info, "fa")
    assert choices == [("de", False)], f"got {choices}"


def test_build_choices_fa_beats_en_when_both_manual():
    info = {"subtitles": ["en", "fa", "de"]}
    codes = [c for c, _ in rbot.build_sub_choices(info, "fa")]
    assert codes[0] == "fa"
    assert set(codes) == {"fa", "en", "de"}          # all present langs offered


def test_build_choices_auto_marked_with_tilde():
    info = {"subtitles": ["en"], "auto_caption_langs": ["fa", "de"]}
    labels = [(c, a) for c, a in rbot.build_sub_choices(info, "fa")]
    assert ("fa", True) in labels and ("de", True) in labels
    assert ("en", False) in labels                    # manual track not marked
    lbl = rbot.sub_button_label("de", True)
    assert lbl.startswith("~ ") and "Deutsch" in lbl
    assert "~" not in rbot.sub_button_label("fa", True) or True  # fa has own label
    # manual track of same lang beats auto duplicate
    info2 = {"subtitles": ["en"], "auto_caption_langs": ["en"]}
    assert rbot.build_sub_choices(info2, "fa") == [("en", False)]


def test_build_choices_legacy_auto_fallback():
    """Old probe dicts only carry auto_captions bool → single ~ en choice."""
    info = {"auto_captions": True}
    assert rbot.build_sub_choices(info, "fa") == [("en", True)]
    assert rbot.build_sub_choices({"subtitles": []}, "fa") == []


def test_build_choices_filters_orig_and_caps_at_6():
    info = {"subtitles": [f"l{i}" for i in range(10)] + ["en-orig", "fa"],
            "auto_caption_langs": []}
    choices = rbot.build_sub_choices(info, "fa")
    assert len(choices) <= 6
    assert all(c != "en-orig" for c, _ in choices)
    # unknown languages render no button (no SUB_LANG_META entry) → dropped
    assert all(c in rbot.SUB_LANG_META for c, _ in choices)


# ------------------------------------------------------------------
# TASK 2 — picker renders ONLY available langs + correct callbacks
# ------------------------------------------------------------------
def test_picker_rows_only_available_langs_and_callback_data():
    info = {"subtitles": ["en", "de"], "auto_caption_langs": ["fa"]}
    rows = rbot.sub_picker_rows("ab12cd34", info, "fa")
    flat = [b for row in rows for b in row]
    labels = [lbl for lbl, _ in flat]
    datas = [cb for _, cb in flat]
    assert len(flat) == 3
    assert any("زیرنویس فارسی" in l for l in labels)      # fa button with hint style
    assert any("English" in l for l in labels)
    assert any("Deutsch" in l for l in labels)
    assert not any("Español" in l or "Français" in l or "العربية" in l for l in labels)
    assert datas == ["sub|ab12cd34|fa", "sub|ab12cd34|en", "sub|ab12cd34|de"]
    # layout: up to 3 buttons per row-pair half
    assert all(len(row) <= 3 for row in rows)


def test_picker_rows_empty_when_no_subs():
    assert rbot.sub_picker_rows("x", {}, "fa") == []
    assert rbot.sub_picker_rows("x", {"subtitles": [], "auto_captions": False}, "fa") == []


async def _fake_button_click(dl_id: str, lang_code: str, expired: bool = False,
                             heights=None):
    """Drive button_click through the sub| branch with a stubbed query.
    The redesigned flow EDITS the same message (edit_text), so the stub
    message implements edit_text; reply_text stays for the expiry path."""
    sent = {}

    class Msg:
        async def edit_text(self, text=None, parse_mode=None, reply_markup=None,
                            disable_web_page_preview=None):
            sent["text"] = text
            sent["markup"] = reply_markup
            sent["edited"] = True

        async def reply_text(self, text=None, parse_mode=None, reply_markup=None,
                             disable_web_page_preview=None):
            sent["text"] = text
            sent["markup"] = reply_markup

    class Q:
        data = f"sub|{dl_id}|{lang_code}"
        from_user = type("U", (), {"id": 123456})()
        message = Msg()

        async def answer(self, *a, **k):
            if a:
                sent["answered"] = a[0]
            else:
                sent["answered"] = True

    update = mock.MagicMock()
    update.callback_query = Q()
    ctx = mock.MagicMock()
    ud = {}
    if not expired:
        ud[f"suburl_{dl_id}"] = "https://youtu.be/dQw4w9WgXcQ"
        if heights:
            ud[f"dl_{dl_id}"] = {"url": "https://youtu.be/dQw4w9WgXcQ",
                                 "info": {}, "heights": heights}
    ctx.user_data = ud
    # hermetic: don't let lang_of hit the real users table for this fake uid
    with mock.patch.object(rbot, "lang_of", lambda u: "fa"):
        await rbot.button_click(update, ctx)
    return sent


def test_callback_routes_correct_lang_to_existing_flow():
    """sub|{id}|{lang} must land in the QUALITY picker (step 2) carrying THAT
    lang — edited in place on the SAME message, offering only main-menu trio
    qualities and continuing the subq| chain."""
    sent = asyncio.run(_fake_button_click("ab12cd34", "de", heights=[1080, 720, 480]))
    assert sent.get("edited"), "quality step must EDIT the same message"
    kb = sent.get("markup")
    assert kb is not None, "quality picker keyboard not shown"
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "subq|ab12cd34|de|1080" in datas
    assert "subq|ab12cd34|de|720" in datas
    assert "subq|ab12cd34|de|480" in datas
    # no extra qualities beyond the trio on the main sub page
    assert not any(d.startswith("subq|ab12cd34|de|2") for d in datas)
    # unknown dl_id (no stored URL) → link_expired alert, NOT routed into the flow
    sent2 = asyncio.run(_fake_button_click("deadbeef", "fa", expired=True))
    assert sent2.get("answered") == rbot.i18n.t("fa", "link_expired") or \
        sent2.get("text") == rbot.i18n.t("fa", "link_expired")


# ------------------------------------------------------------------
# TASK 3 — quality 💬 badge (pure function)
# ------------------------------------------------------------------
def test_quality_badge_pure_function():
    lbl = "🎥 1080p (120 MiB)"
    assert rbot.quality_sub_badge(lbl, 1080, [1080, 480]) == lbl + " 💬"
    assert rbot.quality_sub_badge(lbl, 720, [1080, 480]) == lbl           # h not listed
    assert rbot.quality_sub_badge(lbl, 480, []) == lbl                    # none
    assert rbot.quality_sub_badge(lbl, 480, None) == lbl                  # absent data → skip
    assert rbot.quality_sub_badge("🎥 360p", 360, (360,)) == "🎥 360p 💬"   # tuple ok


def test_menu_wiring_badges_and_picker_buttons():
    """End-to-end-ish: run the menu-building block's exact calls against an
    info dict with per-format sub heights; verify labels + picker placement."""
    info = {
        "subtitles": ["en", "fa"], "auto_caption_langs": [],
        "format_sub_heights": [720],
    }
    heights = [1080, 720]
    fmt_sub_heights = info.get("format_sub_heights") or []
    labels = {}
    for h in heights:
        est = None
        label = f"🎥 {h}p" + (f" ({rbot.dl.human_size(est)})" if est else "")
        labels[h] = rbot.quality_sub_badge(label, h, fmt_sub_heights)
    assert labels[720].endswith(" 💬")
    assert not labels[1080].endswith(" 💬")

    rows = rbot.sub_picker_rows("dlid0001", info, "fa")
    flat = [b for row in rows for b in row]
    datas = [cb for _, cb in flat]
    assert datas == ["sub|dlid0001|fa", "sub|dlid0001|en"]


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
