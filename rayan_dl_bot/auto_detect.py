#!/usr/bin/env python3
"""
auto_detect.py — Heuristic media-type detection for unknown sites.

When the bot receives a link from a site it doesn't know (no plugin, no core
support), this module fetches the page and decides what kind of content it has:
    comic  -> many gallery images (looks like a comic/manga reader) → PDF
    video  -> an mp4/m3u8 media link → video
    photos -> a handful of images → photo album
    text   -> mostly textual content → text message
    none   -> nothing extractable

It's heuristic by design: for unknown sites there are no guarantees, so the bot
warns the user that support is automatic and may be buggy.
"""
import logging
import os
import re
import time as _time
import urllib.parse
import urllib.request

log = logging.getLogger("rayan-dl")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# image URL patterns (absolute or relative)
IMG_PAT = re.compile(
    r'(?:src|data-src|data-original|href)=["\']([^"\']+\.(?:jpg|jpeg|png|webp|avif))["\']',
    re.IGNORECASE)
VIDEO_PAT = re.compile(
    r'["\']([^"\']+\.(?:mp4|m3u8|webm|mov))["\']', re.IGNORECASE)
# comic/manga reader markers (many sequential pages)
READER_PAT = re.compile(
    r'(?:wp-manga|readchapter|reader|chapter-images|page-break-image|comic-page|manga-page|page-images)',
    re.IGNORECASE)
# skip tiny/icon images
SKIP_PAT = re.compile(
    r'(?:logo|icon|avatar|emoji|spinner|loader|banner|sprite|favicon|thumb-?small|-\d+x\d+\.)',
    re.IGNORECASE)


def fetch_page(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def extract_text(url: str, limit: int = 3800) -> str:
    """Extract clean page text (fallback when media extraction fails)."""
    import html as _html
    try:
        html = fetch_page(url)
    except Exception:
        return ""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    # escape markdown-unsafe chars so edit_text doesn't break
    esc = (str(text).replace("\\", "\\\\").replace("_", "\\_")
           .replace("*", "\\*").replace("[", "\\[").replace("`", "\\`"))
    return esc[:limit]


def _resolve(page_url: str, m: str) -> str:
    if m.startswith("http"):
        return m
    return urllib.parse.urljoin(page_url, m)


def _img_count(html: str) -> list:
    """Return list of real-looking image URLs (deduped, in page order)."""
    urls = []
    for m in IMG_PAT.findall(html):
        if SKIP_PAT.search(m):
            continue
        urls.append(m)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _valid_image(p: str) -> bool:
    """Return True if the file is a decodable image (not an HTML error page etc.)."""
    try:
        from PIL import Image
        with Image.open(p) as im:
            im.verify()
        return True
    except Exception:
        return False


def _dl_valid(imgs, outdir, prefix="{:04d}.jpg") -> list:
    """Download images sequentially; keep only decodable image files."""
    os.makedirs(outdir, exist_ok=True)
    paths = []
    for i, u in enumerate(imgs, 1):
        p = os.path.join(outdir, prefix.format(i))
        try:
            _dl_img(u, p)
            if _valid_image(p):
                paths.append(p)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass
    return sorted(paths)


def detect(url: str) -> dict:
    """Analyze a page and return {type, count, samples[], confidence}."""
    try:
        html = fetch_page(url)
    except Exception as e:
        return {"type": "error", "error": str(e)[:150], "count": 0, "confidence": 0}
    if not html:
        return {"type": "error", "error": "empty page", "count": 0, "confidence": 0}

    imgs = _img_count(html)
    videos = list(dict.fromkeys(VIDEO_PAT.findall(html)))
    text_len = len(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL)).strip())
    reader_hint = bool(READER_PAT.search(html))

    n_imgs = len(imgs)
    n_vids = len(videos)

    # Decision logic (heuristic)
    if n_vids > 0 and n_vids >= n_imgs:
        return {"type": "video", "count": n_vids, "samples": videos[:3],
                "confidence": 80, "detail": f"{n_vids} ویدیو پیدا شد"}
    if reader_hint and n_imgs >= 8:
        return {"type": "comic", "count": n_imgs, "samples": imgs[:3],
                "confidence": 85, "detail": f"{n_imgs} صفحه کمیک پیدا شد"}
    if n_imgs >= 8:
        return {"type": "comic", "count": n_imgs, "samples": imgs[:3],
                "confidence": 65, "detail": f"{n_imgs} عکس متوالی (شبیه کمیک)"}
    if 1 < n_imgs < 8:
        return {"type": "photos", "count": n_imgs, "samples": imgs[:3],
                "confidence": 70, "detail": f"{n_imgs} عکس پیدا شد"}
    if n_vids > 0:
        return {"type": "video", "count": n_vids, "samples": videos[:3],
                "confidence": 60, "detail": f"{n_vids} ویدیو پیدا شد"}
    if text_len > 400:
        return {"type": "text", "count": text_len, "samples": [],
                "confidence": 55, "detail": f"محتوای متنی ({text_len} کاراکتر)"}
    return {"type": "none", "count": 0, "samples": [], "confidence": 0,
            "detail": "رسانه قابل استخراجی پیدا نشد"}


def auto_download(url: str, dtype: str, workdir: str, samples=None) -> dict:
    """Actually fetch/download the detected media.
    Returns same result shape as plugins ({type, path/photos/text, error})."""
    import os
    import subprocess
    import concurrent.futures
    import time as _time
    try:
        html = fetch_page(url)
    except Exception as e:
        return {"error": f"خطا در دریافت صفحه: {e}"}

    def _dl_img(u, p):
        req = urllib.request.Request(_resolve(url, u), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
        if len(data) < 100:
            raise ValueError(f"too small ({len(data)}B)")
        with open(p, "wb") as f:
            f.write(data)

    try:
        if dtype == "comic":
            imgs = _img_count(html)[:200]
            if not imgs:
                return {"error": "عکسی پیدا نشد"}
            outdir = os.path.join(workdir, f"ad_{int(_time.time())}")
            paths = _dl_valid(imgs, outdir)
            if not paths:
                return {"error": "دانلود عکس‌ها ناموفق بود"}
            import img2pdf
            # img2pdf can't handle PNG with alpha — flatten via PIL first
            from PIL import Image
            flat_paths = []
            for p in paths:
                try:
                    im = Image.open(p)
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGB")
                        fp = p.rsplit(".", 1)[0] + "_flat.jpg"
                        im.save(fp, "JPEG", quality=92)
                        flat_paths.append(fp)
                    else:
                        flat_paths.append(p)
                except Exception:
                    flat_paths.append(p)
            pdf_path = os.path.join(outdir, "output.pdf")
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(flat_paths))
            for p in flat_paths:
                try:
                    if p.endswith("_flat.jpg"):
                        os.remove(p)
                except OSError:
                    pass
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            return {"type": "pdf", "path": pdf_path, "pages": len(paths),
                    "text": "📄 PDF خودکار", "auto": True}

        if dtype == "video":
            videos = list(dict.fromkeys(VIDEO_PAT.findall(html)))
            if not videos:
                return {"error": "لینک ویدیو پیدا نشد"}
            vurl = _resolve(url, videos[0])
            out = os.path.join(workdir, f"ad_{int(_time.time())}.mp4")
            r = subprocess.run(["curl", "-s", "-L", "--max-time", "300", "-A", UA,
                                "-o", out, vurl], capture_output=True, text=True, timeout=320)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return {"type": "video", "path": out, "text": "🎬 ویدیو خودکار", "auto": True}
            return {"error": "دانلود ویدیو ناموفق بود"}

        if dtype == "photos":
            imgs = _img_count(html)[:20]
            if not imgs:
                return {"error": "عکسی پیدا نشد"}
            outdir = os.path.join(workdir, f"ad_{int(_time.time())}")
            photos = _dl_valid(imgs, outdir, "{:03d}.jpg")
            if not photos:
                return {"error": "دانلود عکس‌ها ناموفق بود"}
            return {"type": "photos", "photos": photos, "text": "🖼 عکس‌ها", "auto": True}

        if dtype == "text":
            import html as _html
            text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL)
            text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL)
            text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"\s+", " ", text).strip()[:3800]
            return {"type": "text", "text": text or "متن خالی", "auto": True}

        return {"error": "نوع ناشناخته"}
    except Exception as e:
        log.error("auto_download: %s", e)
        return {"error": f"خطا: {e}"}