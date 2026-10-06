"""The secrets a new Pi needs, made so that none of them is stored in plain text.

* ``sha512_crypt`` — the ``$6$`` password hash Raspberry Pi OS keeps in
  /etc/shadow (and reads from userconf.txt / cloud-init). Python's ``crypt``
  module does not exist on Windows (and is gone from 3.13), so this is the
  published SHA-crypt algorithm (Drepper, "Unix crypt using SHA-256 and
  SHA-512"), checked against the spec's test vectors and against
  ``openssl passwd -6``. Only the HASH goes on the card; the password itself
  is used once and dropped.
* ``wifi_psk`` — the 64-hex WPA key derived from the passphrase and SSID
  (PBKDF2-HMAC-SHA1, 4096 rounds — what wpa_passphrase prints). Written in
  place of the passphrase, so the card does not carry the Wi-Fi password.
* ``council_key`` — one Ed25519 key pair for the Council, in OpenSSH format,
  kept in the app folder (never the vault) and created once. Every Pi the
  Council sets up trusts its public half, so after setup no password is
  needed or kept.
"""
from __future__ import annotations

import hashlib
import os
import secrets as _secrets
from pathlib import Path
from typing import Optional, Tuple

_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ORDER = ((0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
          (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
          (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
          (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
          (62, 20, 41))
ROUNDS_DEFAULT = 5000


def _b64_24(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_B64[w & 0x3F])
        w >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return (digest * (length // len(digest) + 1))[:length]


def sha512_crypt(password: str, salt: Optional[str] = None, *,
                 rounds: Optional[int] = None) -> str:
    """``$6$[rounds=N$]salt$hash``. A random 16-character salt when none is
    given. ``rounds`` (1000..999999999) is written only when given, as the
    spec requires."""
    if salt is None:
        salt = "".join(_secrets.choice(_B64) for _ in range(16))
    salt = salt[:16]
    if any(c in salt for c in "$:\n"):
        raise ValueError("salt may not contain '$', ':' or a newline")
    n = ROUNDS_DEFAULT if rounds is None else max(1000, min(int(rounds), 999_999_999))
    p, s = password.encode("utf-8"), salt.encode("utf-8")

    b = hashlib.sha512(p + s + p).digest()
    a = hashlib.sha512(p + s)
    cnt = len(p)
    while cnt > 64:
        a.update(b)
        cnt -= 64
    a.update(b[:cnt])
    i = len(p)
    while i:
        a.update(b if i & 1 else p)
        i >>= 1
    a_d = a.digest()
    p_seq = _repeat(hashlib.sha512(p * len(p)).digest(), len(p))
    s_seq = _repeat(hashlib.sha512(s * (16 + a_d[0])).digest(), len(s))

    c = a_d
    for r in range(n):
        h = hashlib.sha512(p_seq if r & 1 else c)
        if r % 3:
            h.update(s_seq)
        if r % 7:
            h.update(p_seq)
        h.update(c if r & 1 else p_seq)
        c = h.digest()

    enc = "".join(_b64_24(c[x], c[y], c[z], 4) for x, y, z in _ORDER)
    enc += _b64_24(0, 0, c[63], 2)
    head = "$6$" + (f"rounds={n}$" if rounds is not None else "")
    return f"{head}{salt}${enc}"


def check_sha512_crypt(password: str, hashed: str) -> bool:
    """True when ``password`` produces ``hashed``."""
    parts = hashed.split("$")
    if len(parts) < 4 or parts[1] != "6":
        return False
    rounds = None
    if parts[2].startswith("rounds="):
        rounds = int(parts[2][7:])
        salt = parts[3]
    else:
        salt = parts[2]
    return _secrets.compare_digest(sha512_crypt(password, salt, rounds=rounds), hashed)


def wifi_psk(ssid: str, passphrase: str) -> str:
    """The 64-hex WPA2-Personal key (what ``wpa_passphrase`` prints). The
    passphrase must be 8-63 printable ASCII characters (the WPA rule — a
    longer or non-ASCII one is a common reason a Pi never joins)."""
    if not 8 <= len(passphrase) <= 63:
        raise ValueError("a Wi-Fi password must be 8 to 63 characters")
    if any(not (32 <= ord(ch) <= 126) for ch in passphrase):
        raise ValueError("a Wi-Fi password may only use printable ASCII characters")
    if not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise ValueError("a network name (SSID) is 1 to 32 bytes")
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode("ascii"),
                               ssid.encode("utf-8"), 4096, 32).hex()


def app_key_dir() -> Path:
    """%LOCALAPPDATA%/Council/keys (or ~/.council_keys elsewhere) — the
    app folder, NOT the vault: a vault is searched, synced and shared."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "Council" if base else Path.home() / ".council_app"
    override = os.environ.get("COUNCIL_KEY_DIR")
    return Path(override) if override else root / "keys"


def council_key(key_dir: Optional[Path] = None) -> Tuple[Path, str]:
    """(private key path, public key line) — created on first use. The
    private key is OpenSSH-format Ed25519, written owner-only where the OS
    supports it, never overwritten."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    d = Path(key_dir) if key_dir else app_key_dir()
    d.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = d / "council_ed25519", d / "council_ed25519.pub"
    if not priv_path.exists():
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                            serialization.PublicFormat.OpenSSH)
        tmp = priv_path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        os.replace(tmp, priv_path)
        pub_path.write_text(pub.decode("ascii") + " council\n", encoding="ascii")
    return priv_path, pub_path.read_text(encoding="ascii").strip()
