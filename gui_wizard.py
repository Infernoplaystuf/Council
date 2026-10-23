"""
gui_wizard.py — a guided on-ramp into the GUI Designer.

The designer is a freeform canvas: powerful once you know it, blank and
unhelpful the first time you open it. This wizard asks five short questions and
hands the answers to that same canvas as ordinary shapes, which the user then
edits normally. It is an ON-RAMP, not a second designer — there is one scene
model, one snap engine, one undo stack, and Generate keeps reading the canvas
it always read.

The layout arithmetic lives in the pure ``gui_templates``; this file is the
result, the step order and ``build_shapes``. The dialog is
``council_qt.wizard`` (over ``council_core.wizard``); the Tk one that used to
be here was retired with the Tk shell (2026-10-23).

WHAT THE HOST DOES WITH THE RESULT
----------------------------------
The wizard calls back with a :class:`WizardResult`. The host creates the
project, applies ``window``/``canvas`` sizing, and puts ``shapes`` on the
canvas. The wizard itself creates nothing and writes nothing: a cancelled
wizard must leave no project directory behind, and the only way to guarantee
that is to never have made one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import gui_templates as _gt
from gui_shapes import Shape

# Kinds offered where a template lets the user choose what fills a region.
# A short, opinionated list: the full 27-row catalogue is what the palette is
# for, and a wizard that reproduced it would just be the palette with more
# clicks.
MAIN_KINDS = ("frame", "text", "treeview", "listbox", "log_pane")
SIDE_KINDS = ("listbox", "treeview", "frame")

DEFAULT_MIN_W = 900
DEFAULT_MIN_H = 600


@dataclass
class WizardResult:
    """Everything the host needs, and nothing it has to guess.

    Pure data with no Tk in it, so a caller can build one in a test and drive
    the host's apply-path without opening a window."""
    name: str = "Untitled"
    mode: str = "linked"
    title: str = "Untitled"
    min_w: int = DEFAULT_MIN_W
    min_h: int = DEFAULT_MIN_H
    template: str = "blank"
    shapes: List[Shape] = field(default_factory=list)
    #: 'tk' or 'qt' (gui_projects.TOOLKITS). Last, and defaulted to Tk, so the
    #: Tk wizard — which never asks — keeps making exactly what it always made.
    toolkit: str = "tk"


def build_shapes(template: str, options: Dict[str, Any],
                 reserve_n: int = 0, reserve_w: int = 240,
                 reserve_h: int = 160) -> List[Shape]:
    """The wizard's answers -> a shape list. Pure; no Tk.

    Reserved frames are appended AFTER the template so they take the higher z,
    which keeps the whole list in a single increasing z order — see the
    reproducibility note in gui_templates."""
    shapes = _gt.build(template, **(options or {}))
    if reserve_n > 0:
        y = max([s.y2 for s in shapes], default=24) + 24
        held = _gt.reserved(n=reserve_n, w=reserve_w, h=reserve_h,
                            origin=(40, y))
        base = len(shapes)
        for i, s in enumerate(held):
            s.z = base + i
        shapes.extend(held)
    return shapes


# ============================================================
# The wizard's steps (the dialog itself is council_qt.wizard)
# ============================================================

STEPS = ("basics", "layout", "contents", "reserve", "review")
STEP_TITLES = {
    "basics": "1 of 5 — Name and window",
    "layout": "2 of 5 — Starting layout",
    "contents": "3 of 5 — What goes in it",
    "reserve": "4 of 5 — Reserve space for later",
    "review": "5 of 5 — Review",
}
