#!/usr/bin/env python3
"""
plugin_manager.py — Plugin system for Rayan Downloader Bot.

Plugins extend the bot to handle sites it doesn't natively support.
Each plugin is a folder under PLUGINS_DIR containing:
    plugin.json   — metadata: {name, version, description, sites[], entry, config{}}
    <entry>.py    — Python module with class `Plugin` and method `handle(url, config, ctx)`

The `handle` method returns a dict:
    { "type": "pdf"|"video"|"photos"|"text",
      "title": str,
      "path": "/abs/path/to/file.pdf",     # for pdf/video
      "photos": ["/abs/1.jpg", ...],        # for photos
      "text": "caption",                    # optional caption
      "error": str                          # on failure instead of file
    }

Install: admin gives a URL to a plugin (zip file or raw .py/.json on GitHub) →
plugin_manager downloads, validates, extracts into PLUGINS_DIR, registers in DB.

Security: entry modules are imported with a restricted importer; the manager
validates plugin.json schema, rejects names with path traversal, and never
executes install scripts — only the declared entry module.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

log = logging.getLogger("rayan-dl")

PLUGINS_DIR = "/root/hermes-workspace/scripts/rayan_dl_bot/plugins"
MANIFEST = "plugin.json"
ALLOWED_ENTRY_EXTS = (".py",)

# ── Loading ───────────────────────────────────────────────────
_module_cache = {}  # path -> (mtime, module)


def _load_module_from_file(path, modname):
    """Load (and CACHE) a plugin module. Cache is invalidated when file mtime
    changes, so plugin updates hot-reload while steady-state calls skip the
    expensive re-exec of module code."""
    import importlib.util
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    cached = _module_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _module_cache[path] = (mtime, mod)
    return mod


_plugin_pool = None  # shared executor for timeout-bounded plugin calls


def call_with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run a blocking plugin function in a worker thread with a HARD timeout.
    On timeout raises TimeoutError; the stray thread dies with the process eventually
    but the caller (bot) is never blocked forever."""
    global _plugin_pool
    import concurrent.futures
    if _plugin_pool is None:
        _plugin_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="rayan-plugin")
    fut = _plugin_pool.submit(fn, *args, **kwargs)
    return fut.result(timeout=timeout_s)


DEFAULT_PLUGIN_TIMEOUT = 90  # seconds — covers slow sites without hanging forever


def discover_plugins():
    """Return list of plugin dicts (from plugin.json) found on disk."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        return plugins
    for name in sorted(os.listdir(PLUGINS_DIR)):
        d = os.path.join(PLUGINS_DIR, name)
        manifest = os.path.join(d, MANIFEST)
        if not os.path.isdir(d) or not os.path.isfile(manifest):
            continue
        try:
            with open(manifest, encoding="utf-8") as f:
                meta = json.load(f)
            meta.setdefault("name", name)
            meta.setdefault("version", "0.0.0")
            meta.setdefault("description", "")
            meta.setdefault("sites", [])
            meta.setdefault("entry", "plugin")
            meta.setdefault("enabled", True)
            meta.setdefault("config", {})
            meta["_dir"] = d
            plugins.append(meta)
        except Exception as e:
            log.warning("plugin %s manifest error: %s", name, e)
    return plugins


def _site_match(site_rule, url):
    """Match a site rule against a URL by HOSTNAME (exact or subdomain suffix).
    Falls back to substring only when the rule isn't a valid domain (opt-in legacy)."""
    if not site_rule:
        return False
    rule = str(site_rule).lower().strip()
    try:
        from urllib.parse import urlparse
        h = (urlparse(url).hostname or "").lower()
        if h.startswith("www."):
            h = h[4:]
        if h and (h == rule or h.endswith("." + rule)):
            return True
    except Exception:
        pass
    # legacy substring fallback for non-domain rules (e.g. "tiktok/wildcard")
    return rule in url.lower()


def find_plugin_for_url(url):
    """Return the first ENABLED plugin that declares support for this URL."""
    for meta in discover_plugins():
        if not meta.get("enabled"):
            continue
        for rule in meta.get("sites", []):
            if _site_match(rule, url):
                return meta
    return None


def get_plugin(name):
    for meta in discover_plugins():
        if meta.get("name") == name:
            return meta
    return None


def plugin_entry_path(meta):
    entry = meta.get("entry", "plugin")
    if not entry.endswith(".py"):
        entry += ".py"
    return os.path.join(meta["_dir"], entry)


def call_plugin(meta, url, config=None):
    """Invoke a plugin's handle() with a HARD timeout. Returns result dict or error dict."""
    entry = plugin_entry_path(meta)
    if not os.path.isfile(entry):
        return {"error": f"plugin entry missing: {os.path.basename(entry)}"}
    try:
        cfg = dict(meta.get("config", {}))
        if config:
            cfg.update(config)
        mod = _load_module_from_file(entry, f"plugin_{meta['name']}")
        if not hasattr(mod, "Plugin"):
            return {"error": "plugin has no Plugin class"}
        plugin = mod.Plugin()
        if not hasattr(plugin, "handle"):
            return {"error": "plugin has no handle() method"}
        timeout = float(meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
        return call_with_timeout(plugin.handle, timeout, url, cfg)
    except TimeoutError:
        log.error("plugin %s TIMEOUT after %ss", meta.get("name"), meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
        return {"error": "پلاگین بیش از حد طول کشید (timeout)"}
    except Exception as e:
        log.error("plugin %s error: %s", meta.get("name"), e)
        return {"error": f"plugin error: {e}"}


def call_plugin_chapters(meta, url):
    """Ask a plugin for its chapter list (multi-chapter support).
    Returns list of {num, title, url} or [] if plugin has no chapters."""
    entry = plugin_entry_path(meta)
    if not os.path.isfile(entry):
        return []
    try:
        mod = _load_module_from_file(entry, f"plugin_{meta['name']}")
        if not hasattr(mod, "Plugin"):
            return []
        plugin = mod.Plugin()
        if not hasattr(plugin, "get_chapters"):
            return []
        timeout = float(meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
        try:
            chapters = call_with_timeout(plugin.get_chapters, timeout, url)
        except TimeoutError:
            log.error("plugin %s get_chapters TIMEOUT after %ss", meta.get("name"), timeout)
            return []
        return chapters or []
    except Exception as e:
        log.error("plugin %s get_chapters error: %s", meta.get("name"), e)
        return []


def call_plugin_detect_type(meta, url) -> str | None:
    """Ask a plugin for its type classification (comic/video/photos/text) — used
    by multi-type plugins. Returns None if the plugin has no detect() method."""
    entry = plugin_entry_path(meta)
    if not os.path.isfile(entry):
        return None
    try:
        mod = _load_module_from_file(entry, f"plugin_{meta['name']}")
        if not hasattr(mod, "Plugin"):
            return None
        plugin = mod.Plugin()
        if not hasattr(plugin, "detect"):
            return None
        timeout = float(meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
        try:
            return call_with_timeout(plugin.detect, timeout, url)
        except TimeoutError:
            log.error("plugin %s detect TIMEOUT after %ss", meta.get("name"), timeout)
            return None
    except Exception as e:
        log.error("plugin %s detect error: %s", meta.get("name"), e)
        return None


def call_plugin_download_chapter(meta, chapter_url, config=None):
    """Ask a plugin to download a specific chapter. Returns result dict."""
    entry = plugin_entry_path(meta)
    if not os.path.isfile(entry):
        return {"error": f"plugin entry missing: {os.path.basename(entry)}"}
    try:
        cfg = dict(meta.get("config", {}))
        if config:
            cfg.update(config)
        mod = _load_module_from_file(entry, f"plugin_{meta['name']}")
        if not hasattr(mod, "Plugin"):
            return {"error": "plugin has no Plugin class"}
        plugin = mod.Plugin()
        timeout = float(meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
        fn = getattr(plugin, "download_chapter", None) or getattr(plugin, "handle", None)
        if fn is None:
            return {"error": "plugin has no download_chapter() or handle() method"}
        try:
            return call_with_timeout(fn, timeout, chapter_url, cfg)
        except TimeoutError:
            log.error("plugin %s download_chapter TIMEOUT after %ss", meta.get("name"), timeout)
            return {"error": "پلاگین بیش از حد طول کشید (timeout)"}
    except Exception as e:
        log.error("plugin %s download_chapter error: %s", meta.get("name"), e)
        return {"error": f"plugin error: {e}"}


# ── Install / remove ──────────────────────────────────────────
MAX_ZIP_UNCOMPRESSED = 30 * 1024 * 1024  # 30 MB total extracted
MAX_ZIP_ENTRIES = 200


def _ip_is_forbidden(ip_str: str) -> bool:
    """True for loopback/private/link-local/reserved/multicast/unspecified IPs,
    including IPv4-mapped IPv6 (::ffff:10.0.0.1 → checks the embedded IPv4)."""
    import ipaddress as _ipa
    try:
        ip = _ipa.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → treat as unsafe
    # Unwrap IPv4-mapped IPv6 so the embedded v4 range decides
    if isinstance(ip, _ipa.IPv6Address) and getattr(ip, "ipv4_mapped", None):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _host_resolves_public(host: str) -> bool:
    """Resolve hostname and require EVERY returned address to be public.
    This closes the non-canonical-literal bypass (2130706433, 127.1, 0x7f…),
    plain IPv6 literals (fd00::1), and privately-resolving names."""
    try:
        import socket as _socket
        infos = _socket.getaddrinfo(host, 443, proto=_socket.IPPROTO_TCP)
    except Exception:
        return False  # does not resolve → nothing public to talk to
    if not infos:
        return False
    return all(not _ip_is_forbidden(info[4][0]) for info in infos)


def _validate_install_url(url: str) -> str | None:
    """SSRF guard for admin-supplied install URLs. Returns error or None if OK.
    Only https:// to PUBLIC hosts; blocks file://, ftp://, private/loopback/
    link-local addresses (v4 AND v6, any literal form) and non-public DNS.
    NOTE: this resolves DNS itself; fetches must go through _safe_urlopen so
    redirects are re-validated per hop (DNS-rebinding window otherwise)."""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme not in ("https",):
            return "فقط URL با پروتکل https مجاز است"
        host = (u.hostname or "").lower()
        if not host:
            return "URL بدون hostname"
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
            return "دانلود از localhost مجاز نیست"
        if not _host_resolves_public(host):
            return "دانلود از آدرس داخلی/خصوصی مجاز نیست"
        return None
    except Exception as e:
        return f"URL نامعتبر: {e}"


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-runs the full URL validation on EVERY hop,
    so a public URL can't bounce us to an internal address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        err = _validate_install_url(newurl)
        if err:
            log.warning("plugin install redirect blocked: %s → %s", req.full_url, newurl)
            raise urllib.error.HTTPError(newurl, code, f"blocked redirect: {err}",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = None

def _safe_urlopen(url: str, timeout: int):
    """urlopen with per-hop SSRF validation on redirects."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_SSRFRedirectHandler())
    return _OPENER.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                        timeout=timeout)


def _safe_extract(zf, tmp: str) -> str | None:
    """Zip-Slip-hardened extraction. Returns error string or None if OK.
    Rejects: absolute paths, .. traversal outside tmp, symlinks, oversized/many entries."""
    total = 0
    if len(zf.namelist()) > MAX_ZIP_ENTRIES:
        return "zip has too many files"
    for info in zf.infolist():
        name = info.filename
        # normalize and check escape
        dest = os.path.realpath(os.path.join(tmp, name))
        base = os.path.realpath(tmp)
        if not (dest == base or dest.startswith(base + os.sep)):
            return f"unsafe path in zip: {name}"
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            return f"symlink in zip not allowed: {name}"
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED:
            return "zip too large when extracted"
    zf.extractall(tmp)
    return None


def _validate_meta(meta) -> str | None:
    """Return error string or None if valid."""
    name = meta.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name or ""):
        return "invalid plugin name (use letters, digits, - or _)"
    if not isinstance(meta.get("sites", []), list) or not meta["sites"]:
        return "plugin.json must declare a non-empty 'sites' list"
    entry = meta.get("entry", "plugin")
    if not entry.endswith(".py"):
        entry += ".py"
    if ".." in entry or "/" in entry.replace("\\", "/"):
        return "invalid entry path"
    return None


def install_plugin_from_file(zip_path: str) -> dict:
    """Install a plugin from a LOCAL .zip file (uploaded via the admin panel).
    Returns {ok, message, name?}."""
    try:
        if not zipfile.is_zipfile(zip_path):
            return {"ok": False, "message": "فایل دریافتی یک ZIP معتبر نیست"}
        tmp = tempfile.mkdtemp(prefix="plugin_install_")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                manifest_name = next((n for n in names if n.endswith(MANIFEST)), None)
                if not manifest_name:
                    return {"ok": False, "message": "zip شامل plugin.json نیست"}
                err_x = _safe_extract(zf, tmp)
                if err_x:
                    return {"ok": False, "message": err_x}
            root = os.path.dirname(os.path.join(tmp, manifest_name))
            with open(os.path.join(tmp, manifest_name), encoding="utf-8") as f:
                meta = json.load(f)
            err = _validate_meta(meta)
            if err:
                return {"ok": False, "message": err}
            name = meta["name"]
            dest = os.path.join(PLUGINS_DIR, name)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(root, dest)
            return {"ok": True, "name": name, "message": f"پلاگین '{name}' نصب شد"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        log.error("install plugin from file error: %s", e)
        return {"ok": False, "message": f"install error: {e}"}


def install_plugin_from_url(url: str) -> dict:
    """Download a plugin (zip or raw plugin.json+entry from GitHub raw) and install.
    Returns {ok, message, name?}."""
    try:
        err_url = _validate_install_url(url)
        if err_url:
            return {"ok": False, "message": err_url}
        if url.endswith(".zip") or "zipball" in url or "codeload" in url:
            return _install_zip(url)
        if url.endswith(".json"):
            return _install_manifest(url)
        if "raw.githubusercontent" in url or url.endswith(".py"):
            return _install_raw(url)
        return {"ok": False, "message": "URL must be a .zip, .json manifest, or raw GitHub file"}
    except Exception as e:
        log.error("install plugin error: %s", e)
        return {"ok": False, "message": f"install error: {e}"}


def _install_zip(url: str) -> dict:
    tmp = tempfile.mkdtemp(prefix="plugin_install_")
    zip_path = os.path.join(tmp, "plugin.zip")
    try:
        with _safe_urlopen(url, 60) as resp, open(zip_path, "wb") as f:
            remaining = MAX_ZIP_UNCOMPRESSED
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    return {"ok": False, "message": "فایل zip بیش از حد بزرگ است"}
                f.write(chunk)
        with zipfile.ZipFile(zip_path) as zf:
            # find plugin.json in the archive
            names = zf.namelist()
            manifest_name = next((n for n in names if n.endswith(MANIFEST)), None)
            if not manifest_name:
                return {"ok": False, "message": "zip has no plugin.json"}
            err_x = _safe_extract(zf, tmp)
            if err_x:
                return {"ok": False, "message": err_x}
        root = os.path.dirname(os.path.join(tmp, manifest_name))
        with open(os.path.join(tmp, manifest_name), encoding="utf-8") as f:
            meta = json.load(f)
        err = _validate_meta(meta)
        if err:
            return {"ok": False, "message": err}
        name = meta["name"]
        dest = os.path.join(PLUGINS_DIR, name)
        # Atomic-ish install: stage into a sibling dir, then swap. A failed
        # copy must not destroy the previously working version of the plugin.
        staged = dest + ".staging"
        if os.path.exists(staged):
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(root, staged)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.replace(staged, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "name": name, "message": f"plugin '{name}' installed"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _validate_entry_code(code: str) -> str | None:
    """Sanity-check fetched entry source before it lands on disk: must be valid
    Python and declare a Plugin class. Catches truncated downloads / HTML error
    pages being installed as plugin code. NOT a security boundary (plugins are
    trusted code by design)."""
    try:
        compile(code, "<plugin-entry>", "exec")
    except SyntaxError as e:
        return f"پلاگین کد پایتون معتبر نیست: {e}"
    if not re.search(r"^\s*class\s+Plugin\b", code, re.M):
        return "کد پلاگین کلاس Plugin ندارد"
    return None


def _install_manifest(url: str) -> dict:
    """Install from a single raw plugin.json — the entry .py must be fetched
    from the same directory (GitHub raw)."""
    try:
        meta = json.loads(_safe_urlopen(url, 30).read().decode("utf-8"))
        err = _validate_meta(meta)
        if err:
            return {"ok": False, "message": err}
        name = meta["name"]
        entry = meta.get("entry", "plugin")
        if not entry.endswith(".py"):
            entry += ".py"
        # entry URL = same dir as manifest, with filename entry — validated too
        base = url.rsplit("/", 1)[0]
        entry_url = f"{base}/{entry}"
        entry_data = _safe_urlopen(entry_url, 30).read()
        err_entry = _validate_entry_code(entry_data.decode("utf-8", "replace"))
        if err_entry:
            return {"ok": False, "message": err_entry}
        # Fetch BOTH files BEFORE touching disk: a failed entry fetch must not
        # leave a registered-but-broken (phantom) plugin behind.
        dest = os.path.join(PLUGINS_DIR, name)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        with open(os.path.join(dest, entry), "wb") as f:
            f.write(entry_data)
        return {"ok": True, "name": name, "entry_missing": False,
                "message": f"plugin '{name}' installed"}
    except Exception as e:
        return {"ok": False, "message": f"manifest install error: {e}"}


def _install_raw(url: str) -> dict:
    """Install from raw GitHub plugin.json URL."""
    return _install_manifest(url)


def remove_plugin(name: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name or ""):
        return False
    dest = os.path.join(PLUGINS_DIR, name)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
        return True
    return False


def toggle_plugin(name: str, enabled: bool) -> bool:
    """Set enabled flag in plugin.json. Returns True on success."""
    meta = get_plugin(name)
    if not meta:
        return False
    try:
        manifest = os.path.join(meta["_dir"], MANIFEST)
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
        data["enabled"] = bool(enabled)
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        log.error("toggle plugin: %s", e)
        return False


def set_plugin_config(name: str, key: str, value) -> bool:
    """Set a config value in plugin.json. Returns True on success."""
    meta = get_plugin(name)
    if not meta:
        return False
    try:
        manifest = os.path.join(meta["_dir"], MANIFEST)
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("config", {})
        data["config"][key] = value
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        log.error("set_plugin_config: %s", e)
        return False