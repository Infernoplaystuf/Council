"""
glimmerquay.ledger — a bounded ledger of weighted entries.

A Ledger keeps at most `capacity` entries. What happens when it is full is
chosen by its overflow mode.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

#: The sequence number given to the first entry of every new ledger.
FIRST_SEQUENCE = 1000


class LedgerFullError(Exception):
    """Raised by Ledger.append when the ledger is full and its overflow mode
    is "raise"."""


class Ledger:
    """A bounded, sequence-numbered ledger of weighted entries.

    Parameters
    ----------
    capacity : int, default 64
        The most entries the ledger holds at once.
    overflow : {"rotate", "raise", "drop"}, keyword-only, default "rotate"
        What append does when the ledger is full:
        "rotate" discards the oldest entry to make room,
        "raise" raises LedgerFullError,
        "drop" silently ignores the new entry (append then returns -1).

    Sequence numbers start at 1000 for the first entry and go up by one for
    every entry appended, including entries later rotated out.
    """

    def __init__(self, capacity: int = 64, *, overflow: str = "rotate"):
        if overflow not in ("rotate", "raise", "drop"):
            raise ValueError(f"unknown overflow mode {overflow!r}")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self.overflow = overflow
        self._entries: List[Tuple[int, Any, float]] = []
        self._next = FIRST_SEQUENCE

    def append(self, entry: Any, *, weight: float = 1.0) -> int:
        """Add an entry and return its sequence number.

        The first entry of a new ledger gets sequence number 1000. `weight`
        (keyword-only, default 1.0) is used by total(). When the ledger is
        full, the overflow mode decides: "rotate" drops the oldest entry,
        "raise" raises LedgerFullError, "drop" returns -1 and keeps nothing.
        """
        if len(self._entries) >= self.capacity:
            if self.overflow == "raise":
                raise LedgerFullError(f"ledger is full ({self.capacity})")
            if self.overflow == "drop":
                return -1
            self._entries.pop(0)
        seq = self._next
        self._next += 1
        self._entries.append((seq, entry, float(weight)))
        return seq

    def total(self, weight_floor: float = 0.0) -> float:
        """The sum of the weights of all entries whose weight is at least
        `weight_floor` (default 0.0)."""
        return sum(w for _s, _e, w in self._entries if w >= weight_floor)

    def snapshot(self) -> Tuple[Any, ...]:
        """The entries currently held, oldest first, as a tuple (without
        their sequence numbers or weights)."""
        return tuple(e for _s, e, _w in self._entries)

    def drain(self, limit: Optional[int] = None) -> List[Any]:
        """Remove and return up to `limit` entries, oldest first (all of them
        when limit is None). Sequence numbering continues afterwards."""
        n = len(self._entries) if limit is None else max(0, int(limit))
        taken, self._entries = self._entries[:n], self._entries[n:]
        return [e for _s, e, _w in taken]

    def __len__(self) -> int:
        return len(self._entries)
