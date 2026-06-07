"""
inferno_local.security — egress guard for the air-gapped runtime.

The only network calls Data's Inferno is allowed to make go to a service
running on this same machine. ``assert_loopback(target)`` is the single
chokepoint every networked code path goes through; anything that fails
its check raises ``EgressBlocked`` and is logged so an auditor can grep
the trail.

Public surface:

    EgressBlocked                  exception raised on any non-loopback target
    is_loopback_host(host)         True iff host resolves to 127/8 or ::1
    is_loopback_url(url)           True iff url's host is loopback
    assert_loopback(target)        raises EgressBlocked if not loopback
    install_socket_guard()         opt-in process-wide socket.connect guard
    uninstall_socket_guard()       remove the guard (test fixtures call this)

Design choices forced by §0 of the Odysseus brief:

  * No DNS resolution shortcuts — we accept a hostname only if EVERY
    address it resolves to is in the loopback set. A name that maps to
    both 127.0.0.1 AND a public IP is rejected (split-horizon attacks).

  * Hostnames are normalised through ``socket.getaddrinfo`` and then each
    returned address is checked with ``ipaddress.ip_address.is_loopback``,
    not by string match — covers IPv6, decimal-128.0.0.1 tricks, etc.

  * No URL-fetch convenience wrappers in this module. Callers explicitly
    ask "is this OK?" then call requests / urllib themselves. That keeps
    the security boundary tiny and reviewable.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from typing import Iterable, List, Optional, Union

_LOG = logging.getLogger("inferno_local.security")


class EgressBlocked(Exception):
    """Raised when a network operation targets something that isn't
    loopback. Always surface this to the user — silent swallow defeats
    the whole point of the guard."""

    def __init__(self, target: str, reason: str = "") -> None:
        msg = f"egress blocked: {target!r}"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg)
        self.target = target
        self.reason = reason


# ── Allow-listed literals (always loopback) ────────────────────────
_LOOPBACK_HOST_LITERALS = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})


def _resolve(host: str) -> List[ipaddress._BaseAddress]:
    """Return every IP address `host` resolves to. May raise socket.gaierror.
    We deliberately do not cache — a freshly-edited /etc/hosts must take
    effect immediately for the audit trail to mean anything."""
    out: List[ipaddress._BaseAddress] = []
    try:
        infos = socket.getaddrinfo(host, None,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise socket.gaierror(f"could not resolve {host!r}: {exc}") from exc
    for fam, _kind, _proto, _canon, sa in infos:
        ip_str = sa[0]
        try:
            out.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return out


def is_loopback_host(host: str) -> bool:
    """Return True iff `host` is loopback (every resolved address is).

    A bare IP literal is checked directly; a hostname has to resolve and
    every returned address must be loopback. This is intentionally
    stricter than "any answer is loopback" — split-horizon DNS that
    returns both 127.0.0.1 and a public IP must not pass.
    """
    if not host:
        return False
    h = host.strip().lower()
    # Strip surrounding brackets for IPv6 literal forms like [::1]
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _LOOPBACK_HOST_LITERALS:
        return True
    # Try parsing as a literal address first — avoids a needless
    # getaddrinfo round-trip and dodges DNS poisoning.
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    try:
        ips = _resolve(h)
    except socket.gaierror:
        return False
    return bool(ips) and all(ip.is_loopback for ip in ips)


def is_loopback_url(url: str) -> bool:
    """Same as is_loopback_host but accepts a URL — we extract the host."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = parsed.hostname or ""
    return is_loopback_host(host)


def assert_loopback(target: str) -> None:
    """Raise ``EgressBlocked`` if ``target`` is not loopback. Accepts
    either a bare host (``localhost``, ``127.0.0.1``, ``::1``) or a full
    URL (``http://localhost:11434/api/chat``).

    This is the only function callers should use day-to-day. The
    ``is_loopback_*`` helpers exist for diagnostics and tests.
    """
    if not target:
        raise EgressBlocked(target, "empty target")
    if "://" in target:
        ok = is_loopback_url(target)
        if not ok:
            try:
                host = urllib.parse.urlparse(target).hostname or "?"
            except Exception:
                host = "?"
            _LOG.warning("egress blocked: target=%r host=%r", target, host)
            raise EgressBlocked(target,
                                f"host {host!r} is not loopback")
        return
    if not is_loopback_host(target):
        _LOG.warning("egress blocked: host=%r", target)
        raise EgressBlocked(target, "not loopback")


# ────────────────────────────────────────────────────────────────────
# Optional process-wide socket guard
#
# Wraps socket.socket.connect so any non-loopback connection attempt
# raises EgressBlocked instead of leaving the machine. Off by default
# because some Python internals (DNS, certificate fetches via Python's
# own ssl module) talk to public IPs. The wizard / launcher can
# install this for ops that should be strictly local (model runner
# calls, the constrained agent's tool loop, the deep-research loop).
# ────────────────────────────────────────────────────────────────────

_orig_socket_connect = None


def _guarded_connect(self, address):
    # IPv4 addresses are (host, port), IPv6 are (host, port, flow, scope)
    if isinstance(address, tuple) and address:
        host = str(address[0])
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Hostname — let getaddrinfo + assert_loopback handle it
            if not is_loopback_host(host):
                raise EgressBlocked(host, "socket.connect blocked by guard")
        else:
            if not ip.is_loopback:
                raise EgressBlocked(host, "socket.connect blocked by guard")
    return _orig_socket_connect(self, address)


def install_socket_guard() -> None:
    """Globally replace ``socket.socket.connect`` so any non-loopback
    target raises ``EgressBlocked``. Idempotent — calling twice has no
    additional effect. Use ``uninstall_socket_guard`` to undo (tests do)."""
    global _orig_socket_connect
    if _orig_socket_connect is not None:
        return
    _orig_socket_connect = socket.socket.connect
    socket.socket.connect = _guarded_connect      # type: ignore[assignment]


def uninstall_socket_guard() -> None:
    """Undo ``install_socket_guard``. No-op if not installed."""
    global _orig_socket_connect
    if _orig_socket_connect is None:
        return
    socket.socket.connect = _orig_socket_connect  # type: ignore[assignment]
    _orig_socket_connect = None
