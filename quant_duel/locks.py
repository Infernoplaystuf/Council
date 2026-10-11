"""Lock files: one daily run per node, one LLM job per machine."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STALE_LOCK_S = 6 * 3600


class LockBusy(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, stale_s: float = STALE_LOCK_S) -> Iterator[None]:
    """Hold ``path`` exclusively; a lock older than ``stale_s`` is broken
    (its holder died)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_s:
                path.unlink(missing_ok=True)
                continue
            raise LockBusy(f"another run holds {path}") from None
    else:
        raise LockBusy(f"could not take {path}")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
