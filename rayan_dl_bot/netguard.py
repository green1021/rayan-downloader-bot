#!/usr/bin/env python3
"""netguard.py — the bot's single egress path for user-controlled URLs.

Why this module exists
----------------------
Every URL that a user (or a site's HTML) can influence is a potential
Server-Side Request Forgery: ``http://127.0.0.1:8080/admin``,
``http://169.254.169.254/latest/meta-data/``, ``http://[::1]/``,
``http://2130706433/``.  Validating a URL and *then* calling ``urlopen`` is
not enough, because the second DNS lookup can return a different (internal)
address — the classic TOCTOU / DNS-rebinding window.

This module therefore does two things:

1. **Classification** — scheme allow-list, hostname sanity, and IP
   classification for IPv4, IPv6, IPv4-mapped IPv6, 6to4, Teredo and the
   legacy numeric forms that ``inet_aton`` accepts (``127.1``,
   ``0x7f.0.0.1``, ``2130706433``).
2. **Pinned connection** — the hostname is resolved exactly once, every
   returned address is validated, and the TCP connection is opened to that
   validated IP.  TLS SNI and the HTTP ``Host`` header still carry the real
   hostname, so public HTTPS sites work normally while a second, attacker
   controlled DNS answer can never be used.

Limits are enforced while streaming, not after the fact: connect timeout,
per-read timeout, absolute deadline for the whole operation (including
redirects), redirect count, and response size.

The module is stdlib-only on purpose: it must keep working on a minimal
deployment with no extra wheels.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEFAULT_MAX_BYTES = 8 * 1024 * 1024        # 8 MiB — plenty for HTML/metadata
DEFAULT_TIMEOUT = 20.0                     # per socket operation
DEFAULT_TOTAL_TIMEOUT = 60.0               # whole request incl. redirects
DEFAULT_MAX_REDIRECTS = 5
MAX_URL_LENGTH = 8192

ALLOWED_SCHEMES = ("http", "https")

# Hosts that are never legitimate download targets. Checked before DNS so a
# name like "metadata.google.internal" fails fast and identically everywhere.
_BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
}
_BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".localdomain", ".home.arpa",
    ".in-addr.arpa", ".ip6.arpa",
)

# Cloud metadata / link-local services, listed explicitly because a few of
# them live in otherwise "public-looking" ranges.
_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure/OpenStack IMDS
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS (alt)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud
    ipaddress.ip_address("192.0.0.192"),       # Oracle Cloud
    ipaddress.ip_address("168.63.129.16"),     # Azure platform
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IPv6 IMDS
}


class NetGuardError(Exception):
    """Base class for every refusal/limit in this module."""


class DestinationBlocked(NetGuardError):
    """The URL points at a forbidden destination (SSRF attempt or mistake)."""


class InvalidURL(NetGuardError):
    """The URL is malformed or uses a disallowed scheme."""


class ResponseTooLarge(NetGuardError):
    """The response exceeded the configured byte budget."""


class TooManyRedirects(NetGuardError):
    """The redirect chain exceeded the configured hop budget."""


class DeadlineExceeded(NetGuardError):
    """The whole operation exceeded its deadline."""


class HTTPStatusError(NetGuardError):
    """The server answered with a 4xx/5xx status."""

    def __init__(self, url: str, status: int, reason: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status
        self.reason = reason


@dataclass
class Target:
    url: str
    scheme: str
    host: str              # IDNA-encoded hostname used for SNI/Host
    display_host: str      # original hostname (for logs)
    port: int
    path: str

    @property
    def host_header(self) -> str:
        """Value for the HTTP ``Host`` header (port included when non-default)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


@dataclass
class Response:
    url: str
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in ctype.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip('"')
                break
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


# ── IP classification ─────────────────────────────────────────
def _embedded_v4(ip: ipaddress._BaseAddress):
    """Return the IPv4 address tunnelled inside ``ip``, if any."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip.teredo is not None:
            # teredo = (server, client); the client is what matters
            return ip.teredo[1]
    return None


def ip_is_public(ip) -> bool:
    """True only for addresses that are safe to connect to on the internet."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            return False
    # Follow one level of tunnelling (v4-mapped / 6to4 / Teredo).
    inner = _embedded_v4(ip)
    if inner is not None:
        return ip_is_public(inner)
    if ip in _BLOCKED_IPS:
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    # ``is_global`` is the catch-all for CGNAT (100.64/10), benchmarking
    # ranges, documentation ranges and other special-purpose blocks.
    try:
        return bool(ip.is_global)
    except AttributeError:          # Python < 3.4 (never here, but cheap)
        return True


def parse_ip_literal(host: str):
    """Parse an IP literal, including legacy numeric IPv4 spellings.

    Returns an ``ipaddress`` object, or ``None`` when ``host`` is a name.
    Raises :class:`DestinationBlocked` for a host that *looks* numeric but
    cannot be parsed — such input is an obfuscation attempt, not a domain.
    """
    host = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # inet_aton accepts "127.1", "0x7f.0.0.1", "2130706433", "0177.0.0.1".
    if any(ch.isdigit() for ch in host) and all(
            ch.isdigit() or ch in "abcdefABCDEFxX." for ch in host):
        try:
            packed = socket.inet_aton(host)
            return ipaddress.IPv4Address(packed)
        except OSError:
            raise DestinationBlocked(f"unparseable numeric host: {host!r}") from None
    return None


def _check_hostname(host: str) -> None:
    if not host:
        raise InvalidURL("URL has no hostname")
    if len(host) > 253:
        raise InvalidURL("hostname is too long")
    low = host.lower().rstrip(".")
    if low in _BLOCKED_HOSTNAMES or any(low.endswith(s) for s in _BLOCKED_SUFFIXES):
        raise DestinationBlocked(f"hostname is not a public destination: {host}")


def parse_target(url: str, allow_private: bool = False) -> Target:
    """Validate *syntax and destination class* without opening a socket.

    ``allow_private`` is for the test-suite only (loopback fixtures).
    """
    if not isinstance(url, str) or not url:
        raise InvalidURL("empty URL")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURL("URL is unreasonably long")
    if any(ord(ch) < 0x20 for ch in url):
        raise InvalidURL("URL contains control characters")
    if "\\" in url.split("?", 1)[0]:
        raise InvalidURL("URL contains a backslash in the host part")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidURL(f"malformed URL: {exc}") from None

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURL(f"scheme {scheme or '(none)'!r} is not allowed")

    host = parts.hostname or ""
    _check_hostname(host)

    try:
        port = parts.port
    except ValueError:
        raise InvalidURL("invalid port") from None
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise InvalidURL("port out of range")

    literal = parse_ip_literal(host)
    if literal is not None and not allow_private and not ip_is_public(literal):
        raise DestinationBlocked(f"destination IP is not public: {host}")

    # IDNA for the wire (Unicode hostnames are legitimate); keep the original
    # for logs and the Host header.
    wire_host = host.rstrip(".")
    try:
        wire_host.encode("ascii")
    except UnicodeEncodeError:
        try:
            wire_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidURL("hostname cannot be encoded for DNS") from None

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(url=url, scheme=scheme, host=wire_host, display_host=host,
                  port=port, path=path)


def resolve_public(target: Target) -> list[str]:
    """Resolve ``target`` once; every answer must be public (fail closed)."""
    literal = parse_ip_literal(target.host)
    if literal is not None:
        if not ip_is_public(literal):
            raise DestinationBlocked(f"destination IP is not public: {target.host}")
        return [str(literal)]

    try:
        infos = socket.getaddrinfo(target.host, target.port,
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DestinationBlocked(f"cannot resolve {target.display_host}: {exc}") from None
    if not infos:
        raise DestinationBlocked(f"{target.display_host} resolved to no addresses")

    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise DestinationBlocked(f"unparseable DNS answer: {addr}") from None
        if not ip_is_public(ip):
            # A single private answer poisons the whole name: an attacker can
            # otherwise win the race by returning one public and one private
            # record and hoping the connection picks the private one.
            raise DestinationBlocked(
                f"{target.display_host} resolves to a non-public address ({addr})")
        if addr not in addresses:
            addresses.append(addr)
    return addresses


# ── pinned connections ────────────────────────────────────────
class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):  # noqa: D401 - stdlib hook
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port),
                                        self.timeout)
        # SNI and certificate validation use the *hostname*, not the IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def default_ssl_context() -> ssl.SSLContext:
    """Verifying TLS context (certificate + hostname checks are on)."""
    ctx = ssl.create_default_context()
    try:
        import config
        insecure = bool(getattr(config, "TLS_INSECURE", False))
    except Exception:
        insecure = os.environ.get("RAYAN_TLS_INSECURE", "").lower() in ("1", "true", "yes")
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── the one public entry point ────────────────────────────────
def fetch(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
          timeout: float = DEFAULT_TIMEOUT,
          total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
          max_redirects: int = DEFAULT_MAX_REDIRECTS,
          headers: dict | None = None,
          method: str = "GET",
          data: bytes | None = None,
          raise_for_status: bool = True,
          allow_private: bool = False,
          ssl_context: ssl.SSLContext | None = None,
          sink=None) -> Response:
    """Fetch ``url`` safely.

    ``allow_private`` exists ONLY so the test-suite can talk to a loopback
    HTTP server; production call sites must never pass it.  When ``sink`` is
    given, body chunks are handed to it instead of being accumulated, so a
    large file never lands in RAM.
    """
    started = time.monotonic()
    deadline = started + total_timeout
    current = url
    hops = 0
    request_method = method.upper()
    request_data = data
    extra_headers = dict(headers or {})

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded(f"deadline exceeded fetching {url}")
        if hops > max_redirects:
            raise TooManyRedirects(f"more than {max_redirects} redirects from {url}")

        target = parse_target(current, allow_private=allow_private)
        if allow_private:
            literal = parse_ip_literal(target.host)
            if literal is not None:
                addresses = [str(literal)]
            else:
                infos = socket.getaddrinfo(target.host, target.port,
                                           type=socket.SOCK_STREAM)
                addresses = [i[4][0] for i in infos] or []
                if not addresses:
                    raise DestinationBlocked(f"cannot resolve {target.display_host}")
        else:
            addresses = resolve_public(target)

        conn = None
        last_error: Exception | None = None
        for address in addresses:
            sock_timeout = max(0.5, min(timeout, deadline - time.monotonic()))
            try:
                if target.scheme == "https":
                    conn = _PinnedHTTPSConnection(
                        target.host, target.port, address, sock_timeout,
                        ssl_context or default_ssl_context())
                else:
                    conn = _PinnedHTTPConnection(
                        target.host, target.port, address, sock_timeout)
                conn.request(request_method, target.path,
                             body=request_data,
                             headers={"Host": target.host_header,
                                      "User-Agent": USER_AGENT,
                                      "Accept": "*/*",
                                      **extra_headers})
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                if conn is not None:
                    conn.close()
                conn = None
        if conn is None:
            raise NetGuardError(
                f"cannot connect to {target.display_host}:{target.port}: {last_error}")

        try:
            remaining = max(0.5, deadline - time.monotonic())
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            resp = conn.getresponse()

            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}

            if status in (301, 302, 303, 307, 308):
                location = resp_headers.get("location")
                if not location:
                    body = _read_capped(resp, max_bytes)
                    return Response(current, status, resp_headers, body)
                # Drain nothing: closing is cheaper and safe for a redirect.
                next_url = urljoin(current, location)
                hops += 1
                if status == 303 or (status in (301, 302) and request_method == "POST"):
                    request_method, request_data = "GET", None
                    extra_headers.pop("Content-Type", None)
                current = next_url
                continue

            body = _read_capped(resp, max_bytes, deadline, sink)
            response = Response(current, status, resp_headers, body)
            if raise_for_status and status >= 400:
                raise HTTPStatusError(current, status, resp.reason or "")
            return response
        except (ResponseTooLarge, DeadlineExceeded, TooManyRedirects):
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise DeadlineExceeded(f"timed out fetching {current}") from exc
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            # Never leak raw socket exceptions to callers: every network
            # failure of this module is a NetGuardError subclass.
            raise NetGuardError(f"connection error fetching {current}: {exc}") from exc
        finally:
            conn.close()


def _read_capped(resp: http.client.HTTPResponse, max_bytes: int,
                 deadline: float | None = None, sink=None) -> bytes:
    declared = resp.getheader("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(f"content-length {declared} exceeds {max_bytes} bytes")

    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise DeadlineExceeded("deadline exceeded while reading the response")
        chunk = resp.read(min(65536, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"response exceeds {max_bytes} bytes")
        if sink is not None:
            sink(chunk)
        else:
            chunks.append(chunk)
    return b"" if sink is not None else b"".join(chunks)


def fetch_text(url: str, **kwargs) -> str:
    kwargs.setdefault("max_bytes", DEFAULT_MAX_BYTES)
    return fetch(url, **kwargs).text


def fetch_json(url: str, **kwargs):
    import json
    kwargs.setdefault("max_bytes", DEFAULT_MAX_BYTES)
    return json.loads(fetch(url, **kwargs).body.decode("utf-8", "replace"))


class Probe(NamedTuple):
    status: int
    url: str
    headers: dict


def probe(url: str, *, method: str = "GET",
          timeout: float = DEFAULT_TIMEOUT,
          max_redirects: int = DEFAULT_MAX_REDIRECTS,
          headers: dict | None = None,
          allow_private: bool = False) -> Probe:
    """Follow redirects (revalidating every hop) and return status + final URL
    without downloading the body.

    Used where the old code shelled out to ``curl -L -w %{url_effective}`` or
    ``%{http_code}``: those followed redirects to any destination, including
    ``file://`` and loopback, and reported success for them.
    """
    deadline = time.monotonic() + max(timeout, 1.0) * (max_redirects + 1)
    current = url
    hops = 0
    while True:
        if hops > max_redirects:
            raise TooManyRedirects(f"more than {max_redirects} redirects from {url}")
        if time.monotonic() > deadline:
            raise DeadlineExceeded(f"deadline exceeded probing {url}")
        target = parse_target(current, allow_private=allow_private)
        addresses = ([str(parse_ip_literal(target.host))] if allow_private
                     and parse_ip_literal(target.host) is not None
                     else resolve_public(target) if not allow_private
                     else [i[4][0] for i in socket.getaddrinfo(
                         target.host, target.port, type=socket.SOCK_STREAM)])
        last_error = None
        conn = None
        for address in addresses:
            try:
                if target.scheme == "https":
                    conn = _PinnedHTTPSConnection(
                        target.host, target.port, address, timeout,
                        default_ssl_context())
                else:
                    conn = _PinnedHTTPConnection(
                        target.host, target.port, address, timeout)
                conn.request(method.upper(), target.path,
                             headers={"Host": target.host_header,
                                      "User-Agent": USER_AGENT,
                                      "Accept": "*/*",
                                      **dict(headers or {})})
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                if conn is not None:
                    conn.close()
                conn = None
        if conn is None:
            raise NetGuardError(
                f"cannot connect to {target.display_host}:{target.port}: {last_error}")
        try:
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            try:
                resp.read(1)  # some servers only send headers after a read
            except (OSError, http.client.HTTPException):
                pass
            if status in (301, 302, 303, 307, 308) and resp_headers.get("location"):
                current = urljoin(current, resp_headers["location"])
                hops += 1
                continue
            return Probe(status, current, resp_headers)
        finally:
            conn.close()


def fetch_to_file(url: str, path: str, *, max_bytes: int,
                  timeout: float = DEFAULT_TIMEOUT,
                  total_timeout: float = 300.0,
                  max_redirects: int = DEFAULT_MAX_REDIRECTS,
                  headers: dict | None = None,
                  allow_private: bool = False) -> int:
    """Stream ``url`` into ``path`` with a hard byte cap. Returns bytes written.

    The body is never held in memory as a whole, so a multi-hundred-MB media
    file costs one 64 KiB buffer.
    """
    written = 0
    try:
        with open(path, "wb") as fh:
            def _sink(chunk: bytes) -> None:
                nonlocal written
                fh.write(chunk)
                written += len(chunk)

            fetch(url, max_bytes=max_bytes, timeout=timeout,
                  total_timeout=total_timeout, max_redirects=max_redirects,
                  headers=headers, allow_private=allow_private, sink=_sink)
    except BaseException:
        # Never leave a partial file behind for the caller to mistake for a
        # completed download (or for disk cleanup to count as real data).
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return written


def is_public_url(url: str) -> bool:
    """Best-effort pre-check for UI/validation (no socket is opened)."""
    try:
        parse_target(url)
        return True
    except NetGuardError:
        return False
