#!/usr/bin/env python3
"""Aparat/Namasha plugin for Rayan Downloader bot.

Uses Aparat's public JSON API (no login needed):
  https://www.aparat.com/etc/api/video/videohash/{uid}
Returns direct mp4 links per quality. Namasha links are resolved to their
underlying aparat video first.
"""
import json
import re
import subprocess
import urllib.parse


class Plugin:
    name = "aparat"

    def _api_get(self, url: str, timeout: int = 25):
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(timeout),
             "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", url],
            capture_output=True, text=True, timeout=timeout + 5)
        return json.loads(r.stdout)

    def _extract_uid(self, url: str):
        # aparat.com/v/{uid} | aparat.com/video/video/{slug}/{uid} | namasha.com/v/{uid}
        m = re.search(r'/(?:v|embed)/([A-Za-z0-9]{4,12})(?:[/?#]|$)', url)
        if m:
            return m.group(1)
        m = re.search(r'/video/video/[^/]+/([A-Za-z0-9]{4,12})', url)
        if m:
            return m.group(1)
        return None

    def detect(self, url: str) -> str | None:
        """Type classification: aparat links are always videos."""
        if self._extract_uid(url):
            return "video"
        return None

    def handle(self, url: str, config: dict = None) -> dict:
        cfg = config or {}
        timeout = int(cfg.get("timeout", 60))
        uid = self._extract_uid(url)
        if not uid:
            return {"error": "شناسه ویدیوی آپارات پیدا نشد"}
        try:
            data = self._api_get(f"https://www.aparat.com/etc/api/video/videohash/{uid}", timeout)
            v = data.get("video") or {}
        except Exception as e:
            return {"error": f"خطای اتصال به آپارات: {e}"}
        if not v:
            return {"error": "ویدیو در آپارات پیدا نشد (خصوصی یا حذف شده)"}

        title = (v.get("title") or f"Aparat {uid}").strip()
        best = v.get("file_link")          # direct file (usually highest quality)
        poster = v.get("big_poster") or v.get("small_poster")
        duration = v.get("duration")

        if not best:
            return {"error": "لینک دانلود در API آپارات موجود نیست"}

        # Real per-quality URLs from the API (each carries its own wmsAuthSign token)
        qualities = {}
        for item in v.get("file_link_all") or []:
            profile = str(item.get("profile", "")).replace("p", "")
            urls = item.get("urls") or []
            if profile and urls:
                qualities[profile] = urls[0]
        if not qualities and best:
            qualities["best"] = best

        result = {
            "type": "video",
            "title": title,
            "uploader": (v.get("username") and ("@" + str(v["username"]))) or "",
            "duration": int(duration) if duration else None,
            "thumbnail": poster,
            "qualities": qualities,
            "webpage_url": f"https://www.aparat.com/v/{uid}",
            "text": title,
        }
        return result


if __name__ == "__main__":
    p = Plugin()
    import sys
    print(json.dumps(p.handle(sys.argv[1]), ensure_ascii=False)[:400])
