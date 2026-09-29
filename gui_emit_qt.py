"""
gui_emit_qt.py — Spec -> PySide6 source. The Qt twin of gui_emit's Tk templates.

WHY A SEPARATE MODULE INSTEAD OF A SECOND BRANCH INSIDE gui_emit
----------------------------------------------------------------
gui_emit.py is where every other in-flight change lands — the props work alone
adds ~320 lines inside construct(), emit_main_ui() and WIDGETS_PY. A Qt target
written as `if target == "qt"` branches through those same functions would
conflict with all of it and would double the length of the file that is already
hardest to review. So the Qt templates live here, gui_emit keeps a small
`_backend()` dispatch, and the NEUTRAL helpers (_py, _c, _prop, _text_of,
_ordered, _region, handler_stub, emit_main_py, the region machinery) are
imported from gui_emit rather than copied. One copy of a rule, two backends.

WHAT IS THE SAME AS THE TK TARGET, ON PURPOSE
---------------------------------------------
Everything a user's code touches:

  * the ports API — get/set/on_change/enable/widget/clear, on_fire/fire,
    Ports.read/apply/[name]/RENAMED
  * MainUi.request_close / on_close / report_error / clear_ports, and
    COUNCIL_NO_DIALOGS
  * the stop protocol, including the literal `_watch_for_stop(self)` that
    gui_runner greps ui/main_ui.py for to decide whether Stop can be clean
  * handlers.py, which is append-only and toolkit-neutral already

so an existing handlers.py keeps working and the designer's Stop/Run/report
paths do not learn a second shape.

WHAT IS GENUINELY DIFFERENT, AND WHY
------------------------------------
* NO CLASSIC/THEMED SWAP. gui_emit swaps a ttk widget for a classic tk one when
  it carries colour, because the Windows "vista" ttk theme ignores a style
  background (measured 0.0% vs clam's 95.9%). Qt has no such split: a colour is
  a stylesheet on the widget. The stylesheet is scoped with an #objectName
  selector because a bare one cascades into children, which would silently
  recolour every descendant and stop the wireframe predicting the app.
* SIZE POLICY IS EMITTED, NOT INHERITED. Tk's `sticky` decides both alignment
  and stretch. Qt decides stretch from the widget's size policy, so sticky has
  to be translated into BOTH — a QPushButton is Minimum/Fixed by default and
  would sit at natural height in a cell Tk would have stretched.
* PADDING RIDES IN A NESTED LAYOUT. QGridLayout has no per-item margins at all.
  A widget with padx/pady is added inside a one-item QVBoxLayout carrying those
  margins; `addLayout` (not a wrapper QWidget) keeps the widget's parent — and
  therefore `self.<name>.parentWidget()` — exactly where the Tk version put it.
* NO .load ANYWHERE. `QPixmap.load(path)` is the natural Qt spelling and the
  project's own policy gate refuses it: `.load` is denied unless the receiver is
  in SAFE_LOAD_RECEIVERS, and a local variable name can never be in that set.
  The constructor form QPixmap(path) does the same job and passes.
* HANDLERS ARE CONNECTED, NOT PASSED. Tk hands `command=self.on_<name>` to the
  constructor. A Qt widget takes no command, so MainUi._build connects each
  COMMAND_KINDS widget's signal to on_<name> once every port exists (see
  _COMMAND_SIGNAL for which signal, and why it is not always the obvious one).
  Before this only a button with an event port ever reached its handler: a
  checkbox, radio, combobox, spinbox, scale, scrubber, file picker, toolbar,
  menu item, or a button whose port was switched off, did nothing at all.
* TWO QT CLASSES GAIN A SIGNAL. Where Tk's widget has a callback Qt's has no
  signal for, main_ui.py defines a subclass that adds it, and construct()
  names it: _SpinBox's `committed` is Tk's Spinbox command= (a step, not a
  keystroke), and _MenuBar's `fired` is the Toolbar's, so a menubar's port
  reaches its on_fire subscribers. Subclasses, so a stylesheet selector
  naming the Qt class, isinstance and findChildren all still match.
"""
from __future__ import annotations

import math
import textwrap
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import gui_colors as _gcol
from gui_emit import _c, _ordered, _prop, _py, _region, _text_of
from gui_shapes import PALETTE  # noqa: F401  (kept for parity with gui_emit)
from gui_spec import Spec, WidgetSpec

NAME = "qt"

# kind -> the composite class in ui/widgets.py. The NAMES must match the Tk
# backend's: they are the region ids in ui/widgets.py, and emit() reports a
# region whose id it does not recognise as orphaned.
COMPOSITE_KINDS = {
    "image_canvas": "ImageCanvas", "chart_panel": "ChartPanel",
    "scrubber": "Scrubber", "log_pane": "LogPane",
    "file_picker": "FilePicker", "status_bar": "StatusBar",
    "toolbar": "Toolbar",
}


# ============================================================
# Per-kind construction
# ============================================================

def _qt_align(anchor: str) -> str:
    """A Tk anchor as a Qt alignment expression."""
    a = (anchor or "w").lower()
    horiz = ("AlignLeft" if "w" in a else
             "AlignRight" if "e" in a else "AlignHCenter")
    vert = ("AlignTop" if "n" in a else
            "AlignBottom" if "s" in a else "AlignVCenter")
    return f"Qt.AlignmentFlag.{horiz} | Qt.AlignmentFlag.{vert}"


_RELIEF_FRAME = {
    "flat": ("NoFrame", "Plain"),
    "raised": ("Panel", "Raised"),
    "sunken": ("Panel", "Sunken"),
    "groove": ("Box", "Sunken"),
    "ridge": ("Box", "Raised"),
}


def construct(w: WidgetSpec, parent: str) -> str:
    """The right-hand side of `self.<name> = ...` for one widget."""
    k = w.kind
    if k in ("frame", "freeform", "generic"):
        return f"QFrame({parent})"
    if k == "labelframe":
        return f"QGroupBox({_py(_text_of(w))}, {parent})"
    if k == "notebook":
        return f"QTabWidget({parent})"
    if k == "panedwindow":
        orient = _prop(w, "orient", "horizontal")
        axis = "Horizontal" if orient == "horizontal" else "Vertical"
        return f"QSplitter(Qt.Orientation.{axis}, {parent})"
    if k == "label":
        return f"QLabel({_py(_text_of(w))}, {parent})"
    if k == "button":
        return f"QPushButton({_py(_text_of(w))}, {parent})"
    if k == "entry":
        return f"QLineEdit({parent})"
    if k == "text":
        return f"QPlainTextEdit({parent})"
    if k == "checkbutton":
        return f"QCheckBox({_py(_text_of(w))}, {parent})"
    if k == "radiobutton":
        return f"QRadioButton({_py(_text_of(w))}, {parent})"
    if k == "combobox":
        return f"QComboBox({parent})"
    if k == "listbox":
        return f"QListWidget({parent})"
    if k == "spinbox":
        return f"_SpinBox({parent})"    # QSpinBox + Tk's command= signal
    if k == "scale":
        orient = _prop(w, "orient", "horizontal")
        axis = "Horizontal" if orient == "horizontal" else "Vertical"
        return f"QSlider(Qt.Orientation.{axis}, {parent})"
    if k == "progressbar":
        return f"QProgressBar({parent})"
    if k == "separator":
        return f"QFrame({parent})"
    if k == "treeview":
        return f"QTreeWidget({parent})"
    if k == "menubar":
        return f"_MenuBar({parent})"    # QMenuBar + the Toolbar's `fired`
    # ---- composites (ui/widgets.py) ----
    if k == "image_canvas":
        return (f"ImageCanvas({parent}, "
                f"overlay={_py(bool(_prop(w, 'overlay', False)))}, "
                f"overlay_alpha={_py(float(_prop(w, 'overlay_alpha', 0.5)))}, "
                f"roi={_py(bool(_prop(w, 'roi', False)))}, "
                f"zoom_to_fit={_py(bool(_prop(w, 'zoom_to_fit', True)))})")
    if k == "chart_panel":
        return (f"ChartPanel({parent}, "
                f"toolbar={_py(bool(_prop(w, 'toolbar', False)))}, "
                f"tight_layout={_py(bool(_prop(w, 'tight_layout', True)))})")
    if k == "scrubber":
        return (f"Scrubber({parent}, from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))}, "
                f"show_total={_py(bool(_prop(w, 'show_total', True)))})")
    if k == "log_pane":
        return (f"LogPane({parent}, "
                f"autoscroll={_py(bool(_prop(w, 'autoscroll', True)))}, "
                f"levels={_py(list(_prop(w, 'levels', []) or []))})")
    if k == "file_picker":
        return (f"FilePicker({parent}, mode={_py(_prop(w, 'mode', 'file'))}, "
                f"filetypes={_py(list(_prop(w, 'filetypes', []) or []))})")
    if k == "status_bar":
        return (f"StatusBar({parent}, "
                f"progress={_py(bool(_prop(w, 'progress', False)))})")
    if k == "toolbar":
        return (f"Toolbar({parent}, "
                f"buttons={_py(list(_prop(w, 'buttons', []) or []))})")
    return f"QFrame({parent})"    # unreachable: gui_spec.validate gates kinds


def configure_lines(w: WidgetSpec, ind: str) -> List[str]:
    """Property calls that follow construction.

    Qt sets most of what Tk passes to a constructor through a method instead,
    and EVERY prop the inspector offers has to land somewhere or it is a
    control that silently does nothing (tests/test_gui_props.py).
    """
    k, n = w.kind, w.name
    L: List[str] = []
    ref = f"self.{n}"

    if k in ("frame", "freeform", "generic"):
        relief = str(_prop(w, "relief", "flat") or "flat")
        shape, shadow = _RELIEF_FRAME.get(relief, ("NoFrame", "Plain"))
        bw = int(_prop(w, "borderwidth", 0) or 0)
        if relief != "flat" and bw <= 0:
            # A relief with no width draws nothing — same rule the Tk target
            # applies, so the two toolkits agree about what a border looks like.
            bw = 2
        L.append(f"{ind}{ref}.setFrameShape(QFrame.Shape.{shape})")
        L.append(f"{ind}{ref}.setFrameShadow(QFrame.Shadow.{shadow})")
        if bw:
            L.append(f"{ind}{ref}.setLineWidth({bw})")
    elif k == "labelframe":
        # Qt draws a group box title at the top; Tk's labelanchor can also put
        # it at the bottom or mid-edge. Only the horizontal half is honest here.
        anchor = str(_prop(w, "labelanchor", "nw") or "nw")
        if "e" in anchor and "w" not in anchor:
            L.append(f"{ind}{ref}.setAlignment(Qt.AlignmentFlag.AlignRight)")
        elif anchor in ("n", "s"):
            L.append(f"{ind}{ref}.setAlignment(Qt.AlignmentFlag.AlignHCenter)")
    elif k == "label":
        L.append(f"{ind}{ref}.setAlignment({_qt_align(_prop(w, 'anchor', 'w'))})")
        try:
            wrap = int(_prop(w, "wraplength", 0) or 0)
        except (TypeError, ValueError):
            wrap = 0
        if wrap > 0:
            # Tk wraps at a pixel width; Qt wraps at the widget's width, so the
            # maximum width is what makes the two agree about where it breaks.
            L.append(f"{ind}{ref}.setWordWrap(True)")
            L.append(f"{ind}{ref}.setMaximumWidth({wrap})")
    elif k == "button":
        if str(_prop(w, "state", "normal")) == "disabled":
            # How the app STARTS; ports.<name>.enable(True) lets it be pressed
            # once whatever it waits for is ready.
            L.append(f"{ind}{ref}.setEnabled(False)")
    elif k == "entry":
        ph = str(_prop(w, "placeholder", "") or "")
        if ph:
            L.append(f"{ind}{ref}.setPlaceholderText({_py(ph)})")
        if str(_prop(w, "show", "") or ""):
            L.append(f"{ind}{ref}.setEchoMode(QLineEdit.EchoMode.Password)")
        just = str(_prop(w, "justify", "left") or "left")
        if just != "left":
            align = _qt_align("e" if just == "right" else "center")
            L.append(f"{ind}{ref}.setAlignment({align})")
    elif k == "text":
        if str(_prop(w, "wrap", "word")) == "none":
            L.append(f"{ind}{ref}.setLineWrapMode("
                     f"QPlainTextEdit.LineWrapMode.NoWrap)")
        if bool(_prop(w, "readonly", False)):
            L.append(f"{ind}{ref}.setReadOnly(True)")
    elif k == "checkbutton":
        if bool(_prop(w, "default", False)):
            L.append(f"{ind}{ref}.setChecked(True)")
    elif k == "combobox":
        values = list(_prop(w, "values", []) or [])
        if values:
            L.append(f"{ind}{ref}.addItems({_py(values)})")
        if not bool(_prop(w, "readonly", True)):
            L.append(f"{ind}{ref}.setEditable(True)")
    elif k == "listbox":
        mode = str(_prop(w, "selectmode", "browse") or "browse")
        qmode = {"browse": "SingleSelection", "single": "SingleSelection",
                 "multiple": "MultiSelection",
                 "extended": "ExtendedSelection"}.get(mode, "SingleSelection")
        L.append(f"{ind}{ref}.setSelectionMode("
                 f"QAbstractItemView.SelectionMode.{qmode})")
    elif k in ("spinbox", "scale"):
        # QSpinBox and QSlider hold WHOLE numbers; Tk's Spinbox and Scale take
        # fractions. A fractional bound is truncated toward zero, as int()
        # always did here, and the generated file now says so on the line
        # before — a range that quietly moved is found by reading, not by
        # dragging. (QDoubleSpinBox would keep the fraction, but the spinbox
        # port writes int() and the stylesheet selector names QSpinBox, so
        # that is a widget swap across three places, not a fix to this one.)
        # A bound that is not a number at all used to kill emit with
        # ValueError; it now falls back to the DEFAULT the inspector shows
        # (from_ 0, to 100) and says so. `to` once fell back to 0 instead:
        # setRange(0, 0), a widget that cannot move, under a comment saying
        # the default was used.
        cls = "QSpinBox" if k == "spinbox" else "QSlider"
        lo, why_lo = _whole(_prop(w, "from_", 0), 0, cls)
        hi, why_hi = _whole(_prop(w, "to", 100), 100, cls)
        for key, why in (("from_", why_lo), ("to", why_hi)):
            if why:
                L += _note(ind, f"{key}: {why}")
        L.append(f"{ind}{ref}.setRange({lo}, {hi})")
        if k == "spinbox":
            step, why = _whole(_prop(w, "increment", 1), 1, cls)
            if not step:
                # An increment of 0.1 truncates to 0, and setSingleStep(0)
                # leaves arrows that do nothing at all. 1 is the smallest
                # step a QSpinBox can take, so it is the honest nearest one.
                dead = "a step of 0 would leave the arrows dead, so it is 1"
                step, why = 1, (f"{why}; {dead}" if why else dead)
            if why:
                L += _note(ind, f"increment: {why}")
            if step != 1:
                L.append(f"{ind}{ref}.setSingleStep({step})")
    elif k == "progressbar":
        if str(_prop(w, "mode", "determinate")) == "indeterminate":
            # Qt's busy indicator IS an empty range.
            L.append(f"{ind}{ref}.setRange(0, 0)")
        else:
            L.append(f"{ind}{ref}.setRange(0, 100)")
        if str(_prop(w, "orient", "horizontal")) == "vertical":
            L.append(f"{ind}{ref}.setOrientation(Qt.Orientation.Vertical)")
    elif k == "separator":
        axis = ("HLine" if str(_prop(w, "orient", "horizontal")) == "horizontal"
                else "VLine")
        L.append(f"{ind}{ref}.setFrameShape(QFrame.Shape.{axis})")
        L.append(f"{ind}{ref}.setFrameShadow(QFrame.Shadow.Sunken)")
    elif k == "treeview":
        cols = [str(c) for c in (_prop(w, "columns", []) or [])]
        L.append(f"{ind}{ref}.setColumnCount({max(1, len(cols))})")
        if cols:
            L.append(f"{ind}{ref}.setHeaderLabels({_py(cols)})")
        if not bool(_prop(w, "show_headings", True)):
            L.append(f"{ind}{ref}.setHeaderHidden(True)")
        if str(_prop(w, "mode", "table")) == "table":
            L.append(f"{ind}{ref}.setRootIsDecorated(False)")
    elif k == "menubar":
        for line in _menu_lines(w, ind, ref):
            L.append(line)

    # Colour and font, for every kind that can carry them.
    L.extend(_style_lines(w, ind, ref))
    return L


def _note(ind: str, text: str) -> List[str]:
    """``text`` as comment lines at ``ind``, wrapped to 79 columns the way
    grid_lines wraps its notes. Never inside a word or at a hyphen, so a
    number or "32-bit" is always read whole."""
    return [f"{ind}# {line}" for line in textwrap.wrap(
        text, 79 - len(ind) - 2, break_long_words=False,
        break_on_hyphens=False)]


#: The range of the C int a QSpinBox or QSlider holds. PySide6 refuses a
#: Python int past it, so a bound of 1e12 — a byte count, say — emitted as
#: setRange(0, 1000000000000) killed _build with OverflowError (measured,
#: PySide6 6.10.2: "libshiboken: Overflow ... exceeds limits of type [signed]
#: int (4bytes)") and the window never opened.
_INT32 = (-2 ** 31, 2 ** 31 - 1)


def _whole(value: Any, fallback: int, cls: str) -> Tuple[int, str]:
    """(a numeric prop as a whole number a ``cls`` can hold, why it is not
    the number given — a whole sentence for the generated file, or "").

    The reason is "" when the value was already a whole number in range, so
    such a prop emits exactly what it always did. A prop that is not there
    (None, blank) is the default, silently; a 0 IS there — `to` of 0 is a
    real bound, and reading it as missing would turn -10..0 into -10..100. A
    fraction truncates toward zero, as int() always did. A non-number falls
    back to the default instead of raising out of emit, and a number past
    32 bits is clamped instead of raising out of _build.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback, ""
    if isinstance(value, int):         # bool included, as int(True) was
        n, why = int(value), ""
    else:
        try:
            n, why = int(str(value).strip()), ""
        except ValueError:
            try:
                f = float(value)
            except (TypeError, ValueError):
                return fallback, (f"{value!r} is not a number, so {fallback} "
                                  f"is used")
            if math.isnan(f) or math.isinf(f):
                return fallback, (f"{value!r} is not a finite number, so "
                                  f"{fallback} is used")
            n = int(f)
            why = ("" if n == f else
                   f"{value!r} is truncated to {n} — {cls} holds whole "
                   f"numbers")
    lo, hi = _INT32
    if not lo <= n <= hi:
        n = max(lo, min(hi, n))
        why = (f"{value!r} is past the 32-bit int a {cls} holds, so {n} is "
               f"used — the number itself raises OverflowError in _build")
    return n, why


# ============================================================
# Menus
# ============================================================
#
# `menus` is typed "tree" in gui_shapes and nothing between a hand-edited .gspec
# and here checks its shape — gui_describe insists on [{"title", "items"}], but
# only for a model-authored description. The designer's own canvas
# (designer_paint._render_menubar) happily draws a plain list of titles, so a
# wireframe can look right and still carry one. Measured before this existed:
# menus=["File", "Edit"] killed emit with "AttributeError: 'str' object has no
# attribute 'get'", and the old code imported gui_shapes.menu_tree, which has
# never existed, so its try/except hid that the "normalised" path never ran.

#: A node in the normalised tree: "-" is a separator, a str is an item, and a
#: (title, children) pair is a submenu.
_MenuNode = Union[str, Tuple[str, list]]

_MENU_TITLE_KEYS = ("title", "label", "name", "text")

#: Keys that say what a menu dict IS, so a one-key dict holding one of them
#: is never read as {title: items}. That reading used to come first: measured,
#: {"title": ""} became a menu called "title", {"items": [...]} one called
#: "items", and {"separator": True} / {"type": "separator"} submenus called
#: "separator" and "type". Exact keys, as _menu_title reads them — {"Type":
#: [...]} is still a menu someone called Type.
_MENU_KEYS = frozenset(_MENU_TITLE_KEYS) | {"items", "separator", "type"}


def _menu_title(d: Dict[str, Any]) -> str:
    for key in _MENU_TITLE_KEYS:
        if d.get(key):
            return str(d[key])
    return ""


def _is_menu_rule(d: Dict[str, Any]) -> bool:
    """{"separator": True} or {"type": "separator"} — Tk's add_separator."""
    return (bool(d.get("separator"))
            or str(d.get("type") or "").strip().lower() == "separator")


def _titled(d: Dict[str, Any]) -> Optional[Tuple[str, Any]]:
    """{"Recent": ["a.txt"]} as ("Recent", ["a.txt"]) — a menu named by its
    only key — or None when that key is blank or says what the dict is."""
    if len(d) != 1:
        return None
    (key, sub), = d.items()
    title = str(key).strip()
    if not title or key in _MENU_KEYS:
        return None
    return title, sub


def _menu_children(raw: Any) -> List[_MenuNode]:
    """The items under one menu, normalised."""
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out: List[_MenuNode] = []
    for item in list(raw) if isinstance(raw, (list, tuple)) else []:
        if item is None:
            continue
        if isinstance(item, dict):
            title = _menu_title(item)
            titled = None if title else _titled(item)
            if _is_menu_rule(item):
                out.append("-")
            elif titled is not None:
                out.append((titled[0], _menu_children(titled[1])))
            elif "items" in item:
                out.append((title or "Menu", _menu_children(item["items"])))
            elif title.strip():
                out.append("-" if set(title.strip()) == {"-"} else title)
            continue
        if isinstance(item, (list, tuple, set)):
            continue                   # no reading of a bare nested list
        label = str(item).strip()
        if not label:
            continue
        # "-" and "---" both read as a rule to whoever typed them.
        out.append("-" if set(label) == {"-"} else label)
    return out


def _menu_tree(menus: Any) -> List[Tuple[str, List[_MenuNode]]]:
    """A menubar's `menus` prop as [(title, children)], whatever form it took.

    Accepted: the canonical [{"title": str, "items": [...]}]; a plain list of
    titles, each a top-level menu with no items yet; a {title: [items]}
    mapping (the "{menu: [items]} tree" gui_shapes' catalogue describes); and
    a comma-separated string of titles, the spelling the designer's canvas
    reads a menubar's label in. Anything unreadable is skipped rather than
    fatal — a menubar that shows fewer menus is still a window that opens.
    """
    if not menus:
        return []
    if isinstance(menus, str):
        menus = [t.strip() for t in menus.split(",") if t.strip()]
    if isinstance(menus, dict):
        menus = [{"title": k, "items": v} for k, v in menus.items()]
    out: List[Tuple[str, List[_MenuNode]]] = []
    for i, menu in enumerate(list(menus) if isinstance(menus, (list, tuple))
                             else []):
        if isinstance(menu, dict):
            if _is_menu_rule(menu):
                # A rule BETWEEN menus: the designer's canvas draws nothing
                # for it, and a menu bar is not a menu to rule off.
                continue
            title = _menu_title(menu)
            titled = None if title else _titled(menu)
            if titled is not None:
                out.append((titled[0], _menu_children(titled[1])))
                continue
            out.append((title or f"Menu {i + 1}",
                        _menu_children(menu.get("items"))))
        elif isinstance(menu, (str, int, float)) and str(menu).strip():
            out.append((str(menu).strip(), []))
    return out


def _menu_lines(w: WidgetSpec, ind: str, ref: str) -> List[str]:
    """A menubar's declared tree as addMenu/addAction calls.

    Every leaf is added through _MenuBar.item, which makes choosing it fire
    the bar's ``fired`` signal with its label — the Toolbar's shape. From
    there it reaches ONE handler, on_menu(label), connected in _handler_lines
    after the ports exist, and every ports.<menubar>.on_fire subscriber,
    through the _EventPort that connects to ``fired``. The leaves used to
    connect straight to _command(self, "on_menu", label), which reached the
    handler but no subscriber: QMenuBar has no clicked and no fired, so the
    port had nothing to connect to (measured: on_fire never ran). Before
    that, `self.on_menu(_n)` named a method nothing defined, and every menu
    click raised AttributeError; MainUi now defines an on_menu stub whenever
    the window has a menubar, the way it always defined on_toolbar.
    """
    out: List[str] = []

    def items(var: str, children: List[_MenuNode]) -> None:
        for j, node in enumerate(children):
            if isinstance(node, tuple):
                title, sub = node
                child = f"{var}_{j}"
                out.append(f"{ind}{child} = {var}.addMenu({_py(title)})")
                items(child, sub)
            elif node == "-":
                out.append(f"{ind}{var}.addSeparator()")
            else:
                out.append(f"{ind}{ref}.item({var}, {_py(node)})")

    for i, (title, children) in enumerate(_menu_tree(_prop(w, "menus", []))):
        if not children:
            # A title with nothing under it yet: a menu that opens empty,
            # which is what the wireframe says. No variable, or it is unused.
            out.append(f"{ind}{ref}.addMenu({_py(title)})")
            continue
        var = f"_menu_{i}"
        out.append(f"{ind}{var} = {ref}.addMenu({_py(title)})")
        items(var, children)
    return out


def _style_lines(w: WidgetSpec, ind: str, ref: str) -> List[str]:
    """Colour and font for one widget.

    Colour goes through a stylesheet scoped by objectName. An unscoped
    stylesheet cascades into children, so a coloured frame would repaint every
    descendant's text and override the per-widget answer gui_colors.resolve_scene
    already computed — the wireframe would stop predicting the app.

    A widget the user did not colour gets NO stylesheet at all, so it keeps the
    native Windows look, which is the same honesty property the Tk target has.
    """
    out: List[str] = []
    cap = _gcol.caps(w.kind)
    bits: List[str] = []
    bg = getattr(w, "bg", "") or ""
    fg = getattr(w, "fg", "") or ""
    if cap:
        if bg and "bg" in cap:
            try:
                bits.append(f"background-color: {_gcol.normalise(bg)};")
            except ValueError:
                pass
        if fg and "fg" in cap:
            try:
                bits.append(f"color: {_gcol.normalise(fg)};")
            except ValueError:
                pass
    if bits:
        cls = _qt_class_of(w.kind)
        css = cls + "#" + w.name + " { " + " ".join(bits) + " }"
        out.append(f"{ind}{ref}.setObjectName({_py(w.name)})")
        out.append(f"{ind}{ref}.setStyleSheet({_py(css)})")
    font = str(getattr(w, "font", "") or "").strip()
    if font and _gcol.can_font(w.kind):
        out.append(f"{ind}{ref}.setFont(_font({_py(font)}))")
    return out


def _qt_class_of(kind: str) -> str:
    """The Qt class name a stylesheet selector must name for this kind."""
    return {
        "frame": "QFrame", "freeform": "QFrame", "generic": "QFrame",
        "labelframe": "QGroupBox", "notebook": "QTabWidget",
        "panedwindow": "QSplitter", "label": "QLabel", "button": "QPushButton",
        "entry": "QLineEdit", "text": "QPlainTextEdit",
        "checkbutton": "QCheckBox", "radiobutton": "QRadioButton",
        "combobox": "QComboBox", "listbox": "QListWidget",
        "spinbox": "QSpinBox", "scale": "QSlider",
        "progressbar": "QProgressBar", "separator": "QFrame",
        "treeview": "QTreeWidget", "menubar": "QMenuBar",
    }.get(kind, COMPOSITE_KINDS.get(kind, "QWidget"))


# ============================================================
# Layout
# ============================================================

def place_call(w: WidgetSpec, parent_layout: str) -> str:
    """The layout call for one widget, as Tk's geometry manager would place it.

    Every parent layout this is handed is a QGridLayout — `_root`, or the
    `_lay_<name>` each container gets; notebook and panedwindow children never
    reach here. So a pack has to be a grid cell too (see _pack).
    """
    if w.manager == "pack":
        return f"_pack({parent_layout}, self.{w.name}, {w.padx}, {w.pady})"
    if w.manager == "place":
        # Tk's place has no Qt equivalent: _Rel keeps the child at a fraction of
        # its parent's size by following the parent's resize events.
        return (f"_place(self.{w.name}, {w.relx}, {w.rely}, "
                f"{w.relwidth}, {w.relheight})")
    return (f"_cell({parent_layout}, self.{w.name}, {w.row}, {w.column}, "
            f"{w.rowspan}, {w.columnspan}, {_py(w.sticky)}, "
            f"{w.padx}, {w.pady})")


def _request_call(made: str, width: int, height: int) -> str:
    """construct()'s ``Class(args)``, built through the generated _requested
    helper so the widget ASKS for ``width`` x ``height`` rather than being
    held at least that big. Only an empty container gets here, and every
    container kind constructs as a plain ``Class(args)`` call."""
    cls, args = made.split("(", 1)
    return f"_requested({width}, {height}, {cls}, {args}"


def _grid_config(target: str, rows: Sequence[int], cols: Sequence[int],
                 row_min: Sequence[int], col_min: Sequence[int],
                 indent: str) -> List[str]:
    """Row/column stretch and minimum size for one layout.

    Emitted even when a weight is 0, for the same reason the Tk target does it:
    stating the grid makes the generated file a readable record of the inferred
    layout rather than something the reader reconstructs.
    """
    out: List[str] = []
    for i, wgt in enumerate(rows):
        out.append(f"{indent}{target}.setRowStretch({i}, {wgt})")
        ms = row_min[i] if i < len(row_min) else 0
        if ms:
            out.append(f"{indent}{target}.setRowMinimumHeight({i}, {ms})")
    for i, wgt in enumerate(cols):
        out.append(f"{indent}{target}.setColumnStretch({i}, {wgt})")
        ms = col_min[i] if i < len(col_min) else 0
        if ms:
            out.append(f"{indent}{target}.setColumnMinimumWidth({i}, {ms})")
    return out


def _window_menu_bar(spec: Spec) -> Optional[WidgetSpec]:
    """The menubar emit_main_ui hands to setMenuBar, or None: the first in
    build order drawn on the WINDOW itself, with no container around it.

    A menubar drawn inside a panel belongs to the panel, and setMenuBar would
    lift it out to the top of the window. It used to be taken anyway when it
    came first in build order — a panel is built before a menubar drawn on
    the window after it — and the window's own menubar then went into a grid
    cell as "A SECOND menu bar" (measured). A menubar in any container is now
    laid out in that container, as a tab page's and a pane's always were.
    """
    for w in _ordered(spec):
        if w.kind == "menubar" and not w.parent:
            return w
    return None


def _fills(sticky: str, axis: str) -> bool:
    """Whether ``sticky`` stretches a widget along ``axis`` ("row" is down,
    "col" across) — the reading _sticky_policy gives it at run time, and the
    one gui_layout's _sticky_for writes from the resolved resize."""
    s = (sticky or "").lower()
    ends = "ns" if axis == "row" else "we"
    return ends[0] in s and ends[1] in s


def _lift_menu_bar(spec: Spec, bar: WidgetSpec, rows: Sequence[int],
                   cols: Sequence[int], row_min: Sequence[int],
                   col_min: Sequence[int]):
    """The grid ``bar`` was drawn in, once setMenuBar has lifted it out:
    (rows, cols, row_min, col_min, notes for the generated file).

    gui_layout lays the menubar out as a cell, which is what the drawing
    shows, and weights its bands with it. setMenuBar then takes it OUT of the
    grid, and the stretch it brought stays behind: measured on
    qt_tests/c_menubar_dicts, the two columns only the menubar spanned became
    EMPTY elastic columns either side of the text, and the text got 356 of
    1100 px; with those stretches at 0 it gets 1074. On c_menubar_titles the
    menubar's own row held the only row stretch, and the label drawn at y=48
    sat at y=611; it now sits at y=60. So the stretch is worked
    out again from what is still in the grid, by gui_layout's own rule (a
    band stretches when a widget spanning it fills along that axis), and a
    band only the menubar held keeps neither stretch nor minimum — its
    minimum was the menubar's own.

    gui_layout never leaves an axis with no stretch, and neither does this:
    when nothing left fills along one, the slack goes to one EMPTY band past
    the last, which is where Tk's grid (anchored nw) leaves it. Measured with
    every stretch 0 instead, Qt spread the widgets across the window. The
    widest band — gui_layout's own choice — cannot be picked here: the spec
    keeps no band sizes.
    """
    others = [w for w in spec.widgets
              if w.parent == bar.parent and w.name != bar.name
              and w.manager == "grid"]
    got: Dict[str, Tuple[List[int], List[int]]] = {}
    slack: List[str] = []             # the axes given a band past the last
    for axis, weights, mins in (("row", rows, row_min),
                                ("col", cols, col_min)):
        first = bar.row if axis == "row" else bar.column
        span = bar.rowspan if axis == "row" else bar.columnspan
        held, elastic = set(), set()
        for w in others:
            at = w.row if axis == "row" else w.column
            band = range(at, at + (w.rowspan if axis == "row"
                                   else w.columnspan))
            held.update(band)
            if _fills(w.sticky, axis):
                elastic.update(band)
        emptied = set(range(first, first + span)) - held
        # Along an axis the menubar filled, every band it spanned was made
        # elastic by it; along any other, only an emptied band loses stretch.
        keep = (elastic if _fills(bar.sticky, axis)
                else set(range(len(weights))) - emptied)
        new = [wgt if i in keep else 0 for i, wgt in enumerate(weights)]
        if not any(new):
            new.append(1)
            slack.append(axis)
        got[axis] = (new, [0 if i in emptied else m
                           for i, m in enumerate(mins)])
    notes = [f"{bar.name} is window chrome (setMenuBar below), not a cell: "
             f"the stretch its bands had is left out, or they would sit "
             f"empty and take space from the widgets."]
    if slack:
        ways = " or ".join({"row": "down", "col": "across"}[a] for a in slack)
        bands = " and ".join({"row": "row", "col": "column"}[a]
                             for a in slack)
        notes.append(f"Nothing left in this grid stretches {ways}, so the "
                     f"slack goes to an empty {bands} after the last, and "
                     f"every widget stays where it was drawn.")
    return got["row"][0], got["col"][0], got["row"][1], got["col"][1], notes


# ============================================================
# ui/main_ui.py
# ============================================================

STOP_WATCHER = '''

def _watch_for_stop(ui) -> None:
    """Close cleanly when the GUI Designer asks, or when it goes away.

    Active only when the designer launched this app (it sets
    COUNCIL_PREVIEW_CONTROL=stdin). The designer writes "stop" on stdin to ask
    for a clean close; if the designer exits or crashes, stdin reaches
    end-of-file and the app closes the same way. Either way on_close runs, so
    a camera app gets to stop its grab and close the device.

    The reader thread never touches Qt — a widget touched from a non-GUI thread
    is undefined behaviour, exactly as it is in Tk. It only sets a flag, which a
    QTimer on the GUI thread polls. It reads the raw file descriptor rather than
    sys.stdin: a thread parked inside sys.stdin's buffered reader when the
    window is closed by its X can abort the interpreter at shutdown.
    """
    import os
    import sys
    import threading

    from PySide6.QtCore import QTimer
    if os.environ.get("COUNCIL_PREVIEW_CONTROL") != "stdin" or sys.stdin is None:
        return
    asked = threading.Event()
    gone = []

    def _read():
        seen = b""
        try:
            fd = sys.stdin.fileno()
            while True:
                chunk = os.read(fd, 64)
                if not chunk:
                    gone.append(True)  # end-of-file: the designer is gone
                    break
                seen = (seen + chunk)[-64:]
                if b"stop" in seen.lower():
                    break
        except Exception:
            pass
        asked.set()

    threading.Thread(target=_read, name="stop-watcher", daemon=True).start()

    timer = QTimer(ui)
    ui._stop_timer = timer          # a QTimer with no reference is collected

    def _poll():
        if not asked.is_set():
            return
        timer.stop()
        if gone:
            # Nobody reads this app's output any more, and on Windows every
            # write to the orphaned pipe raises OSError — so the first print()
            # in on_close would abort the very cleanup it was reporting on.
            try:
                sys.stdout = sys.stderr = open(os.devnull, "w")
            except OSError:
                pass
        ui.request_close()

    timer.timeout.connect(_poll)
    timer.start(150)
'''


LAYOUT_HELPERS = '''

def _font(spec: str):
    """A Tk font string ("Magneto 18 bold") as a QFont.

    The wireframe stores Tk's own font format because that is what the designer
    edits; parsing it here keeps the .gspec toolkit-neutral. A negative size is
    Tk's spelling of "pixels, not points".
    """
    from PySide6.QtGui import QFont
    parts = str(spec or "").split()
    family, size, styles = [], 0, []
    for token in parts:
        low = token.lower()
        if low in ("bold", "italic", "underline", "overstrike", "roman",
                   "normal"):
            styles.append(low)
            continue
        try:
            size = int(token)
            continue
        except ValueError:
            pass
        family.append(token)
    font = QFont(" ".join(family) or "Segoe UI")
    if size < 0:
        font.setPixelSize(-size)
    elif size:
        font.setPointSize(size)
    font.setBold("bold" in styles)
    font.setItalic("italic" in styles)
    font.setUnderline("underline" in styles)
    font.setStrikeOut("overstrike" in styles)
    return font


def _sticky_policy(widget, sticky: str):
    """Tk's sticky decides alignment AND stretch; Qt takes stretch from the
    widget's size policy, so both have to be set.

    Without this a QPushButton in a stretched cell keeps its natural height
    (it is Minimum/Fixed by default) where Tk would have filled the cell.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSizePolicy
    s = (sticky or "").lower()
    fill_x = "w" in s and "e" in s
    fill_y = "n" in s and "s" in s
    policy = widget.sizePolicy()
    if fill_x:
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
    if fill_y:
        policy.setVerticalPolicy(QSizePolicy.Policy.Expanding)
    widget.setSizePolicy(policy)
    align = Qt.AlignmentFlag(0)
    if not fill_x:
        align |= (Qt.AlignmentFlag.AlignLeft if "w" in s else
                  Qt.AlignmentFlag.AlignRight if "e" in s else
                  Qt.AlignmentFlag.AlignHCenter)
    if not fill_y:
        align |= (Qt.AlignmentFlag.AlignTop if "n" in s else
                  Qt.AlignmentFlag.AlignBottom if "s" in s else
                  Qt.AlignmentFlag.AlignVCenter)
    return align


def _cell(grid, widget, row, column, rowspan, columnspan, sticky, padx, pady):
    """Place one widget the way Tk's grid would.

    QGridLayout has no per-item margins, so a widget with padding is added
    inside a one-item QVBoxLayout that carries them. addLayout rather than a
    wrapper QWidget on purpose: a layout does not reparent, so
    self.<name>.parentWidget() stays the container the spec named.
    """
    from PySide6.QtWidgets import QVBoxLayout
    align = _sticky_policy(widget, sticky)
    if padx or pady:
        box = QVBoxLayout()
        box.setContentsMargins(padx, pady, padx, pady)
        box.addWidget(widget, 0, align)
        grid.addLayout(box, row, column, rowspan, columnspan)
    else:
        grid.addWidget(widget, row, column, rowspan, columnspan, align)


def _pack(grid, widget, padx, pady):
    """Tk's pack(fill="both", expand=True): a container's ONLY child.

    gui_layout packs a sole child rather than gridding it — the root window's
    one shape, or the one widget inside a frame, labelframe or freeform area.
    Every layout here is a QGridLayout, which has no pack. The old emission
    called addWidget(w, 1) / addLayout(inner, 1), overloads only a box layout
    has; measured: a window holding one frame with one label died in _build
    with "QGridLayout.addLayout(): not enough arguments". One cell that fills
    AND stretches is what pack(fill="both", expand=True) means.
    """
    grid.setRowStretch(0, 1)
    grid.setColumnStretch(0, 1)
    _cell(grid, widget, 0, 0, 1, 1, "nsew", padx, pady)


def _requested(width, height, cls, *args):
    """``cls(*args)`` asking for ``width`` x ``height``: Tk's
    configure(width=, height=) on an EMPTY container, which would otherwise
    collapse to nothing and lose the region that was drawn.

    Tk's is a REQUEST — the window opens at it where there is room, and the
    grid shrinks it where there is not. setMinimumSize, the spelling this
    replaces, is a FLOOR: measured, one empty notebook drawn 544x360 held its
    window at 1100x780 when it was resized to the 1100x700 it was drawn on.
    Qt's request is sizeHint(), which has no setter, only an answer — so the
    widget is made from a subclass of its own class that answers it. Its
    minimum stays the class's own, so the window can still shrink, and a
    stylesheet selector naming the Qt class still matches it.
    """
    from PySide6.QtCore import QSize

    class _Requesting(cls):
        def sizeHint(self):
            return QSize(width, height)

    return _Requesting(*args)


def _command(ui, handler, *fixed):
    """A slot that runs ui.<handler> — Tk's command=self.on_<name>, for Qt.

    LATE LOOKUP, for the reason _EventPort uses it: App(HandlerMixin, MainUi)
    puts the handlers.py body ahead of MainUi's stub, and resolving the name
    at the moment of the click also lets an override made after construction
    win. A name that resolves to nothing is a click that does nothing, never
    an AttributeError — the menubar used to call self.on_menu directly, which
    nothing defined, so every menu click raised.

    ``fixed`` replaces the signal's own arguments: a menu item's triggered
    carries a checked flag, and the handler wants the item's label.

    An exception from the handler is deliberately NOT caught here. PySide6
    prints the full traceback to stderr — the designer's log — and keeps the
    window running, which is what Tk's report_callback_exception does for a
    command=. Measured on PySide6 6.10.2: a slot that raised printed its
    traceback and click() returned normally. Catching it would only trade the
    traceback for a one-line repr.
    """
    def _slot(*args):
        method = getattr(ui, handler, None)
        if callable(method):
            method(*(fixed or args))
    return _slot


def _place(widget, relx, rely, relwidth, relheight):
    """Tk's place(), which Qt has no manager for.

    The child follows its parent's size through an event filter rather than a
    layout, which is what relx/relwidth mean: a fraction of the parent, kept
    across every resize.
    """
    from PySide6.QtCore import QEvent, QObject

    parent = widget.parentWidget()
    if parent is None:
        return

    def _apply():
        w = parent.width()
        h = parent.height()
        widget.setGeometry(int(relx * w), int(rely * h),
                           int((relwidth or 1.0) * w),
                           int((relheight or 1.0) * h))

    class _Follow(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Resize:
                _apply()
            return False

    follower = _Follow(parent)
    parent.installEventFilter(follower)
    if not hasattr(parent, "_placed"):
        parent._placed = []
    parent._placed.append(follower)     # keep it alive with the parent
    _apply()
    widget.show()
'''


# Qt classes given the one signal Tk's version of the widget has and Qt's has
# not. Subclasses, not wrappers: a QSS type selector matches a subclass (Qt
# walks the metaobject chain), so the `QSpinBox#name` / `QMenuBar#name` rule
# _style_lines writes still colours them — measured on the palette the rule
# sets, which a selector naming another class leaves alone — and isinstance,
# findChildren and layout().menuBar() see the class they always saw.

SPIN_BOX = '''

class _SpinBox(QSpinBox):
    """A QSpinBox with the signal Tk's Spinbox command= fires on.

    Tk runs command= for an arrow and never for a keystroke. QSpinBox has no
    such signal: valueChanged fires per KEY while a number is typed (keyboard
    tracking — measured, typing 12 is valueChanged(1), then (12)), on every
    setValue, and once more on Return. So ``committed`` fires for a step —
    an arrow, Up/Down, PageUp/PageDown, the wheel, stepUp() — and for a typed
    value once it is committed, by Return or by leaving the box, if it
    differs from the value before typing began. MainUi connects on_<name> to
    it. valueChanged is untouched, so the port's on_change still follows
    every keystroke and every write, as it always has.
    """

    committed = Signal(int)

    def __init__(self, *args):
        super().__init__(*args)
        # _settled: the value before typing began. _seen: the last value.
        self._settled = self._seen = self.value()
        self.valueChanged.connect(self._follow)
        self.editingFinished.connect(self._commit_typed)

    def stepBy(self, steps):
        super().stepBy(steps)
        self._settled = self.value()
        self.committed.emit(self._settled)

    def _follow(self, value):
        # A write that did not come from typing moves _settled. Typing
        # leaves the line edit modified until the value is committed;
        # setValue, setRange's clamp and a step rewrite the edit, which
        # clears that (measured, PySide6 6.10.2). Return re-emits the value
        # typing already reached with the edit no longer modified, so only a
        # CHANGE counts — or that echo would settle the typed value before
        # editingFinished could see that it moved.
        if value != self._seen and not self.lineEdit().isModified():
            self._settled = value
        self._seen = value

    def _commit_typed(self):
        # editingFinished: Return, and EVERY focus loss — so it is compared.
        if self.value() != self._settled:
            self._settled = self.value()
            self.committed.emit(self._settled)
'''

MENU_BAR = '''

class _MenuBar(QMenuBar):
    """A QMenuBar whose items fire ``fired(label)``, as a Toolbar's buttons do.

    _EventPort connects a widget's `clicked` or `fired`, and QMenuBar has
    neither, so ports.<menubar>.on_fire(f) subscribers never ran. With
    ``fired`` the menubar is wired exactly as the toolbar is: the port runs
    its subscribers for every item chosen, MainUi connects fired to
    on_menu(label) once, and the port's own handler is "" for both, so
    ports.<name>.fire() runs the subscribers and not the handler.
    """

    fired = Signal(str)

    def item(self, menu, label):
        """Add ``label`` to ``menu``; choosing it fires ``label``."""
        menu.addAction(label).triggered.connect(
            lambda _checked=False: self.fired.emit(label))
'''

#: kind -> the generated subclass its construct() names.
_SUBCLASSES = {"spinbox": SPIN_BOX, "menubar": MENU_BAR}


def emit_main_ui(spec: Spec, regions: Optional[Dict[str, str]] = None) -> str:
    r = dict(regions or {})
    composites = sorted({w.kind for w in spec.widgets
                         if w.kind in COMPOSITE_KINDS})
    used = {w.kind for w in spec.widgets}
    # Only the subclasses this window builds: an unused class, like an unused
    # import, would misstate what the window contains.
    subclasses = [_SUBCLASSES[k] for k in sorted(used & set(_SUBCLASSES))]
    L: List[str] = [
        '"""Generated by the GUI Designer. DO NOT EDIT — regeneration',
        'overwrites this file. Behaviour belongs in app.py; small in-place',
        'additions belong in a `# region: custom:<id>` block, which survives.',
        '"""',
        "from __future__ import annotations",
        "",
        "from PySide6.QtCore import Qt" + (", Signal" if subclasses else ""),
        "from PySide6.QtWidgets import (" + ", ".join(sorted(
            _widget_imports(used))) + ")",
        "",
    ]
    if composites:
        L.append("from .widgets import " + ", ".join(
            COMPOSITE_KINDS[k] for k in composites))
        L.append("")
    L.append("from .ports import Ports")
    L.append(STOP_WATCHER.rstrip())
    L.append(LAYOUT_HELPERS.rstrip())
    L += [s.rstrip() for s in subclasses]
    L.append("")

    root_bg = _gcol.normalise(spec.root_bg) if spec.root_bg else ""
    root_fg = _gcol.normalise(getattr(spec, "root_fg", "") or "") if getattr(
        spec, "root_fg", "") else ""
    L += [
        "",
        "class MainUi(QWidget):",
        '    """Every widget, built and placed. Handlers live in app.py."""',
        "",
        "    def __init__(self, parent=None, **kw):",
        "        super().__init__(parent)",
        "        self._closing = False",
        "        self._build()",
        "        # ONE close path: the window's X, the designer's Stop, and the",
        "        # designer going away all run on_close before the window goes.",
        "        _watch_for_stop(self)",
        "",
        "    # -- closing ---------------------------------------------------",
        "    def closeEvent(self, event) -> None:",
        '        """The window\'s X. Routed through request_close so the one',
        '        cleanup path runs, then accepted on the second pass."""',
        "        if not self._closing:",
        "            event.ignore()",
        "            self.request_close()",
        "            return",
        "        event.accept()",
        "",
        "    def on_close(self) -> None:",
        '        """Release hardware here: stop a camera grab, close the device,',
        "        finish writing a file. Runs for the window's X, the designer's",
        "        Stop, and the designer exiting. Override it in handlers.py or",
        '        app.py."""',
        "",
        "    def request_close(self) -> None:",
        '        """Close cleanly: on_close first, then the window. Runs once."""',
        "        if self._closing:",
        "            return",
        "        self._closing = True",
        "        # A value still being TYPED is committed first, so its handler",
        "        # runs before on_close rather than after it. Measured: a spinbox",
        "        # holding a typed 9 at Stop ran on_close and THEN its handler,",
        "        # because hiding the window is what took focus from the box -",
        "        # a handler reaching hardware on_close had already released.",
        "        try:",
        "            from PySide6.QtWidgets import QApplication",
        "            focused = QApplication.focusWidget()",
        "            if focused is not None and self.isAncestorOf(focused):",
        "                focused.clearFocus()",
        "        except Exception:",
        "            pass",
        "        try:",
        "            self.on_close()",
        "        except Exception as exc:",
        "            # A failing cleanup must not keep a window the user asked to",
        "            # close open; say what failed and close anyway.",
        "            try:",
        '                print(f"on_close failed: {exc!r}")',
        "            except Exception:",
        "                pass                       # no one is reading output",
        "        try:",
        "            from PySide6.QtWidgets import QApplication",
        "            self.window().close()",
        "            # Qt's close() closes ONE window; Tk's destroy() ended the",
        "            # application. A preview that opened a second window would",
        "            # otherwise keep the process alive after Stop.",
        "            app = QApplication.instance()",
        "            if app is not None:",
        "                app.quit()",
        "        except Exception:",
        "            pass",
        "",
        "    # -- failures --------------------------------------------------",
        "    def report_error(self, what, exc) -> None:",
        '        """Say what failed IN THE WINDOW, not only on a console nobody',
        "        reads. Also written to stderr, which is the GUI Designer's log.",
        "        COUNCIL_NO_DIALOGS=1 skips the dialog for unattended runs, where",
        '        a modal box would wait for a click that never comes."""',
        "        import os",
        "        import sys",
        "        msg = str(exc) or type(exc).__name__",
        "        try:",
        "            if sys.stderr is not None:     # None under pythonw",
        '                sys.stderr.write(f"{what} failed: {msg}\\n")',
        "        except (OSError, ValueError):",
        "            pass                           # nobody is reading it",
        "        if os.environ.get('COUNCIL_NO_DIALOGS'):",
        "            return",
        "        try:",
        "            from PySide6.QtWidgets import QMessageBox",
        "            QMessageBox.critical(self, f'{what} failed', msg)",
        "        except Exception:",
        "            pass                           # no display, or shutting down",
        "",
        "    def clear_ports(self, *names) -> None:",
        '        """Blank the named ports after a failed call (Port.clear).',
        "        Never raises: a port renamed since handlers.py was written",
        '        must not stop report_error from saying what went wrong."""',
        "        for name in names:",
        "            try:",
        "                self.ports[name].clear()",
        "            except Exception:",
        "                pass",
        "",
        "    def _build(self) -> None:",
    ]
    ind = " " * 8
    L.append(f"{ind}_root = QGridLayout(self)")
    if root_bg or root_fg:
        bits = []
        if root_bg:
            bits.append(f"background-color: {root_bg};")
        if root_fg:
            bits.append(f"color: {root_fg};")
        # SCOPED BY objectName, like every other colour this emitter writes.
        # `QWidget { ... }` here would cascade into every descendant and repaint
        # the inside of each entry, spinbox and list — measured: the whole app
        # came out pink. Tk's root background reaches only the window and
        # MainUi, and gui_colors.resolve_scene has already decided each child's
        # own colour, so the scoped form is also the faithful one.
        css = "QWidget#_MainUi { " + " ".join(bits) + " }"
        L.append(f"{ind}self.setObjectName('_MainUi')")
        L.append(f"{ind}self.setAutoFillBackground(True)")
        L.append(f"{ind}self.setStyleSheet({_py(css)})")
    # The menubar that takes the window's one menu-bar slot, known up front:
    # the grid it was drawn in is configured before the menubar is reached.
    menu_bar = _window_menu_bar(spec)
    menu_bar_of_window = menu_bar.name if menu_bar is not None else ""

    def grid_lines(target: str, holder: Optional[str], rows, cols, row_min,
                   col_min) -> List[str]:
        notes: List[str] = []
        if (menu_bar is not None and menu_bar.parent == holder
                and menu_bar.manager == "grid"):
            rows, cols, row_min, col_min, notes = _lift_menu_bar(
                spec, menu_bar, rows, cols, row_min, col_min)
        out = [f"{ind}# {line}" for note in notes
               for line in textwrap.wrap(note, 79 - len(ind) - 2)]
        return out + _grid_config(target, rows, cols, row_min, col_min, ind)

    L += grid_lines("_root", None, spec.root_row_weights,
                    spec.root_col_weights, spec.root_row_minsizes,
                    spec.root_col_minsizes)
    L.append("")

    layouts: Dict[str, str] = {}      # widget name -> the layout of its children
    _tab_index: Dict[str, int] = {}

    for w in _ordered(spec):
        parent = f"self.{w.parent}" if w.parent else "self"
        parent_spec = spec.by_name(w.parent) if w.parent else None
        parent_kind = parent_spec.kind if parent_spec else ""
        parent_layout = layouts.get(w.parent or "", "_root")
        L.append(f"{ind}# {w.kind}: {_c(w.label or w.name)}")
        made = construct(w, parent)
        if w.is_container and w.explicit_w and w.explicit_h:
            # EMPTY: gui_layout sizes only an empty container, at the size it
            # was drawn. Asked for, not imposed — see _requested. This line
            # used to be setMinimumSize after placement, a floor Tk never had.
            made = _request_call(made, w.explicit_w, w.explicit_h)
        L.append(f"{ind}self.{w.name} = {made}")
        L.extend(configure_lines(w, ind))
        if parent_kind == "notebook":
            i = _tab_index.get(w.parent, 0)
            _tab_index[w.parent] = i + 1
            tabs = list(_prop(parent_spec, "tabs", []) or [])
            title = str(tabs[i]) if i < len(tabs) else (w.label or w.name)
            L.append(f"{ind}{parent}.addTab(self.{w.name}, {_py(title)})")
        elif parent_kind == "panedwindow":
            L.append(f"{ind}{parent}.addWidget(self.{w.name})")
        elif w.name == menu_bar_of_window:
            # A menu bar is chrome, not a cell: Tk's spelling is the window's
            # menu= slot, outside the geometry manager, and setMenuBar is the
            # same move — it works on a plain QWidget's layout. The grid it was
            # drawn in was configured without it (_lift_menu_bar). (The Tk
            # TARGET does not do this yet: measured, it grids its tk.Menu and
            # _build dies with "can't manage ...: it's a top-level window".)
            L.append(f"{ind}_root.setMenuBar(self.{w.name})")
        elif w.kind == "menubar" and w.parent:
            # Drawn inside a container, so it is that container's: the
            # window's menu-bar slot would lift it to the top of the window.
            L += _note(ind, f"Drawn inside {w.parent}, so laid out there, "
                            f"not given the window's menu-bar slot, which "
                            f"would lift it to the top of the window.")
            L.append(f"{ind}{place_call(w, parent_layout)}")
        elif w.kind == "menubar":
            # A window has ONE menu-bar slot. A second setMenuBar would replace
            # the first without a word and one drawn menubar would vanish, so
            # this one is laid out where it was drawn, like any other widget.
            L.append(f"{ind}# A SECOND menu bar. {menu_bar_of_window} already "
                     f"holds the window's one")
            L.append(f"{ind}# menu-bar slot, so this one is laid out where it "
                     f"was drawn.")
            L.append(f"{ind}{place_call(w, parent_layout)}")
        else:
            L.append(f"{ind}{place_call(w, parent_layout)}")
        if w.is_container:
            lay = f"_lay_{w.name}"
            layouts[w.name] = lay
            if w.kind not in ("notebook", "panedwindow"):
                L.append(f"{ind}{lay} = QGridLayout(self.{w.name})")
                if w.row_weights or w.col_weights:
                    L += grid_lines(lay, w.name, w.row_weights,
                                    w.col_weights, w.row_minsizes,
                                    w.col_minsizes)
        L += _region(w.name, ind, r.pop(w.name, ""))
        L.append("")

    L.append(f"{ind}# -- typed binding surface ---------------------------")
    L.append(f"{ind}self.ports = Ports(self)")
    L.append("")

    wiring = _handler_lines(spec, ind)
    if wiring:
        L.append(f"{ind}# -- handlers: Tk's command=self.on_<name> -----------")
        L.append(f"{ind}# Connected AFTER the ports: a port seeding its default")
        L.append(f"{ind}# must not run a handler while the window is half built.")
        L += wiring
        L.append("")

    hooks = spec.handlers + ["on_toolbar"]
    hooks += [h for h in _menu_handlers(spec) if h not in hooks]
    L += [
        "    # -- handler hooks -------------------------------------------",
        "    # Defined here so main_ui is runnable on its own; app.py overrides",
        "    # them. Without these a preview of the raw UI would die on the",
        "    # first click with an AttributeError.",
    ]
    for h in hooks:
        L.append(f"    def {h}(self, *args) -> None:")
        L.append("        pass")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


#: kind -> the signal MainUi connects to on_<name>: what Tk's command= fires on.
#:
#: Tk's command= runs on a USER action and never on a programmatic write —
#: ports.<name>.set() does not call it, and handlers.py is written against that.
#: Where Qt has a signal that means exactly "the user did it", that is the one
#: used; measured on PySide6 6.10.2, offscreen:
#:   QCheckBox     setChecked -> toggled only;  click() -> toggled AND clicked
#:   QRadioButton  click() on the checked one again -> clicked only (Tk's
#:                 command fires on every click too; toggled would not)
#:   QComboBox     setCurrentIndex -> currentTextChanged only;
#:                 a Down key -> currentTextChanged, activated, textActivated
#: So `toggled` / `currentTextChanged` would ALSO run the handler whenever the
#: app or a port default wrote the widget. Measured with toggled connected at
#: build time: the first radio of EVERY group (gui_ports gives each group a
#: default) and a checkbox with a port default both ran their handler inside
#: Ports(self), where self.ports does not exist yet — the very thing a
#: script-linked handler reads first.
#:
#: QSpinBox and QSlider have no user-only value signal (setValue, stepUp and a
#: key press all emit valueChanged and nothing else). QSlider uses
#: valueChanged, connected after the ports are built so construction never
#: fires it. The difference from Tk that remains THERE: a later programmatic
#: write — a port set, or a frame browser resizing its index — runs its
#: handler too.
#:
#: The spinbox no longer does. On valueChanged its handler ran once per
#: KEYSTROKE with a partial number — measured, typing 12 ran it with 1 and
#: then 12, and Return ran it with 12 again — where Tk's Spinbox runs
#: command= for an arrow only. It is built as the generated _SpinBox
#: (SPIN_BOX), whose `committed` fires for a step, or for a typed value once
#: Return or leaving the box commits it and it differs from what was there;
#: never for a keystroke or a programmatic write. The one difference from Tk
#: left: Tk runs nothing for a committed typed value at all.
#:
#: Scrubber and FilePicker are ours (WIDGETS_PY), so they carry the user-only
#: signal Tk's versions have as command=: `stepped` and `chosen`. Their
#: `changed` fires on every set, and Scrubber.set emits it even when the value
#: did not change, so a handler that wrote its own scrubber re-enters itself
#: until the stack runs out — measured: 332 nested calls, then RecursionError.
_COMMAND_SIGNAL = {
    "button": "clicked",
    "checkbutton": "clicked",
    "radiobutton": "clicked",
    "combobox": "textActivated",
    "spinbox": "committed",
    "scale": "valueChanged",
    "scrubber": "stepped",
    "file_picker": "chosen",
}


def _handler_lines(spec: Spec, ind: str) -> List[str]:
    """The connect() lines that make each widget reach its handler.

    A button WITH an event port is skipped: its _EventPort already connects
    clicked to the handler (and to every on_fire subscriber), and a second
    connection would run the handler twice per click. A button whose port is
    switched off has no _EventPort, and before this line existed no click on
    it reached anything.

    The toolbar is not a COMMAND_KIND — its _EventPort gets handler "" — but
    Tk passes it command=self.on_toolbar, and MainUi has always defined an
    on_toolbar stub for it to reach. It did not reach it on Qt until now.
    The menubar is wired the same way, through the `fired` signal _MenuBar
    gives it, so its port reaches its subscribers exactly as a toolbar's does.
    """
    out: List[str] = []
    for w in _ordered(spec):
        if w.kind == "toolbar":
            out.append(f"{ind}self.{w.name}.fired.connect("
                       f"_command(self, {_py('on_toolbar')}))")
            continue
        if w.kind == "menubar":
            out.append(f"{ind}self.{w.name}.fired.connect("
                       f"_command(self, {_py(w.handler or 'on_menu')}))")
            continue
        signal = _COMMAND_SIGNAL.get(w.kind)
        if not signal or not w.handler:
            continue
        if (w.kind == "button" and w.port is not None
                and w.port.binder == "event"):
            continue
        out.append(f"{ind}self.{w.name}.{signal}.connect("
                   f"_command(self, {_py(w.handler)}))")
    return out


def _menu_handlers(spec: Spec) -> List[str]:
    """The handler names menu items call, for MainUi's stubs."""
    return sorted({w.handler or "on_menu" for w in spec.widgets
                   if w.kind == "menubar"})


def _widget_imports(kinds) -> set:
    """Exactly the QtWidgets names the generated file uses.

    Built from the kinds present rather than a fixed list: an unused import is
    a lie about what the window contains, and ruff would flag it.
    """
    need = {"QGridLayout", "QWidget"}
    per_kind = {
        "frame": {"QFrame"}, "freeform": {"QFrame"}, "generic": {"QFrame"},
        "separator": {"QFrame"}, "labelframe": {"QGroupBox"},
        "notebook": {"QTabWidget"}, "panedwindow": {"QSplitter"},
        "label": {"QLabel"}, "button": {"QPushButton"}, "entry": {"QLineEdit"},
        "text": {"QPlainTextEdit"}, "checkbutton": {"QCheckBox"},
        "radiobutton": {"QRadioButton"}, "combobox": {"QComboBox"},
        "listbox": {"QListWidget", "QAbstractItemView"},
        "spinbox": {"QSpinBox"}, "scale": {"QSlider"},
        "progressbar": {"QProgressBar"}, "treeview": {"QTreeWidget"},
        "menubar": {"QMenuBar"},
    }
    for k in kinds:
        need |= per_kind.get(k, set())
    return need


# ============================================================
# app.py — hand-written, created once, never rewritten
# ============================================================

def links_frame_camera(spec: Spec) -> bool:
    """Whether any widget's script link calls into frame_camera."""
    return any((getattr(w, "script", None) or {}).get("module") == "frame_camera"
               for w in spec.widgets)


#: What app.py gains when the wireframe drives a camera. Emitted rather than
#: left to the user because the live view AND the first-run camera setup
#: wizard both start here — an app that needs a hand edit before it can show
#: its own setup wizard has not really got one.
FRAME_CAMERA_ATTACH = '''
        # The live camera view, and the camera setup wizard on first run.
        # Written because this wireframe links buttons to frame_camera.
        import frame_camera
        frame_camera.attach(self)'''


def emit_app_py(spec: Spec) -> str:
    attach = FRAME_CAMERA_ATTACH if links_frame_camera(spec) else ""
    return f'''"""Hand-written application code for {spec.project}.

This file is created ONCE and never rewritten by the designer. Put behaviour
here: the generated MainUi builds the widgets and calls self.on_<name>, and
those methods live below.

Regenerating the wireframe rewrites ui/ only. Nothing here is touched.

App inherits HandlerMixin FIRST so the handler bodies in handlers.py win over
the no-op stubs MainUi defines. Without that order the stubs shadow them and
every handler silently does nothing.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from handlers import HandlerMixin
from ui.main_ui import MainUi


class App(HandlerMixin, MainUi):
    def __init__(self, parent=None, **kw):
        super().__init__(parent){attach}


def main() -> None:
    app = QApplication(sys.argv)
    ui = App()
    ui.setWindowTitle({_py(spec.title)})
    ui.setMinimumSize({spec.min_w}, {spec.min_h})
    ui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
'''


def requires_dialog() -> List[str]:
    """The toolkit half of gui_emit._requires_block.

    A Qt project must not reach for tkinter to report that PySide6 is missing —
    which is exactly the case that would fire. The designer already renders an
    exit-3 with the list well, so this target stays text-only and keeps the
    generated tree free of any tkinter reference.
    """
    return []


# ============================================================
# ui/widgets.py — the composites, hand-written
# ============================================================

WIDGETS_PY = '''"""Composite widgets used by the generated UI.

Generated once per project and overwritten on regeneration, but hand-written
rather than assembled from templates: these are the difference between a mockup
and a usable tool, and each carries a `# region: custom:` block for per-project
extension.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton,
                               QSlider, QTextEdit, QVBoxLayout, QWidget)


def to_qimage(image):
    """A PIL.Image, a numpy array, a QImage or a QPixmap as a QImage.

    THE BUFFER MUST OUTLIVE THE QImage. QImage does not copy the bytes it is
    handed, so the source object is attached to the result; without that the
    image renders as noise or crashes once Python frees the buffer.

    A numpy array goes straight to QImage with no PIL round trip, which is what
    makes a live camera feed cheap: measured 194 fps for 1024x768 grayscale
    against 59 through PIL/ImageTk.
    """
    if image is None:
        return None
    if isinstance(image, QPixmap):
        return image.toImage()
    if isinstance(image, QImage):
        return image
    arr = getattr(image, "shape", None)
    if arr is not None:                      # numpy, without importing numpy
        h, w = image.shape[0], image.shape[1]
        stride = image.strides[0]
        if image.ndim == 2:
            img = QImage(image.data, w, h, stride, QImage.Format.Format_Grayscale8)
        elif image.shape[2] == 3:
            img = QImage(image.data, w, h, stride, QImage.Format.Format_RGB888)
        else:
            img = QImage(image.data, w, h, stride, QImage.Format.Format_RGBA8888)
        img._buffer = image
        return img
    mode = getattr(image, "mode", None)
    if mode is None:
        return None
    src = image if mode in ("RGB", "RGBA", "L") else image.convert("RGBA")
    data = src.tobytes()
    w, h = src.size
    fmt = {"L": QImage.Format.Format_Grayscale8,
           "RGB": QImage.Format.Format_RGB888,
           "RGBA": QImage.Format.Format_RGBA8888}[src.mode]
    per = {"L": 1, "RGB": 3, "RGBA": 4}[src.mode]
    img = QImage(data, w, h, w * per, fmt)
    img._buffer = data
    return img


class ImageCanvas(QWidget):
    """Image viewer: pan, zoom-to-fit, zoom-to-cursor, optional alpha overlay,
    and an optional region of interest (roi=True).

    Zoom is anchored to the CURSOR, not the widget centre. Centre-anchored zoom
    is the classic mistake — the thing under the pointer slides away and the
    user chases it, which on a layer-wise scan is unusable.

    THE ROI. Draw ROI arms the next left-drag to draw a box instead of panning.
    Apply ROI crops the view to that box and zooms it to fit, and the crop is
    re-applied to EVERY new frame, so a scrubbed or live sequence stays zoomed
    on the region instead of snapping back to the whole frame. Clear ROI
    returns to the whole frame.

    The box is stored in FULL-IMAGE pixels, never widget pixels. Widget pixels
    change with every pan and zoom; image pixels mean the same region at any
    zoom, which is exactly what a crop-on-save routine needs to be handed.

    Unlike the Tk version this never resizes the image to paint it — QPainter
    scales while drawing, so panning a large frame costs nothing per pixel.
    """

    ROI_COLOUR = "#00e5ff"

    def __init__(self, parent=None, *, overlay: bool = False,
                 overlay_alpha: float = 0.5, roi: bool = False,
                 zoom_to_fit: bool = True):
        super().__init__(parent)
        self.roi_enabled = bool(roi)
        self._roi = None           # (x, y, w, h) in full-image pixels
        self._roi_applied = False
        self._roi_armed = False    # the next left-drag draws instead of pans
        self._roi_from = None      # widget point where the ROI drag began
        self._roi_to = None
        self._roi_listeners = []
        self._fit_on_set = bool(zoom_to_fit)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        if self.roi_enabled:
            bar = QHBoxLayout()
            self._btn_draw = QPushButton("Draw ROI", self)
            self._btn_apply = QPushButton("Apply ROI", self)
            self._btn_clear = QPushButton("Clear ROI", self)
            self._btn_draw.clicked.connect(self.arm_roi)
            self._btn_apply.clicked.connect(self.apply_roi)
            self._btn_clear.clicked.connect(self.clear_roi)
            for b in (self._btn_draw, self._btn_apply, self._btn_clear):
                bar.addWidget(b)
            self._roi_note = QLabel("", self)
            bar.addWidget(self._roi_note)
            bar.addStretch(1)
            outer.addLayout(bar)
        self._view_area = _Viewport(self)
        outer.addWidget(self._view_area, 1)
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._pan_from = None
        self._base = None          # QImage — the whole frame
        self._view = None          # what is shown: _base, or its ROI crop
        self._crop_at = (0, 0)     # full-image position of _view's top-left
        self._overlay_img = None
        self._fitted_at = (0, 0)
        self._message = ""
        self.overlay_enabled = bool(overlay)
        self.overlay_alpha = float(overlay_alpha)
        self._sync_roi_controls()

    # -- public ------------------------------------------------------
    def set_image(self, image) -> None:
        """``image`` is a PIL.Image, a numpy array, a QImage or a QPixmap.
        An applied ROI is re-applied to it."""
        self._base = to_qimage(image)
        if self._base is not None:
            self._message = ""
        self._refresh_view()
        if self._fit_on_set:
            self.zoom_to_fit()
        else:
            self._view_area.update()

    def set_array(self, arr, copy: bool = True) -> None:
        """A numpy frame straight from a camera grab. ``copy`` because a
        driver usually hands back a buffer it will overwrite on the next
        frame, and a QImage that does not own its bytes would then show the
        NEXT frame's pixels half-drawn."""
        if arr is not None and copy:
            arr = arr.copy()
        self.set_image(arr)

    def show_message(self, text: str) -> None:
        """Say something IN the panel when there is no image — why a folder
        showed no frames — instead of leaving a blank rectangle while the
        reason goes to a console nobody reads."""
        self._base = None
        self._message = str(text or "")
        self._refresh_view()
        self._view_area.update()

    def set_overlay(self, image, alpha: float = None) -> None:
        self._overlay_img = to_qimage(image)
        if alpha is not None:
            self.overlay_alpha = float(alpha)
        self._view_area.update()

    def clear_overlay(self) -> None:
        self._overlay_img = None
        self._view_area.update()

    def zoom_to_fit(self) -> None:
        """Fit what is SHOWN — the ROI crop when one is applied, which is how
        applying an ROI zooms the view onto it."""
        if self._view is None:
            self._view_area.update()
            return
        cw = max(1, self._view_area.width())
        ch = max(1, self._view_area.height())
        iw, ih = self._view.width(), self._view.height()
        self._scale = min(cw / iw, ch / ih) if iw and ih else 1.0
        self._ox = (cw - iw * self._scale) / 2
        self._oy = (ch - ih * self._scale) / 2
        self._fitted_at = (cw, ch)
        self._view_area.update()

    # -- region of interest -------------------------------------------
    def get_roi(self):
        """(x, y, w, h) in full-image pixels, or None."""
        return self._roi

    @property
    def roi_applied(self) -> bool:
        return bool(self._roi_applied and self._roi)

    def on_roi_change(self, fn) -> None:
        """Call ``fn(roi)`` whenever the box is drawn, cleared or replaced."""
        self._roi_listeners.append(fn)

    def set_roi(self, roi, notify: bool = False) -> None:
        """Replace the box with ``roi`` — (x, y, w, h) or None."""
        roi = self._valid_roi(roi)
        if roi == self._roi:
            return
        self._roi = roi
        if roi is None:
            self._roi_applied = False
        self._refresh_view()
        if self._roi_applied:
            self.zoom_to_fit()
        else:
            self._view_area.update()
        if notify:
            self._emit_roi()

    def arm_roi(self) -> None:
        """The next left-drag draws the ROI instead of panning."""
        if self._roi_applied:
            return           # Clear first: a box drawn on a crop is ambiguous
        self._roi_armed = True
        self._view_area.setCursor(Qt.CursorShape.CrossCursor)
        self._sync_roi_controls()

    def apply_roi(self) -> None:
        """Crop the view to the ROI and zoom it to fit — now and every frame."""
        if not self._roi:
            return
        self._roi_applied = True
        self._refresh_view()
        self.zoom_to_fit()

    def clear_roi(self) -> None:
        had = self._roi is not None
        self._roi = None
        self._roi_applied = False
        self._roi_armed = False
        self._roi_from = None
        self._view_area.unsetCursor()
        self._refresh_view()
        self.zoom_to_fit()
        if had:
            self._emit_roi()

    def _valid_roi(self, roi):
        try:
            x, y, w, h = (int(round(float(v))) for v in roi)
        except (TypeError, ValueError):
            return None
        if w < 2 or h < 2 or x < 0 or y < 0:
            return None
        return (x, y, w, h)

    def _roi_box(self, size):
        """The ROI clamped to an image of ``size`` (w, h) as (l, t, r, b), or
        None when it lies entirely outside that image."""
        if not self._roi:
            return None
        iw, ih = size
        x, y, w, h = self._roi
        left, top = max(0, x), max(0, y)
        right, bottom = min(iw, x + w), min(ih, y + h)
        if right - left < 1 or bottom - top < 1:
            return None
        return (left, top, right, bottom)

    def _refresh_view(self) -> None:
        """Recompute what is shown: the whole frame, or its ROI crop."""
        b = self._base
        self._view, self._crop_at = b, (0, 0)
        if b is not None and self._roi_applied:
            box = self._roi_box((b.width(), b.height()))
            if box is not None:
                self._view = b.copy(QRect(box[0], box[1],
                                          box[2] - box[0], box[3] - box[1]))
                self._crop_at = (box[0], box[1])
        self._sync_roi_controls()

    def _to_image(self, cx, cy):
        """Widget point -> full-image pixel, through pan, zoom and any crop."""
        s = self._scale or 1.0
        return (self._crop_at[0] + (cx - self._ox) / s,
                self._crop_at[1] + (cy - self._oy) / s)

    def _to_widget(self, ix, iy):
        s = self._scale
        return (self._ox + (ix - self._crop_at[0]) * s,
                self._oy + (iy - self._crop_at[1]) * s)

    def _emit_roi(self) -> None:
        for fn in list(self._roi_listeners):
            try:
                fn(self._roi)
            except Exception as exc:
                print(f"[ImageCanvas] ROI listener failed: {exc!r}")

    def _sync_roi_controls(self) -> None:
        if not self.roi_enabled:
            return
        have, applied = self._roi is not None, self.roi_applied
        self._btn_draw.setEnabled(not applied)
        self._btn_apply.setEnabled(have and not applied)
        self._btn_clear.setEnabled(have)
        if self._roi_armed:
            text = "Drag a box on the image"
        elif not have:
            text = "No ROI"
        elif applied and self._base is not None and self._view is self._base:
            text = "ROI lies outside this frame"
        else:
            x, y, w, h = self._roi
            box = (self._roi_box((self._base.width(), self._base.height()))
                   if self._base is not None else None)
            if box is not None:
                x, y, w, h = box[0], box[1], box[2] - box[0], box[3] - box[1]
            text = f"{'Applied' if applied else 'ROI'}: {w} x {h} at ({x}, {y})"
            if box is not None and (w, h) != (self._roi[2], self._roi[3]):
                text += " (clamped)"
        self._roi_note.setText(text)

    # -- interaction (forwarded by the viewport) -----------------------
    def _press(self, pos) -> None:
        if self._roi_armed and self._view is not None:
            self._roi_from = (pos.x(), pos.y())
            self._roi_to = (pos.x(), pos.y())
            return
        self._pan_from = (pos.x(), pos.y())

    def _drag(self, pos) -> None:
        if self._roi_from is not None:
            self._roi_to = (pos.x(), pos.y())
            self._view_area.update()
            return
        if self._pan_from is None:
            return
        dx = pos.x() - self._pan_from[0]
        dy = pos.y() - self._pan_from[1]
        self._pan_from = (pos.x(), pos.y())
        self._ox += dx
        self._oy += dy
        self._view_area.update()

    def _release(self, pos) -> None:
        if self._roi_from is None:
            self._pan_from = None
            return
        x0, y0 = self._roi_from
        self._roi_from = None
        self._roi_to = None
        self._roi_armed = False
        self._view_area.unsetCursor()
        if self._base is None:
            return
        ax, ay = self._to_image(min(x0, pos.x()), min(y0, pos.y()))
        bx, by = self._to_image(max(x0, pos.x()), max(y0, pos.y()))
        iw, ih = self._base.width(), self._base.height()
        # Rounded, not truncated: widget->image is float maths, and int() on
        # 9.9999 would shift the box a pixel left of where it was drawn.
        ax, ay = max(0, int(round(ax))), max(0, int(round(ay)))
        bx, by = min(iw, int(round(bx))), min(ih, int(round(by)))
        drawn = self._valid_roi((ax, ay, bx - ax, by - ay))
        if drawn is not None:
            # A click without a drag must not wipe a box drawn earlier.
            self.set_roi(drawn, notify=True)
        self._sync_roi_controls()
        self._view_area.update()

    def _wheel(self, delta, pos) -> None:
        self._zoom_at(1.1 if delta > 0 else 1 / 1.1, pos.x(), pos.y())

    def _zoom_at(self, factor: float, cx: float, cy: float) -> None:
        new = max(0.02, min(40.0, self._scale * factor))
        if new == self._scale:
            return
        # Keep the image point under the cursor fixed.
        self._ox = cx - (cx - self._ox) * (new / self._scale)
        self._oy = cy - (cy - self._oy) * (new / self._scale)
        self._scale = new
        self._view_area.update()

    def _resized(self) -> None:
        """Re-fit if the last fit ran before the widget had a real size.

        zoom_to_fit divides by the viewport width, which is tiny until Qt has
        laid the window out. An app that sets an image during construction —
        the normal thing to do when a folder is preloaded — would otherwise
        fit at a dead scale and draw the image a few pixels across."""
        if min(self._fitted_at) <= 1 and self._view is not None:
            self.zoom_to_fit()
        else:
            self._view_area.update()

    # -- painting ----------------------------------------------------
    def _paint(self, painter, width, height) -> None:
        painter.fillRect(0, 0, width, height, QColor("#1e1e2e"))
        view = self._view
        if view is None:
            if self._message:
                painter.setPen(QPen(QColor("#f5c2e7")))
                painter.drawText(QRect(20, 0, max(80, width - 40), height),
                                 int(Qt.AlignmentFlag.AlignCenter)
                                 | int(Qt.TextFlag.TextWordWrap),
                                 self._message)
            return
        iw, ih = view.width(), view.height()
        target = QRect(int(self._ox), int(self._oy),
                       max(1, int(iw * self._scale)),
                       max(1, int(ih * self._scale)))
        painter.drawImage(target, view)
        if self.overlay_enabled and self._overlay_img is not None:
            ov = self._overlay_img
            if view is not self._base and self._base is not None \\
                    and ov.size() == self._base.size():
                box = self._roi_box((self._base.width(), self._base.height()))
                if box is not None:
                    ov = ov.copy(QRect(box[0], box[1], box[2] - box[0],
                                       box[3] - box[1]))
            painter.setOpacity(max(0.0, min(1.0, self.overlay_alpha)))
            painter.drawImage(target, ov)
            painter.setOpacity(1.0)
        pen = QPen(QColor(self.ROI_COLOUR))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.DashLine)
        if self._roi_from is not None and self._roi_to is not None:
            painter.setPen(pen)
            x0, y0 = self._roi_from
            x1, y1 = self._roi_to
            painter.drawRect(QRect(QPoint(int(min(x0, x1)), int(min(y0, y1))),
                                   QPoint(int(max(x0, x1)), int(max(y0, y1)))))
        elif self._roi and not self._roi_applied and self._base is not None:
            box = self._roi_box((self._base.width(), self._base.height()))
            if box is not None:
                painter.setPen(pen)
                ax, ay = self._to_widget(box[0], box[1])
                bx, by = self._to_widget(box[2], box[3])
                painter.drawRect(QRect(QPoint(int(ax), int(ay)),
                                       QPoint(int(bx), int(by))))

    # region: custom:ImageCanvas -- preserved across regeneration
    # endregion


class _Viewport(QWidget):
    """The drawing surface ImageCanvas paints on.

    Separate from ImageCanvas so the ROI button bar is a normal laid-out row
    and the image area is the rest — the same split the Tk version got from
    packing a Canvas below a Frame.
    """

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self.setMouseTracking(False)
        self.setMinimumSize(40, 40)

    def sizeHint(self):
        """What the Tk version's Canvas asks for: Tk's default 10c x 7c, which
        is 378x265 at 96 dpi. A plain QWidget asks for nothing, so the hint
        fell to the 40x40 minimum, and wherever gui_layout does not stretch
        the canvas (a "fixed" frame holding it) that IS its size: measured, a
        frame drawn 336x560 rendered 66x66 on Qt and 382x269 on Tk. Only the
        hint, not the minimum — a request the layout can shrink, as Tk's is.
        """
        return QSize(378, 265)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self._owner._paint(painter, self.width(), self.height())
        finally:
            painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._owner._resized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._press(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._owner._drag(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._release(event.position())

    def wheelEvent(self, event):
        self._owner._wheel(event.angleDelta().y(), event.position())


class ChartPanel(QWidget):
    """A matplotlib Figure embedded via FigureCanvasQTAgg.

    Builds Figure() directly and never touches pyplot — a pyplot figure is held
    forever by its global registry, so a panel that redrew on every update
    would leak one figure per redraw. Same rule the app's plots_pane follows.

    matplotlib is imported inside the methods, not at module scope: a project
    with no chart must not need matplotlib installed to start.
    """

    def __init__(self, parent=None, *, toolbar: bool = False,
                 tight_layout: bool = True):
        super().__init__(parent)
        self._want_toolbar = bool(toolbar)
        self._tight = bool(tight_layout)
        self._canvas = None
        self._toolbar = None
        self.figure = None
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("(no chart yet)", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._box.addWidget(self._placeholder)

    def figure_for_drawing(self):
        """A fresh Figure, cleared and ready. Draw on it, then call redraw()."""
        from matplotlib.figure import Figure
        if self.figure is None:
            self.figure = Figure(figsize=(5, 3), dpi=100)
        self.figure.clear()
        return self.figure

    def redraw(self) -> None:
        from matplotlib.backends.backend_qtagg import (FigureCanvasQTAgg,
                                                       NavigationToolbar2QT)
        if self.figure is None:
            return
        if self._canvas is None:
            self._placeholder.hide()
            self._box.removeWidget(self._placeholder)
            self._canvas = FigureCanvasQTAgg(self.figure)
            if self._want_toolbar:
                self._toolbar = NavigationToolbar2QT(self._canvas, self)
                self._box.addWidget(self._toolbar)
            self._box.addWidget(self._canvas, 1)
        if self._tight:
            try:
                self.figure.tight_layout()
            except Exception:
                pass            # a figure with no axes cannot be tightened
        self._canvas.draw()

    # region: custom:ChartPanel -- preserved across regeneration
    # endregion


class Scrubber(QWidget):
    """Slider + index box + prev/next + total, bound to one integer index.

    TWO SIGNALS, ON PURPOSE. ``changed`` fires on EVERY set — programmatic
    ones included, and even when the value did not move — because that is
    what a port's on_change must see. ``stepped`` fires only when the USER
    moved it (prev/next, a drag, a typed index): that is what Tk's command=
    fires on, and what MainUi connects on_<name> to. A handler on ``changed``
    that set its own scrubber re-entered itself 332 times, then RecursionError.

    THE INDEX BOX. Tk binds its <Return> and nothing else, and runs command=
    for it whether or not the index moved; returnPressed is that. Leaving the
    box is Qt's editingFinished too, and Qt 6 emits it on focus loss whenever
    the text changed since it last fired — which set() does on every write,
    so a set(4) by the app and a click through the box ran the handler with
    4, and Return then leaving ran it twice (measured). Leaving now commits
    a typed index so the box never shows one number and the slider another,
    and runs the handler only if the index MOVED.
    """

    changed = Signal(int)
    stepped = Signal(int)

    def __init__(self, parent=None, *, from_: int = 0, to: int = 100,
                 show_total: bool = True):
        super().__init__(parent)
        self._lo = int(from_)
        self._hi = int(to)
        self._value = self._lo
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self._prev = QPushButton("\\u25c0", self)
        self._next = QPushButton("\\u25b6", self)
        for b in (self._prev, self._next):
            b.setFixedWidth(30)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(self._lo, self._hi)
        self.entry = QLineEdit(self)
        self.entry.setFixedWidth(60)
        self.entry.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.total = QLabel(f"/ {self._hi}" if show_total else "", self)
        box.addWidget(self._prev)
        box.addWidget(self.slider, 1)
        box.addWidget(self._next)
        box.addWidget(self.entry)
        box.addWidget(self.total)
        self._prev.clicked.connect(lambda: self.step(-1))
        self._next.clicked.connect(lambda: self.step(1))
        self.slider.valueChanged.connect(self._from_slider)
        self.entry.returnPressed.connect(self._from_entry)
        self.entry.editingFinished.connect(self._left_entry)
        self.set(self._lo)

    def set_range(self, lo: int, hi: int) -> None:
        self._lo, self._hi = int(lo), int(hi)
        # BLOCKED, as in set(). setRange clamps the slider's value and emits
        # valueChanged, which _from_slider takes for a drag: measured, a
        # scrubber at 5 given set_range(0, 2) ran its handler with 2 — and
        # _FrameBrowser.reload calls this on every folder load and once at
        # startup. The set() below moves slider, box and `changed` together.
        was = self.slider.blockSignals(True)
        self.slider.setRange(self._lo, self._hi)
        self.slider.blockSignals(was)
        self.total.setText(f"/ {self._hi}")
        self.set(min(max(self.get(), self._lo), self._hi))

    def get(self) -> int:
        return int(self._value)

    def set(self, value: int, notify: bool = False) -> None:
        v = max(self._lo, min(self._hi, int(value)))
        self._value = v
        # The slider is the only one of the three that fires back into here;
        # block it so a programmatic set does not bounce through _from_slider.
        was = self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(was)
        self.entry.setText(str(v))
        self.changed.emit(v)
        if notify:
            self._fire(v)

    def step(self, delta: int) -> None:
        self.set(self.get() + delta, notify=True)

    def on_step(self, fn) -> None:
        """Called with the new index when the USER moves the scrubber."""
        self._listener = fn

    def _fire(self, value) -> None:
        self.stepped.emit(value)
        fn = getattr(self, "_listener", None)
        if fn is not None:
            try:
                fn(value)
            except Exception as exc:
                print(f"[Scrubber] listener failed: {exc!r}")

    def _from_slider(self, raw) -> None:
        v = int(raw)
        if v != self._value:
            self.set(v, notify=True)

    def _from_entry(self) -> None:
        """Return: Tk's <Return> binding, which notifies moved or not."""
        try:
            self.set(int(self.entry.text()), notify=True)
        except ValueError:
            self.set(self.get())

    def _left_entry(self) -> None:
        """editingFinished: after Return (whose _from_entry already made the
        value match the box, so this does nothing) or on leaving the box."""
        try:
            v = max(self._lo, min(self._hi, int(self.entry.text())))
        except ValueError:
            v = self._value
        if v != self._value:
            self.set(v, notify=True)
        elif self.entry.text() != str(v):
            self.entry.setText(str(v))  # unreadable or past the end: re-show

    # region: custom:Scrubber -- preserved across regeneration
    # endregion


class LogPane(QWidget):
    """Read-only log, coloured by level, with an autoscroll toggle."""

    COLOURS = {"info": "#cdd6f4", "warn": "#f9e2af", "error": "#f38ba8",
               "debug": "#a6adc8", "ok": "#a6e3a1"}

    def __init__(self, parent=None, *, autoscroll: bool = True, levels=None):
        super().__init__(parent)
        self.levels = list(levels or ("info", "warn", "error"))
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.text = QTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setStyleSheet(
            "QTextEdit { background-color: #181825; color: #cdd6f4; "
            "border: none; }")
        box.addWidget(self.text, 1)
        self.autoscroll = QCheckBox("Autoscroll", self)
        self.autoscroll.setChecked(bool(autoscroll))
        box.addWidget(self.autoscroll)

    def append(self, message: str, level: str = "info") -> None:
        """A line at ``level``. A level the project did not declare still
        prints — dropping it would lose the message, which is worse than
        showing it in the default colour."""
        lvl = level if level in self.COLOURS else "info"
        colour = self.COLOURS[lvl]
        safe = (str(message).rstrip()
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.text.append(f'<span style="color:{colour}">{safe}</span>')
        if self.autoscroll.isChecked():
            bar = self.text.verticalScrollBar()
            bar.setValue(bar.maximum())

    def clear(self) -> None:
        self.text.clear()

    # region: custom:LogPane -- preserved across regeneration
    # endregion


class FilePicker(QWidget):
    """Entry + Browse, for a file, a folder, or a save target.

    ``changed`` follows the text (every keystroke, every set) for the port.
    ``chosen`` fires only when Browse picked a path — the one moment Tk's
    FilePicker calls its command=, and what MainUi connects on_<name> to.
    """

    changed = Signal(str)
    chosen = Signal(str)

    def __init__(self, parent=None, *, mode: str = "file", filetypes=None):
        super().__init__(parent)
        self.mode = mode
        self.filetypes = list(filetypes or [])
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.entry = QLineEdit(self)
        self.button = QPushButton("Browse...", self)
        box.addWidget(self.entry, 1)
        box.addWidget(self.button)
        self.button.clicked.connect(self.browse)
        self.entry.textChanged.connect(self.changed.emit)

    def get(self) -> str:
        return self.entry.text()

    def set(self, path: str) -> None:
        self.entry.setText("" if path is None else str(path))

    def browse(self) -> None:
        ft = ";;".join(f"{t} ({t})" for t in self.filetypes) or "All files (*.*)"
        if self.mode == "folder":
            path = QFileDialog.getExistingDirectory(self, "Choose a folder")
        elif self.mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, "Save as", "", ft)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose a file", "", ft)
        if path:
            self.set(path)
            self.chosen.emit(path)

    # region: custom:FilePicker -- preserved across regeneration
    # endregion


class StatusBar(QWidget):
    """Bottom strip: a message and an optional inline progress bar."""

    def __init__(self, parent=None, *, progress: bool = False):
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(4, 0, 4, 0)
        self.message = QLabel("", self)
        box.addWidget(self.message, 1)
        self.progress = None
        if progress:
            self.progress = QProgressBar(self)
            self.progress.setFixedWidth(140)
            self.progress.setRange(0, 100)
            box.addWidget(self.progress)

    def set(self, text: str) -> None:
        self.message.setText("" if text is None else str(text))

    def set_progress(self, value: float) -> None:
        if self.progress is not None:
            self.progress.setValue(int(max(0.0, min(100.0, float(value)))))

    # region: custom:StatusBar -- preserved across regeneration
    # endregion


class Toolbar(QWidget):
    """A horizontal button strip. ``fired`` carries the button label."""

    fired = Signal(str)

    def __init__(self, parent=None, *, buttons=None):
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.buttons = {}
        for name in list(buttons or []):
            b = QPushButton(str(name), self)
            b.clicked.connect(lambda _checked=False, n=str(name):
                              self.fired.emit(n))
            box.addWidget(b)
            self.buttons[str(name)] = b
        box.addStretch(1)

    # region: custom:Toolbar -- preserved across regeneration
    # endregion


class _Separator(QFrame):
    """Kept so a project that used one before still imports cleanly."""

    # region: custom:_Separator -- preserved across regeneration
    # endregion
'''


# ============================================================
# ui/ports.py — the typed binding surface
# ============================================================
#
# The API is the Tk target's, name for name, because handlers.py and app.py are
# written against it and are never rewritten. What changes underneath is only
# HOW a value is read: Tk hangs a variable off the widget and traces it, Qt asks
# the widget and connects a signal.
#
# THE ONE SEMANTIC CHECK THAT MATTERED: a Tk trace fires on programmatic writes
# as well as user edits, and app.py relies on that (a _FrameBrowser scrub is a
# programmatic write). Qt's textChanged / toggled / valueChanged /
# currentTextChanged do the same and do NOT re-fire for an unchanged value. A
# widget with no change signal at all (QLabel) is covered by firing from set()
# instead, so .on_change means the same thing on every port.

PORTS_RUNTIME = '''"""Generated by the GUI Designer. DO NOT EDIT — regeneration
overwrites this file. Behaviour belongs in app.py.

TYPED BINDING SURFACE. app.py talks to self.ports.<name> and never touches a
widget option, so widget substitutions ripple only through this file. Every
port has:

    .get()        read (raises for `out`-only kinds — see .set)
    .set(v)       write (raises for `in`-only kinds; is a no-op for events)
    .on_change(f) f(new_value) fires on user OR programmatic writes
    .enable(b)    True/False
    .widget       the underlying Qt widget, if you really need it

An event port (button/toolbar/menubar) exposes:

    .on_fire(f)   f() runs when the button is pressed, or a toolbar button
                  or menu item chosen
    .fire()       run the subscribers — and a button's handler — as if the
                  user pressed it. A toolbar's on_toolbar and a menubar's
                  on_menu take the label chosen, which fire() has not got
    .enable(b)

Ports as a whole exposes:

    .read()       {name: value} for every in/inout port
    .apply(d)     write into every out/inout port from {name: value}
    ports[name]   look up by name (also `name in ports`, `for p in ports`)
"""
from __future__ import annotations

__all__ = ["Ports", "RENAMED"]


# old name -> current name. Emitted only while hand-written code still uses the
# old one; vanishes on the first regeneration after the last reference is gone.
RENAMED: dict = {}


def _s(value):
    return "" if value is None else str(value)


def _coerce(v, t):
    """Nudge a widget value into the port's declared type.

    A blank or unreadable number is None, not 0. A count box cleared after a
    failed call (Port.clear) reading back as 0 is the same false zero clear()
    exists to keep off the screen, handed to whatever read it next.
    """
    if t in ("int", "float"):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v) if t == "int" else float(v)
        s = "" if v is None else str(v).strip()
        try:
            return int(s) if t == "int" else float(s)
        except ValueError:
            pass
        try:
            f = float(s)                     # "3.0" typed into an int box
            return int(f) if t == "int" else f
        except (ValueError, OverflowError):
            return None
    if t == "bool":
        return bool(v)
    if t == "path":
        return "" if v is None else str(v)
    return v


# kind -> (read, write, signal-name, blank)
#
# `blank` is what clear() writes. None means this kind has NO honest empty
# state — a checkbox, a slider or a spinbox would show a value that reads as an
# answer — so it keeps what it has rather than lie.
def _combo_write(w, v):
    text = _s(v)
    i = w.findText(text)
    if i >= 0:
        w.setCurrentIndex(i)
    elif w.isEditable():
        w.setEditText(text)
    elif text:
        # Show what was set rather than silently keeping the old value: a
        # non-editable QComboBox ignores setCurrentText for an unknown item.
        w.addItem(text)
        w.setCurrentIndex(w.count() - 1)


_ADAPTERS = {
    "label": (lambda w: w.text(), lambda w, v: w.setText(_s(v)), "", ""),
    "entry": (lambda w: w.text(), lambda w, v: w.setText(_s(v)),
              "textChanged", ""),
    "checkbutton": (lambda w: w.isChecked(),
                    lambda w, v: w.setChecked(bool(v)), "toggled", None),
    "combobox": (lambda w: w.currentText(), _combo_write,
                 "currentTextChanged", ""),
    "spinbox": (lambda w: w.value(),
                lambda w, v: w.setValue(int(_coerce(v, "int") or 0)),
                "valueChanged", None),
    "scale": (lambda w: w.value(),
              lambda w, v: w.setValue(int(_coerce(v, "float") or 0)),
              "valueChanged", None),
    "progressbar": (lambda w: w.value(),
                    lambda w, v: w.setValue(int(_coerce(v, "float") or 0)),
                    "valueChanged", 0),
    "file_picker": (lambda w: w.get(), lambda w, v: w.set(v), "changed", ""),
    "scrubber": (lambda w: w.get(), lambda w, v: w.set(v), "changed", None),
}


class _Port:
    """Base class. Subclasses fill in .get / .set / .on_change."""

    __slots__ = ("name", "widget", "direction", "type", "_deep")

    def __init__(self, name, widget, *, direction, type, deep=False):
        self.name, self.widget = name, widget
        self.direction, self.type = direction, type
        self._deep = deep

    def enable(self, on: bool = True) -> None:
        """Qt disables a widget's children with it, so `deep` needs no walk —
        the flag is kept for API parity with the Tk target."""
        self.widget.setEnabled(bool(on))

    # -- default failures with actionable messages ---------------------
    def get(self):
        raise TypeError(f"port {self.name!r} is not readable "
                        f"(direction {self.direction!r})")

    def set(self, value) -> None:
        raise TypeError(f"port {self.name!r} is not writable "
                        f"(direction {self.direction!r})")

    def on_change(self, fn) -> None:
        raise TypeError(f"port {self.name!r} has no change hook "
                        f"(direction {self.direction!r})")

    def clear(self) -> None:
        """Show NOTHING — used when the call that fills this port failed.

        Deliberately not set(0) or set(""): a zero in a count box after a
        failed scan is exactly the false answer this exists to prevent."""


class _WidgetPort(_Port):
    """A port over a plain Qt widget, driven by the _ADAPTERS table.

    Replaces the Tk target's _VarPort: there is no Tk variable to own, so the
    widget itself is the value and a signal replaces trace_add('write').
    """

    __slots__ = ("_kind", "_subs", "_last", "_wired")

    def __init__(self, name, widget, *, kind, type, direction,
                 default=None, deep=False):
        super().__init__(name, widget, direction=direction, type=type, deep=deep)
        self._kind = kind
        self._subs = []
        self._wired = False
        self._last = None
        if default is not None:
            try:
                self.set(default)
            except Exception:
                pass
        self._last = self._raw()

    def _raw(self):
        try:
            return _ADAPTERS[self._kind][0](self.widget)
        except Exception:
            return None

    def get(self):
        return _coerce(self._raw(), self.type)

    def set(self, value) -> None:
        _ADAPTERS[self._kind][1](self.widget, value)
        if not _ADAPTERS[self._kind][2]:
            # No change signal on this widget (a QLabel), so a programmatic
            # write is the only way it can change — fire from here, or
            # .on_change would mean something different on this port.
            self._fire()

    def on_change(self, fn) -> None:
        self._subs.append(fn)
        signal = _ADAPTERS[self._kind][2]
        if signal and not self._wired:
            self._wired = True
            getattr(self.widget, signal).connect(lambda *_a: self._fire())

    def _fire(self) -> None:
        value = self._raw()
        if value == self._last:
            return                                 # value-debounced, like Tk
        self._last = value
        typed = _coerce(value, self.type)
        for fn in list(self._subs):
            try:
                fn(typed)
            except Exception as exc:
                # One bad subscriber must not stop the others, and must not
                # take down the widget's signal emission with it.
                print(f"[ports] {self.name}.on_change callback raised: {exc!r}")

    def clear(self) -> None:
        blank = _ADAPTERS[self._kind][3]
        if blank is not None:
            self.set(blank)


class _RadioPort(_Port):
    """One value shared by a group of QRadioButtons.

    `_pending` IS IN __slots__. _Port declares slots, so an attribute missing
    from them cannot be set at all — and gui_ports gives every radio group a
    default (the first radio's value), so `self._pending = default` ran for
    every group. Measured: any window with a radiobutton died inside
    Ports(self) with "'_RadioPort' object has no attribute '_pending'".
    """

    __slots__ = ("_group", "_values", "_subs", "_last", "_pending")

    def __init__(self, name, widget, *, type, direction, default=None):
        from PySide6.QtWidgets import QButtonGroup
        super().__init__(name, widget, direction=direction, type=type)
        self._group = QButtonGroup(widget)
        self._values = {}
        self._subs = []
        self._last = None
        self._pending = default
        # ONCE, here. Connected in add() it was connected once per radio, and
        # Qt keeps every duplicate: measured, one switch in a three-radio
        # group ran _on_toggle six times (three per toggled button).
        self._group.idToggled.connect(self._on_toggle)

    def add(self, button, value) -> None:
        ident = len(self._values)
        self._values[ident] = value
        self._group.addButton(button, ident)
        pending = getattr(self, "_pending", None)
        if pending is not None and str(pending) == str(value):
            button.setChecked(True)
            self._last = value

    def get(self):
        ident = self._group.checkedId()
        return _coerce(self._values.get(ident, ""), self.type)

    def set(self, value) -> None:
        for ident, val in self._values.items():
            if str(val) == str(value):
                button = self._group.button(ident)
                if button is not None:
                    button.setChecked(True)
                return

    def on_change(self, fn) -> None:
        self._subs.append(fn)

    def _on_toggle(self, ident, checked) -> None:
        if not checked:
            return                         # the other half of every switch
        value = self._values.get(ident, "")
        if value == self._last:
            return
        self._last = value
        for fn in list(self._subs):
            try:
                fn(_coerce(value, self.type))
            except Exception as exc:
                print(f"[ports] {self.name}.on_change callback raised: {exc!r}")

    def enable(self, on: bool = True) -> None:
        for button in self._group.buttons():
            button.setEnabled(bool(on))


class _EventPort(_Port):
    """A port that carries no value — buttons, toolbars, menubars.

    Installs its own dispatcher that fans out to every on_fire subscriber AND
    calls the app.py method by late name lookup, so an override in App still
    wins.
    """

    __slots__ = ("_subs", "_ui", "_handler")

    def __init__(self, name, widget, *, ui, handler):
        super().__init__(name, widget, direction="e", type="event")
        self._subs = []
        self._ui, self._handler = ui, handler
        for signal in ("clicked", "fired"):
            sig = getattr(widget, signal, None)
            if sig is not None:
                sig.connect(lambda *a: self._dispatch(*a))
                break

    def _dispatch(self, *args) -> None:
        for fn in list(self._subs):
            try:
                fn()
            except Exception as exc:
                print(f"[ports] {self.name}.on_fire callback raised: {exc!r}")
        m = getattr(self._ui, self._handler, None)
        if callable(m):
            try:
                m(*args)
            except Exception as exc:
                print(f"[ports] {self.name} handler raised: {exc!r}")

    def on_fire(self, fn) -> None:
        self._subs.append(fn)

    def fire(self) -> None:
        self._dispatch()


class _TextPort(_Port):
    """A port over a QPlainTextEdit."""

    __slots__ = ("_subs", "_last")

    def __init__(self, name, widget, *, direction, type):
        super().__init__(name, widget, direction=direction, type=type)
        self._subs = []
        self._last = self.get()

    def get(self):
        return self.widget.toPlainText()

    def set(self, value) -> None:
        was = self.widget.isReadOnly()
        if was:
            self.widget.setReadOnly(False)
        self.widget.setPlainText(_s(value))
        if was:
            self.widget.setReadOnly(True)

    def on_change(self, fn) -> None:
        self._subs.append(fn)
        if len(self._subs) == 1:
            self.widget.textChanged.connect(self._fire)

    def _fire(self) -> None:
        value = self.get()
        if value == self._last:
            return
        self._last = value
        for fn in list(self._subs):
            try:
                fn(value)
            except Exception as exc:
                print(f"[ports] {self.name}.on_change callback raised: {exc!r}")

    def clear(self) -> None:
        self.set("")


class _ListPort(_Port):
    """A port over a QListWidget — the selection is the value."""

    __slots__ = ()

    def get(self):
        return [i.text() for i in self.widget.selectedItems()]

    def items(self):
        w = self.widget
        return [w.item(i).text() for i in range(w.count())]

    def set(self, values) -> None:
        self.widget.clear()
        for v in (values or []):
            self.widget.addItem(_s(v))

    def on_change(self, fn) -> None:
        self.widget.itemSelectionChanged.connect(lambda: fn(self.get()))

    def clear(self) -> None:
        self.set([])


class _TablePort(_Port):
    """A port over a QTreeWidget — selection returns each row's values."""

    __slots__ = ()

    def _row(self, item):
        return tuple(item.text(c) for c in range(self.widget.columnCount()))

    def get(self):
        return [self._row(i) for i in self.widget.selectedItems()]

    def rows(self):
        w = self.widget
        return [self._row(w.topLevelItem(i)) for i in range(w.topLevelItemCount())]

    def set(self, rows) -> None:
        from PySide6.QtWidgets import QTreeWidgetItem
        self.widget.clear()
        for row in (rows or []):
            QTreeWidgetItem(self.widget, [_s(v) for v in row])

    def on_change(self, fn) -> None:
        self.widget.itemSelectionChanged.connect(lambda: fn(self.get()))

    def clear(self) -> None:
        self.set([])


class _TabPort(_Port):
    """The current-tab port for a QTabWidget."""

    __slots__ = ()

    def get(self):
        return self.widget.currentIndex()

    def set(self, value) -> None:
        try:
            self.widget.setCurrentIndex(int(value))
        except (ValueError, TypeError):
            pass

    def on_change(self, fn) -> None:
        self.widget.currentChanged.connect(lambda i: fn(i))


class _ProxyPort(_Port):
    """A port that calls a WRITER method on a composite — LogPane.append,
    StatusBar.set, ImageCanvas.set_image, ChartPanel.figure_for_drawing."""

    __slots__ = ("_writer",)

    def __init__(self, name, widget, *, writer, type, direction):
        super().__init__(name, widget, direction=direction, type=type)
        self._writer = writer

    def set(self, value) -> None:
        m = getattr(self.widget, self._writer, None)
        if callable(m):
            m(value)

    def get(self):
        raise TypeError(
            f"port {self.name!r} is write-only "
            f"(composite {type(self.widget).__name__})")

    def clear(self) -> None:
        # An image panel and a status bar have an honest empty state — a status
        # bar left saying "3 of 5 frames bad" after the next scan failed was the
        # previous folder's answer shown beside this one. A log is the record of
        # what happened, so it is never wiped.
        if self._writer == "set_image":
            self.set(None)
        elif self._writer == "set":
            self.set("")


_SEQ_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                 ".webp")


def _natkey(path):
    """Sort key: frame_9 before frame_10, layer_9/ before layer_10/.

    A lexical sort puts 10 before 9, so a layer-wise scan comes out reordered
    and NOTHING LOOKS WRONG — the frames are all there, in a plausible order,
    just not the order they were captured in.
    """
    import re
    parts = []
    for seg in str(path).replace(chr(92), "/").split("/"):
        row = []
        for tok in re.split("([0-9]+)", seg):
            if tok.isdigit():
                try:
                    row.append((0, int(tok), ""))
                    continue
                except ValueError:
                    pass
            row.append((1, 0, tok.lower()))
        parts.append(row)
    return parts


class _FrameBrowser:
    """Folder -> ordered files -> index -> one displayed image.

    Wired from a `drives` declaration on the index widget. Talks only to PORTS,
    never to widget attribute names, so renaming a widget that keeps its port
    leaves this intact.

    ONE IMAGE IS DECODED AT A TIME. A capture folder can hold thousands of
    frames; listing them is cheap (names only) but decoding them is not.

    BOTH EVENTS ARE COALESCED. A slider fires once per integer crossed, so
    dragging across 5,000 frames asks for ~600 decodes; a typed path fires once
    per KEYSTROKE. Debouncing is what makes the drag smooth instead of a
    sequence of stalls. The timers are QTimers rather than Tk after() handles;
    the intervals are the ones the Tk target measured.
    """

    SUFFIXES = _SEQ_SUFFIXES
    SCAN_MS = 150     # coalesce a typed folder path
    SHOW_MS = 30      # coalesce a scrubber drag

    def __init__(self, folder_port, index_port, target_port, *,
                 suffixes=(), recursive=False, status_port=None,
                 roi_port=None, current_port=None):
        from PySide6.QtCore import QTimer
        self.folder, self.index, self.target = folder_port, index_port, target_port
        self.status = status_port
        self.roi = roi_port
        self.current = current_port
        self.suffixes = tuple(s.lower() for s in (suffixes or self.SUFFIXES))
        self.recursive = bool(recursive)
        self.files = []
        self._last = None
        self._root = ""
        self._roi_syncing = False
        self._scan_timer = QTimer()
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(self.reload)
        self._show_timer = QTimer()
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self.show)
        folder_port.on_change(self._folder_changed)
        index_port.on_change(self._index_changed)
        canvas = getattr(target_port, "widget", None)
        hook = getattr(canvas, "on_roi_change", None)
        if roi_port is not None and callable(hook):
            hook(self._roi_drawn)
            roi_port.on_change(self._roi_typed)
        # DEFERRED. __init__ runs inside MainUi._build(), before the window is
        # shown, so a folder restored from a port default would be scanned and
        # drawn into a widget that has no size yet.
        QTimer.singleShot(0, self.reload)

    # -- scheduling ---------------------------------------------------
    def _folder_changed(self, folder=None):
        self._scan_timer.start(self.SCAN_MS)

    def _index_changed(self, i=None):
        self._show_timer.start(self.SHOW_MS)

    # -- the two events ----------------------------------------------
    def reload(self, folder=None) -> None:
        """A new folder: relist, RESIZE THE INDEX to fit, show the first."""
        import os
        self._scan_timer.stop()
        folder = str(folder if folder is not None else self.folder.get() or "")
        folder = folder.strip().strip('"')
        if folder and not os.path.isdir(folder):
            return          # half-typed path: keep what is already loaded
        self._root = folder
        files, stack = [], ([folder] if folder else [])
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        if e.is_dir():
                            if self.recursive:
                                stack.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in self.suffixes:
                            files.append(e.path)
            except OSError:
                continue
        files.sort(key=_natkey)
        self.files = files
        hi = max(0, len(files) - 1)
        w = getattr(self.index, "widget", None)
        setter = getattr(w, "set_range", None)
        if callable(setter):
            try:
                setter(0, hi)
            except Exception:
                pass
        else:
            try:
                w.setRange(0, hi)
            except Exception:
                pass
        try:
            self.index.set(0)
        except Exception:
            pass
        # Both writes above trip the index change and queue a show. Cancel it,
        # or switching folders from a non-zero index decodes twice.
        self._show_timer.stop()
        self._last = None
        self.show(0)

    def show(self, i=None) -> None:
        """Display frame ``i``. Decodes exactly one image."""
        import os
        try:
            i = int(i if i is not None else self.index.get())
        except (TypeError, ValueError):
            return
        if not self.files:
            # CLEAR. Leaving the previous folder's frame on screen under a
            # message saying the folder is empty is worse than showing nothing.
            self._last = None
            try:
                self.target.set(None)
            except Exception:
                pass
            self._set_current("")
            folder = str(self.folder.get() or "").strip()
            if folder:
                self._say(f"No images in {folder}", error=True)
            else:
                self._say("Choose a folder of frames to view them", error=True)
            return
        i = max(0, min(i, len(self.files) - 1))
        if i == self._last:
            return                    # scrubbing fires repeatedly on one frame
        try:
            from PIL import Image
        except ImportError:
            self._say("Pillow is not installed, so frames cannot be shown "
                      "(pip install Pillow)", error=True)
            return
        try:
            with Image.open(self.files[i]) as im:
                # draft() makes libjpeg DCT-scale the decode, so a 6000x4000
                # JPEG never materialises full size. A no-op for PNG/TIFF.
                im.draft("RGB", (2048, 2048))
                if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
                    # 16-bit CT / layer scans clamp to near-white when shown
                    # directly; scaling here is what makes it look like the scan.
                    frame = im.point(lambda v: v * (1.0 / 256)).convert("L")
                else:
                    # copy() forces the decode, so the pixels survive the
                    # `with`. An explicit im.load() would be the obvious way and
                    # is refused by gui_policy — `.load` is denied wherever it
                    # appears, because that is also pickle.load's spelling.
                    frame = im.copy()
            self.target.set(frame)
            self._last = i
            self._set_current(self.files[i])
            self._say(f"{i + 1} / {len(self.files)}  "
                      f"{os.path.basename(self.files[i])}")
        except Exception as exc:
            self._last = None
            try:
                self.target.set(None)
            except Exception:
                pass
            self._set_current("")
            self._say(f"Cannot read {os.path.basename(self.files[i])}: {exc}",
                      error=True)

    def _set_current(self, path) -> None:
        """Publish the displayed file relative to the folder ("" for none)."""
        if self.current is None:
            return
        import os
        rel = ""
        if path:
            try:
                rel = os.path.relpath(path, getattr(self, "_root", "") or ".")
            except ValueError:            # another drive: keep it absolute
                rel = str(path)
        try:
            self.current.set(rel)
        except Exception:
            pass

    # -- ROI sync -----------------------------------------------------
    def _roi_drawn(self, roi) -> None:
        if self._roi_syncing or self.roi is None:
            return
        self._roi_syncing = True
        try:
            self.roi.set("" if roi is None else ", ".join(str(v) for v in roi))
        finally:
            self._roi_syncing = False

    def _roi_typed(self, text=None) -> None:
        if self._roi_syncing:
            return
        import re
        text = str(text if text is not None else self.roi.get() or "")
        nums = re.findall("[0-9]+", text)
        canvas = getattr(self.target, "widget", None)
        setter = getattr(canvas, "set_roi", None)
        if not callable(setter):
            return
        self._roi_syncing = True
        try:
            if not text.strip():
                setter(None)
            elif len(nums) == 4:
                setter(tuple(int(n) for n in nums))
            # Anything else is a half-typed box: leave the current one alone.
        finally:
            self._roi_syncing = False

    def _say(self, message, error: bool = False) -> None:
        """Report to the status port if one is declared. An ERROR with no
        status port is shown in the image panel itself — the user is looking
        there."""
        if self.status is not None:
            try:
                self.status.set(message)
                return
            except Exception:
                pass
        if error:
            show = getattr(getattr(self.target, "widget", None),
                           "show_message", None)
            if callable(show):
                try:
                    show(message)
                except Exception:
                    pass
        print(f"[ports] {message}")

    # -- read-only detail, for hand-written code ---------------------
    def count(self) -> int:
        return len(self.files)

    def path(self, i=None) -> str:
        try:
            i = int(i if i is not None else self.index.get())
        except (TypeError, ValueError):
            return ""
        return self.files[i] if 0 <= i < len(self.files) else ""


class _PortsBase:
    """Common iteration + rename shim for the generated Ports subclass."""

    def __getitem__(self, name):
        if name in RENAMED:
            name = RENAMED[name]
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(name)

    def __contains__(self, name):
        return hasattr(self, RENAMED.get(name, name))

    def __iter__(self):
        for name in getattr(self, "_names", ()):
            yield getattr(self, name)

    def read(self) -> dict:
        """{name: value} for every in/inout port. Snapshot for the app."""
        out = {}
        for name in getattr(self, "_names", ()):
            p = getattr(self, name)
            if p.direction in ("i", "io"):
                try:
                    out[name] = p.get()
                except Exception:
                    pass
        return out

    def apply(self, values) -> None:
        """Write out/inout ports from {name: value}. Unknown names ignored."""
        for name, value in dict(values or {}).items():
            p = getattr(self, RENAMED.get(name, name), None)
            if p is not None and p.direction in ("o", "io"):
                try:
                    p.set(value)
                except Exception:
                    pass
'''


def emit_ports(spec: Spec, aliases: Optional[Dict[str, str]] = None) -> str:
    """Generate ui/ports.py: PORTS_RUNTIME + a project-specific Ports."""
    aliases = dict(aliases or {})
    L: List[str] = [PORTS_RUNTIME]

    if aliases:
        L.append("")
        L.append("# aliases — hand-written app.py still uses these old names")
        for old, new in sorted(aliases.items()):
            L.append(f"RENAMED[{_py(old)}] = {_py(new)}")
        L.append("")

    ports = spec.ports
    L.append("")
    L.append("class Ports(_PortsBase):")
    L.append('    """Every declared binding for this window, by name.\n')
    L.append('    Built by ui.main_ui.MainUi._build() as `self.ports = Ports(self)`.')
    L.append('    """')
    L.append(f"    _names = {tuple(p.name for p in ports)!r}"
             if ports else "    _names = ()")
    L.append("")
    L.append("    def __init__(self, ui):")
    L.append("        self._ui = ui")
    if not ports:
        L.append("        # no bindable widgets on this window")
        L.append("        pass")
        return "\n".join(L).rstrip() + "\n"

    by_shape = {w.shape_id: w for w in spec.widgets}

    for p in ports:
        primary = by_shape.get(p.shape_ids[0]) if p.shape_ids else None
        if primary is None:
            continue
        wname = f"ui.{primary.name}"
        default = _py(p.default) if p.default is not None else "None"
        label = _c(primary.label or p.name)

        if p.binder == "event":
            # A menubar's handler is connected in MainUi (_handler_lines), as
            # on_toolbar is, so its port calls none — naming it here too would
            # run every menu item twice.
            handler = "" if primary.kind == "menubar" else primary.handler
            L.append(f"        # {p.kind}: {label} -> event")
            L.append(f"        self.{p.name} = _EventPort(\n"
                     f"            {_py(p.name)}, {wname}, ui=ui, "
                     f"handler={_py(handler or '')})")
        elif p.binder == "text":
            L.append(f"        # {p.kind}: {label} -> str")
            L.append(f"        self.{p.name} = _TextPort(\n"
                     f"            {_py(p.name)}, {wname}, "
                     f"direction={_py(p.direction)}, type={_py(p.type)})")
            if p.default is not None:
                L.append(f"        self.{p.name}.set({default})")
        elif p.binder == "list":
            L.append(f"        # {p.kind}: {label} -> selection")
            L.append(f"        self.{p.name} = _ListPort(\n"
                     f"            {_py(p.name)}, {wname}, "
                     f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "table":
            L.append(f"        # {p.kind}: {label} -> selection (rows)")
            L.append(f"        self.{p.name} = _TablePort(\n"
                     f"            {_py(p.name)}, {wname}, "
                     f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "tab":
            L.append(f"        # {p.kind}: {label} -> current tab")
            L.append(f"        self.{p.name} = _TabPort(\n"
                     f"            {_py(p.name)}, {wname}, "
                     f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "proxy":
            L.append(f"        # {p.kind}: {label} -> {p.writer}")
            L.append(f"        self.{p.name} = _ProxyPort(\n"
                     f"            {_py(p.name)}, {wname}, writer={_py(p.writer)}, "
                     f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "var":
            if p.kind == "radiobutton":
                L.append(f"        # {p.kind}: {label} -> {p.type}, {p.direction}")
                L.append(f"        self.{p.name} = _RadioPort(\n"
                         f"            {_py(p.name)}, {wname}, "
                         f"type={_py(p.type)}, direction={_py(p.direction)},\n"
                         f"            default={default})")
                for sid in p.shape_ids:
                    member = by_shape.get(sid)
                    if member is None:
                        continue
                    val = str(member.props.get("value")
                              or _default_radio_value(member))
                    L.append(f"        self.{p.name}.add(ui.{member.name}, "
                             f"{_py(val)})")
                continue
            # A caption the user bound must not be blanked by the binding: seed
            # the port with the text the widget was built with.
            if (p.default is None and p.kind == "label" and _text_of(primary)):
                default = _py(_text_of(primary))
            L.append(f"        # {p.kind}: {label} -> {p.type}, {p.direction}")
            L.append(f"        self.{p.name} = _WidgetPort(\n"
                     f"            {_py(p.name)}, {wname}, kind={_py(p.kind)},\n"
                     f"            type={_py(p.type)}, "
                     f"direction={_py(p.direction)},\n"
                     f"            default={default}, deep={_py(bool(p.deep))})")
        else:
            L.append(f"        # {p.kind}: unknown binder {p.binder!r} — dropped")
            continue

    # -- sequence links, LAST: a browser references three finished ports ----
    by_port = {p.name: p for p in ports}
    for w in spec.widgets:
        d = dict(getattr(w, "drives", None) or {})
        if not d:
            continue
        folder, target = str(d.get("folder") or ""), str(d.get("target") or "")
        status = str(d.get("status") or "")
        roi = str(d.get("roi") or "")
        current = str(d.get("current") or "")
        index = w.port.name if w.port else ""
        if not (folder in by_port and target in by_port and index):
            L.append(f"        # {w.name}: sequence link skipped — "
                     f"folder={folder!r} target={target!r} index={index!r}")
            continue
        L.append(f"        # {w.name} steps through {folder} -> {target}")
        L.append(f"        self.browse_{index} = _FrameBrowser(")
        L.append(f"            self.{folder}, self.{index}, self.{target},")
        L.append(f"            suffixes={_py(tuple(d.get('suffixes') or ()))},")
        L.append(f"            recursive={_py(bool(d.get('recursive')))},")
        L.append(f"            status_port="
                 f"{('self.' + status) if status in by_port else 'None'},")
        L.append(f"            roi_port="
                 f"{('self.' + roi) if roi in by_port else 'None'},")
        L.append(f"            current_port="
                 f"{('self.' + current) if current in by_port else 'None'})")
    return "\n".join(L).rstrip() + "\n"


def _default_radio_value(w: WidgetSpec) -> str:
    """Fallback for a radio whose ``value=`` prop is empty."""
    import re
    return re.sub(r"[^a-z0-9]+", "_",
                  str(w.label or w.name).strip().lower()).strip("_")
