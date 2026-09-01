"""
gui_colors.py — colour data, colour maths, and who may be coloured.

PURE: stdlib only. No tkinter, no gui_shapes, no council_engine, no vault_*.
It is imported by the canvas (to paint the wireframe), by gui_spec (to resolve
inheritance) and by gui_emit (to derive hover/selection colours), so it must sit
below all of them. ``resolve_scene`` takes anything with .id/.kind/.bg/.fg
rather than importing Shape, which is what keeps that true.

WHY COLOURING A WIDGET DROPS IT OUT OF ttk
------------------------------------------
ttk widgets have no bg/fg options at all — ``ttk.Button(bg=...)`` raises
``unknown option "-bg"``. Colour in ttk goes through a named style, and a style
option is honoured only if the ACTIVE THEME's element for that widget reads it.

Measured on Windows 11 by rendering the widgets and reading the pixels back
(scaled for display DPI, or the reading is garbage):

    theme      ttk style background      classic tk background
    vista      0.0%  — ignored           97.6% — works      <- the default
    clam       95.9% — works             97.5%
    alt        96.3% — works             93.8%
    default    96.3% — works             93.3%

``vista`` IS the default theme on Windows. So a style-based colour system is a
picker that silently does nothing on the platform this app ships to, while
working perfectly on a clam-themed development box — worse than not working at
all, because the failure is invisible until a user reports it.

The one fix — ``style.theme_use("clam")`` — restyles EVERY widget in the
generated app, including all the uncoloured ones, because one progressbar was
made green. That trade is not ours to make silently, so it is not built.

Instead: a shape with a colour is emitted as the CLASSIC tk widget, which reads
``background`` from its own option database with no theme involved. A shape
without one stays ttk and keeps the native look. Four kinds have no classic
equivalent at all and are declared uncolourable in COLOUR_CAPS, with the reason
in COLOUR_NOTE — the inspector shows the reason instead of a dead picker.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ============================================================
# Grammar
# ============================================================
#
# A closed grammar on purpose. Tk also accepts colour NAMES ("red",
# "SystemButtonFace"), but a typo in a name is not caught until the generated
# app starts, and then it is a TclError on a line the user did not write. "" is
# the only non-colour, and it means "inherit".

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

EMPTY = ""


def to_hex(rgb: Iterable[int]) -> str:
    """(r, g, b) -> "#rrggbb". A 4-tuple is accepted; alpha is dropped.

    Alpha has no meaning to a Tk widget option, so carrying it would only let
    it be silently lost somewhere less obvious than here."""
    vals = [int(v) for v in rgb][:3]
    if len(vals) != 3:
        raise ValueError(f"need three channels, got {list(rgb)!r}")
    r, g, b = (max(0, min(255, v)) for v in vals)
    return f"#{r:02x}{g:02x}{b:02x}"


def is_colour(v: Any) -> bool:
    """True for "" (inherit) or a #rgb / #rrggbb literal."""
    if v is None or v == EMPTY:
        return True
    return bool(_HEX_RE.match(str(v)))


def normalise(v: Any) -> str:
    """Canonical lower-case "#rrggbb", expanding #rgb. "" stays "".

    Raises for anything else, so an unparseable colour is caught at the point
    it enters the model rather than at widget-construction time in the
    generated app."""
    if v is None or v == EMPTY:
        return EMPTY
    s = str(v).strip()
    if not _HEX_RE.match(s):
        raise ValueError(f"not a colour: {v!r} (use #rgb or #rrggbb)")
    s = s[1:].lower()
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return "#" + s


def to_rgb(v: str) -> Tuple[int, int, int]:
    s = normalise(v)
    if not s:
        raise ValueError("cannot convert the empty colour")
    return (int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))


# ============================================================
# Maths
# ============================================================

def mix(a: str, b: str, t: float) -> str:
    """Blend ``t`` of ``b`` into ``a``, in linear-ish sRGB space.

    Used at EMIT time to derive hover and selection colours, so the generated
    source carries literal hexes and does no colour maths at run time."""
    ar, ag, ab = to_rgb(a)
    br, bg_, bb = to_rgb(b)
    t = max(0.0, min(1.0, float(t)))
    return to_hex((round(ar + (br - ar) * t),
                   round(ag + (bg_ - ag) * t),
                   round(ab + (bb - ab) * t)))


def luminance(v: str) -> float:
    """WCAG relative luminance, 0.0 (black) to 1.0 (white)."""
    out = []
    for c in to_rgb(v):
        c = c / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast, 1.0 (identical) to 21.0 (black on white)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


INK = "#11111b"
PAPER = "#f5f5f7"


def auto_fg(bg: str) -> str:
    """Readable text for ``bg`` — whichever of ink/paper contrasts more.

    Without this, a widget that INHERITED a dark background would keep the
    platform's default black text and be unreadable, which is a worse outcome
    than not inheriting at all."""
    if not bg:
        return EMPTY
    return INK if contrast_ratio(INK, bg) >= contrast_ratio(PAPER, bg) else PAPER


# ============================================================
# Swatches
# ============================================================

# The app's own dark theme, so a designed app can match the host chrome.
THEME_DARK: List[Tuple[str, str]] = [
    ("bg", "#1e1e2e"), ("surface", "#313244"), ("overlay", "#585b70"),
    ("text", "#cdd6f4"), ("subtext", "#a6adc8"),
    ("blue", "#89b4fa"), ("green", "#a6e3a1"), ("red", "#f38ba8"),
    ("yellow", "#f9e2af"), ("mauve", "#cba6f7"),
]

# A light set. The designer canvas is dark but a generated app runs on the OS
# theme, which is light on Windows by default — a swatch that reads well on the
# wireframe can read badly in the app, so both ends of the range must ship.
THEME_LIGHT: List[Tuple[str, str]] = [
    ("paper", "#f5f5f7"), ("card", "#e6e6ea"), ("edge", "#c8c8d0"),
    ("ink", "#11111b"), ("muted", "#5c5f77"),
    ("blue", "#1e66f5"), ("green", "#40a02b"), ("red", "#d20f39"),
    ("yellow", "#df8e1d"), ("mauve", "#8839ef"),
]

# Ported from pixel_art.py on origin/odysseus-council, converted from the
# (r, g, b) tuples it stored. Same repo, same author — a straight port.
GAMEBOY = ["#0f380f", "#306230", "#8bac0f", "#9bbc0f"]

PICO8 = [
    "#000000", "#1d2b53", "#7e2553", "#008751",
    "#ab5236", "#5f574f", "#c2c3c7", "#fff1e8",
    "#ff004d", "#ffa300", "#ffec27", "#00e436",
    "#29adff", "#83769c", "#ff77a8", "#ffccaa",
]

# The NES NTSC palette with duplicate blacks removed. The source list ended
# with a filler #ffffff that is NOT an NES colour — the real NES white is
# #fcfcfc, already present two entries earlier — so the filler is dropped here
# rather than carried forward.
NES = [
    "#7c7c7c", "#0000fc", "#0000bc", "#4428bc",
    "#940084", "#a80020", "#a81000", "#881400",
    "#503000", "#007800", "#006800", "#005800",
    "#004058", "#000000",
    "#bcbcbc", "#0078f8", "#0058f8", "#6844fc",
    "#d800cc", "#e40058", "#f83800", "#e45c10",
    "#ac7c00", "#00b800", "#00a800", "#00a844",
    "#008888",
    "#f8f8f8", "#3cbcfc", "#6888fc", "#9878f8",
    "#f878f8", "#f85898", "#f87858", "#fca044",
    "#f8b800", "#b8f818", "#58d854", "#58f898",
    "#00e8d8", "#787878",
    "#fcfcfc", "#a4e4fc", "#b8b8f8", "#d8b8f8",
    "#f8b8f8", "#f8a4c0", "#f0d0b0", "#fce0a8",
    "#f8d878", "#d8f878", "#b8f8b8", "#b8f8d8",
    "#00fcfc", "#f8d8f8",
]

PALETTES: Dict[str, List[str]] = {
    "Data's Inferno": [hexv for _tok, hexv in THEME_DARK],
    "Light": [hexv for _tok, hexv in THEME_LIGHT],
    "Game Boy": GAMEBOY,
    "PICO-8": PICO8,
    "NES": NES,
}

# The picker opens here; the rest are one dropdown away. A free colour chooser
# and a hex field sit alongside, so the curated sets are a shortcut and never a
# ceiling.
DEFAULT_PALETTE = "Data's Inferno"


def palette(name: str) -> List[str]:
    if name not in PALETTES:
        raise KeyError(f"unknown palette: {name!r}")
    return list(PALETTES[name])


# ============================================================
# Who can actually be coloured
# ============================================================
#
# Keyed by the gui_shapes palette kind. A test asserts this covers exactly the
# catalogue, so a new widget kind cannot be added without deciding its answer —
# the alternative is a kind that defaults to "colourable" and silently is not.

COLOUR_CAPS: Dict[str, Tuple[str, ...]] = {
    # Classic equivalents that take both.
    "labelframe": ("bg", "fg"), "label": ("bg", "fg"),
    "button": ("bg", "fg"), "entry": ("bg", "fg"),
    "text": ("bg", "fg"), "checkbutton": ("bg", "fg"),
    "radiobutton": ("bg", "fg"), "listbox": ("bg", "fg"),
    "spinbox": ("bg", "fg"), "scale": ("bg", "fg"),
    "menubar": ("bg", "fg"), "log_pane": ("bg", "fg"),
    # No text of their own, so background only.
    "frame": ("bg",), "freeform": ("bg",), "panedwindow": ("bg",),
    "separator": ("bg",), "image_canvas": ("bg",), "chart_panel": ("bg",),
    # Nothing to swap to. See COLOUR_NOTE.
    "notebook": (), "treeview": (), "combobox": (), "progressbar": (),
    "scrubber": (), "file_picker": (), "status_bar": (), "toolbar": (),
    "generic": (),
}

COLOUR_NOTE: Dict[str, str] = {
    "notebook": ("Tk has no classic notebook, so a colour here would do "
                 "nothing. Draw a Frame behind it and colour that instead."),
    "treeview": ("Tk has no classic table. Per-row colours do work, but they "
                 "are set on the data from app.py at insert time, not on the "
                 "widget."),
    "combobox": ("Tk has no classic combobox. tk.OptionMenu is a different "
                 "widget — no free text, no values reconfiguration — so "
                 "swapping it would change behaviour to change colour."),
    "progressbar": ("Tk has no classic progress bar. Drawing one on a canvas "
                    "is re-implementing a widget, not colouring one."),
    "scrubber": "Built from ttk parts; colouring it means rebuilding it.",
    "file_picker": "Built from ttk parts; colouring it means rebuilding it.",
    "status_bar": "Built from ttk parts; colouring it means rebuilding it.",
    "toolbar": "Built from ttk parts; colouring it means rebuilding it.",
    "generic": "An unclassified box has no widget yet, so nothing to colour.",
    "menubar": ("Windows draws the menu bar itself; these colours reach the "
                "drop-down menus only."),
    "scale": ("The classic Scale is a rectangular slider in a trough, so "
              "colouring it changes its shape as well as its colour."),
    "frame": "A Frame has no text of its own.",
    "freeform": "A free area has no text of its own.",
    "panedwindow": "A paned window has no text of its own.",
    "separator": "A separator is a filled rule; it has no text.",
    "image_canvas": "The image sits on top; this is the surround.",
    "chart_panel": "This is the figure background, not the plot area.",
}


def caps(kind: str) -> Tuple[str, ...]:
    """Which of ("bg", "fg") ``kind`` can honour. Unknown kinds get none."""
    return COLOUR_CAPS.get(kind, ())


def can_colour(kind: str, channel: str) -> bool:
    return channel in caps(kind)


def note(kind: str) -> str:
    return COLOUR_NOTE.get(kind, "")


# ============================================================
# Inheritance
# ============================================================

def resolve_scene(shapes: Sequence[Any],
                  parents: Optional[Mapping[str, str]] = None
                  ) -> Dict[str, Tuple[str, str]]:
    """shape id -> (effective_bg, effective_fg).

    THE ONLY implementation of the inheritance rule. The canvas calls it with
    its live containment map and the emitter calls it with the layout tree's
    parent map, so the wireframe cannot promise a colour the generated app will
    not paint — including refusing to show one on a kind that cannot honour it.

    A shape's own colour wins. Otherwise a background is inherited from the
    nearest ancestor that has one, but only when this kind is classic-capable.
    The foreground, if unset, is derived from the effective background — an
    inherited dark panel with the platform's default black text would be
    unreadable, and that is the common case, not the corner case."""
    pmap = dict(parents or {})
    by_id = {}
    for s in shapes:
        by_id[getattr(s, "id", "")] = s

    def own(s, channel: str) -> str:
        v = getattr(s, channel, EMPTY) or EMPTY
        if not v or not can_colour(getattr(s, "kind", ""), channel):
            return EMPTY
        try:
            return normalise(v)
        except ValueError:
            return EMPTY

    out: Dict[str, Tuple[str, str]] = {}
    for s in shapes:
        sid = getattr(s, "id", "")
        kind = getattr(s, "kind", "")
        bg = own(s, "bg")
        if not bg and can_colour(kind, "bg"):
            seen = {sid}
            cur = pmap.get(sid)
            while cur and cur not in seen:
                seen.add(cur)
                parent = by_id.get(cur)
                if parent is None:
                    break
                pbg = own(parent, "bg")
                if pbg:
                    bg = pbg
                    break
                cur = pmap.get(cur)
        fg = own(s, "fg")
        if not fg and bg and can_colour(kind, "fg"):
            fg = auto_fg(bg)
        out[sid] = (bg, fg)
    return out
