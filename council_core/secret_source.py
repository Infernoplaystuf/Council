"""
council_core.secret_source — read a token from where the user keeps it,
never from a Council settings file.

WHY
An MCP server that wants a token (Dyndrite's, say) gets it from the CLIENT —
Council — as an HTTP header on every request, or as an environment variable
of the server process it starts. The model never sees it: a model only writes
text, and Council decides what goes over the wire. So the token has to be
somewhere Council can read at connect time and nowhere a model, a log or a
settings file the user might share can show it.

A SOURCE is a short string saved in place of the token itself:

    env:DYNDRITE_TOKEN            an environment variable
    credential:council/dyndrite   a Windows Credential Manager entry
                                  (generic credential; its password is the
                                  token). Make one with:
                                  cmdkey /generic:council/dyndrite /user:token /pass:<token>

`read(source)` returns the token or raises SecretMissing with a sentence
that says where Council looked — never the value. `fill(text)` replaces
``${NAME}`` in a header value with the environment variable NAME, so a saved
header can be ``Bearer ${DYNDRITE_TOKEN}``.

`looks_secret(name, value)` is the check that keeps a pasted token out of a
saved file: a header such as Authorization or X-Api-Key whose value holds no
``${...}`` reference is refused by the callers.

Stdlib only; the Credential Manager is reached through ctypes (advapi32
CredReadW), so nothing is installed for it.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

#: Header names that carry credentials.
SECRET_HEADERS = frozenset({"authorization", "proxy-authorization",
                            "x-api-key", "api-key", "apikey", "x-auth-token",
                            "x-access-token", "token", "cookie"})

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SOURCE = re.compile(r"^(env|credential):(.+)$")

#: Tests replace this to stand in for the Credential Manager.
credential_reader: Optional[Callable[[str], Optional[str]]] = None


class SecretMissing(LookupError):
    """The token is not where its source says."""


def valid_source(source: str) -> str:
    """Why `source` is not a token source, or ""."""
    source = (source or "").strip()
    if not source:
        return ""
    m = _SOURCE.match(source)
    if not m:
        return ("A token source is env:NAME (an environment variable) or "
                "credential:NAME (a Windows Credential Manager entry).")
    kind, name = m.group(1), m.group(2).strip()
    if kind == "env" and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return f"{name!r} is not an environment variable name."
    if not name:
        return "The token source names nothing."
    return ""


def describe(source: str) -> str:
    """Where the token comes from, in words (never the token)."""
    m = _SOURCE.match((source or "").strip())
    if not m:
        return "no token"
    if m.group(1) == "env":
        return f"environment variable {m.group(2).strip()}"
    return f"Windows Credential Manager entry {m.group(2).strip()!r}"


def _windows_credential(target: str) -> Optional[str]:
    if credential_reader is not None:
        return credential_reader(target)
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pcred)):   # GENERIC
        return None
    try:
        cred = pcred.contents
        raw = bytes(cred.CredentialBlob[:cred.CredentialBlobSize])
    finally:
        advapi.CredFree(pcred)
    # cmdkey stores the password as UTF-16; other tools may store bytes.
    for codec in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        if text and all(ch.isprintable() for ch in text):
            return text
    return raw.decode("latin-1")


def read(source: str) -> str:
    """The token `source` names. SecretMissing when it is not there."""
    problem = valid_source(source)
    if problem or not (source or "").strip():
        raise SecretMissing(problem or "No token source is set.")
    kind, name = _SOURCE.match(source.strip()).groups()
    name = name.strip()
    if kind == "env":
        value = os.environ.get(name, "")
        if not value.strip():
            raise SecretMissing(
                f"The environment variable {name} is not set (or is empty). "
                f"Set it in Windows (System > Environment Variables), then "
                f"restart Council.")
        return value.strip()
    value = _windows_credential(name)
    if not value:
        where = ("" if sys.platform == "win32"
                 else " (the Credential Manager exists only on Windows)")
        raise SecretMissing(
            f"There is no Windows Credential Manager entry {name!r}{where}. "
            f"Make one with: cmdkey /generic:{name} /user:token /pass:<token>")
    return value.strip()


def fill(text: str) -> str:
    """`text` with each ${NAME} replaced by the environment variable NAME.
    SecretMissing names the first one that is not set."""
    def one(m: "re.Match[str]") -> str:
        value = os.environ.get(m.group(1), "")
        if not value:
            raise SecretMissing(f"The environment variable {m.group(1)} is "
                                f"not set (or is empty).")
        return value
    return _REF.sub(one, str(text))


def has_reference(text: str) -> bool:
    return bool(_REF.search(str(text or "")))


def looks_secret(name: str, value: str) -> bool:
    """True when a saved header would hold a credential in plain text."""
    return (str(name or "").strip().lower() in SECRET_HEADERS
            and bool(str(value or "").strip())
            and not has_reference(value))


def mask(text: str, secret: str) -> str:
    """`text` with `secret` blotted out (for error messages and logs)."""
    if secret and len(secret) >= 4:
        return str(text).replace(secret, "••••")
    return str(text)


__all__ = ["SecretMissing", "SECRET_HEADERS", "valid_source", "describe",
           "read", "fill", "has_reference", "looks_secret", "mask"]
