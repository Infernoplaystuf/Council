"""
council_core.preload — load the models a question will need while the user
is still typing it.

The first call of a question often waits for its model to load (83 s
measured for a cold gpt-oss:20b). Typing takes long enough to hide most of
that. When the user starts typing, the Council tab kicks a Preloader, which
— at most once every MIN_GAP_S seconds, on a background thread — asks the
engine to warm the roles every question uses (council_engine.warm: an
Ollama load request or an in-app GGUF load, skipped when the model is
already loaded, and never two models of one machine that would swap).

COUNCIL_PRELOAD=0 turns it off. Nothing here talks to anything but the
machines the council already uses.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional, Sequence

MIN_GAP_S = 120.0
#: The roles every question uses: the Writer answers or synthesises, the
#: Judge checks.
ROLES = ("writer", "judge")


def enabled() -> bool:
    return os.environ.get("COUNCIL_PRELOAD", "1").strip().lower() \
        not in ("0", "false", "no", "off")


class Preloader:
    def __init__(self, warm: Optional[Callable[[Sequence[str]], List[str]]] = None,
                 min_gap_s: float = MIN_GAP_S):
        self._warm = warm
        self._gap = float(min_gap_s)
        self._last = -1e18
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.log: List[str] = []

    def kick(self, roles: Sequence[str] = ROLES) -> bool:
        """Start warming `roles` unless it ran recently or is running.
        Returns whether it started."""
        if not enabled():
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._last < self._gap or (
                    self._thread is not None and self._thread.is_alive()):
                return False
            self._last = now
            self._thread = threading.Thread(
                target=self._run, args=(tuple(roles),),
                name="council-preload", daemon=True)
            self._thread.start()
        return True

    def _run(self, roles: Sequence[str]) -> None:
        warm = self._warm
        if warm is None:
            try:
                import council_engine
                warm = council_engine.warm
            except Exception as exc:                      # noqa: BLE001
                self.log.append(f"preload unavailable: {exc}")
                return
        try:
            self.log.extend(warm(roles))
        except Exception as exc:                          # noqa: BLE001
            self.log.append(f"preload failed: {exc}")

    def join(self, timeout: Optional[float] = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)


__all__ = ["Preloader", "enabled", "ROLES", "MIN_GAP_S"]
