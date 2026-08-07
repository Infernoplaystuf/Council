"""
gui_emit.py — Spec -> Tkinter source. Template-driven, deterministic.

No Tk, no model: this module writes text. It reads ONLY a validated gui_spec.Spec,
so it never has to ask whether a kind is real or a prop is allowed — gui_spec
answered that, and every template can be unconditional.

WHY THE OUTPUT IS SPLIT ACROSS FILES
------------------------------------
ui/ is 100% generated and overwritten on every regeneration. app.py and
handlers.py are hand-written, created once, and NEVER rewritten. That structural
split — not a three-way AST merge — is what makes "regenerate without losing my
edits" true. A merge is fragile exactly when it matters most: the moment the
generated side and the hand-written side both changed. Keeping them in separate
files means the question never arises.

main_ui.py builds every widget and binds command=self.on_<name>; the method
on_<name> lives in app.py. Layout changes go on the canvas, behaviour changes go
in app.py, and the two cannot collide.

WHY THERE IS NO TIMESTAMP IN ui/
--------------------------------
Regeneration must be byte-identical for an unchanged spec — that is what lets a
diff preview show "nothing changed" honestly and what makes the idempotence test
meaningful. A generated-on header would break it on every run and quietly turn
every regeneration into a diff.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gui_shapes import PALETTE
from gui_spec import COMMAND_KINDS, Spec, WidgetSpec

# Sentinel regions (spec 7.4). Body text between these markers survives
# regeneration. Used sparingly — the two-file split is the primary mechanism.
REGION_OPEN = re.compile(r"^(\s*)#\s*region:\s*custom:([A-Za-z0-9_.]+)")
REGION_CLOSE = re.compile(r"^\s*#\s*endregion\b")

# Modules a "linked" project may import (spec 8). council_engine is absent on
# purpose: importing it from a generated app would build a SECOND GGUF singleton
# in a second process.
LINKED_ALLOWLIST = (
    "image_stats", "image_index", "plot_registry", "plots_pane", "graph_data",
    "vault_analyst", "data_index", "df_cache", "stats_cache", "provenance",
)


@dataclass
class EmitResult:
    files_written: List[str] = field(default_factory=list)
    files_skipped: List[str] = field(default_factory=list)   # never rewritten
    handlers_added: List[str] = field(default_factory=list)
    orphaned_regions: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ============================================================
# Sentinel regions
# ============================================================

def extract_regions(source: str) -> Dict[str, str]:
    """{region_id: body} from existing generated source.

    Line-based rather than AST-based on purpose: a region body is arbitrary user
    code that may not parse on its own, and an unparseable body must still be
    carried across rather than silently dropped."""
    out: Dict[str, str] = {}
    cur: Optional[str] = None
    buf: List[str] = []
    for line in (source or "").splitlines():
        if cur is None:
            m = REGION_OPEN.match(line)
            if m:
                cur, buf = m.group(2), []
            continue
        if REGION_CLOSE.match(line):
            out[cur] = "\n".join(buf)
            cur = None
            continue
        buf.append(line)
    return out


def _region(rid: str, indent: str, body: str = "") -> List[str]:
    """Emit a region block, carrying ``body`` if one was preserved."""
    lines = [f"{indent}# region: custom:{rid} -- preserved across regeneration"]
    if body.strip():
        lines.extend(body.splitlines())
    lines.append(f"{indent}# endregion")
    return lines


# ============================================================
# Value rendering
# ============================================================

def _py(value: Any) -> str:
    """A prop value as a Python literal, DOUBLE-quoted where black would be.

    repr() gives single quotes, which is valid Python and wrong-looking: the
    brief asks for output that is black-clean in shape, and black normalises
    string literals to double quotes. Mixed quoting is the tell that a file was
    machine-written, and the whole point of quantising padding and naming
    widgets from labels is that the result reads as if a person wrote it.

    A string already containing a double quote keeps repr()'s choice rather
    than growing a backslash — which is also what black does."""
    if isinstance(value, str):
        if '"' in value:
            return repr(value)
        return '"' + value.replace("\\", "\\\\") + '"'
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_py(v) for v in value) + "]"
    if value is None:
        return "None"
    return repr(value)


def _prop(w: WidgetSpec, key: str, default: Any = None) -> Any:
    schema = PALETTE.get(w.kind, {}).get("prop_schema") or {}
    if key in (w.props or {}):
        return w.props[key]
    if key in schema and "default" in schema[key]:
        return schema[key]["default"]
    return default


def _text_of(w: WidgetSpec) -> str:
    return str(_prop(w, "text", "") or w.label or "")


# ============================================================
# Per-kind construction
# ============================================================

def construct(w: WidgetSpec, parent: str) -> str:
    """The right-hand side of `self.<name> = ...` for one widget."""
    k = w.kind
    cmd = f", command=self.{w.handler}" if w.handler else ""

    if k in ("frame", "freeform"):
        return f"ttk.Frame({parent})"
    if k == "labelframe":
        return f"ttk.LabelFrame({parent}, text={_py(_text_of(w))})"
    if k == "notebook":
        return f"ttk.Notebook({parent})"
    if k == "panedwindow":
        return (f"ttk.PanedWindow({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))})")
    if k == "label":
        return (f"ttk.Label({parent}, text={_py(_text_of(w))}, "
                f"anchor={_py(_prop(w, 'anchor', 'w'))})")
    if k == "button":
        return f"ttk.Button({parent}, text={_py(_text_of(w))}{cmd})"
    if k == "entry":
        show = _prop(w, "show", "")
        extra = f", show={_py(show)}" if show else ""
        return (f"ttk.Entry({parent}, "
                f"justify={_py(_prop(w, 'justify', 'left'))}{extra})")
    if k == "text":
        return f"tk.Text({parent}, wrap={_py(_prop(w, 'wrap', 'word'))})"
    if k == "checkbutton":
        return f"ttk.Checkbutton({parent}, text={_py(_text_of(w))}{cmd})"
    if k == "radiobutton":
        return (f"ttk.Radiobutton({parent}, text={_py(_text_of(w))}, "
                f"value={_py(_prop(w, 'value', ''))}{cmd})")
    if k == "combobox":
        state = "readonly" if _prop(w, "readonly", True) else "normal"
        return (f"ttk.Combobox({parent}, "
                f"values={_py(_prop(w, 'values', []))}, state={_py(state)})")
    if k == "listbox":
        return (f"tk.Listbox({parent}, "
                f"selectmode={_py(_prop(w, 'selectmode', 'browse'))})")
    if k == "spinbox":
        return (f"ttk.Spinbox({parent}, from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))}, "
                f"increment={_py(_prop(w, 'increment', 1))}{cmd})")
    if k == "scale":
        return (f"ttk.Scale({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))}, "
                f"from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))})")
    if k == "progressbar":
        return (f"ttk.Progressbar({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))}, "
                f"mode={_py(_prop(w, 'mode', 'determinate'))})")
    if k == "separator":
        return (f"ttk.Separator({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))})")
    if k == "treeview":
        cols = list(_prop(w, "columns", []) or [])
        show = "headings" if _prop(w, "mode", "table") == "table" else "tree"
        return (f"ttk.Treeview({parent}, columns={_py(cols)}, "
                f"show={_py(show)})")
    # ---- composites (ui/widgets.py) ----
    if k == "image_canvas":
        return (f"ImageCanvas({parent}, "
                f"overlay={_py(bool(_prop(w, 'overlay', False)))}, "
                f"overlay_alpha={_py(float(_prop(w, 'overlay_alpha', 0.5)))})")
    if k == "chart_panel":
        return (f"ChartPanel({parent}, "
                f"toolbar={_py(bool(_prop(w, 'toolbar', False)))})")
    if k == "scrubber":
        return (f"Scrubber({parent}, from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))}, "
                f"show_total={_py(bool(_prop(w, 'show_total', True)))}{cmd})")
    if k == "log_pane":
        return (f"LogPane({parent}, "
                f"autoscroll={_py(bool(_prop(w, 'autoscroll', True)))})")
    if k == "file_picker":
        return (f"FilePicker({parent}, mode={_py(_prop(w, 'mode', 'file'))}, "
                f"filetypes={_py(_prop(w, 'filetypes', []))}{cmd})")
    if k == "status_bar":
        return (f"StatusBar({parent}, "
                f"progress={_py(bool(_prop(w, 'progress', False)))})")
    if k == "toolbar":
        return (f"Toolbar({parent}, buttons={_py(_prop(w, 'buttons', []))}, "
                f"command=self.on_toolbar)")
    if k == "menubar":
        return f"tk.Menu({parent})"
    return f"ttk.Frame({parent})"    # unreachable: gui_spec.validate gates kinds


def place_call(w: WidgetSpec) -> str:
    """The geometry-manager call for one widget."""
    if w.manager == "pack":
        return f'self.{w.name}.pack(fill="both", expand=True, padx={w.padx}, pady={w.pady})'
    if w.manager == "place":
        return (f"self.{w.name}.place(relx={w.relx}, rely={w.rely}, "
                f"relwidth={w.relwidth}, relheight={w.relheight})")
    bits = [f"row={w.row}", f"column={w.column}"]
    if w.rowspan > 1:
        bits.append(f"rowspan={w.rowspan}")
    if w.columnspan > 1:
        bits.append(f"columnspan={w.columnspan}")
    if w.sticky:
        bits.append(f"sticky={_py(w.sticky)}")
    bits.append(f"padx={w.padx}")
    bits.append(f"pady={w.pady}")
    return f"self.{w.name}.grid({', '.join(bits)})"


def _grid_config(target: str, rows: Sequence[int], cols: Sequence[int],
                 row_min: Sequence[int], col_min: Sequence[int],
                 indent: str) -> List[str]:
    """rowconfigure/columnconfigure lines for a container.

    Emitted even when a weight is 0: stating it makes the grid explicit and
    keeps the generated file a faithful, readable record of the inferred
    layout rather than something the reader has to reconstruct."""
    out: List[str] = []
    for i, wgt in enumerate(rows):
        ms = row_min[i] if i < len(row_min) else 0
        extra = f", minsize={ms}" if ms else ""
        out.append(f"{indent}{target}.rowconfigure({i}, weight={wgt}{extra})")
    for i, wgt in enumerate(cols):
        ms = col_min[i] if i < len(col_min) else 0
        extra = f", minsize={ms}" if ms else ""
        out.append(f"{indent}{target}.columnconfigure({i}, weight={wgt}{extra})")
    return out


# ============================================================
# ui/main_ui.py
# ============================================================

def _ordered(spec: Spec) -> List[WidgetSpec]:
    """Parents before children — a widget cannot be constructed before the
    widget it is parented to exists."""
    by_name = {w.name: w for w in spec.widgets}
    out: List[WidgetSpec] = []
    seen: set = set()

    def visit(w: WidgetSpec) -> None:
        if w.name in seen:
            return
        if w.parent and w.parent in by_name and w.parent not in seen:
            visit(by_name[w.parent])
        seen.add(w.name)
        out.append(w)

    for w in spec.widgets:
        visit(w)
    return out


def emit_main_ui(spec: Spec, regions: Optional[Dict[str, str]] = None) -> str:
    r = dict(regions or {})
    composites = sorted({w.kind for w in spec.widgets
                         if w.kind in _COMPOSITE_KINDS})
    L: List[str] = [
        '"""Generated by the GUI Designer. DO NOT EDIT — regeneration',
        'overwrites this file. Behaviour belongs in app.py; small in-place',
        'additions belong in a `# region: custom:<id>` block, which survives.',
        '"""',
        "from __future__ import annotations",
        "",
        "import tkinter as tk",
        "from tkinter import ttk",
        "",
    ]
    if composites:
        L.append("from .widgets import " + ", ".join(
            _COMPOSITE_KINDS[k] for k in composites))
        L.append("")
    L += [
        "",
        "class MainUi(ttk.Frame):",
        '    """Every widget, built and placed. Handlers live in app.py."""',
        "",
        "    def __init__(self, master=None, **kw):",
        "        super().__init__(master, **kw)",
        "        self._build()",
        "",
        "    def _build(self) -> None:",
    ]
    ind = " " * 8
    L += _grid_config("self", spec.root_row_weights, spec.root_col_weights,
                      spec.root_row_minsizes, spec.root_col_minsizes, ind)
    L.append("")

    for w in _ordered(spec):
        parent = f"self.{w.parent}" if w.parent else "self"
        L.append(f"{ind}# {w.kind}: {w.label or w.name}")
        L.append(f"{ind}self.{w.name} = {construct(w, parent)}")
        L.append(f"{ind}{place_call(w)}")
        if w.is_container and (w.row_weights or w.col_weights):
            L += _grid_config(f"self.{w.name}", w.row_weights, w.col_weights,
                              w.row_minsizes, w.col_minsizes, ind)
        if w.is_container and w.explicit_w and w.explicit_h:
            L.append(f"{ind}self.{w.name}.configure(width={w.explicit_w}, "
                     f"height={w.explicit_h})")
            L.append(f"{ind}self.{w.name}.grid_propagate(False)")
        if w.kind == "treeview":
            cols = list(_prop(w, "columns", []) or [])
            for c in cols:
                L.append(f"{ind}self.{w.name}.heading({_py(c)}, text={_py(c)})")
        L += _region(w.name, ind, r.pop(w.name, ""))
        L.append("")

    L += [
        "    # -- handler hooks -------------------------------------------",
        "    # Defined here so main_ui is runnable on its own; app.py overrides",
        "    # them. Without these a preview of the raw UI would die on the",
        "    # first click with an AttributeError.",
    ]
    for h in spec.handlers + ["on_toolbar"]:
        L.append(f"    def {h}(self, *args) -> None:")
        L.append("        pass")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


_COMPOSITE_KINDS = {
    "image_canvas": "ImageCanvas", "chart_panel": "ChartPanel",
    "scrubber": "Scrubber", "log_pane": "LogPane",
    "file_picker": "FilePicker", "status_bar": "StatusBar",
    "toolbar": "Toolbar",
}


# ============================================================
# ui/widgets.py — the composites, hand-written
# ============================================================

WIDGETS_PY = '''"""Composite widgets used by the generated UI.

Generated once per project and overwritten on regeneration, but hand-written
here rather than assembled from templates: these are the difference between a
mockup and a usable tool, and each carries a `# region: custom:` block for
per-project extension.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk


class ImageCanvas(ttk.Frame):
    """Image viewer: pan, zoom-to-fit, zoom-to-cursor, optional alpha overlay.

    Zoom is anchored to the CURSOR, not the widget centre. Centre-anchored zoom
    is the classic mistake — the thing under the pointer slides away and the
    user chases it, which on a layer-wise scan is unusable.
    """

    def __init__(self, master=None, *, overlay: bool = False,
                 overlay_alpha: float = 0.5, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#1e1e2e")
        self.canvas.pack(fill="both", expand=True)
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._pan_from = None
        self._base = None          # PIL.Image
        self._overlay_img = None   # PIL.Image
        self._photo = None         # keep a reference or Tk drops the image
        self.overlay_enabled = bool(overlay)
        self.overlay_alpha = float(overlay_alpha)

        self.canvas.bind("<ButtonPress-1>", self._pan_start)
        self.canvas.bind("<B1-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_pan_from", None))
        self.canvas.bind("<MouseWheel>", self._wheel)          # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(1.1, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(1 / 1.1, e.x, e.y))
        self.canvas.bind("<Configure>", lambda e: self._render())

    # -- public ------------------------------------------------------
    def set_image(self, image) -> None:
        """``image`` is a PIL.Image."""
        self._base = image
        self.zoom_to_fit()

    def set_overlay(self, image, alpha: float = None) -> None:
        self._overlay_img = image
        if alpha is not None:
            self.overlay_alpha = float(alpha)
        self._render()

    def clear_overlay(self) -> None:
        self._overlay_img = None
        self._render()

    def zoom_to_fit(self) -> None:
        if self._base is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self._base.size
        self._scale = min(cw / iw, ch / ih) if iw and ih else 1.0
        self._ox = (cw - iw * self._scale) / 2
        self._oy = (ch - ih * self._scale) / 2
        self._render()

    # -- interaction -------------------------------------------------
    def _pan_start(self, e) -> None:
        self._pan_from = (e.x, e.y)

    def _pan_move(self, e) -> None:
        if self._pan_from is None:
            return
        dx = e.x - self._pan_from[0]
        dy = e.y - self._pan_from[1]
        self._pan_from = (e.x, e.y)
        self._ox += dx
        self._oy += dy
        self._render()

    def _wheel(self, e) -> None:
        self._zoom_at(1.1 if e.delta > 0 else 1 / 1.1, e.x, e.y)

    def _zoom_at(self, factor: float, cx: float, cy: float) -> None:
        new = max(0.02, min(40.0, self._scale * factor))
        if new == self._scale:
            return
        # Keep the image point under the cursor fixed.
        self._ox = cx - (cx - self._ox) * (new / self._scale)
        self._oy = cy - (cy - self._oy) * (new / self._scale)
        self._scale = new
        self._render()

    # -- painting ----------------------------------------------------
    def _render(self) -> None:
        self.canvas.delete("all")
        if self._base is None:
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.canvas.create_text(10, 10, anchor="nw", fill="#f38ba8",
                                    text="Pillow is required to show images")
            return
        iw, ih = self._base.size
        w = max(1, int(iw * self._scale))
        h = max(1, int(ih * self._scale))
        img = self._base.resize((w, h), Image.NEAREST).convert("RGBA")
        if self.overlay_enabled and self._overlay_img is not None:
            ov = self._overlay_img.resize((w, h), Image.NEAREST).convert("RGBA")
            img = Image.blend(img, ov, max(0.0, min(1.0, self.overlay_alpha)))
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(self._ox, self._oy, anchor="nw",
                                 image=self._photo)

    # region: custom:ImageCanvas -- preserved across regeneration
    # endregion


class ChartPanel(ttk.Frame):
    """A matplotlib Figure embedded via FigureCanvasTkAgg.

    Builds Figure() directly and never touches pyplot — a pyplot figure is held
    forever by its global registry, so a panel that redrew on every update
    would leak one figure per redraw. Same rule the app's plots_pane follows.
    """

    def __init__(self, master=None, *, toolbar: bool = False, **kw):
        super().__init__(master, **kw)
        self._want_toolbar = bool(toolbar)
        self._canvas = None
        self._toolbar = None
        self.figure = None
        self._placeholder = ttk.Label(
            self, text="(no chart yet)", anchor="center")
        self._placeholder.pack(fill="both", expand=True)

    def figure_for_drawing(self):
        """A fresh Figure, cleared and ready. Draw on it, then call redraw()."""
        from matplotlib.figure import Figure
        if self.figure is None:
            self.figure = Figure(figsize=(5, 3), dpi=100)
        self.figure.clear()
        return self.figure

    def redraw(self) -> None:
        from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                       NavigationToolbar2Tk)
        if self.figure is None:
            return
        self._placeholder.pack_forget()
        if self._canvas is None:
            self._canvas = FigureCanvasTkAgg(self.figure, master=self)
            if self._want_toolbar:
                self._toolbar = NavigationToolbar2Tk(self._canvas, self,
                                                     pack_toolbar=False)
                self._toolbar.update()
                self._toolbar.pack(side="bottom", fill="x")
            self._canvas.get_tk_widget().pack(side="top", fill="both",
                                              expand=True)
        self._canvas.draw()

    # region: custom:ChartPanel -- preserved across regeneration
    # endregion


class Scrubber(ttk.Frame):
    """Scale + index box + prev/next + total, bound to one integer index."""

    def __init__(self, master=None, *, from_: int = 0, to: int = 100,
                 show_total: bool = True, command=None, **kw):
        super().__init__(master, **kw)
        self._command = command
        self._lo = int(from_)
        self._hi = int(to)
        self.var = tk.IntVar(value=self._lo)

        ttk.Button(self, text="\\u25c0", width=3,
                   command=lambda: self.step(-1)).pack(side="left")
        self.scale = ttk.Scale(self, orient="horizontal", from_=self._lo,
                               to=self._hi, command=self._from_scale)
        self.scale.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(self, text="\\u25b6", width=3,
                   command=lambda: self.step(1)).pack(side="left")
        self.entry = ttk.Entry(self, width=6, justify="right")
        self.entry.pack(side="left", padx=(4, 0))
        self.entry.bind("<Return>", self._from_entry)
        self.total = ttk.Label(self, text=f"/ {self._hi}" if show_total else "")
        self.total.pack(side="left", padx=(2, 0))
        self.set(self._lo)

    def set_range(self, lo: int, hi: int) -> None:
        self._lo, self._hi = int(lo), int(hi)
        self.scale.configure(from_=self._lo, to=self._hi)
        self.total.configure(text=f"/ {self._hi}")
        self.set(min(max(self.get(), self._lo), self._hi))

    def get(self) -> int:
        return int(self.var.get())

    def set(self, value: int, notify: bool = False) -> None:
        v = max(self._lo, min(self._hi, int(value)))
        self.var.set(v)
        self.scale.set(v)
        self.entry.delete(0, "end")
        self.entry.insert(0, str(v))
        if notify and self._command:
            self._command(v)

    def step(self, delta: int) -> None:
        self.set(self.get() + delta, notify=True)

    def _from_scale(self, raw) -> None:
        # Tk hands the Scale callback a float string; rounding here keeps the
        # entry and the callback integral, which an index must be.
        try:
            v = int(round(float(raw)))
        except (TypeError, ValueError):
            return
        if v != self.get():
            self.set(v, notify=True)

    def _from_entry(self, _e=None) -> None:
        try:
            self.set(int(self.entry.get()), notify=True)
        except ValueError:
            self.set(self.get())

    # region: custom:Scrubber -- preserved across regeneration
    # endregion


class LogPane(ttk.Frame):
    """Read-only log, tag-coloured by level, with an autoscroll toggle."""

    COLOURS = {"info": "#cdd6f4", "warn": "#f9e2af", "error": "#f38ba8",
               "debug": "#a6adc8", "ok": "#a6e3a1"}

    def __init__(self, master=None, *, autoscroll: bool = True, **kw):
        super().__init__(master, **kw)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.text = tk.Text(self, wrap="word", state="disabled", height=8,
                            background="#181825", foreground="#cdd6f4",
                            insertbackground="#cdd6f4", relief="flat")
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)
        for level, colour in self.COLOURS.items():
            self.text.tag_configure(level, foreground=colour)
        self.autoscroll = tk.BooleanVar(value=bool(autoscroll))
        ttk.Checkbutton(self, text="Autoscroll",
                        variable=self.autoscroll).grid(row=1, column=0,
                                                       sticky="w")

    def append(self, message: str, level: str = "info") -> None:
        # The widget is disabled so the user cannot type into it; writing
        # requires flipping it back briefly. Doing that here keeps every caller
        # from having to remember.
        self.text.configure(state="normal")
        self.text.insert("end", str(message).rstrip() + "\\n",
                         level if level in self.COLOURS else "info")
        self.text.configure(state="disabled")
        if self.autoscroll.get():
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    # region: custom:LogPane -- preserved across regeneration
    # endregion


class FilePicker(ttk.Frame):
    """Entry + Browse, for a file, a folder, or a save target."""

    def __init__(self, master=None, *, mode: str = "file",
                 filetypes=None, command=None, **kw):
        super().__init__(master, **kw)
        self.mode = mode
        self.filetypes = list(filetypes or [])
        self._command = command
        self.var = tk.StringVar()
        ttk.Entry(self, textvariable=self.var).pack(side="left", fill="x",
                                                    expand=True)
        ttk.Button(self, text="Browse...", command=self.browse).pack(
            side="left", padx=(4, 0))

    def get(self) -> str:
        return self.var.get()

    def set(self, path: str) -> None:
        self.var.set(str(path))

    def browse(self) -> None:
        ft = [(t, t) for t in self.filetypes] or [("All files", "*.*")]
        if self.mode == "folder":
            path = filedialog.askdirectory()
        elif self.mode == "save":
            path = filedialog.asksaveasfilename(filetypes=ft)
        else:
            path = filedialog.askopenfilename(filetypes=ft)
        if path:
            self.set(path)
            if self._command:
                self._command(path)

    # region: custom:FilePicker -- preserved across regeneration
    # endregion


class StatusBar(ttk.Frame):
    """Bottom strip: a message and an optional inline progress bar."""

    def __init__(self, master=None, *, progress: bool = False, **kw):
        super().__init__(master, **kw)
        self.columnconfigure(0, weight=1)
        self.message = ttk.Label(self, text="", anchor="w")
        self.message.grid(row=0, column=0, sticky="ew", padx=4)
        self.progress = None
        if progress:
            self.progress = ttk.Progressbar(self, mode="determinate",
                                            length=140)
            self.progress.grid(row=0, column=1, sticky="e", padx=4)

    def set(self, text: str) -> None:
        self.message.configure(text=str(text))

    def set_progress(self, value: float) -> None:
        if self.progress is not None:
            self.progress["value"] = max(0.0, min(100.0, float(value)))

    # region: custom:StatusBar -- preserved across regeneration
    # endregion


class Toolbar(ttk.Frame):
    """A horizontal button strip. ``command`` receives the button label."""

    def __init__(self, master=None, *, buttons=None, command=None, **kw):
        super().__init__(master, **kw)
        self._command = command
        self.buttons = {}
        for name in list(buttons or []):
            b = ttk.Button(self, text=name,
                           command=lambda n=name: self._fire(n))
            b.pack(side="left", padx=2, pady=2)
            self.buttons[name] = b

    def _fire(self, name: str) -> None:
        if self._command:
            self._command(name)

    # region: custom:Toolbar -- preserved across regeneration
    # endregion
'''


# ============================================================
# Hand-written files (created once, never rewritten)
# ============================================================

def emit_app_py(spec: Spec) -> str:
    return f'''"""Hand-written application code for {spec.project}.

This file is created ONCE and never rewritten by the designer. Put behaviour
here: the generated MainUi builds the widgets and calls self.on_<name>, and
those methods live below.

Regenerating the wireframe rewrites ui/ only. Nothing here is touched.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.main_ui import MainUi


class App(MainUi):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)


def main() -> None:
    root = tk.Tk()
    root.title({_py(spec.title)})
    root.minsize({spec.min_w}, {spec.min_h})
    app = App(root)
    app.pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
'''


def handler_stub(name: str) -> str:
    return (f"\n    def {name}(self, *args) -> None:\n"
            f'        """TODO: implement."""\n'
            f"        pass\n")


def emit_handlers_py(spec: Spec) -> str:
    body = "".join(handler_stub(h) for h in spec.handlers) or "\n    pass\n"
    return f'''"""Handler stubs for {spec.project}.

APPEND-ONLY. Regeneration adds stubs for NEW widgets to the end of this file
and never rewrites what is already here.

Mix HandlerMixin into App (in app.py) if you would rather keep behaviour out of
app.py itself.
"""
from __future__ import annotations


class HandlerMixin:
{body}'''


def emit_launch_py(spec: Spec, project_dir: Path) -> str:
    linked = spec.mode == "linked"
    note = (
        "# linked mode: the app's own directory is put on sys.path so the\n"
        "# generated code may import the allowlisted analysis modules\n"
        f"# ({', '.join(LINKED_ALLOWLIST)}).\n"
        "# council_engine is deliberately NOT importable: it would build a\n"
        "# second GGUF singleton in this second process.\n"
        if linked else
        "# standalone mode: stdlib plus pandas/numpy/matplotlib/Pillow only.\n"
        "# Portable — zip this directory and it runs anywhere.\n")
    path_block = (
        "_APP_ROOT = Path(__file__).resolve().parent.parent.parent\n"
        "if str(_APP_ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(_APP_ROOT))\n" if linked else "")
    return f'''"""Launcher for {spec.project}. Generated."""
from __future__ import annotations

import sys
from pathlib import Path

{note}
sys.path.insert(0, str(Path(__file__).resolve().parent))
{path_block}
from app import main

if __name__ == "__main__":
    main()
'''


# ============================================================
# emit
# ============================================================

def emit(spec: Spec, project_path: Any, *,
         preserve_regions: Optional[Dict[str, str]] = None) -> EmitResult:
    """Write the project. ui/ is overwritten; app.py and handlers.py are not.

    Sentinel-region bodies are read from the EXISTING ui/ files before anything
    is written, so a region survives even though the file around it is
    regenerated from scratch. A region whose id no longer exists is reported
    rather than dropped — losing user code silently is the one outcome the
    round-trip design exists to prevent."""
    res = EmitResult()
    root = Path(project_path)
    ui = root / "ui"
    ui.mkdir(parents=True, exist_ok=True)

    # Harvest regions from what is already on disk, then let the caller's map
    # win (it may carry regions rescued from a backup).
    regions: Dict[str, str] = {}
    for existing in sorted(ui.glob("*.py")):
        regions.update(extract_regions(
            existing.read_text(encoding="utf-8", errors="replace")))
    regions.update(dict(preserve_regions or {}))

    known = set(spec.widget_names) | set(_COMPOSITE_KINDS.values())
    res.orphaned_regions = {k: v for k, v in regions.items()
                            if k not in known and v.strip()}

    _write(ui / "__init__.py", '"""Generated UI package."""\n', res)
    _write(ui / "widgets.py", WIDGETS_PY, res, regions=regions)
    _write(ui / "main_ui.py", emit_main_ui(spec, regions), res)

    # app.py / handlers.py: created once, never rewritten (spec 7.1).
    app = root / "app.py"
    if app.exists():
        res.files_skipped.append(str(app))
    else:
        _write(app, emit_app_py(spec), res)

    handlers = root / "handlers.py"
    if handlers.exists():
        src = handlers.read_text(encoding="utf-8", errors="replace")
        missing = [h for h in spec.handlers if f"def {h}(" not in src]
        if missing:
            # APPEND, never rewrite. Existing bodies are untouched.
            with handlers.open("a", encoding="utf-8") as fh:
                fh.write("\n    # --- added by regeneration ---\n")
                for h in missing:
                    fh.write(handler_stub(h))
            res.handlers_added.extend(missing)
        res.files_skipped.append(str(handlers))
    else:
        _write(handlers, emit_handlers_py(spec), res)
        res.handlers_added.extend(spec.handlers)

    _write(root / "launch.py", emit_launch_py(spec, root), res)

    if res.orphaned_regions:
        backups = root / ".backups"
        backups.mkdir(exist_ok=True)
        out = backups / "orphaned_regions.txt"
        out.write_text(
            "Regions whose widget no longer exists. Kept here rather than\n"
            "discarded; move anything you still need back into app.py.\n\n"
            + "\n\n".join(f"# region: custom:{k}\n{v}"
                          for k, v in sorted(res.orphaned_regions.items())),
            encoding="utf-8")
        res.warnings.append(
            f"{len(res.orphaned_regions)} custom region(s) no longer match a "
            f"widget; saved to {out}")
    return res


def _write(path: Path, text: str, res: EmitResult,
           regions: Optional[Dict[str, str]] = None) -> None:
    if regions:
        text = _splice_regions(text, regions)
    path.write_text(text, encoding="utf-8")
    res.files_written.append(str(path))


def _splice_regions(text: str, regions: Dict[str, str]) -> str:
    """Put preserved bodies back into freshly generated source."""
    out: List[str] = []
    it = iter(text.splitlines())
    for line in it:
        out.append(line)
        m = REGION_OPEN.match(line)
        if not m:
            continue
        rid = m.group(2)
        body = regions.get(rid, "")
        if body.strip():
            out.extend(body.splitlines())
        for nxt in it:                       # skip the template's empty body
            if REGION_CLOSE.match(nxt):
                out.append(nxt)
                break
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
