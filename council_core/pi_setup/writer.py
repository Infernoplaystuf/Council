"""Write an OS image to a card and prove it arrived intact.

Toolkit- and OS-neutral: the target is any binary file object (on Windows the
elevated helper hands in ``\\\\.\\PhysicalDriveN``; tests hand in a plain file).
The image is decompressed on the fly (.img.xz via the standard lzma module, or
a plain .img), written in sector-aligned chunks, hashed on the way, and then
READ BACK from the target and hashed again. The write is good only if both
hashes agree with each other and, when known, with the official list's
``extract_sha256`` of the uncompressed image.
"""
from __future__ import annotations

import hashlib
import lzma
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

SECTOR = 512
CHUNK = 4 * 1024 * 1024        # a multiple of every sector size in use


class WriteFailed(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def open_image(path: Path) -> BinaryIO:
    p = Path(path)
    return lzma.open(p, "rb") if p.name.endswith(".xz") else open(p, "rb")


def _chunks(src: BinaryIO, size: int = CHUNK) -> Iterator[bytes]:
    while True:
        b = src.read(size)
        if not b:
            return
        yield b


def write_image(image: Path, target: BinaryIO, *, capacity: int,
                expected_sha256: str = "",
                on_progress: Optional[Callable[[str, int, int], None]] = None,
                cancelled: Callable[[], bool] = lambda: False,
                expected_size: int = 0) -> dict:
    """Write ``image`` to ``target`` from offset 0, then verify by reading back.
    Returns ``{"bytes": n, "sha256": hex}``. Raises WriteFailed / Cancelled.

    ``capacity`` is the card's size: an image larger than the card is refused
    BEFORE anything is written."""
    if expected_size and expected_size > capacity:
        raise WriteFailed(f"the image needs {expected_size / 1e9:.1f} GB; the card "
                          f"holds {capacity / 1e9:.1f} GB")
    say = on_progress or (lambda *_: None)
    h = hashlib.sha256()
    written = 0
    with open_image(image) as src:
        for chunk in _chunks(src):
            if cancelled():
                raise Cancelled("cancelled while writing — the card is incomplete "
                                "and will not boot until written again")
            h.update(chunk)
            if written + len(chunk) > capacity:
                raise WriteFailed("the image is larger than the card")
            pad = (-len(chunk)) % SECTOR
            target.write(chunk + b"\0" * pad if pad else chunk)
            written += len(chunk)
            say("writing", written, expected_size or written)
    try:
        target.flush()
    except OSError:
        pass
    got = h.hexdigest()
    if expected_sha256 and got != expected_sha256.lower():
        raise WriteFailed("the image file does not match the official checksum "
                          "(corrupt or a different release); the card must be "
                          "written again")
    # Read back what is ON THE CARD.
    target.seek(0)
    rb = hashlib.sha256()
    left = written
    while left > 0:
        if cancelled():
            raise Cancelled("cancelled while verifying")
        n = min(CHUNK, left)
        aligned = n + ((-n) % SECTOR)
        data = target.read(aligned)
        if len(data) < n:
            raise WriteFailed("the card returned less data than was written")
        rb.update(data[:n])
        left -= n
        say("verifying", written - left, written)
    if rb.hexdigest() != got:
        raise WriteFailed("what was read back from the card differs from the image "
                          "— the card or reader may be failing")
    return {"bytes": written, "sha256": got}
