"""
inferno_local.security — egress guard for the air-gapped runtime.

The only network calls Data's Inferno is allowed to make go to a service
running on this same machine. ``assert_loopback(target)`` is the single
chokepoint every networked code path goes through; anything that fails
its check raises ``EgressBlocked`` and is logged so an auditor can grep
the trail.

Public surface:

    EgressBlocked                  exception raised on any non-loopback target
    is_loopback_host(host)         True iff host is "localhost" or a
                                   loopback IP literal (127/8, ::1)
    is_loopback_url(url)           True iff url's host is loopback
    assert_loopback(target)        raises EgressBlocked if not loopback
    install_socket_guard()         opt-in process-wide socket.connect guard
    uninstall_socket_guard()       remove the guard (test fixtures call this)

Design choices forced by §0 of the Odysseus brief:

  * No DNS at all. This PC is "localhost" or a loopback IP LITERAL,
    checked with ``ipaddress.ip_address.is_loopback`` (IPv6 too); any
    other name is refused without being looked up — the rule
    council_core.local_models.is_loopback_url and mcp_client already
    follow. Resolving a name and accepting it when every answer was
    loopback (the old rule) had two holes, both measured 2026-10-05:
    the answer the check saw was not the one urllib connected with — it
    resolves the name again, so a DNS answer that changed in between
    (rebinding) took the prompt elsewhere; and "ip6-localhost" /
    "ip6-loopback" were trusted WITHOUT a lookup, though they are Linux
    /etc/hosts entries Windows does not know — this PC's LAN DNS
    answered both with the router, 192.168.1.1.

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


# ── The one name that is always this PC ────────────────────────────
# "localhost" only. "ip6-localhost" and "ip6-loopback" used to be here too;
# they are /etc/hosts conventions on Linux, and on Windows the name goes to
# the network's DNS (this PC's answered with its router).
_LOOPBACK_HOST_LITERALS = frozenset({"localhost"})


def is_loopback_host(host: str) -> bool:
    """Return True iff `host` is this PC: "localhost" or a loopback IP
    literal (127/8, ::1, with or without brackets).

    Any other NAME is refused WITHOUT a lookup, even one that resolves to
    127.0.0.1 now: the caller's own connection resolves it again, and that
    second answer is the one that counts (DNS rebinding). Split-horizon
    names, hosts-file aliases and "decimal" spellings like 2130706433 are
    refused for the same reason — a URL that means this PC can say so.
    """
    if not host:
        return False
    h = host.strip().lower()
    # Strip surrounding brackets for IPv6 literal forms like [::1]
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _LOOPBACK_HOST_LITERALS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


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
            # A name: only "localhost" passes, and nothing is looked up
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
