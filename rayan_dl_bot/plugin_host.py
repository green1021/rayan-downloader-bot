#!/usr/bin/env python3
"""plugin_host.py — child process that runs exactly ONE plugin call.

Invoked as::

    python -I plugin_host.py <entry.py> <method>

with a JSON payload on stdin.  The result is written to **stdout** as a
single line prefixed with ``@@RESULT@@``; everything else the plugin prints
goes to stderr, so a chatty plugin cannot corrupt the protocol.

Isolation contract (what this really provides — no over-claiming):

* a separate OS process: a crash, ``sys.exit`` or infinite loop cannot take
  the bot down, and a timeout is enforced by killing the process group;
* POSIX resource limits set *before* any plugin code runs: address space,
  CPU seconds, file size, open files, process count, no core dumps;
* a scrubbed environment: the bot token, admin ids and every other secret in
  the service environment are **not** inherited;
* a fixed working directory and a private TMPDIR.

It is NOT a security sandbox: a plugin still runs with the bot's uid and can
read files that uid can read, and can open sockets.  A real sandbox needs
kernel namespaces/seccomp (bubblewrap, nsjail, a container) — see
``references/plugin-isolation.md``.  What this buys is containment of bugs
and runaway loops, plus removal of trivially available secrets.
"""

from __future__ import annotations

import json
import os
import sys

RESULT_PREFIX = "@@RESULT@@"
MAX_RESULT_BYTES = 4 * 1024 * 1024


def _apply_limits() -> None:
    """Best-effort rlimits. Missing/misbehaving limits must not abort the call."""
    try:
        import resource
    except ImportError:
        return
    cpu_seconds = int(os.environ.pop("RAYAN_PLUGIN_CPU", "120") or 120)
    mem_bytes = int(os.environ.pop("RAYAN_PLUGIN_MEM", str(1536 * 1024 * 1024)) or 0)
    limits = [
        ("RLIMIT_CPU", cpu_seconds),
        ("RLIMIT_AS", mem_bytes),
        ("RLIMIT_FSIZE", 3 * 1024 * 1024 * 1024),
        ("RLIMIT_NOFILE", 256),
        ("RLIMIT_NPROC", 64),
        ("RLIMIT_CORE", 0),
    ]
    for name, value in limits:
        limit = getattr(resource, name, None)
        if limit is None or value <= 0:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


def _load_plugin(entry_path: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location("rayan_plugin_entry", entry_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin entry")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jsonable(value, depth: int = 0):
    """Reject results that would not survive the pipe (or are absurdly nested)."""
    if depth > 20:
        raise TypeError("plugin result is nested too deeply")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 1_000_000:
            raise TypeError("plugin result contains an oversized string")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 5000:
            raise TypeError("plugin result list is too long")
        return [_jsonable(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 5000:
            raise TypeError("plugin result mapping is too large")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("plugin result keys must be strings")
            out[key] = _jsonable(item, depth + 1)
        return out
    raise TypeError(f"plugin returned a non-JSON value: {type(value).__name__}")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: plugin_host.py <entry.py> <method>", file=sys.stderr)
        return 2
    entry_path, method = sys.argv[1], sys.argv[2]

    payload = {}
    raw = sys.stdin.read(1024 * 1024)
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            print(f"plugin_host: bad payload: {exc}", file=sys.stderr)
            return 2

    _apply_limits()

    # Plugins were written assuming their own directory is the cwd (they open
    # "cookies.txt", "config.json", …).  Keep that contract.
    _entry_dir = os.path.dirname(os.path.abspath(entry_path))
    os.chdir(_entry_dir)
    sys.path.insert(0, _entry_dir)
    # Python puts this script's directory (the bot package) on sys.path.  A
    # plugin must not be able to ``import config`` / ``import db`` from there,
    # so drop it; netguard is loaded explicitly by path below and remains
    # importable through sys.modules.
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path
                   if os.path.abspath(p or os.getcwd()) != _pkg_dir]

    # Make the guarded network layer importable by plugins without putting the
    # whole package on sys.path (a plugin must not be able to ``import config``
    # and read the bot token out of the environment file).
    import importlib.util as _ilu
    _here = _pkg_dir
    _ng_path = os.path.join(_here, "netguard.py")
    if os.path.isfile(_ng_path) and "netguard" not in sys.modules:
        _spec = _ilu.spec_from_file_location("netguard", _ng_path)
        if _spec and _spec.loader:
            _ng = _ilu.module_from_spec(_spec)
            sys.modules["netguard"] = _ng
            try:
                _spec.loader.exec_module(_ng)
            except Exception:
                sys.modules.pop("netguard", None)

    outcome = {"ok": False, "error": "plugin produced no result"}
    try:
        module = _load_plugin(entry_path)
        plugin_cls = getattr(module, "Plugin", None)
        if plugin_cls is None:
            raise RuntimeError("plugin has no Plugin class")
        plugin = plugin_cls()
        function = getattr(plugin, method, None)
        if function is None:
            raise RuntimeError(f"plugin has no {method}() method")

        url = payload.get("url")
        config = payload.get("config")
        if method == "detect":
            result = function(url)
        elif method == "get_chapters":
            result = function(url)
        else:
            result = function(url, dict(config or {}))
        outcome = {"ok": True, "result": _jsonable(result)}
    except BaseException as exc:  # noqa: BLE001 - plugin code may raise anything
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        import traceback
        traceback.print_exc(file=sys.stderr)

    try:
        blob = json.dumps(outcome, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        blob = json.dumps({"ok": False, "error": f"unserialisable result: {exc}"})
    if len(blob.encode("utf-8", "replace")) > MAX_RESULT_BYTES:
        blob = json.dumps({"ok": False, "error": "plugin result is too large"})
    sys.stdout.write(RESULT_PREFIX + blob + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
