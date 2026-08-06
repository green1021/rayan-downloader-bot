#!/usr/bin/env python3
"""
allporncomic plugin — download comics from allporncomic.com as PDF.

Handles two URL shapes:
  1. A chapter page  (…/porncomic/<slug>/1-<slug>/  or any /<n>-<slug>/)
  2. A comic listing (…/porncomic/<slug>/)
     → picks the FIRST chapter ("Read First") automatically.

Algorithm (verified against live site):
  - GET the chapter page with a desktop UA
  - extract all page images matching the WP-manga CDN pattern
  - download each page concurrently (thread pool)
  - build a single PDF with img2pdf (no re-encode, fast)
"""
import concurrent.futures
import logging
import os
import re
import urllib.request

log = logging.getLogger("rayan-dl")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PAGE_PAT = re.compile(r"https://cdn\.allporncomic\.com/wp-content/uploads/WP-manga/data/[^\"']+\.(?:jpg|jpeg|png)")
CHAPTER_PAT = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>\s*(\d+)\s*\.\s*([^<]+?)</a>')
# any link matching the chapter URL shape /<n>-<slug>/ (catches special/extra chapters)
CHAPTER_LINK_PAT = re.compile(r'href="([^"]*porncomic/[^"]*/\d+(?:-\d+)?-[^"/]*/)"')


def _fetch(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data


def get_chapters(url: str) -> list:
    """Return chapter list [{num, title, url}] for a comic listing page.
    Source of truth = chapter links in HTML (all shapes: 1, 3-5 (=3.5), 22-5 (=22.5)).
    Sorted ascending. Duplicate URLs are dropped."""
    try:
        html = _fetch(url).decode("utf-8", "ignore")
        chaps = []
        seen_urls = set()

        def _add(href, title=None):
            nonlocal chaps
            if not href.startswith("http"):
                href = ("https://allporncomic.com" + href) if href.startswith("/") else href
            if not href or href in seen_urls:
                return
            seen_urls.add(href)
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            m = re.match(r"(\d+)(?:-(\d+))?-(.+)", slug)
            if not m:
                return
            num = int(m.group(1))
            frac = m.group(2)
            if frac:
                num = float(f"{m.group(1)}.{frac}")
            if title is None:
                title = m.group(3).replace("-", " ").title()
            else:
                # clean anchor title: "5 . Au Naturel - Bonus - [..]" → "Au Naturel - Bonus"
                title = re.sub(r"^\s*\d+(?:\.\d+)?\s*\.\s*", "", title).strip()
                title = re.sub(r"\s*\[[^\]]*\]\s*$", "", title).strip()
            chaps.append({"num": num, "title": title.strip(), "url": href})

        # pass 1: numbered anchors with their visible titles (nicer names)
        for href, num, title in CHAPTER_PAT.findall(html):
            _add(href, title)
        # pass 2: any chapter-shaped link missed by pass 1 (special/bonus slugs)
        for href in CHAPTER_LINK_PAT.findall(html):
            _add(href)

        return sorted(chaps, key=lambda c: c["num"])
    except Exception as e:
        log.error("allporncomic get_chapters: %s", e)
        return []


def _find_first_chapter(url):
    """If URL is a comic listing, find the 'Read First' chapter URL from its HTML."""
    html = _fetch(url).decode("utf-8", "ignore")
    # Madara lists chapters with links to /<n>-<slug>/
    # The first chapter link in the "LATEST MANGA RELEASES" is usually the first.
    m = re.search(r'href="(https://allporncomic\.com/porncomic/[^"]+?/\d+-[^"/]+/)"', html)
    if m:
        return m.group(1)
    # fallback: any chapter URL pattern in page
    m = re.search(r'href="(https://allporncomic\.com/[^"]*/\d+-[^"/]+/)"', html)
    return m.group(1) if m else url


def _extract_pages(chapter_url, max_pages=300):
    html = _fetch(chapter_url).decode("utf-8", "ignore")
    urls = sorted(set(PAGE_PAT.findall(html)))
    # dedupe + filter tiny icons (already filtered by pattern, but be safe)
    urls = [u for u in urls if not re.search(r"-\d+x\d+\.", u)]
    return urls[:max_pages]


def _download_image(url, out_path, timeout=60):
    data = _fetch(url, timeout=timeout)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


class Plugin:
    def get_chapters(self, url: str) -> list:
        """Return chapter list [{num, title, url}] for a comic listing page."""
        return get_chapters(url)

    def download_chapter(self, chapter_url: str, config=None) -> dict:
        """Download ONE specific chapter page -> PDF. Returns same dict shape as handle()."""
        config = config or {}
        max_pages = int(config.get("max_pages", 300))
        timeout = int(config.get("timeout", 60))
        max_workers = int(config.get("max_workers", 8))
        image_quality = int(config.get("image_quality", 92))
        keep_images = bool(config.get("keep_images", False))
        workdir = config.get("workdir", "/root/hermes-workspace/tmp/downloads")
        try:
            pages = _extract_pages(chapter_url, max_pages)
            if not pages:
                return {"error": "هیچ صفحه‌ای از کامیک پیدا نشد (ممکنه نیاز به ورود داشته باشه)"}

            slug = chapter_url.rstrip("/").rsplit("/", 1)[-1]
            # nicer title: "1-milf-catchers" -> "فصل ۱ …", take chapter number
            m = re.match(r"(\d+)-(.+)", slug)
            if m:
                title = f"فصل {m.group(1)} — {m.group(2).replace('-', ' ').title()}"
            else:
                title = slug.replace("-", " ").title()
            title = title[:80]

            outdir = os.path.join(workdir, f"apc_{int(__import__('time').time())}")
            os.makedirs(outdir, exist_ok=True)
            img_paths = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {
                    ex.submit(_download_image, u, os.path.join(outdir, f"{i:04d}.jpg"), timeout): u
                    for i, u in enumerate(pages, 1)
                }
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        img_paths.append(fut.result())
                    except Exception as e:
                        log.warning("خطا در دانلود صفحه: %s", e)
            img_paths = sorted(img_paths)
            if not img_paths:
                return {"error": "دانلود صفحات ناموفق بود"}

            import img2pdf
            from PIL import Image
            # img2pdf can't embed PNG with alpha — flatten transparent images to JPEG
            flat_paths = []
            for p in img_paths:
                try:
                    im = Image.open(p)
                    if im.mode in ("RGBA", "LA", "P"):
                        fp = p.rsplit(".", 1)[0] + "_flat.jpg"
                        im.convert("RGB").save(fp, "JPEG", quality=image_quality)
                        flat_paths.append(fp)
                    else:
                        flat_paths.append(p)
                except Exception:
                    flat_paths.append(p)
            pdf_path = os.path.join(outdir, f"{slug}.pdf")
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(flat_paths))
            for p in flat_paths:
                try:
                    if p.endswith("_flat.jpg"):
                        os.remove(p)
                except OSError:
                    pass
            if not keep_images:
                for p in img_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            return {
                "type": "pdf",
                "title": title,
                "path": pdf_path,
                "pages": len(img_paths),
                "text": f"📄 {title}",
            }
        except Exception as e:
            log.error("allporncomic download_chapter: %s", e)
            return {"error": f"خطای پلاگین کامیک: {e}"}

    def handle(self, url, config=None):
        config = config or {}
        workdir = config.get("workdir", "/root/hermes-workspace/tmp/downloads")
        # If it's a specific chapter URL, download it directly
        if re.search(r"/\d+-[^/]+/$", url.rstrip("/") + "/"):
            return self.download_chapter(url, config)
        # Otherwise try to find first chapter (backward compatible)
        first = _find_first_chapter(url)
        if not first:
            return {"error": "نتونستم قسمتی در این کامیک پیدا کنم"}
        return self.download_chapter(first, config)
