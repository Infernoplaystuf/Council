"""council_core/pi_setup/pi_secrets.py — the password hash, Wi-Fi key and
Council key a new Pi gets. References: the SHA-crypt spec's own test vectors
and `openssl passwd -6` (OpenSSL 3.5.5, run on this PC 2026-10-06)."""
from __future__ import annotations

import os
import stat

import pytest

from council_core.pi_setup import pi_secrets as ps


@pytest.mark.parametrize("password, salt, rounds, expected", [
    # Drepper, "Unix crypt using SHA-256 and SHA-512", test vectors.
    ("Hello world!", "saltstring", None,
     "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4"
     "OTLiBFdcbYEdFCoEOfaS35inz1"),
    ("Hello world!", "saltstringsaltstring", 10000,
     "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNS"
     "nCM/UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v."),
    # openssl passwd -6 -salt Ab1Cd2Ef3 ...
    ("Hello world!", "Ab1Cd2Ef3", None,
     "$6$Ab1Cd2Ef3$u.5mTK8mU/FkwjjwDoKtFuVKJiJzGbzkkMwXrX0RE8HxDONbZnMWORZYvTK/Mo"
     "CjPuqjcVnwQ0w.BEszdTZxB1"),
    ("p@ss w0rd", "Ab1Cd2Ef3", None,
     "$6$Ab1Cd2Ef3$QNhLT5RAvaq.i3S0g6eaBwLvzjGhcY07uG2PYwqSGdPl3zWKrBrL2LDJSHeTqX"
     "Z2FfKsBqAmPuOsyzY9zpVER."),
    # Non-ASCII: the Pi's locale is UTF-8, so the UTF-8 bytes are hashed.
    # (printf 'Tom\xc3\xa1s-\xc3\xa9' | openssl passwd -6 -salt Ab1Cd2Ef3 -stdin;
    # passing it as a Windows argument gave openssl cp1252 bytes instead.)
    ("Tomás-é", "Ab1Cd2Ef3", None,
     "$6$Ab1Cd2Ef3$AIi8ZA2jw.3kQPKVbMbbig5UQRvfJ1GxmDUi3Yc9kg.dsw/KMRT53U2NMlSGev"
     "Qw9/k1/SLYTaXTNbOFaYex7."),
])
def test_sha512_crypt_matches_references(password, salt, rounds, expected):
    assert ps.sha512_crypt(password, salt, rounds=rounds) == expected
    assert ps.check_sha512_crypt(password, expected)
    assert not ps.check_sha512_crypt(password + "x", expected)


def test_random_salt_is_used_and_checks():
    a, b = ps.sha512_crypt("same"), ps.sha512_crypt("same")
    assert a != b and a.startswith("$6$")
    assert ps.check_sha512_crypt("same", a) and ps.check_sha512_crypt("same", b)


def test_wifi_psk_matches_wpa_passphrase():
    # IEEE 802.11i Annex H.4 test vector.
    assert ps.wifi_psk("IEEE", "password") == (
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")


@pytest.mark.parametrize("ssid, pw", [("home", "short"), ("home", "x" * 64),
                                      ("home", "pässwort1"), ("", "password1"),
                                      ("x" * 33, "password1")])
def test_wifi_psk_refuses_what_a_pi_cannot_join(ssid, pw):
    with pytest.raises(ValueError):
        ps.wifi_psk(ssid, pw)


@pytest.mark.parametrize("ssid", ["a\nCOUNCILWIFI\ntouch /PWNED\nZ", "home\r", "tab\there",
                                  "nul\x00", "del\x7f"])
def test_an_ssid_with_a_control_character_is_refused(ssid):
    # 28 bytes passed the length check, and in the legacy firstrun.sh the
    # newline closed the quoted heredoc: 'touch /PWNED' ran as root.
    from council_core.pi_setup import firstboot as fb
    with pytest.raises(ValueError, match="control character"):
        ps.wifi_psk(ssid, "password123")
    cfg = fb.FirstBoot(hostname="council-pi-1", username="council",
                       password="correct-horse-42", wifi_ssid=ssid,
                       wifi_password="password123")
    assert any("control character" in p for p in cfg.problems())
    with pytest.raises(ValueError):
        fb.build(cfg, fb.SYSTEMD)


def test_council_key_is_made_once(tmp_path):
    priv, pub = ps.council_key(tmp_path)
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" council")
    assert priv.read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    before = priv.read_bytes()
    priv2, pub2 = ps.council_key(tmp_path)
    assert (priv2, pub2) == (priv, pub) and priv.read_bytes() == before
    if os.name != "nt":
        assert stat.S_IMODE(priv.stat().st_mode) == 0o600


def test_council_key_loads_in_paramiko(tmp_path):
    paramiko = pytest.importorskip("paramiko")
    priv, pub = ps.council_key(tmp_path)
    key = paramiko.Ed25519Key.from_private_key_file(str(priv))
    assert pub.split()[1] == key.get_base64()


def test_key_dir_is_not_the_vault(monkeypatch, tmp_path):
    monkeypatch.delenv("COUNCIL_KEY_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ps.app_key_dir() == tmp_path / "Council" / "keys"
