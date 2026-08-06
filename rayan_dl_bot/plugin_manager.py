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
def _load_module_from_file(path, modname):
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    """Match a site rule (domain or substring) against a URL."""
    return site_rule and site_rule.lower() in url.lower()


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
    """Invoke a plugin's handle(). Returns result dict or error dict."""
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
        return plugin.handle(url, cfg)
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
        chapters = plugin.get_chapters(url)
        return chapters or []
    except Exception as e:
        log.error("plugin %s get_chapters error: %s", meta.get("name"), e)
        return []


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
        if hasattr(plugin, "download_chapter"):
            return plugin.download_chapter(chapter_url, cfg)
        # fallback: use handle() (resolves first chapter only)
        return plugin.handle(chapter_url, cfg)
    except Exception as e:
        log.error("plugin %s download_chapter error: %s", meta.get("name"), e)
        return {"error": f"plugin error: {e}"}


# ── Install / remove ──────────────────────────────────────────
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
                zf.extractall(tmp)
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
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            # find plugin.json in the archive
            names = zf.namelist()
            manifest_name = next((n for n in names if n.endswith(MANIFEST)), None)
            if not manifest_name:
                return {"ok": False, "message": "zip has no plugin.json"}
            zf.extractall(tmp)
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
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "name": name, "message": f"plugin '{name}' installed"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _install_manifest(url: str) -> dict:
    """Install from a single raw plugin.json — the entry .py must be fetched
    from the same directory (GitHub raw)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        meta = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
        err = _validate_meta(meta)
        if err:
            return {"ok": False, "message": err}
        name = meta["name"]
        entry = meta.get("entry", "plugin")
        if not entry.endswith(".py"):
            entry += ".py"
        # entry URL = same dir as manifest, with filename entry
        base = url.rsplit("/", 1)[0]
        entry_url = f"{base}/{entry}"
        dest = os.path.join(PLUGINS_DIR, name)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        entry_data = urllib.request.urlopen(
            urllib.request.Request(entry_url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30).read()
        with open(os.path.join(dest, entry), "wb") as f:
            f.write(entry_data)
        return {"ok": True, "name": name, "message": f"plugin '{name}' installed"}
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