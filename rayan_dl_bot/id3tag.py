#!/usr/bin/env python3
"""
id3tag — minimal ID3v2.3 tag writer/reader for Rayan Downloader Bot.

mutagen is not installed on this box (and no pip), so this hand-rolls just
what the MP3 flow needs: TIT2 / TPE1 / TALB text frames + one APIC cover art
frame, written in pure stdlib. An existing ID3v2 tag at the head of the file
is REPLACED (yt-dlp/ffmpeg sometimes writes one); everything after it (the
actual MPEG audio, and any trailing ID3v1 tag) is preserved untouched.

Spec notes honoured here (ID3v2.3, not v2.4!):
  * tag size in the 10-byte header is SYNCSAFE (4 x 7-bit)
  * frame sizes are PLAIN big-endian uint32
  * text encodings: 0x00 = ISO-8859-1, 0x01 = UTF-16 with BOM
    (UTF-8 is v2.4-only — using it would produce tags Persian players misread,
    so non-Latin titles go through UTF-16)
"""
import logging
import os
import re

log = logging.getLogger("rayan-dl-id3")

MAX_TAG_BYTES = 4 * 1024 * 1024  # refuse to build absurd tags (huge covers)

# ── helpers ───────────────────────────────────────────────────

def _syncsafe(n: int) -> bytes:
    """int → 4 bytes, 28-bit value split into four 7-bit groups (v2.3 tag size)."""
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _enc_text(s: str) -> bytes:
    """Text-frame payload: encoding byte 0x01 + UTF-16LE with BOM.
    UTF-16 keeps Persian/Arabic/CJK titles readable in players."""
    return b"\x01" + b"\xff\xfe" + s.encode("utf-16-le", errors="replace")


def _frame(fid: str, payload: bytes) -> bytes:
    """One ID3v2.3 frame: id + plain-BE u32 size + flags + body."""
    return fid.encode("ascii") + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _text_frame(fid: str, text: str) -> bytes:
    # UTF-16 strings are terminated by a two-byte NUL
    return _frame(fid, _enc_text(text) + b"\x00\x00")


def _apic_frame(jpeg_path: str) -> bytes | None:
    """APIC payload: <enc> <mime\0> <pic-type> <desc(enc-term)> <image data>.
    Picture type 0x03 = front cover. Description kept empty — players show
    the art either way and empty avoids double-termination bugs."""
    try:
        if os.path.getsize(jpeg_path) > MAX_TAG_BYTES // 2:
            log.warning("cover too large to embed (%s)", jpeg_path)
            return None
        with open(jpeg_path, "rb") as f:
            img = f.read()
        if not img:
            return None
        mime = "image/jpeg"
        payload = (b"\x01"                      # description encoding = UTF-16
                   + mime.encode("ascii") + b"\x00"
                   + b"\x03"                    # front cover
                   + b"\xff\xfe" + b"\x00\x00"  # empty UTF-16 desc + terminator
                   + img)
        if len(payload) > MAX_TAG_BYTES:
            log.warning("APIC frame too large, skipping cover")
            return None
        return _frame("APIC", payload)
    except Exception as e:
        log.warning("APIC read failed: %s", e)
        return None


# ── write ─────────────────────────────────────────────────────

def _strip_existing_tag(data: bytes) -> bytes:
    """Return data with any leading ID3v2.x tag (+footer) removed."""
    if len(data) < 10 or data[:3] != b"ID3":
        return data
    size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
           ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
    end = 10 + size
    if data[5] & 0x10:   # footer-present flag → another 10 bytes of footer
        end += 10
    return data[min(end, len(data)):]


def write_tags(path: str, title: str = "", artist: str = "",
               album: str = "", cover_jpeg: str = "") -> bool:
    """Embed an ID3v2.3 tag at the head of an MP3. Replaces any existing v2 tag.
    Returns True when the file was rewritten (or nothing needed writing and the
    file is intact). Never raises."""
    tmp = path + ".id3tmp"
    try:
        with open(path, "rb") as f:
            body = _strip_existing_tag(f.read())
        frames = b""
        if title:
            frames += _text_frame("TIT2", title[:300])
        if artist:
            frames += _text_frame("TPE1", artist[:200])
        if album:
            frames += _text_frame("TALB", album[:200])
        if cover_jpeg and os.path.exists(cover_jpeg):
            apic = _apic_frame(cover_jpeg)
            if apic:
                frames += apic
        if not frames:
            return True  # nothing to say — file already untagged+intact
        tag = b"ID3" + b"\x03\x00" + b"\x00" + _syncsafe(len(frames)) + frames
        with open(tmp, "wb") as f:
            f.write(tag)
            f.write(body)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error("write_tags(%s): %s", path, e)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


# ── read back (verification + tests) ──────────────────────────

def read_tags(path: str) -> dict:
    """Parse the leading ID3v2.3 tag back out.
    {version, title, artist, album, has_cover, frames_bytes, mpeg_found}.
    Empty dict when no/invalid tag."""
    out = {}
    try:
        size_total = os.path.getsize(path)
        if size_total < 10:
            return out
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] != b"ID3":
                return out
            major = head[3]
            tsize = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | \
                    ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            out["version"] = f"2.{major}"
            raw = f.read(min(tsize, MAX_TAG_BYTES))
            pos = 0
            while pos + 10 <= len(raw):
                fid = raw[pos:pos + 4].decode("latin-1", errors="replace")
                # frame ids are [A-Z0-9]{4} (TIT2/TPE1/APIC… contain digits!)
                if not re.fullmatch(r"[A-Z0-9]{4}", fid):
                    break  # padding / corrupt
                fsz = int.from_bytes(raw[pos + 4:pos + 8], "big")
                pos += 10 + fsz
                body = raw[pos - fsz:pos] if fsz <= len(raw) else b""
                if fid in ("TIT2", "TPE1", "TALB") and body:
                    out[{ "TIT2": "title", "TPE1": "artist", "TALB": "album"}[fid]] = \
                        _decode_text(body)
                elif fid == "APIC":
                    out["has_cover"] = True
            out["frames_bytes"] = tsize
            out["mpeg_found"] = _mpeg_sync_after(path, 10 + tsize)
    except Exception as e:
        log.error("read_tags(%s): %s", path, e)
        return {}
    return out


def _decode_text(payload: bytes) -> str:
    """Decode a text-frame body (first byte = encoding selector)."""
    try:
        enc = payload[0]
        data = payload[1:]
        # strip ONLY the proper terminator — a bare .rstrip(b"\x00") would eat
        # the high byte of a trailing ASCII char in UTF-16 ('d' = 64 00)
        if data.endswith(b"\x00\x00"):
            data = data[:-2]
        elif data.endswith(b"\x00"):
            data = data[:-1]
        if enc == 1:
            if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return data[2:].decode("utf-16-le" if data[:2] == b"\xff\xfe"
                                       else "utf-16-be", errors="replace")
            return data.decode("utf-16-le", errors="replace")
        return data.decode("latin-1", errors="replace")
    except Exception:
        return ""


def _mpeg_sync_after(path: str, offset: int, window: int = 8192) -> bool:
    """Lenient 'does this still look like MPEG audio' probe: hunt for a frame
    sync (0xFF followed by 0b111xxxxx) shortly after the tag. LAME/Xing heads
    can put a little junk up front, hence the window instead of offset-only."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            buf = f.read(window)
        for i in range(len(buf) - 1):
            if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
                return True
    except Exception:
        pass
    return False
