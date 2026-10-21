#!/usr/bin/env python3
"""config.py — single source of truth for runtime configuration.

Everything the bot needs to know about *where* things live and *how big*
they may get is resolved here, from the environment, once at import time.

Design rules
------------
* stdlib only, no side effects beyond reading env vars and (lazily)
  creating the directories the application owns;
* every value is validated — a bad value raises ``ConfigError`` with a
  message that is safe to show an operator (never a token or a secret);
* defaults reproduce the historical deployment layout, so an existing
  installation keeps working with no ``.env`` changes;
* tokens are NEVER logged, echoed in exceptions, or stored in this module.

Environment variables (all optional):

    TG_BOT_TOKEN / BOT_TOKEN     bot token (preferred source)
    RAYAN_TOKEN_FILE             file containing the token on its first line
    TG_ADMIN_IDS                 comma-separated numeric admin ids
    RAYAN_DATA_DIR               data directory            (default /root/hermes-workspace/data)
    RAYAN_DB_PATH                sqlite database path      (default <data>/rayan.db)
    RAYAN_DOWNLOAD_DIR           scratch download directory(default /root/hermes-workspace/tmp/downloads)
    RAYAN_PLUGINS_DIR            plugin directory          (default <package>/plugins)
    TG_API_BASE                  local Bot API base url    (default: official API)
    RAYAN_API_STORAGE            local Bot API file store  (default: disabled)
    RAYAN_DISK_LIMIT_GB          download area budget      (default 2)
    RAYAN_MAX_FILE_SIZE_MB       per-file size cap         (default 1900)
    RAYAN_TLS_INSECURE           "1" disables TLS verification everywhere (default off)
    RAYAN_PLUGIN_ISOLATION       "0" runs plugins in-process (default: isolated subprocess)
    RAYAN_YTDLP                  path to the yt-dlp executable (default: auto-detect)
"""

from __future__ import annotations

import os
import shutil
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)


class ConfigError(RuntimeError):
    """Raised for invalid or missing configuration. Safe to show to operators."""


# ── helpers ───────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not (lo <= value <= hi):
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (0/1), got {raw!r}")


# ── paths ─────────────────────────────────────────────────────
DATA_DIR = os.path.abspath(_env("RAYAN_DATA_DIR", "/root/hermes-workspace/data"))
DB_PATH = os.path.abspath(_env("RAYAN_DB_PATH", os.path.join(DATA_DIR, "rayan.db")))
DOWNLOAD_DIR = os.path.abspath(
    _env("RAYAN_DOWNLOAD_DIR", "/root/hermes-workspace/tmp/downloads"))
# Historical plugin store. Plugins installed before the code moved into this
# repository still live here; keep using it when present so upgrades do not
# orphan them, and fall back to the in-repo directory on fresh installs.
_LEGACY_PLUGINS_DIR = "/root/hermes-workspace/scripts/rayan_dl_bot/plugins"
PLUGINS_DIR = os.path.abspath(_env(
    "RAYAN_PLUGINS_DIR",
    _LEGACY_PLUGINS_DIR if os.path.isdir(_LEGACY_PLUGINS_DIR)
    else os.path.join(PACKAGE_DIR, "plugins")))

# The local Bot API server keeps its own copy of every file it has seen.
# That directory belongs to Telegram's server, not to this bot: cleanup must
# never delete from it unless an operator explicitly says it is ours.
API_STORAGE = os.path.abspath(_env("RAYAN_API_STORAGE")) if _env("RAYAN_API_STORAGE") else ""

# ── limits ────────────────────────────────────────────────────
DISK_LIMIT_GB = _env_int("RAYAN_DISK_LIMIT_GB", 2, 1, 10_000)
MAX_FILE_SIZE_MB = _env_int("RAYAN_MAX_FILE_SIZE_MB", 1900, 1, 2000)

# ── network ───────────────────────────────────────────────────
TG_API_BASE = _env("TG_API_BASE")
TLS_INSECURE = _env_bool("RAYAN_TLS_INSECURE", False)
PLUGIN_ISOLATION = _env_bool("RAYAN_PLUGIN_ISOLATION", True)

# ── identity ──────────────────────────────────────────────────
# Only numeric ids are accepted; anything else is ignored (with a warning)
# so one typo cannot take the whole bot down at import time.
def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            print(f"[config] ignoring non-numeric TG_ADMIN_IDS entry {chunk!r}",
                  file=sys.stderr)
            continue
        if value > 0:
            ids.append(value)
    return ids


ADMIN_IDS = _parse_admin_ids(_env("TG_ADMIN_IDS"))


def _load_token() -> str:
    """Resolve the bot token without ever echoing it.

    Order: environment (``TG_BOT_TOKEN`` or ``BOT_TOKEN``) → the first line of
    the optional ``RAYAN_TOKEN_FILE``.  The old fallback that scraped the token
    out of a developer script was removed: a stray source file must never be
    able to supply credentials to the service.
    """
    for name in ("TG_BOT_TOKEN", "BOT_TOKEN"):
        value = _env(name)
        if value and "***" not in value:
            return value

    token_file = _env("RAYAN_TOKEN_FILE")
    if not token_file or not os.path.isfile(token_file):
        return ""
    try:
        with open(token_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return ""
    token = text.strip().splitlines()[0].strip() if text.strip() else ""
    return token if token and "***" not in token else ""


BOT_TOKEN = _load_token()


# ── validation ────────────────────────────────────────────────
_TOKEN_SHAPE = None


def token_looks_valid(token: str) -> bool:
    """``<digits>:<35-ish chars>`` — shape check only, no network call."""
    if not token or ":" not in token:
        return False
    head, _, tail = token.partition(":")
    return head.isdigit() and len(head) >= 5 and len(tail) >= 20


def validate(startup: bool = True) -> list[str]:
    """Validate the configuration.

    Returns a list of non-fatal warnings. Raises :class:`ConfigError` for
    anything that would make the bot misbehave silently.

    ``startup=False`` skips the token requirement (used by tests and tools
    that only need paths/limits).
    """
    warnings: list[str] = []

    if startup and not token_looks_valid(BOT_TOKEN):
        raise ConfigError(
            "No valid bot token configured. Set TG_BOT_TOKEN in the environment "
            "(or point RAYAN_TOKEN_FILE at a file containing it)."
        )

    if not os.path.isabs(DB_PATH):
        raise ConfigError("RAYAN_DB_PATH must be an absolute path")

    for label, path in (("RAYAN_DOWNLOAD_DIR", DOWNLOAD_DIR),
                        ("RAYAN_DATA_DIR", DATA_DIR)):
        parent = path if os.path.isdir(path) else os.path.dirname(path)
        if parent and not os.path.isdir(parent) and not os.access(
                os.path.dirname(parent) or "/", os.W_OK):
            raise ConfigError(f"{label} is not creatable: {path}")

    if API_STORAGE:
        if API_STORAGE in ("/", "/var", "/var/lib", "/tmp", "/root", "/home"):
            raise ConfigError("RAYAN_API_STORAGE points at a system directory")
        if not os.path.isdir(API_STORAGE):
            warnings.append(
                f"RAYAN_API_STORAGE={API_STORAGE} does not exist; disk cleanup will ignore it")
    else:
        warnings.append(
            "RAYAN_API_STORAGE is not set: the local Bot API file store is not "
            "managed by this bot (correct unless you run the Bot API server here)")

    if TLS_INSECURE:
        warnings.append(
            "RAYAN_TLS_INSECURE=1 — TLS certificate verification is DISABLED. "
            "Only use this on a trusted network; every download is interceptable.")

    if not ADMIN_IDS:
        warnings.append(
            "TG_ADMIN_IDS is empty: nobody can use /admin unless an admin row "
            "already exists in the database")

    if max_file_size_mb_effective() < 1:
        raise ConfigError("MAX_FILE_SIZE_MB resolved to 0")

    return warnings


def max_file_size_mb_effective() -> int:
    """Upload cap implied by the API endpoint (cloud API caps at 50 MB)."""
    return MAX_FILE_SIZE_MB if TG_API_BASE else 49


def ensure_dirs() -> None:
    """Create the directories this application owns (idempotent)."""
    for path in (DATA_DIR, os.path.dirname(DB_PATH), DOWNLOAD_DIR, PLUGINS_DIR):
        if path:
            os.makedirs(path, exist_ok=True)
    # Ownership marker: cleanup only ever touches directories that carry it,
    # so a mis-configured path cannot make us delete another service's files.
    marker = os.path.join(DOWNLOAD_DIR, ".rayan-owned")
    if not os.path.exists(marker):
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("rayan-downloader-bot scratch directory\n")
        except OSError:
            pass


def resolve_ytdlp() -> str:
    """Locate the yt-dlp executable (env → venv module → PATH → common paths)."""
    override = _env("RAYAN_YTDLP")
    if override:
        if not os.path.isfile(override):
            raise ConfigError(f"RAYAN_YTDLP does not exist: {override}")
        return override
    # Prefer the interpreter's own yt_dlp module: no PATH assumptions, and it
    # is the version pinned in requirements.txt.
    candidate = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("yt-dlp")
    if found:
        return found
    for path in ("/usr/local/bin/yt-dlp", "/usr/bin/yt-dlp"):
        if os.path.isfile(path):
            return path
    raise ConfigError(
        "yt-dlp executable not found. Install it (pip install -r requirements.txt) "
        "or set RAYAN_YTDLP to its path."
    )


def ytdlp_command() -> list[str]:
    """Command prefix for yt-dlp.

    Uses ``python -m yt_dlp`` when the executable script is unavailable, which
    is what a pip-installed venv provides on every platform.
    """
    try:
        return [resolve_ytdlp()]
    except ConfigError:
        return [sys.executable, "-m", "yt_dlp"]
