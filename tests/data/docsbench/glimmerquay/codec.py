"""
glimmerquay.codec — frames for the wire.

A frame is: the two magic bytes b"GQ", the payload length as two big-endian
bytes, the payload, zero padding up to a multiple of `pad_to`, a two-byte
checksum of everything before it, and one tag byte naming the checksum
(b"f" for fletcher16, b"x" for xor8).
"""
from __future__ import annotations

#: The checksum algorithms encode_frame accepts, by name.
CHECKSUMS = ("fletcher16", "xor8")

MAGIC = b"GQ"


class FrameError(ValueError):
    """Raised by decode_frame when a frame is malformed: wrong magic bytes,
    a length that does not fit, or a checksum that does not match."""


def _fletcher16(data: bytes) -> int:
    a = b = 0
    for byte in data:
        a = (a + byte) % 255
        b = (b + a) % 255
    return (b << 8) | a


def _xor8(data: bytes) -> int:
    x = 0
    for byte in data:
        x ^= byte
    return x


def _checksum(name: str, data: bytes) -> bytes:
    if name == "fletcher16":
        return _fletcher16(data).to_bytes(2, "big")
    if name == "xor8":
        return _xor8(data).to_bytes(2, "big")
    raise ValueError(f"unknown checksum {name!r}; choose one of {CHECKSUMS}")


def encode_frame(payload: bytes, *, checksum: str = "fletcher16",
                 pad_to: int = 8) -> bytes:
    """Wrap `payload` in a glimmerquay frame and return the frame bytes.

    Every frame starts with the two magic bytes b"GQ". The default checksum is
    "fletcher16"; "xor8" is the only other choice (see CHECKSUMS). The payload
    is zero-padded up to a multiple of `pad_to` bytes (default 8) before the
    checksum is appended. Both options are keyword-only.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    if pad_to < 1:
        raise ValueError("pad_to must be at least 1")
    body = MAGIC + len(payload).to_bytes(2, "big") + bytes(payload)
    pad = (-len(payload)) % pad_to
    body += b"\x00" * pad
    return body + _checksum(checksum, body) + checksum[:1].encode("ascii")


def decode_frame(frame: bytes, *, verify: bool = True) -> bytes:
    """Return the payload carried by a frame made by encode_frame.

    With verify=True (the default) the checksum is checked, and a mismatch
    raises FrameError. Wrong magic bytes also raise FrameError.
    """
    if len(frame) < 7 or frame[:2] != MAGIC:
        raise FrameError("not a glimmerquay frame")
    length = int.from_bytes(frame[2:4], "big")
    tag = frame[-1:]
    name = {b"f": "fletcher16", b"x": "xor8"}.get(tag)
    if name is None:
        raise FrameError("unknown checksum tag")
    body, check = frame[:-3], frame[-3:-1]
    if 4 + length > len(body):
        raise FrameError("length does not fit the frame")
    if verify and _checksum(name, body) != check:
        raise FrameError("checksum mismatch")
    return bytes(body[4:4 + length])
