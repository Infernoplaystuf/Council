"""
council_core.plots — the session's figures, and what happens when there are too many.

A Spyder-style plots pane keeps every figure you made this session: a big
render surface for the current one, a rail of thumbnails for the rest. What
needs no toolkit is the part that gets it wrong — the history: how many to
keep, which to drop, and what the selection means afterwards.

WHY A CAP EXISTS AT ALL
Figures are cheap but not free; each holds its own rendered buffers. A long
session makes hundreds, and without a cap the app grows until it is swapping.

WHY DROPPING IS THE INTERESTING PART
Every widget in the rail is bound to an INDEX. Drop the oldest figure and every
remaining index shifts by one, so a rail that does not re-bind shows you figure
N and hands you figure N+1 when you click it. The Tk pane re-binds each button
after a trim, which is the right answer written in the most fragile possible
place; here the shift is arithmetic on a list and a test can run a thousand
figures through it with no display.

NOTHING HERE CALLS PYPLOT
Figures are built as `Figure()` objects and rendered through their own canvas.
pyplot would put every one on a global registry that nothing ever clears — a
leak across a session — and would let a backend open a window of its own, which
on a headless run means a crash and on a user's machine means a stray window.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, List, Optional

#: The rail's thumbnail width, in pixels.
THUMB_W = 180

#: How many figures a session keeps. Past this the oldest go.
MAX_HISTORY = 60

#: Thumbnails are rendered small; the surface figure is rendered at its own dpi.
THUMB_DPI = 72


def figure_to_png_bytes(fig: Any, dpi: int = THUMB_DPI) -> bytes:
    """A Figure as PNG bytes.

    Bytes rather than a toolkit image, so the same call feeds a PIL Image in Tk
    and a QPixmap in Qt. `bbox_inches="tight"` because a thumbnail of a figure
    with default margins is mostly margin.
    """
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
    return buffer.getvalue()


def thumbnail_png(fig: Any, width: int = THUMB_W) -> bytes:
    """A small PNG of a figure, at most `width` wide.

    Scaled by rendering at a lower dpi rather than by resampling a full-size
    render: a 4x downsample of an Agg render turns axis ticks and 1px grid
    lines into grey mush, and the whole point of a thumbnail rail is that you
    can tell the charts apart.
    """
    size = getattr(fig, "get_size_inches", lambda: (6.4, 4.8))()
    fig_width = float(size[0]) or 6.4
    dpi = max(16, min(THUMB_DPI, int(width / fig_width)))
    return figure_to_png_bytes(fig, dpi=dpi)


@dataclass
class Trim:
    """What a trim did, so a view can apply the same change to its widgets."""
    #: How many were dropped from the front.
    dropped: int = 0
    #: Where the selection ended up, or None if there is nothing selected.
    active: Optional[int] = None


class FigureHistory:
    """Every figure made this session, oldest first, capped.

    Index 0 is the oldest. That ordering is what makes trimming a pop from the
    front and makes "the newest" the last index — which is what `add` selects,
    because the figure you just made is the one you want to look at.
    """

    def __init__(self, max_history: int = MAX_HISTORY):
        if max_history < 1:
            # A zero cap would drop each figure as it arrived and show nothing,
            # which reads as a broken plotter rather than as a setting.
            raise ValueError("max_history must be at least 1")
        self.max_history = max_history
        self._figures: List[Any] = []
        self._active: Optional[int] = None

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._figures)

    @property
    def figures(self) -> List[Any]:
        return list(self._figures)

    @property
    def active(self) -> Optional[int]:
        return self._active

    def at(self, index: int) -> Optional[Any]:
        if 0 <= index < len(self._figures):
            return self._figures[index]
        return None

    def current(self) -> Optional[Any]:
        return self.at(self._active) if self._active is not None else None

    # ------------------------------------------------------------------
    def add(self, figure: Any) -> Trim:
        """Append a figure, select it, and trim if that went over the cap."""
        self._figures.append(figure)
        self._active = len(self._figures) - 1
        return self._trim()

    def select(self, index: int) -> bool:
        """Make `index` current. False if there is no such figure."""
        if not (0 <= index < len(self._figures)):
            return False
        self._active = index
        return True

    def clear(self) -> None:
        self._figures.clear()
        self._active = None

    # ------------------------------------------------------------------
    def _trim(self) -> Trim:
        """Drop the oldest past the cap and move the selection with them.

        THE SELECTION MOVES BY THE NUMBER DROPPED, not by one. The Tk pane
        decrements it once per iteration inside the loop, which is the same
        thing only because it drops one at a time — raise the cap change to
        several and it points at the wrong figure.

        It also floors at 0 rather than going negative, because a rail with a
        negative selection highlights nothing and `current()` would raise.
        """
        dropped = len(self._figures) - self.max_history
        if dropped <= 0:
            return Trim(dropped=0, active=self._active)
        del self._figures[:dropped]
        if self._active is not None:
            self._active = max(0, self._active - dropped)
        return Trim(dropped=dropped, active=self._active)
