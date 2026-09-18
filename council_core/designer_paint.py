"""
council_core.designer_paint — how each widget is drawn, for any painter.

Twenty-seven renderers that draw a button with a raised face, an entry with a
caret and a placeholder, a notebook with a tab strip — so the canvas shows what
the generated app will LOOK like rather than coloured boxes.

THE FIVE-METHOD PROTOCOL IS WHY PHASE 8 IS AFFORDABLE
There are twenty canvas-primitive call sites in the whole 1,972-line original,
and fifteen of them are behind five three-line wrappers: `_r`, `_ln`, `_tx`,
`_poly`, `_oval`. Every one of the 27 renderers calls ONLY those. So the
renderers were never talking to tkinter — they talk to an object with five
methods:

    create_rectangle(x1, y1, x2, y2, *, fill, outline, width, dash)
    create_line(*coords, fill, width, dash)
    create_text(x, y, *, text, fill, anchor, font, width)
    create_polygon(*points, fill, outline)
    create_oval(x1, y1, x2, y2, *, fill, outline, width)

`tests/test_gui_canvas.py` already proves it: a `_Fake` recorder with exactly
those five methods drives all 27 renderers with no display at all.

So the Qt port of 647 lines of drawing is an adapter that implements five
methods against QPainter — and the existing renderer tests validate it by
substitution, which is a better test than anything written fresh for the Qt
side would be.

A NOTE FOR WHOEVER WRITES THAT ADAPTER
`create_line` is called with a variadic flat coordinate list, not only with
four arguments. And Qt antialiases by default, which makes a 1px stroke on an
integer coordinate blurry — offset by 0.5 or turn it off.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gui_shapes import GENERIC_KIND, Shape

from .designer_scene import THEME


_INPUT_BG    = "#181825"    # slightly darker than surface — "a hole to fill"
_BUTTON_BG   = "#45475a"    # raised face
_BORDER      = "#585b70"    # overlay
_PLACEHOLDER = "#6c7086"    # ghost text
_CARET       = "#89b4fa"    # blue, matches the selection ring
_TAB_ON      = "#313244"
_TAB_OFF     = "#181825"
_CHART_PAPER = "#f5f5f7"
class _Ctx:
    """One per shape per redraw. Plain object (not a dataclass) to keep the
    allocation cheap — this runs 27 times per motion frame during a drag."""
    __slots__ = ("c", "x", "y", "w", "h", "label", "props",
                 "bg", "fg", "tier")
def _tier(w: int, h: int) -> int:
    """Which decoration budget the shape can afford, from its box.

    0 = outline + fill only; 1 = silhouette + primary framing; 2 = everything.
    The outline is the LAST thing to drop — a widget without one is less
    recognisable than one without decoration."""
    if w < 24 or h < 14:
        return 0
    if w < 60 or h < 22:
        return 1
    return 2
def _mk_ctx(canvas, s: Shape, bg: str, fg: str) -> "_Ctx":
    ctx = _Ctx()
    ctx.c = canvas
    ctx.x, ctx.y, ctx.w, ctx.h = s.x, s.y, s.w, s.h
    ctx.label = s.label or ""
    ctx.props = s.props or {}
    ctx.bg, ctx.fg = bg, fg
    ctx.tier = _tier(s.w, s.h)
    return ctx
def _r(c, x, y, w, h, *, fill="", outline="", width=1, dash=None):
    c.create_rectangle(x, y, x + w, y + h, fill=fill or "",
                       outline=outline or "", width=width, dash=dash)
def _ln(c, x1, y1, x2, y2, *, fill, width=1, dash=None):
    c.create_line(x1, y1, x2, y2, fill=fill, width=width, dash=dash)
def _tx(c, x, y, txt, *, fill, anchor="w", size=9, width=0):
    kw = {"width": width} if width else {}
    c.create_text(x, y, text=str(txt or ""), fill=fill, anchor=anchor,
                  font=("Segoe UI", size), **kw)
def _poly(c, pts, *, fill, outline=""):
    c.create_polygon(*pts, fill=fill, outline=outline)
def _oval(c, x, y, w, h, *, fill="", outline="", width=1):
    c.create_oval(x, y, x + w, y + h, fill=fill or "",
                  outline=outline or "", width=width)
def _bg(ctx: "_Ctx", default: str) -> str:
    return ctx.bg or default
def _fg(ctx: "_Ctx", default: Optional[str] = None) -> str:
    return ctx.fg or (default or THEME["text"])
def _render_frame(c, ctx):
    """No visible chrome — the caller paints the container ring. When the
    frame has a label the ID sits at top-left in subtext, so an empty
    Frame is still identifiable at a glance."""
    if ctx.tier and ctx.label:
        _tx(c, ctx.x + 6, ctx.y + 6, ctx.label,
            fill=THEME["subtext"], anchor="nw", size=8)
def _render_freeform(c, ctx):
    """A freeform region reads as a Frame that lets children place() freely.
    The mauve dashed container ring says "loose"; the diagonal cross-hatch
    inside makes it visibly not the same as a bare Frame."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    if ctx.tier == 0:
        return
    step = 16
    for i in range(step, w + h, step):
        x1 = max(0, i - h)
        y1 = min(h, i)
        x2 = min(w, i)
        y2 = max(0, i - w)
        _ln(c, x + x1, y + y1, x + x2, y + y2, fill=THEME["surface"])
    if ctx.label and ctx.tier == 2:
        _tx(c, x + 6, y + 6, ctx.label, fill=THEME["subtext"],
            anchor="nw", size=8)
def _render_labelframe(c, ctx):
    """Outline with a title notch on the top edge — tk's native look."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    label = ctx.label or str(ctx.props.get("text") or "") or "Label Frame"
    edge = _fg(ctx, THEME["mauve"])
    if ctx.tier == 2 and label:
        tw = min(max(40, len(label) * 6 + 10), max(20, w - 24))
        _ln(c, x, y, x + 8, y, fill=edge)
        _ln(c, x + 8 + tw, y, x + w, y, fill=edge)
        _tx(c, x + 12, y, label, fill=_fg(ctx), anchor="w", size=9)
    else:
        _ln(c, x, y, x + w, y, fill=edge)
    _ln(c, x, y + h, x + w, y + h, fill=edge)
    _ln(c, x, y, x, y + h, fill=edge)
    _ln(c, x + w, y, x + w, y + h, fill=edge)
def _render_notebook(c, ctx):
    """Tab strip on top, page area below. Two placeholder tabs when the tabs
    prop is empty — a notebook with no tabs is indistinguishable from a Frame,
    and "this is a notebook" is worth the small over-promise."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    tabs = list(ctx.props.get("tabs") or []) or [ctx.label or "Tab 1", "Tab 2"]
    th = min(22, max(14, h // 8))
    if ctx.tier == 0:
        _r(c, x, y, w, h, fill=_TAB_ON, outline=_BORDER)
        return
    _r(c, x, y + th, w, h - th, fill=_TAB_ON, outline=_BORDER)
    tx_ = x + 2
    for i, name in enumerate(tabs[:8]):
        tw = min(max(48, len(str(name)) * 7 + 14), w - (tx_ - x) - 8)
        if tw < 20:
            break
        active = i == 0
        _r(c, tx_, y + (0 if active else 3),
           tw, th - (0 if active else 3),
           fill=_TAB_ON if active else _TAB_OFF, outline=_BORDER)
        if ctx.tier == 2:
            _tx(c, tx_ + tw / 2, y + th / 2, name,
                fill=_fg(ctx) if active else THEME["subtext"],
                anchor="center", size=8)
        tx_ += tw
        if tx_ > x + w - 30:
            break
def _render_panedwindow(c, ctx):
    """Two panes with a sash. Orient from props."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    _r(c, x, y, w, h, outline=_BORDER)
    if ctx.tier == 0:
        return
    if str(ctx.props.get("orient") or "horizontal") == "horizontal":
        mid = x + w // 2
        _ln(c, mid, y + 3, mid, y + h - 3, fill=_BORDER, width=3)
    else:
        mid = y + h // 2
        _ln(c, x + 3, mid, x + w - 3, mid, fill=_BORDER, width=3)
def _render_label(c, ctx):
    """Text, no chrome. Anchor from prop, default west."""
    if ctx.tier == 0:
        return
    anc = str(ctx.props.get("anchor") or "w")
    ax = {"w": ctx.x + 4, "center": ctx.x + ctx.w / 2,
          "e": ctx.x + ctx.w - 4}.get(anc, ctx.x + 4)
    _tx(c, ax, ctx.y + ctx.h / 2, ctx.label or "Label",
        fill=_fg(ctx), anchor=anc, size=9, width=max(20, ctx.w - 8))
def _render_button(c, ctx):
    """Raised face with centred label."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _BUTTON_BG), outline=_BORDER)
    if ctx.tier >= 1:
        _tx(c, ctx.x + ctx.w / 2, ctx.y + ctx.h / 2,
            ctx.label or "Button", fill=_fg(ctx),
            anchor="center", size=9, width=max(20, ctx.w - 8))
def _render_entry(c, ctx):
    """Input surface with a subtle border, a caret at the write-anchor, and
    placeholder text from shape.label — an empty entry then reads "empty and
    inviting" rather than blank-and-broken. show="*" turns the placeholder
    into bullets, matching the emitter."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier == 0:
        return
    show = str(ctx.props.get("show") or "")
    placeholder = str(ctx.props.get("placeholder") or "") or ctx.label
    justify = str(ctx.props.get("justify") or "left")
    inset = 6
    cx = {"left": ctx.x + inset, "center": ctx.x + ctx.w / 2,
          "right": ctx.x + ctx.w - inset}.get(justify, ctx.x + inset)
    _ln(c, cx, ctx.y + 4, cx, ctx.y + ctx.h - 4, fill=_CARET)
    if placeholder and ctx.tier == 2:
        text = "•" * min(len(placeholder), 12) if show else placeholder
        anc = {"left": "w", "center": "center",
               "right": "e"}.get(justify, "w")
        tx_ = {"w": ctx.x + inset + 2, "center": ctx.x + ctx.w / 2,
               "e": ctx.x + ctx.w - inset - 2}[anc]
        _tx(c, tx_, ctx.y + ctx.h / 2, text,
            fill=_PLACEHOLDER, anchor=anc, size=9,
            width=max(20, ctx.w - 2 * inset))
def _render_text(c, ctx):
    """Multi-line input: surface + faint line hints so the widget reads as
    MANY lines, not one tall Entry."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier == 0:
        return
    y = ctx.y + 12
    while y < ctx.y + ctx.h - 6:
        _ln(c, ctx.x + 6, y, ctx.x + ctx.w - 6, y, fill=THEME["surface"])
        y += 14
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 8, ctx.y + 8, ctx.label,
            fill=_PLACEHOLDER, anchor="nw", size=9,
            width=max(20, ctx.w - 16))
def _render_checkbutton(c, ctx):
    """Box + label. default=True shows a green check mark."""
    box = min(14, max(8, ctx.h - 4))
    bx, by = ctx.x + 2, ctx.y + (ctx.h - box) / 2
    _r(c, bx, by, box, box, fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if bool(ctx.props.get("default")) and ctx.tier >= 1:
        _ln(c, bx + 2, by + box / 2, bx + box / 2, by + box - 2,
            fill=THEME["green"], width=2)
        _ln(c, bx + box / 2, by + box - 2, bx + box - 2, by + 2,
            fill=THEME["green"], width=2)
    if ctx.tier >= 1:
        _tx(c, bx + box + 6, ctx.y + ctx.h / 2,
            ctx.label or "Check", fill=_fg(ctx), anchor="w",
            size=9, width=max(10, ctx.w - box - 12))
def _render_radiobutton(c, ctx):
    """Circle + label. The "selected" dot is intentionally NOT drawn from a
    shape prop — radio state is a runtime group property, not a per-shape
    default, so painting one here would misrepresent it."""
    diam = min(14, max(8, ctx.h - 4))
    bx, by = ctx.x + 2, ctx.y + (ctx.h - diam) / 2
    _oval(c, bx, by, diam, diam, fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier >= 1:
        _tx(c, bx + diam + 6, ctx.y + ctx.h / 2,
            ctx.label or "Radio", fill=_fg(ctx), anchor="w",
            size=9, width=max(10, ctx.w - diam - 12))
def _render_combobox(c, ctx):
    """Entry + chevron dropdown on the right."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    ch = 16
    _r(c, ctx.x + ctx.w - ch, ctx.y, ch, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    cx = ctx.x + ctx.w - ch / 2
    cy = ctx.y + ctx.h / 2
    _poly(c, [cx - 4, cy - 2, cx + 4, cy - 2, cx, cy + 3],
          fill=THEME["subtext"])
    vals = list(ctx.props.get("values") or [])
    display = ctx.label or (str(vals[0]) if vals else "")
    if display and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + ctx.h / 2, display,
            fill=THEME["text"] if vals else _PLACEHOLDER,
            anchor="w", size=9, width=max(10, ctx.w - ch - 10))
def _render_listbox(c, ctx):
    """Input surface with row separators and one "selected" row so the widget
    reads as a selectable list, not a blank pane."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    row_h = 16
    y = ctx.y + 4
    if y + row_h < ctx.y + ctx.h - 2:
        _r(c, ctx.x + 2, y, ctx.w - 4, row_h - 2,
           fill=THEME["blue"], outline="")
        if ctx.tier == 2:
            _tx(c, ctx.x + 6, y + row_h / 2 - 1,
                ctx.label or "Item 1", fill=THEME["bg"],
                anchor="w", size=8, width=max(10, ctx.w - 12))
    y += row_h
    while y + row_h < ctx.y + ctx.h - 2 and ctx.tier == 2:
        _ln(c, ctx.x + 6, y + row_h - 2, ctx.x + ctx.w - 6, y + row_h - 2,
            fill=THEME["surface"])
        y += row_h
def _render_spinbox(c, ctx):
    """Entry + stacked up/down arrows on the right."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    ch = 14
    ax = ctx.x + ctx.w - ch
    _r(c, ax, ctx.y, ch, ctx.h / 2, fill=THEME["surface"], outline=_BORDER)
    _r(c, ax, ctx.y + ctx.h / 2, ch, ctx.h / 2,
       fill=THEME["surface"], outline=_BORDER)
    ux = ax + ch / 2
    uy1 = ctx.y + ctx.h / 4
    _poly(c, [ux - 3, uy1 + 2, ux + 3, uy1 + 2, ux, uy1 - 2],
          fill=THEME["subtext"])
    uy2 = ctx.y + 3 * ctx.h / 4
    _poly(c, [ux - 3, uy2 - 2, ux + 3, uy2 - 2, ux, uy2 + 2],
          fill=THEME["subtext"])
    if ctx.tier == 2:
        val = str(ctx.props.get("from_") or "0")
        _tx(c, ctx.x + 6, ctx.y + ctx.h / 2, val,
            fill=THEME["text"], anchor="w", size=9)
def _render_scale(c, ctx):
    """Trough with a thumb. Orient from prop; label captions above."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    if orient == "vertical":
        tx1 = ctx.x + ctx.w / 2 - 2
        _r(c, tx1, ctx.y + 4, 4, ctx.h - 8, fill=_BORDER, outline="")
        thumb_y = ctx.y + ctx.h / 3
        _r(c, ctx.x + ctx.w / 2 - 6, thumb_y - 4, 12, 8,
           fill=_bg(ctx, THEME["blue"]), outline=_BORDER)
    else:
        ty1 = ctx.y + ctx.h / 2 - 2
        _r(c, ctx.x + 4, ty1, ctx.w - 8, 4, fill=_BORDER, outline="")
        thumb_x = ctx.x + ctx.w / 3
        _r(c, thumb_x - 4, ctx.y + ctx.h / 2 - 6, 8, 12,
           fill=_bg(ctx, THEME["blue"]), outline=_BORDER)
def _render_progressbar(c, ctx):
    """Trough + a 40% fill so the widget reads as a bar."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    if ctx.tier == 0:
        return
    if orient == "vertical":
        f = ctx.h * 0.4
        _r(c, ctx.x, ctx.y + ctx.h - f, ctx.w, f,
           fill=_bg(ctx, THEME["blue"]), outline="")
    else:
        _r(c, ctx.x, ctx.y, ctx.w * 0.4, ctx.h,
           fill=_bg(ctx, THEME["blue"]), outline="")
def _render_separator(c, ctx):
    """One rule."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if orient == "vertical":
        cx = ctx.x + ctx.w / 2
        _ln(c, cx, ctx.y, cx, ctx.y + ctx.h,
            fill=_fg(ctx, THEME["subtext"]))
    else:
        cy = ctx.y + ctx.h / 2
        _ln(c, ctx.x, cy, ctx.x + ctx.w, cy,
            fill=_fg(ctx, THEME["subtext"]))
def _render_treeview(c, ctx):
    """Header + column dividers + a few row baselines."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    if ctx.tier == 0:
        return
    hh = 18
    _r(c, ctx.x, ctx.y, ctx.w, hh, fill=THEME["surface"], outline=_BORDER)
    cols = list(ctx.props.get("columns") or []) or ["A", "B", "C"]
    cw = ctx.w / max(1, len(cols))
    for i, col in enumerate(cols[:6]):
        cx = ctx.x + i * cw
        if i > 0:
            _ln(c, cx, ctx.y + 1, cx, ctx.y + ctx.h - 1, fill=_BORDER)
        if ctx.tier == 2:
            _tx(c, cx + 6, ctx.y + hh / 2, col,
                fill=_fg(ctx), anchor="w", size=8)
    y = ctx.y + hh + 4
    while y + 14 < ctx.y + ctx.h - 2 and ctx.tier == 2:
        _ln(c, ctx.x + 4, y + 12, ctx.x + ctx.w - 4, y + 12,
            fill=THEME["surface"])
        y += 16
def _render_image_canvas(c, ctx):
    """Dark pane with a sun-over-mountains glyph — a universally readable
    "this is an image viewer" mark that costs three primitives."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, "#0f0f18"), outline=_BORDER)
    if ctx.tier == 0:
        return
    r = min(10, min(ctx.w, ctx.h) // 6)
    _oval(c, ctx.x + ctx.w * 0.15, ctx.y + ctx.h * 0.2,
          r * 1.3, r * 1.3, fill=THEME["yellow"], outline="")
    _poly(c, [ctx.x + ctx.w * 0.15, ctx.y + ctx.h - 8,
              ctx.x + ctx.w * 0.45, ctx.y + ctx.h * 0.45,
              ctx.x + ctx.w * 0.65, ctx.y + ctx.h - 8],
          fill=THEME["surface"])
    _poly(c, [ctx.x + ctx.w * 0.45, ctx.y + ctx.h - 8,
              ctx.x + ctx.w * 0.70, ctx.y + ctx.h * 0.60,
              ctx.x + ctx.w * 0.90, ctx.y + ctx.h - 8],
          fill=THEME["overlay"])
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + 6, ctx.label,
            fill=THEME["subtext"], anchor="nw", size=8)
def _render_chart_panel(c, ctx):
    """L-shaped axes and a small polyline. The real widget shows "(no chart
    yet)" until data is drawn; painting axes here does over-promise (see
    design notes), but no axes at all reads as a blank Frame, which is worse
    for a wireframe."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _CHART_PAPER), outline=_BORDER)
    if ctx.tier == 0:
        return
    pad = 14
    ax0, ay0 = ctx.x + pad, ctx.y + ctx.h - pad
    ax1, ay1 = ctx.x + ctx.w - pad, ctx.y + pad
    _ln(c, ax0, ay0, ax1, ay0, fill="#5c5f77")
    _ln(c, ax0, ay0, ax0, ay1, fill="#5c5f77")
    if ctx.tier == 2:
        pts = []
        wpx, hpx = ax1 - ax0, ay0 - ay1
        for i, frac in enumerate((0.05, 0.25, 0.15, 0.45, 0.35,
                                  0.6, 0.5, 0.8, 0.7, 0.95)):
            pts.extend((ax0 + (i / 9) * wpx, ay0 - frac * hpx))
        c.create_line(*pts, fill=THEME["blue"], width=2)
    if ctx.label:
        _tx(c, ctx.x + 4, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
def _render_scrubber(c, ctx):
    """Prev button, trough+thumb, next button, index entry — mirrors the
    Scrubber composite in gui_emit.WIDGETS_PY."""
    if ctx.tier == 0:
        _r(c, ctx.x, ctx.y, ctx.w, ctx.h, outline=_BORDER)
        return
    bw = 20
    _r(c, ctx.x, ctx.y, bw, ctx.h, fill=_BUTTON_BG, outline=_BORDER)
    _poly(c, [ctx.x + bw - 6, ctx.y + ctx.h / 2 - 4,
              ctx.x + bw - 6, ctx.y + ctx.h / 2 + 4,
              ctx.x + 6, ctx.y + ctx.h / 2], fill=THEME["text"])
    nx = ctx.x + ctx.w - bw * 2
    _r(c, nx, ctx.y, bw, ctx.h, fill=_BUTTON_BG, outline=_BORDER)
    _poly(c, [nx + 6, ctx.y + ctx.h / 2 - 4,
              nx + 6, ctx.y + ctx.h / 2 + 4,
              nx + bw - 6, ctx.y + ctx.h / 2], fill=THEME["text"])
    tx_, tw = ctx.x + bw + 4, ctx.w - bw * 3 - 8
    _r(c, tx_, ctx.y + ctx.h / 2 - 2, tw, 4, fill=_BORDER, outline="")
    _r(c, tx_ + tw / 3 - 4, ctx.y + ctx.h / 2 - 6, 8, 12,
       fill=THEME["blue"], outline=_BORDER)
    _r(c, ctx.x + ctx.w - bw, ctx.y, bw, ctx.h,
       fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 2:
        _tx(c, ctx.x + ctx.w - bw / 2, ctx.y + ctx.h / 2, "1",
            fill=THEME["text"], anchor="center", size=8)
def _render_log_pane(c, ctx):
    """Text surface with coloured level bars — the tags ARE the identity of a
    LogPane, so drawing three lines each in its level colour is what tells
    the eye "this is a log", not a Text."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, "#181825"), outline=_BORDER)
    if ctx.tier == 0:
        return
    levels = list(ctx.props.get("levels") or ["info", "warn", "error"])
    palette = {"info": THEME["text"], "warn": THEME["yellow"],
               "error": THEME["red"], "debug": THEME["subtext"],
               "ok": THEME["green"]}
    y = ctx.y + 6
    for lvl in levels[:6]:
        if y + 12 > ctx.y + ctx.h - 4:
            break
        _ln(c, ctx.x + 6, y + 3, ctx.x + max(20, ctx.w * 0.7), y + 3,
            fill=palette.get(str(lvl), THEME["text"]))
        y += 12
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + ctx.h - 8, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
def _render_file_picker(c, ctx):
    """Entry + Browse button — the composite the user cited by name. An
    unset path is drawn as a mode-appropriate placeholder so a File Picker
    LOOKS empty rather than reading as broken."""
    if ctx.tier == 0:
        _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
           fill=_INPUT_BG, outline=_BORDER)
        return
    bw = min(72, max(40, ctx.w // 4))
    _r(c, ctx.x, ctx.y, ctx.w - bw, ctx.h,
       fill=_INPUT_BG, outline=_BORDER)
    _r(c, ctx.x + ctx.w - bw, ctx.y, bw, ctx.h,
       fill=_BUTTON_BG, outline=_BORDER)
    _ln(c, ctx.x + 6, ctx.y + 4, ctx.x + 6, ctx.y + ctx.h - 4,
        fill=_CARET)
    mode = str(ctx.props.get("mode") or "file")
    hint = ctx.label or {"file": "Select a file...",
                         "folder": "Select a folder...",
                         "save": "Save as..."}.get(mode, "Select...")
    if ctx.tier == 2:
        _tx(c, ctx.x + 10, ctx.y + ctx.h / 2, hint,
            fill=_PLACEHOLDER, anchor="w", size=9,
            width=max(10, ctx.w - bw - 16))
    _tx(c, ctx.x + ctx.w - bw / 2, ctx.y + ctx.h / 2, "Browse...",
        fill=THEME["text"], anchor="center", size=8)
def _render_status_bar(c, ctx):
    """Message strip; optional progress on the right when the prop is on."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, THEME["surface"]), outline=_BORDER)
    if ctx.tier == 0:
        return
    right = ctx.x + ctx.w - 4
    if bool(ctx.props.get("progress")) and ctx.w > 160:
        pw = 120
        _r(c, right - pw, ctx.y + 4, pw, ctx.h - 8,
           fill=_INPUT_BG, outline=_BORDER)
        _r(c, right - pw, ctx.y + 4, pw * 0.35, ctx.h - 8,
           fill=THEME["blue"], outline="")
        right -= pw + 8
    _tx(c, ctx.x + 8, ctx.y + ctx.h / 2, ctx.label or "Ready",
        fill=_fg(ctx), anchor="w", size=9,
        width=max(10, right - ctx.x - 12))
def _render_toolbar(c, ctx):
    """Horizontal button strip from the buttons prop; label is used as a
    default single button when the prop is empty."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    if ctx.tier == 0:
        return
    names = list(ctx.props.get("buttons") or []) or [ctx.label or "Action"]
    bx = ctx.x + 4
    for name in names[:10]:
        bw = min(80, max(24, len(str(name)) * 7 + 12))
        if bx + bw > ctx.x + ctx.w - 4:
            break
        _r(c, bx, ctx.y + 4, bw, ctx.h - 8,
           fill=_BUTTON_BG, outline=_BORDER)
        if ctx.tier == 2:
            _tx(c, bx + bw / 2, ctx.y + ctx.h / 2, name,
                fill=THEME["text"], anchor="center", size=8)
        bx += bw + 4
def _render_menubar(c, ctx):
    """Menu strip: top-level menu titles. Pulls from the props tree when
    present, otherwise from the label as a comma-separated list."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, THEME["surface"]), outline=_BORDER)
    if ctx.tier == 0:
        return
    titles: List[str] = []
    menus = ctx.props.get("menus")
    if isinstance(menus, list):
        for m in menus[:8]:
            if isinstance(m, dict) and m.get("title"):
                titles.append(str(m["title"]))
            elif isinstance(m, str):
                titles.append(m)
    if not titles:
        titles = [s.strip() for s in (ctx.label or "File, Edit, View").split(",")
                  if s.strip()]
    tx_ = ctx.x + 8
    for t in titles:
        tw = len(t) * 7 + 8
        if tx_ + tw > ctx.x + ctx.w - 6:
            break
        _tx(c, tx_, ctx.y + ctx.h / 2, t,
            fill=_fg(ctx), anchor="w", size=9)
        tx_ += tw + 8
def _render_generic(c, ctx):
    """A dashed rectangle with the label — untyped is still untyped, and its
    look must SAY 'not decided yet'."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=THEME["subtext"], dash=(4, 3))
    if ctx.tier >= 1:
        _tx(c, ctx.x + ctx.w / 2, ctx.y + ctx.h / 2,
            ctx.label or "?", fill=THEME["subtext"],
            anchor="center", size=9, width=max(20, ctx.w - 8))


#: kind -> renderer. The dispatch belongs WITH the renderers: it lived in
#: gui_canvas, so the Qt canvas fell back to _render_generic for every
#: shape and every widget drew as a plain box. Caught by rendering one.
RENDERERS: Dict[str, Any] = {
    "frame": _render_frame, "labelframe": _render_labelframe,
    "notebook": _render_notebook, "panedwindow": _render_panedwindow,
    "freeform": _render_freeform,
    "label": _render_label, "button": _render_button, "entry": _render_entry,
    "text": _render_text, "checkbutton": _render_checkbutton,
    "radiobutton": _render_radiobutton, "combobox": _render_combobox,
    "listbox": _render_listbox, "spinbox": _render_spinbox,
    "scale": _render_scale, "progressbar": _render_progressbar,
    "separator": _render_separator, "treeview": _render_treeview,
    "image_canvas": _render_image_canvas, "chart_panel": _render_chart_panel,
    "scrubber": _render_scrubber, "log_pane": _render_log_pane,
    "file_picker": _render_file_picker, "status_bar": _render_status_bar,
    "toolbar": _render_toolbar, "menubar": _render_menubar,
    GENERIC_KIND: _render_generic,
}
