"""Tests for inferno_local.security — the egress guard."""
from __future__ import annotations

import socket
import unittest
from unittest import mock

from inferno_local import security


class TestIsLoopbackHost(unittest.TestCase):

    def test_ipv4_loopback_literal(self):
        self.assertTrue(security.is_loopback_host("127.0.0.1"))
        self.assertTrue(security.is_loopback_host("127.10.20.30"))   # all of 127/8
        self.assertTrue(security.is_loopback_host("127.255.255.255"))

    def test_ipv6_loopback_literal(self):
        self.assertTrue(security.is_loopback_host("::1"))
        # Bracketed form (URLs)
        self.assertTrue(security.is_loopback_host("[::1]"))

    def test_localhost_literal(self):
        self.assertTrue(security.is_loopback_host("localhost"))

    def test_rejects_public_ipv4(self):
        self.assertFalse(security.is_loopback_host("8.8.8.8"))
        self.assertFalse(security.is_loopback_host("1.1.1.1"))

    def test_rejects_link_local(self):
        # 169.254.x.x — link-local is NOT loopback.
        self.assertFalse(security.is_loopback_host("169.254.0.1"))

    def test_rejects_private_ranges(self):
        # Private RFC1918 != loopback. Even your office subnet is egress.
        self.assertFalse(security.is_loopback_host("10.0.0.1"))
        self.assertFalse(security.is_loopback_host("192.168.1.1"))
        self.assertFalse(security.is_loopback_host("172.16.0.1"))

    def test_empty_or_missing(self):
        self.assertFalse(security.is_loopback_host(""))
        self.assertFalse(security.is_loopback_host("   "))


class TestIsLoopbackUrl(unittest.TestCase):

    def test_basic(self):
        self.assertTrue(security.is_loopback_url("http://127.0.0.1:11434/api/chat"))
        self.assertTrue(security.is_loopback_url("http://localhost/"))
        self.assertTrue(security.is_loopback_url("http://[::1]:8080/foo"))

    def test_rejects_public(self):
        self.assertFalse(security.is_loopback_url("https://api.openai.com/v1/chat"))
        self.assertFalse(security.is_loopback_url("http://example.com/"))


class TestAssertLoopback(unittest.TestCase):

    def test_passes_loopback(self):
        security.assert_loopback("127.0.0.1")
        security.assert_loopback("localhost")
        security.assert_loopback("http://127.0.0.1:11434/api/chat")
        security.assert_loopback("http://localhost:8080/")
        security.assert_loopback("::1")

    def test_blocks_public_host(self):
        with self.assertRaises(security.EgressBlocked):
            security.assert_loopback("example.com")
        with self.assertRaises(security.EgressBlocked):
            security.assert_loopback("8.8.8.8")

    def test_blocks_public_url(self):
        with self.assertRaises(security.EgressBlocked):
            security.assert_loopback("https://api.anthropic.com/v1/messages")
        with self.assertRaises(security.EgressBlocked):
            security.assert_loopback("http://192.168.1.1/")

    def test_blocks_empty(self):
        with self.assertRaises(security.EgressBlocked):
            security.assert_loopback("")


class TestSplitHorizonHostname(unittest.TestCase):
    """A hostname that resolves to BOTH loopback and a public IP must be
    rejected. This guards against /etc/hosts split-horizon tricks."""

    def test_split_horizon_resolution_rejected(self):
        # Mock getaddrinfo to return one loopback + one public address.
        fake_results = [
            (socket.AF_INET, 1, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET, 1, 6, "", ("8.8.8.8", 0)),
        ]
        with mock.patch.object(socket, "getaddrinfo",
                                return_value=fake_results):
            self.assertFalse(security.is_loopback_host("sneaky.example"))
            with self.assertRaises(security.EgressBlocked):
                security.assert_loopback("sneaky.example")


class TestSocketGuard(unittest.TestCase):

    def tearDown(self):
        security.uninstall_socket_guard()

    def test_guard_blocks_public_ip(self):
        security.install_socket_guard()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(security.EgressBlocked):
                s.connect(("8.8.8.8", 53))
        finally:
            s.close()

    def test_guard_permits_loopback(self):
        # We don't actually try to connect (no service listening). But the
        # connection should fail with a *connection* error, not EgressBlocked.
        security.install_socket_guard()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.05)
        try:
            try:
                s.connect(("127.0.0.1", 1))   # unlikely to be in use
            except OSError as exc:
                # Connection refused / timeout are fine. EgressBlocked is NOT.
                self.assertNotIsInstance(exc, security.EgressBlocked)
            except security.EgressBlocked:
                self.fail("loopback connection blocked by guard")
        finally:
            s.close()

    def test_guard_is_idempotent(self):
        security.install_socket_guard()
        security.install_socket_guard()
        security.uninstall_socket_guard()
        # And calling uninstall twice is fine.
        security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
