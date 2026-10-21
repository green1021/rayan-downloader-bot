#!/usr/bin/env python3
"""
plugin_manager.py — Plugin system for Rayan Downloader Bot.

Plugins extend the bot to handle sites it doesn't natively support.
Each plugin is a folder under ``config.PLUGINS_DIR`` containing:

    plugin.json   — metadata: {name, version, description, sites[], entry, config{}}
    <entry>.py    — Python module with class ``Plugin`` and method
                    ``handle(url, config)`` (plus optional ``get_chapters``,
                    ``detect``, ``download_chapter``)

The ``handle`` method returns a dict::

    { "type": "pdf"|"video"|"photos"|"text",
      "title": str,
      "path": "/abs/path/to/file.pdf",     # for pdf/video
      "photos": ["/abs/1.jpg", ...],        # for photos
      "text": "caption",
      "error": str }

Two independent properties matter here and are deliberately kept apart:

**Installation** is treated as untrusted input: admin-supplied URLs are
fetched through :mod:`netguard` (scheme allow-list, public-IP-only,
DNS-pinned connections, redirect revalidation, byte caps), archives are
inspected *before* extraction (traversal, symlinks, entry count, expansion
ratio), and the swap into ``PLUGINS_DIR`` is transactional — a failed or
invalid archive can never replace a working plugin.

**Execution** is trusted-by-decision (an administrator installed the code)
but still runs in a separate process with resource limits and a scrubbed
environment, so a buggy plugin cannot hang or crash the bot.  That is
containment, not a sandbox; see ``plugin_host.py`` for the exact contract.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import OrderedDict

try:  # package import (tests, `python -m rayan_dl_bot.bot`)
    from . import config
    from . import netguard
except ImportError:  # flat import (production: sys.path contains the package dir)
    import config
    import netguard

log = logging.getLogger("rayan-dl")

PLUGINS_DIR = config.PLUGINS_DIR
MANIFEST = "plugin.json"
ALLOWED_ENTRY_EXTS = (".py",)
DEFAULT_PLUGIN_TIMEOUT = 90
MIN_PLUGIN_TIMEOUT = 1
MAX_PLUGIN_TIMEOUT = 600

NAME_RE = re.compile(r"[A-Za-z0-9_-]{2,40}\Z")
MANIFEST_MAX_BYTES = 512 * 1024
ENTRY_MAX_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 30 * 1024 * 1024          # compressed download cap
MAX_ZIP_UNCOMPRESSED = 60 * 1024 * 1024   # expanded cap
MAX_ZIP_ENTRIES = 400
MAX_COMPRESSION_RATIO = 120               # per-entry bomb heuristic
MAX_ENTRY_DEPTH = 2                       # plugin.json at root or one dir deep

HOST_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_host.py")
RESULT_PREFIX = "@@RESULT@@"
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                    "PYTHONIOENCODING", "SSL_CERT_FILE", "SSL_CERT_DIR",
                    "RAYAN_DOWNLOAD_DIR", "RAYAN_DATA_DIR", "RAYAN_PLUGINS_DIR",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


# ── Discovery ─────────────────────────────────────────────────
def _read_manifest(directory: str, name: str) -> dict | None:
    path = os.path.join(directory, MANIFEST)
    try:
        if os.path.getsize(path) > MANIFEST_MAX_BYTES:
            log.warning("plugin %s: manifest too large", name)
            return None
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("plugin %s manifest error: %s", name, exc)
        return None
    if not isinstance(meta, dict):
        log.warning("plugin %s: manifest is not an object", name)
        return None
    meta.setdefault("name", name)
    meta.setdefault("version", "0.0.0")
    meta.setdefault("description", "")
    meta.setdefault("sites", [])
    meta.setdefault("entry", "plugin")
    meta.setdefault("enabled", True)
    meta.setdefault("config", {})
    if not isinstance(meta["sites"], list) or not isinstance(meta["config"], dict):
        log.warning("plugin %s: malformed sites/config", name)
        return None
    meta["_dir"] = directory
    return meta


def discover_plugins() -> list[dict]:
    """Return metadata for every plugin folder with a valid manifest."""
    plugins: list[dict] = []
    try:
        entries = sorted(os.listdir(PLUGINS_DIR))
    except OSError:
        return plugins
    for name in entries:
        if not NAME_RE.match(name):
            continue
        directory = os.path.join(PLUGINS_DIR, name)
        if not os.path.isdir(directory) or not os.path.isfile(
                os.path.join(directory, MANIFEST)):
            continue
        meta = _read_manifest(directory, name)
        if meta is not None:
            plugins.append(meta)
    return plugins


def _site_match(site_rule, url: str) -> bool:
    """Match a site rule against a URL by hostname (exact or subdomain)."""
    if not site_rule or not isinstance(site_rule, str):
        return False
    rule = site_rule.lower().strip().lstrip(".")
    if not rule:
        return False
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host and (host == rule or host.endswith("." + rule)):
            return True
    except Exception:
        pass
    # Legacy rules that are not domains (e.g. "tiktok/wildcard") may still
    # use substring matching; a domain rule must match the parsed hostname, or
    # "instagram.com" would also match "notinstagram.com".
    if "/" in rule or "*" in rule:
        return rule in url.lower()
    return False


def find_plugin_for_url(url: str) -> dict | None:
    for meta in discover_plugins():
        if not meta.get("enabled"):
            continue
        for rule in meta.get("sites", []):
            if _site_match(rule, url):
                return meta
    return None


def get_plugin(name: str) -> dict | None:
    if not isinstance(name, str):
        return None
    for meta in discover_plugins():
        if meta.get("name") == name or os.path.basename(meta["_dir"]) == name:
            return meta
    return None


def plugin_entry_path(meta: dict) -> str:
    entry = meta.get("entry", "plugin")
    if not isinstance(entry, str) or not entry:
        entry = "plugin"
    if not entry.endswith(".py"):
        entry += ".py"
    base = os.path.realpath(meta["_dir"])
    path = os.path.realpath(os.path.join(base, entry))
    # Installation validates this, but a hand-edited plugin.json must not be
    # able to point the host at an arbitrary file outside the plugin folder.
    if not (path == base or path.startswith(base + os.sep)):
        raise ValueError("plugin entry escapes the plugin directory")
    return path


def _timeout_for(meta: dict) -> float:
    try:
        value = float(meta.get("timeout", DEFAULT_PLUGIN_TIMEOUT))
    except (TypeError, ValueError):
        return float(DEFAULT_PLUGIN_TIMEOUT)
    return float(max(MIN_PLUGIN_TIMEOUT, min(value, MAX_PLUGIN_TIMEOUT)))


# ── Execution ─────────────────────────────────────────────────
_module_cache: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
_MODULE_CACHE_MAX = 16


def _load_module_from_file(path: str, modname: str):
    """In-process loader, used only when isolation is disabled."""
    import importlib.util

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _module_cache.get(path)
    if cached and cached[0] == mtime:
        _module_cache.move_to_end(path)
        return cached[1]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {modname}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _module_cache[path] = (mtime, module)
    while len(_module_cache) > _MODULE_CACHE_MAX:
        _module_cache.popitem(last=False)
    return module


def call_with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Legacy in-process call with a timeout. NOT a sandbox: a stuck call keeps
    its thread alive until the process exits. Only used when
    ``RAYAN_PLUGIN_ISOLATION=0``."""
    import concurrent.futures

    global _plugin_pool
    if _plugin_pool is None:
        _plugin_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="rayan-plugin")
    return _plugin_pool.submit(fn, *args, **kwargs).result(timeout=timeout_s)


_plugin_pool = None


def _child_env(tmpdir: str) -> dict:
    env = {key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ}
    env["TMPDIR"] = tmpdir
    env["HOME"] = tmpdir
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_isolated(entry: str, method: str, payload: dict, timeout: float):
    """Run one plugin method in a child process. Raises TimeoutError on expiry."""
    tmpdir = tempfile.mkdtemp(prefix="rayan-plugin-")
    env = _child_env(tmpdir)
    env["RAYAN_PLUGIN_CPU"] = str(int(timeout) + 15)
    payload = dict(payload)
    payload.setdefault("config", {})
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", HOST_SCRIPT, entry, method],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=os.path.dirname(entry), start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"cannot start plugin host: {exc}") from None

    try:
        try:
            stdout, stderr = process.communicate(
                json.dumps(payload).encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            raise TimeoutError(f"plugin exceeded {timeout:.0f}s") from None
    finally:
        if process.poll() is None:
            _kill_group(process)
        shutil.rmtree(tmpdir, ignore_errors=True)

    text_err = (stderr or b"").decode("utf-8", "replace").strip()
    if text_err:
        for line in text_err.splitlines()[-20:]:
            log.warning("plugin[%s] %s", method, line)

    for line in reversed((stdout or b"").decode("utf-8", "replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                envelope = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                break
            if envelope.get("ok"):
                return envelope.get("result")
            raise RuntimeError(envelope.get("error") or "plugin failed")

    raise RuntimeError(
        f"plugin host exited with {process.returncode} without a result")


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _invoke(meta: dict, method: str, url: str, config_override: dict | None = None):
    """Dispatch one method to the plugin, isolated when enabled."""
    entry = plugin_entry_path(meta)
    if not os.path.isfile(entry):
        raise FileNotFoundError(f"plugin entry missing: {os.path.basename(entry)}")
    if os.path.getsize(entry) > ENTRY_MAX_BYTES:
        raise RuntimeError("plugin entry is too large")

    timeout = _timeout_for(meta)
    cfg = dict(meta.get("config") or {})
    if config_override:
        cfg.update(config_override)

    if not config.PLUGIN_ISOLATION:
        module = _load_module_from_file(entry, f"plugin_{meta['name']}")
        plugin_cls = getattr(module, "Plugin", None)
        if plugin_cls is None:
            raise RuntimeError("plugin has no Plugin class")
        plugin = plugin_cls()
        function = getattr(plugin, method, None)
        if function is None:
            raise RuntimeError(f"plugin has no {method}() method")
        if method in ("detect", "get_chapters"):
            return call_with_timeout(function, timeout, url)
        return call_with_timeout(function, timeout, url, cfg)

    return _run_isolated(entry, method, {"url": url, "config": cfg}, timeout)


def call_plugin(meta: dict, url: str, config: dict | None = None) -> dict:
    """Invoke a plugin's handle() with a hard timeout. Returns dict or error dict."""
    try:
        result = _invoke(meta, "handle", url, config)
    except TimeoutError:
        log.error("plugin %s TIMEOUT", meta.get("name"))
        return {"error": "پلاگین بیش از حد طول کشید (timeout)"}
    except Exception as exc:  # noqa: BLE001 - plugin boundary
        log.error("plugin %s error: %s", meta.get("name"), exc)
        return {"error": f"plugin error: {exc}"}
    if not isinstance(result, dict):
        return {"error": "plugin returned an invalid result"}
    return result


def call_plugin_chapters(meta: dict, url: str) -> list:
    try:
        result = _invoke(meta, "get_chapters", url)
    except Exception as exc:  # noqa: BLE001
        log.error("plugin %s get_chapters error: %s", meta.get("name"), exc)
        return []
    if not isinstance(result, list):
        return []
    chapters = []
    for item in result:
        if isinstance(item, dict) and item.get("url"):
            chapters.append(item)
    return chapters


def call_plugin_detect_type(meta: dict, url: str):
    try:
        result = _invoke(meta, "detect", url)
    except Exception as exc:  # noqa: BLE001
        log.error("plugin %s detect error: %s", meta.get("name"), exc)
        return None
    return result if isinstance(result, str) else None


def call_plugin_download_chapter(meta: dict, chapter_url: str,
                                 config: dict | None = None) -> dict:
    try:
        result = _invoke(meta, "download_chapter", chapter_url, config)
    except TimeoutError:
        return {"error": "پلاگین بیش از حد طول کشید (timeout)"}
    except Exception as exc:  # noqa: BLE001
        log.error("plugin %s download_chapter error: %s", meta.get("name"), exc)
        return {"error": f"plugin error: {exc}"}
    if not isinstance(result, dict):
        return {"error": "plugin returned an invalid result"}
    return result


# ── Install: validation helpers ───────────────────────────────
def _ip_is_forbidden(ip: str) -> bool:
    """Backwards-compatible helper: True when an address must not be dialled.

    The authoritative check lives in :func:`netguard.ip_is_public`; this shim
    keeps older call sites/tests working without duplicating range tables.
    """
    try:
        return not netguard.ip_is_public(ip)
    except (TypeError, ValueError):
        return True


def _validate_install_url(url: str) -> str | None:
    """SSRF guard for administrator-supplied URLs (compat shim).

    Returns a Persian error message, or ``None`` when the URL may be fetched.
    Only ``https`` is accepted; every resolved address must be public.
    """
    try:
        target = netguard.parse_target(url)
    except netguard.NetGuardError as exc:
        return str(exc)
    if target.scheme != "https":
        return "فقط URL با پروتکل https مجاز است"
    try:
        netguard.resolve_public(target)
    except netguard.NetGuardError as exc:
        return str(exc)
    return None


class _CompatResponse:
    """Minimal ``urlopen``-shaped wrapper kept for plugin_builder's caller."""

    def __init__(self, response: netguard.Response):
        self._response = response
        self.status = response.status
        self.headers = response.headers

    def read(self, *_args, **_kwargs) -> bytes:
        return self._response.body

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _safe_urlopen(url: str, timeout: float = 30, max_bytes: int = 4 * 1024 * 1024):
    """Compatibility shim: fetch through netguard, 8 MiB cap by default."""
    return _CompatResponse(netguard.fetch(url, timeout=timeout, max_bytes=max_bytes))


def _validate_meta(meta) -> str | None:
    if not isinstance(meta, dict):
        return "plugin.json must be a JSON object"
    name = meta.get("name", "")
    if not NAME_RE.match(name or ""):
        return "invalid plugin name (use letters, digits, - or _)"
    sites = meta.get("sites")
    if not isinstance(sites, list) or not sites:
        return "plugin.json must declare a non-empty 'sites' list"
    if not all(isinstance(site, str) and site.strip() for site in sites):
        return "plugin.json 'sites' must contain non-empty strings"
    entry = meta.get("entry", "plugin")
    if not isinstance(entry, str) or not entry:
        return "plugin.json 'entry' must be a non-empty string"
    if not entry.endswith(".py"):
        entry += ".py"
    if ".." in entry or "/" in entry.replace("\\", "/") or entry.startswith("."):
        return "invalid entry path"
    if entry not in ALLOWED_ENTRY_EXTS and not entry.endswith(".py"):
        return "entry must be a .py file"
    if not isinstance(meta.get("config", {}), dict):
        return "plugin.json 'config' must be an object"
    return None


def _safe_extract(zf: zipfile.ZipFile, tmp: str) -> str | None:
    """Inspect then extract an archive. Returns an error string or ``None``.

    Guards: entry count, total size, per-entry expansion ratio, absolute
    paths, drive letters, ``..`` traversal, symlinks, and duplicate targets.
    """
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        return f"zip has too many files ({len(infos)})"
    base = os.path.realpath(tmp)
    total = 0
    seen: set[str] = set()
    for info in infos:
        name = info.filename or ""
        if "\x00" in name:
            return "zip contains a NUL byte in a filename"
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return f"absolute path in zip: {name}"
        parts = [p for p in normalized.split("/") if p not in ("", ".")]
        if any(part == ".." for part in parts):
            return f"path traversal in zip: {name}"
        if len(parts) > MAX_ENTRY_DEPTH + 4:
            return f"zip entry nested too deeply: {name}"

        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            return f"symlink in zip not allowed: {name}"
        if mode not in (0, 0o100000, 0o040000, 0o040755, 0o100755, 0o100644):
            return f"unsupported file type in zip: {name}"

        dest = os.path.realpath(os.path.join(tmp, *parts)) if parts else base
        if not (dest == base or dest.startswith(base + os.sep)):
            return f"unsafe path in zip: {name}"
        if dest in seen:
            return f"duplicate path in zip: {name}"
        seen.add(dest)

        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED:
            return "zip too large when extracted"
        if info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            return f"suspicious compression ratio in zip: {name}"
        if info.flag_bits & 0x1:
            return "encrypted zip entries are not supported"

    try:
        zf.extractall(tmp)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return f"zip extraction failed: {exc}"
    return None


def _prepare_install_root(tmp: str, manifest_name: str) -> tuple[str | None, str | None]:
    """Return (plugin_root, error). ``plugin_root`` is the folder holding plugin.json."""
    relative = os.path.dirname(manifest_name.replace("\\", "/"))
    depth = 0 if relative in ("", ".") else len(relative.split("/"))
    if depth > MAX_ENTRY_DEPTH:
        return None, "plugin.json is nested too deeply"
    root = os.path.join(tmp, *[p for p in relative.split("/") if p not in ("", ".")])
    real_root = os.path.realpath(root)
    real_tmp = os.path.realpath(tmp)
    if not (real_root == real_tmp or real_root.startswith(real_tmp + os.sep)):
        return None, "plugin.json escapes the archive"
    return real_root, None


def _staged_install(root: str, name: str, meta: dict) -> dict:
    """Validate and atomically swap a prepared plugin folder into place."""
    entry = meta.get("entry", "plugin")
    if not entry.endswith(".py"):
        entry += ".py"
    entry_path = os.path.join(root, entry)
    if not os.path.isfile(entry_path):
        return {"ok": False, "message": f"entry file '{entry}' is missing"}
    try:
        with open(entry_path, encoding="utf-8", errors="replace") as handle:
            source = handle.read(ENTRY_MAX_BYTES + 1)
        if len(source) > ENTRY_MAX_BYTES:
            return {"ok": False, "message": "entry file is too large"}
        compile(source, entry, "exec")
    except (OSError, SyntaxError, ValueError) as exc:
        return {"ok": False, "message": f"پلاگین کد پایتون معتبر نیست: {exc}"}
    if not re.search(r"^\s*class\s+Plugin\b", source, re.M):
        return {"ok": False, "message": "کد پلاگین کلاس Plugin ندارد"}

    os.makedirs(PLUGINS_DIR, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=f".{name}.staging-", dir=PLUGINS_DIR)
    backup = None
    dest = os.path.join(PLUGINS_DIR, name)
    try:
        shutil.copytree(root, staging, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if os.path.exists(dest):
            backup = f"{dest}.old-{int(time.time())}"
            os.replace(dest, backup)
        os.replace(staging, dest)
        staging = None
    except OSError as exc:
        if backup and not os.path.exists(dest):
            try:
                os.replace(backup, dest)
            except OSError:
                log.error("plugin install rollback failed for %s", name)
        return {"ok": False, "message": f"install error: {exc}"}
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    if backup:
        shutil.rmtree(backup, ignore_errors=True)
    return {"ok": True, "name": name, "message": f"پلاگین '{name}' نصب شد"}


def install_plugin_from_file(zip_path: str) -> dict:
    """Install a plugin from a local .zip (uploaded via the admin panel)."""
    tmp = tempfile.mkdtemp(prefix="plugin_install_")
    try:
        try:
            if os.path.getsize(zip_path) > MAX_ZIP_BYTES:
                return {"ok": False, "message": "zip file is too large"}
        except OSError as exc:
            return {"ok": False, "message": f"cannot read zip: {exc}"}
        if not zipfile.is_zipfile(zip_path):
            return {"ok": False, "message": "فایل دریافتی یک ZIP معتبر نیست"}
        with zipfile.ZipFile(zip_path) as zf:
            manifest_name = next((n for n in zf.namelist()
                                  if n.replace("\\", "/").endswith(MANIFEST)), None)
            if not manifest_name:
                return {"ok": False, "message": "zip شامل plugin.json نیست"}
            error = _safe_extract(zf, tmp)
            if error:
                return {"ok": False, "message": error}
        root, error = _prepare_install_root(tmp, manifest_name)
        if error or root is None:
            return {"ok": False, "message": error or "invalid archive layout"}
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as handle:
            meta = json.load(handle)
        error = _validate_meta(meta)
        if error:
            return {"ok": False, "message": error}
        return _staged_install(root, meta["name"], meta)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        log.error("install plugin from file error: %s", exc)
        return {"ok": False, "message": f"install error: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_plugin_from_url(url: str) -> dict:
    """Download a plugin (zip archive or raw GitHub files) and install it."""
    error = _validate_install_url(url)
    if error:
        return {"ok": False, "message": error}
    cleaned = url.split("?", 1)[0].split("#", 1)[0].lower()
    try:
        if cleaned.endswith(".zip") or "zipball" in url or "codeload" in url:
            return _install_zip(url)
        if cleaned.endswith(".json") or cleaned.endswith(".py"):
            return _install_manifest(url)
        return {"ok": False,
                "message": "URL must be a .zip, .json manifest, or raw GitHub file"}
    except netguard.NetGuardError as exc:
        return {"ok": False, "message": f"download blocked: {exc}"}
    except Exception as exc:  # noqa: BLE001
        log.error("install plugin error: %s", exc)
        return {"ok": False, "message": f"install error: {exc}"}


def _install_zip(url: str) -> dict:
    tmp = tempfile.mkdtemp(prefix="plugin_install_")
    zip_path = os.path.join(tmp, "plugin.zip")
    try:
        try:
            netguard.fetch_to_file(url, zip_path, max_bytes=MAX_ZIP_BYTES,
                                   timeout=30, total_timeout=120)
        except netguard.ResponseTooLarge:
            return {"ok": False, "message": "فایل zip بیش از حد بزرگ است"}
        if not zipfile.is_zipfile(zip_path):
            return {"ok": False, "message": "فایل دریافتی یک ZIP معتبر نیست"}
        with zipfile.ZipFile(zip_path) as zf:
            manifest_name = next((n for n in zf.namelist()
                                  if n.replace("\\", "/").endswith(MANIFEST)), None)
            if not manifest_name:
                return {"ok": False, "message": "zip has no plugin.json"}
            error = _safe_extract(zf, tmp)
            if error:
                return {"ok": False, "message": error}
        root, error = _prepare_install_root(tmp, manifest_name)
        if error or root is None:
            return {"ok": False, "message": error or "invalid archive layout"}
        manifest_path = os.path.join(root, MANIFEST)
        try:
            if os.path.getsize(manifest_path) > MANIFEST_MAX_BYTES:
                return {"ok": False, "message": "plugin.json is too large"}
        except OSError as exc:
            return {"ok": False, "message": f"cannot read plugin.json: {exc}"}
        with open(manifest_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        error = _validate_meta(meta)
        if error:
            return {"ok": False, "message": error}
        return _staged_install(root, meta["name"], meta)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _validate_entry_code(code: str) -> str | None:
    """Cheap sanity check for fetched entry source (not a security boundary)."""
    try:
        compile(code, "<plugin-entry>", "exec")
    except SyntaxError as exc:
        return f"پلاگین کد پایتون معتبر نیست: {exc}"
    if not re.search(r"^\s*class\s+Plugin\b", code, re.M):
        return "کد پلاگین کلاس Plugin ندارد"
    return None


def _install_manifest(url: str) -> dict:
    """Install from a raw plugin.json; the entry file is fetched from the same
    directory. Both files are downloaded and validated before anything on disk
    changes, then written atomically — a failure cannot leave a half plugin."""
    try:
        meta = netguard.fetch_json(url, max_bytes=MANIFEST_MAX_BYTES, timeout=20)
    except netguard.NetGuardError as exc:
        return {"ok": False, "message": f"manifest download blocked: {exc}"}
    except ValueError as exc:
        return {"ok": False, "message": f"plugin.json is not valid JSON: {exc}"}
    error = _validate_meta(meta)
    if error:
        return {"ok": False, "message": error}

    name = meta["name"]
    entry = meta.get("entry", "plugin")
    if not entry.endswith(".py"):
        entry += ".py"
    base = url.rsplit("/", 1)[0]
    entry_url = f"{base}/{entry}"
    error = _validate_install_url(entry_url)
    if error:
        return {"ok": False, "message": error}
    try:
        entry_response = netguard.fetch(entry_url, max_bytes=ENTRY_MAX_BYTES, timeout=20)
    except netguard.NetGuardError as exc:
        return {"ok": False, "message": f"entry download blocked: {exc}"}
    source = entry_response.body.decode("utf-8", "replace")
    error = _validate_entry_code(source)
    if error:
        return {"ok": False, "message": error}

    tmp = tempfile.mkdtemp(prefix="plugin_install_")
    try:
        root = os.path.join(tmp, "payload")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, MANIFEST), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
        with open(os.path.join(root, entry), "w", encoding="utf-8") as handle:
            handle.write(source)
        return _staged_install(root, name, meta)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _install_raw(url: str) -> dict:
    return _install_manifest(url)


# ── Admin operations ──────────────────────────────────────────
def remove_plugin(name: str) -> bool:
    if not NAME_RE.match(name or ""):
        return False
    dest = os.path.join(PLUGINS_DIR, name)
    if not os.path.isdir(dest):
        return False
    trash = f"{dest}.removed-{int(time.time())}"
    try:
        os.replace(dest, trash)
    except OSError as exc:
        log.error("remove plugin %s: %s", name, exc)
        return False
    shutil.rmtree(trash, ignore_errors=True)
    return True


def _write_manifest(meta: dict, data: dict) -> bool:
    manifest = os.path.join(meta["_dir"], MANIFEST)
    tmp_path = manifest + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, manifest)
        return True
    except OSError as exc:
        log.error("write manifest %s: %s", manifest, exc)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def toggle_plugin(name: str, enabled: bool) -> bool:
    meta = get_plugin(name)
    if not meta:
        return False
    try:
        with open(os.path.join(meta["_dir"], MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    data["enabled"] = bool(enabled)
    return _write_manifest(meta, data)


def set_plugin_config(name: str, key: str, value) -> bool:
    if not isinstance(key, str) or not key.strip():
        return False
    meta = get_plugin(name)
    if not meta:
        return False
    try:
        with open(os.path.join(meta["_dir"], MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    data.setdefault("config", {})[key] = value
    return _write_manifest(meta, data)
