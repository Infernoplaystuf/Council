"""
gui_templates.py — starter layouts, as plain shape lists.

The wizard's knowledge of what a form or a toolbar app LOOKS like lives here,
not in the Tk shell, because layout arithmetic is the part worth testing and a
Toplevel cannot be imported without a display. gui_wizard.py is then only
widgets and step order; every coordinate it hands to the canvas comes from
here.

PURE: imports gui_shapes and nothing else. No tkinter, no council_engine, no
vault_*, no model call — a starter layout is a fact about geometry, and asking
a model to place a label next to an entry would be slower, offline-hostile and
less reliable than arithmetic.

TWO RULES EVERY TEMPLATE OBEYS
------------------------------
1. **Children sit geometrically inside their container, with margin to spare.**
   Nesting is INFERRED from geometry — Shape has no parent field — so a child
   that overshoots its frame by a few pixels is not merely detached: it can
   drop the whole window out of grid inference into free positioning. Every
   nested shape here clears its container by at least ``MARGIN`` on all sides,
   which is an order of magnitude more than the 4px containment tolerance.

2. **z increases in creation order.** Nesting itself sorts on (-area, id), so z
   does not change the inferred layout — but the id half of that tiebreak is a
   random uuid4, so leaving every z at 0 makes emission order and the ``_2``
   duplicate-name suffixes shuffle between two runs of the same template. A
   wizard that produced a different file each time it was run with the same
   answers would be indefensible.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from gui_shapes import PALETTE, Shape, new_shape

# The real DesignerCanvas in the tab. Templates are laid out to fit inside it
# without scrolling, because a starter layout the user has to go looking for is
# a bad start.
CANVAS_W = 1100
CANVAS_H = 700

# Clearance between a container's edge and anything inside it. Well above the
# 4px containment tolerance so hand-editing a shape a little cannot silently
# un-nest it.
MARGIN = 16
GAP = 12          # between sibling rows
ROW_H = 34        # one label+entry row, including its breathing room


def _w(kind: str) -> int:
    return int(PALETTE[kind]["default_w"])


def _h(kind: str) -> int:
    return int(PALETTE[kind]["default_h"])


def _stack(shapes: Sequence[Shape]) -> List[Shape]:
    """Assign z in list order. See rule 2 in the module docstring."""
    out = list(shapes)
    for i, s in enumerate(out):
        s.z = i
    return out


# ============================================================
# The templates
# ============================================================

def blank() -> List[Shape]:
    """An empty canvas.

    Present so "start from nothing" is a choice the wizard offers rather than a
    reason to skip the wizard."""
    return []


def form(n_fields: int = 3, labels: Optional[Sequence[str]] = None,
         buttons: Sequence[str] = ("OK", "Cancel"),
         title: str = "Details") -> List[Shape]:
    """A labelled frame of label+entry rows, with a button row beneath it.

    ``labels`` names the rows; missing names fall back to Field 1, Field 2, …
    so a user who wants four rows and cannot yet name them still gets four."""
    n = max(0, int(n_fields))
    names = list(labels or [])
    names += [f"Field {i + 1}" for i in range(len(names), n)]
    names = names[:n]

    lab_w, ent_w = _w("label"), _w("entry")
    inner_w = lab_w + GAP + ent_w
    box_w = inner_w + 2 * MARGIN
    box_h = 2 * MARGIN + max(1, n) * ROW_H

    box = new_shape("labelframe", 40, 40, w=box_w, h=box_h, label=title)
    out: List[Shape] = [box]

    for i, name in enumerate(names):
        y = box.y + MARGIN + i * ROW_H
        out.append(new_shape("label", box.x + MARGIN, y,
                             w=lab_w, h=_h("label"), label=name))
        out.append(new_shape("entry", box.x + MARGIN + lab_w + GAP, y,
                             w=ent_w, h=_h("entry"),
                             label=f"{name} input"))

    bx = box.x
    by = box.y2 + GAP
    for text in buttons:
        out.append(new_shape("button", bx, by,
                             w=_w("button"), h=_h("button"), label=text))
        bx += _w("button") + GAP
    return _stack(out)


def toolbar_main_status(main_kind: str = "frame") -> List[Shape]:
    """Toolbar across the top, a main working area, a status bar at the bottom.

    The shape of most small desktop tools, and the one where getting the resize
    behaviour right by hand is most annoying: the toolbar and status bar stretch
    horizontally only, the middle takes the slack."""
    if main_kind not in PALETTE:
        raise KeyError(f"unknown widget kind: {main_kind!r}")
    pad = 40
    width = CANVAS_W - 2 * pad
    tb_h, sb_h = _h("toolbar"), _h("status_bar")

    top = new_shape("toolbar", pad, pad, w=width, h=tb_h, label="Toolbar")
    main_y = top.y2 + GAP
    main_h = 420
    main = new_shape(main_kind, pad, main_y, w=width, h=main_h,
                     label="Main area")
    status = new_shape("status_bar", pad, main.y2 + GAP, w=width, h=sb_h,
                       label="Ready")
    return _stack([top, main, status])


def split_view(left_kind: str = "listbox",
               right_kind: str = "frame") -> List[Shape]:
    """A narrow chooser on the left, a wide detail panel on the right.

    Sized so the divider lands near a third, which is where it ends up by hand
    anyway once the list has real entries in it."""
    for k in (left_kind, right_kind):
        if k not in PALETTE:
            raise KeyError(f"unknown widget kind: {k!r}")
    pad = 40
    total = CANVAS_W - 2 * pad
    left_w = int(total * 0.32)
    right_w = total - left_w - GAP
    height = 460

    left = new_shape(left_kind, pad, pad, w=left_w, h=height, label="Items")
    right = new_shape(right_kind, pad + left_w + GAP, pad,
                      w=right_w, h=height, label="Details")
    return _stack([left, right])


def reserved(n: int = 1, w: int = 240, h: int = 160,
             origin: Sequence[int] = (40, 520),
             label: str = "Reserved") -> List[Shape]:
    """``n`` empty frames, each holding a spot for something not built yet.

    An empty frame is emitted with an explicit size and grid_propagate(False),
    so the gap survives into the generated app at the size drawn here — which
    is the whole point of reserving it. Deliberately NOT the "generic" kind:
    generic asks a model to turn the box into some real widget, which is the
    opposite of leaving room."""
    x0, y0 = int(origin[0]), int(origin[1])
    out = []
    for i in range(max(0, int(n))):
        out.append(new_shape("frame", x0 + i * (int(w) + GAP), y0,
                             w=int(w), h=int(h),
                             label=f"{label} {i + 1}" if n > 1 else label))
    return _stack(out)


# ============================================================
# The catalogue the wizard offers
# ============================================================

TEMPLATES: Dict[str, Dict[str, object]] = {
    "blank": {
        "label": "Blank canvas",
        "blurb": "Start from nothing.",
        "build": blank,
    },
    "form": {
        "label": "Form",
        "blurb": "Labelled fields with a row of buttons underneath.",
        "build": form,
    },
    "toolbar_main_status": {
        "label": "Toolbar, main area, status bar",
        "blurb": "The shape of most small desktop tools.",
        "build": toolbar_main_status,
    },
    "split_view": {
        "label": "Split view",
        "blurb": "A list on the left, details on the right.",
        "build": split_view,
    },
}


def build(name: str, **kw) -> List[Shape]:
    """Build one named template. Unknown names raise rather than returning an
    empty layout, which would look like the template silently produced nothing."""
    entry = TEMPLATES.get(name)
    if entry is None:
        raise KeyError(f"unknown template: {name!r}")
    fn = entry["build"]
    assert callable(fn)
    return list(fn(**kw))          # type: ignore[operator]


def names() -> List[str]:
    return list(TEMPLATES)
