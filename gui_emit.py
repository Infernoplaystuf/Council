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

import gui_colors as _gcol
from gui_ports import PortSpec
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
    "frame_timing", "frame_roi", "frame_classes",
)


@dataclass
class EmitResult:
    files_written: List[str] = field(default_factory=list)
    files_skipped: List[str] = field(default_factory=list)   # never rewritten
    handlers_added: List[str] = field(default_factory=list)
    handlers_upgraded: List[str] = field(default_factory=list)
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
    than growing a backslash — which is also what black does.

    A control character (a newline in a label) also goes through repr(),
    which escapes it; written raw it ended the string literal mid-line."""
    if isinstance(value, str):
        if '"' in value or any(ord(c) < 32 or c == "\x7f" for c in value):
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


def _c(text: Any) -> str:
    """Text for a generated COMMENT: control characters become spaces. A
    label's newline written into `# button: Scan<newline>folder` put the rest
    of the label on a code line of its own."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))


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
#
# WHY A COLOURED WIDGET IS EMITTED AS CLASSIC tk INSTEAD OF ttk
# ------------------------------------------------------------
# ttk widgets have no bg/fg option — ``ttk.Button(bg=...)`` raises `unknown
# option "-bg"`. ttk colour goes through a named style, and a style option is
# honoured only if the ACTIVE THEME's element for the widget reads it.
# Measured on Windows 11 by rendering the widgets and reading pixels:
#     vista (the Windows default): ttk style background = 0.0% (IGNORED)
#     clam:                        ttk style background = 95.9% (works)
# So a style-based colour system on the default Windows theme is a picker
# that silently does nothing — worse than one that never works. The one fix,
# style.theme_use("clam"), restyles EVERY widget in the app, so it is not
# built. Instead: a shape with a colour becomes the classic tk widget, which
# reads background from its own option database with no theme involved.
# COLOUR_CAPS in gui_colors names which kinds have a classic equivalent;
# Notebook, Treeview, Combobox and Progressbar have none, and the inspector
# refuses to offer a picker for them rather than emit an unhonoured colour.

# ttk kind -> classic (tk.*) constructor for the kinds that CAN be swapped.
# Anything not in the map falls back to its ttk form even when coloured — the
# inspector cannot offer the picker for those kinds (COLOUR_CAPS[kind] == ()).
_CLASSIC_CLASS: Dict[str, str] = {
    "frame": "tk.Frame", "freeform": "tk.Frame",
    "labelframe": "tk.LabelFrame", "panedwindow": "tk.PanedWindow",
    "label": "tk.Label", "button": "tk.Button", "entry": "tk.Entry",
    "checkbutton": "tk.Checkbutton", "radiobutton": "tk.Radiobutton",
    "spinbox": "tk.Spinbox", "scale": "tk.Scale",
    # separator IS a filled rectangle — a tk.Frame with a fixed height IS the
    # widget, not an approximation.
    "separator": "tk.Frame",
}


def _colour_kwargs(w: WidgetSpec) -> str:
    """`, background="#...", foreground="#..."` when the widget declares any.

    Reads from WidgetSpec.bg / .fg, which gui_spec.build fills with the
    EFFECTIVE colours from gui_colors.resolve_scene — i.e. after inheritance.
    That is what makes a "transparent" label work: Tk has no transparency, but
    a label whose background equals its parent's IS visually transparent, and
    inheritance produces exactly that with no extra concept.

    Skips kinds that cannot honour colour anyway — what makes the inspector's
    "cannot be coloured" story honest rather than a pretence.
    """
    cap = _gcol.caps(w.kind)
    if not cap:
        return ""
    bits = []
    bg = getattr(w, "bg", "") or ""
    fg = getattr(w, "fg", "") or ""
    if bg and "bg" in cap:
        try:
            bits.append(f"background={_py(_gcol.normalise(bg))}")
        except ValueError:
            pass
    if fg and "fg" in cap:
        try:
            bits.append(f"foreground={_py(_gcol.normalise(fg))}")
        except ValueError:
            pass
    return (", " + ", ".join(bits)) if bits else ""


def _font_kwarg(w: WidgetSpec) -> str:
    """`, font="Magneto 18 bold"` for kinds that render text.

    The stored string is Tk's own font format, so it passes through verbatim.
    Both ttk and classic tk widgets accept `font=`, so unlike colour this needs
    no widget swap — a ttk.Label honours a font perfectly well."""
    font = str(getattr(w, "font", "") or "").strip()
    if not font or not _gcol.can_font(w.kind):
        return ""
    # Brace-quote a multi-word family. Passed verbatim, "Segoe UI 12" makes
    # Tk parse "UI" as the size and the app dies at construction.
    return f", font={_py(_gcol.tk_font(font))}"


def _uses_classic(w: WidgetSpec) -> bool:
    """A shape becomes classic tk when it carries ANY colour that the kind
    can honour. The uncolourable native kinds (Notebook/Treeview/Combobox/
    Progressbar) always stay ttk, and so do composites."""
    if w.kind not in _CLASSIC_CLASS:
        return False
    cap = _gcol.caps(w.kind)
    if not cap:
        return False
    bg = getattr(w, "bg", "") or ""
    fg = getattr(w, "fg", "") or ""
    return bool((bg and "bg" in cap) or (fg and "fg" in cap))


def construct(w: WidgetSpec, parent: str) -> str:
    """The right-hand side of `self.<name> = ...` for one widget."""
    k = w.kind
    cmd = f", command=self.{w.handler}" if w.handler else ""
    ck = _colour_kwargs(w)               # empty when no colour or kind cannot
    classic = _uses_classic(w)
    # Font rides along on the same kinds that render text. Appended to ck so
    # every construct branch picks it up without a second interpolation.
    ck = ck + _font_kwarg(w)

    if k in ("frame", "freeform"):
        cls = "tk.Frame" if classic else "ttk.Frame"
        return f"{cls}({parent}{ck})"
    if k == "labelframe":
        cls = "tk.LabelFrame" if classic else "ttk.LabelFrame"
        return f"{cls}({parent}, text={_py(_text_of(w))}{ck})"
    if k == "notebook":
        return f"ttk.Notebook({parent})"
    if k == "panedwindow":
        cls = "tk.PanedWindow" if classic else "ttk.PanedWindow"
        return (f"{cls}({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))}{ck})")
    if k == "label":
        cls = "tk.Label" if classic else "ttk.Label"
        # wraplength was in the label's catalogue and the inspector offered
        # it, but it was never emitted — a long status line was cut off
        # mid-word at the label's edge instead of wrapping. justify="left"
        # keeps the wrapped lines aligned with the first.
        try:
            wrap = int(_prop(w, "wraplength", 0) or 0)
        except (TypeError, ValueError):
            wrap = 0
        wrapkw = f", wraplength={wrap}, justify=\"left\"" if wrap > 0 else ""
        return (f"{cls}({parent}, text={_py(_text_of(w))}, "
                f"anchor={_py(_prop(w, 'anchor', 'w'))}{wrapkw}{ck})")
    if k == "button":
        cls = "tk.Button" if classic else "ttk.Button"
        return f"{cls}({parent}, text={_py(_text_of(w))}{cmd}{ck})"
    if k == "entry":
        show = _prop(w, "show", "")
        extra = f", show={_py(show)}" if show else ""
        cls = "tk.Entry" if classic else "ttk.Entry"
        return (f"{cls}({parent}, "
                f"justify={_py(_prop(w, 'justify', 'left'))}{extra}{ck})")
    if k == "text":
        return (f"tk.Text({parent}, "
                f"wrap={_py(_prop(w, 'wrap', 'word'))}{ck})")
    if k == "checkbutton":
        cls = "tk.Checkbutton" if classic else "ttk.Checkbutton"
        return f"{cls}({parent}, text={_py(_text_of(w))}{cmd}{ck})"
    if k == "radiobutton":
        cls = "tk.Radiobutton" if classic else "ttk.Radiobutton"
        return (f"{cls}({parent}, text={_py(_text_of(w))}, "
                f"value={_py(_prop(w, 'value', ''))}{cmd}{ck})")
    if k == "combobox":
        state = "readonly" if _prop(w, "readonly", True) else "normal"
        return (f"ttk.Combobox({parent}, "
                f"values={_py(_prop(w, 'values', []))}, state={_py(state)})")
    if k == "listbox":
        # exportselection=False: with Tk's default, selecting in ANY other
        # listbox or entry silently CLEARS this one's selection — so a class
        # picked in one list vanished when the user clicked in another, and
        # "Mark this frame" found nothing selected.
        return (f"tk.Listbox({parent}, "
                f"selectmode={_py(_prop(w, 'selectmode', 'browse'))}, "
                f"exportselection=False{ck})")
    if k == "spinbox":
        cls = "tk.Spinbox" if classic else "ttk.Spinbox"
        return (f"{cls}({parent}, from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))}, "
                f"increment={_py(_prop(w, 'increment', 1))}{cmd}{ck})")
    if k == "scale":
        cls = "tk.Scale" if classic else "ttk.Scale"
        return (f"{cls}({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))}, "
                f"from_={_py(_prop(w, 'from_', 0))}, "
                f"to={_py(_prop(w, 'to', 100))}{ck})")
    if k == "progressbar":
        return (f"ttk.Progressbar({parent}, "
                f"orient={_py(_prop(w, 'orient', 'horizontal'))}, "
                f"mode={_py(_prop(w, 'mode', 'determinate'))})")
    if k == "separator":
        # A separator IS a 2px filled rectangle when coloured — tk.Frame with
        # a fixed height. When uncoloured it stays ttk.Separator's native look.
        if classic:
            axis = "height=2" if _prop(w, "orient", "horizontal") == "horizontal" else "width=2"
            return f"tk.Frame({parent}, {axis}{ck})"
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
                f"overlay_alpha={_py(float(_prop(w, 'overlay_alpha', 0.5)))}, "
                f"roi={_py(bool(_prop(w, 'roi', False)))})")
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


STOP_WATCHER = '''

def _watch_for_stop(ui) -> None:
    """Close cleanly when the GUI Designer asks, or when it goes away.

    Active only when the designer launched this app (it sets
    COUNCIL_PREVIEW_CONTROL=stdin). The designer writes "stop" on stdin to ask
    for a clean close; if the designer exits or crashes, stdin reaches
    end-of-file and the app closes the same way. Either way on_close runs, so
    a camera app gets to stop its grab and close the device.

    Before this, Stop was TerminateProcess on Windows: no finally, no atexit,
    no window handler ran, and a crashed designer left its preview running
    with nothing able to stop it.

    The reader thread never touches Tk — Tk is not safe to call from another
    thread. It only sets a flag, which the Tk thread polls. It reads the raw
    file descriptor rather than sys.stdin: a thread parked inside sys.stdin's
    buffered reader when the window is closed by its X can abort the
    interpreter at shutdown ("could not acquire lock for stdin").
    """
    import os
    import sys
    import threading
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

    def _poll():
        if asked.is_set():
            if gone:
                # Nobody reads this app's output any more, and on Windows
                # every write to the orphaned pipe raises OSError — so the
                # first print() in on_close aborted the very cleanup it was
                # reporting on. Measured: exit code 1, camera never released.
                try:
                    sys.stdout = sys.stderr = open(os.devnull, "w")
                except OSError:
                    pass
            ui.request_close()
            return
        try:
            ui.after(150, _poll)
        except Exception:
            pass                       # the window is already gone

    ui.after(150, _poll)
'''


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
    L.append("from .ports import Ports")
    L.append(STOP_WATCHER.rstrip())
    L.append("")
    # When the window carries a background, MainUi becomes tk.Frame — ttk.Frame
    # ignores `background=` and there is no theme-portable way around that.
    # A ttk theme swap would recolour every uncoloured widget in the app too.
    root_bg = _gcol.normalise(spec.root_bg) if spec.root_bg else ""
    base = "tk.Frame" if root_bg else "ttk.Frame"
    L += [
        "",
        f"class MainUi({base}):",
        '    """Every widget, built and placed. Handlers live in app.py."""',
        "",
        "    def __init__(self, master=None, **kw):",
        "        super().__init__(master, **kw)",
        "        self._closing = False",
        "        self._build()",
        "        # ONE close path: the window's X, the designer's Stop, and the",
        "        # designer going away all run on_close before the window goes.",
        "        try:",
        "            self.winfo_toplevel().protocol('WM_DELETE_WINDOW',",
        "                                           self.request_close)",
        "        except tk.TclError:",
        "            pass",
        "        _watch_for_stop(self)",
        "",
        "    # -- closing ---------------------------------------------------",
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
        "            self.winfo_toplevel().destroy()",
        "        except tk.TclError:",
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
        "            from tkinter import messagebox",
        "            messagebox.showerror(f'{what} failed', msg,",
        "                                 parent=self.winfo_toplevel())",
        "        except tk.TclError:",
        "            pass",
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
    if root_bg:
        # Both MainUi (self) and the toplevel (root). The toplevel matters
        # because a MainUi that does not fill it leaves the OS-default frame
        # visible around the edges. tk.Tk.configure(background=) always works.
        L.append(f'{ind}self.configure(background={_py(root_bg)})')
        L.append(f'{ind}try: self.winfo_toplevel().configure('
                 f'background={_py(root_bg)})')
        L.append(f'{ind}except tk.TclError: pass')
    L += _grid_config("self", spec.root_row_weights, spec.root_col_weights,
                      spec.root_row_minsizes, spec.root_col_minsizes, ind)
    L.append("")

    # Which parents manage their children with .add() instead of a geometry
    # manager. A child of a Notebook that is .grid()-ed is CONSTRUCTED,
    # PARENTED, AND INVISIBLE — the notebook shows an empty tab strip and the
    # widget never appears. Same for PanedWindow. This produced a completely
    # blank generated app and nothing anywhere warned.
    _tab_index: Dict[str, int] = {}

    for w in _ordered(spec):
        parent = f"self.{w.parent}" if w.parent else "self"
        parent_spec = spec.by_name(w.parent) if w.parent else None
        parent_kind = parent_spec.kind if parent_spec else ""
        L.append(f"{ind}# {w.kind}: {_c(w.label or w.name)}")
        L.append(f"{ind}self.{w.name} = {construct(w, parent)}")
        if parent_kind == "notebook":
            # Tab titles come from the parent's `tabs` prop, in child order —
            # which is what that prop's comment in the catalogue promises.
            i = _tab_index.get(w.parent, 0)
            _tab_index[w.parent] = i + 1
            tabs = list(_prop(parent_spec, "tabs", []) or [])
            title = str(tabs[i]) if i < len(tabs) else (w.label or w.name)
            L.append(f"{ind}{parent}.add(self.{w.name}, text={_py(title)})")
        elif parent_kind == "panedwindow":
            L.append(f"{ind}{parent}.add(self.{w.name})")
        else:
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

    # Ports is built LAST — every widget must exist first, and adopting a
    # composite's own var (FilePicker/Scrubber) requires the composite to have
    # run its __init__ already.
    L.append(f"{ind}# -- typed binding surface ---------------------------")
    L.append(f"{ind}self.ports = Ports(self)")
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

    The box is stored in FULL-IMAGE pixels, never canvas pixels. Canvas pixels
    change with every pan and zoom; image pixels mean the same region at any
    zoom, which is exactly what a crop-on-save routine needs to be handed.
    """

    ROI_COLOUR = "#00e5ff"

    def __init__(self, master=None, *, overlay: bool = False,
                 overlay_alpha: float = 0.5, roi: bool = False, **kw):
        super().__init__(master, **kw)
        self.roi_enabled = bool(roi)
        self._roi = None           # (x, y, w, h) in full-image pixels
        self._roi_applied = False
        self._roi_armed = False    # the next left-drag draws instead of pans
        self._roi_from = None      # canvas point where the ROI drag began
        self._roi_listeners = []
        if self.roi_enabled:
            bar = ttk.Frame(self)
            bar.pack(side="top", fill="x")
            self._btn_draw = ttk.Button(bar, text="Draw ROI",
                                        command=self.arm_roi)
            self._btn_apply = ttk.Button(bar, text="Apply ROI",
                                         command=self.apply_roi)
            self._btn_clear = ttk.Button(bar, text="Clear ROI",
                                         command=self.clear_roi)
            for b in (self._btn_draw, self._btn_apply, self._btn_clear):
                b.pack(side="left", padx=2, pady=2)
            self._roi_note = ttk.Label(bar, text="")
            self._roi_note.pack(side="left", padx=8)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#1e1e2e")
        self.canvas.pack(fill="both", expand=True)
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._pan_from = None
        self._base = None          # PIL.Image — the whole frame
        self._view = None          # what is shown: _base, or its ROI crop
        self._crop_at = (0, 0)     # full-image position of _view's top-left
        self._overlay_img = None   # PIL.Image
        self._photo = None         # keep a reference or Tk drops the image
        self._fitted_at = (0, 0)   # canvas size when zoom_to_fit last ran
        self._message = ""         # shown when there is no image to show
        self.overlay_enabled = bool(overlay)
        self.overlay_alpha = float(overlay_alpha)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)          # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(1.1, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(1 / 1.1, e.x, e.y))
        self.canvas.bind("<Configure>", self._on_configure)
        self._sync_roi_controls()

    def _on_configure(self, _e=None) -> None:
        """Re-fit if the last fit ran before the widget had a real size.

        zoom_to_fit divides by winfo_width(), which is 1 until Tk has laid the
        window out. An app that sets an image during construction — the normal
        thing to do when a folder is preloaded — therefore fitted at scale
        1/width and drew the image one pixel across, then every later
        <Configure> re-rendered at that same dead scale. Refitting once the
        canvas has real dimensions is what makes a preloaded image visible.
        """
        if min(self._fitted_at) <= 1 and self._view is not None:
            self.zoom_to_fit()
        else:
            self._render()

    # -- public ------------------------------------------------------
    def set_image(self, image) -> None:
        """``image`` is a PIL.Image. An applied ROI is re-applied to it."""
        self._base = image
        if image is not None:
            self._message = ""
        self._refresh_view()
        self.zoom_to_fit()

    def show_message(self, text: str) -> None:
        """Say something IN the panel when there is no image — why a folder
        showed no frames — instead of leaving a blank dark rectangle while the
        reason goes to a console nobody reads."""
        self._base = None
        self._message = str(text or "")
        self._refresh_view()
        self._render()

    def set_overlay(self, image, alpha: float = None) -> None:
        self._overlay_img = image
        if alpha is not None:
            self.overlay_alpha = float(alpha)
        self._render()

    def clear_overlay(self) -> None:
        self._overlay_img = None
        self._render()

    def zoom_to_fit(self) -> None:
        """Fit what is SHOWN — the ROI crop when one is applied, which is how
        applying an ROI zooms the view onto it."""
        if self._view is None:
            # CLEARING IS A RENDER. set_image(None) assigns _base then calls
            # here; returning early left the PREVIOUS frame painted, so an
            # empty folder showed the last folder's image under a message
            # saying there was nothing to show. _render deletes and returns.
            self._render()
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self._view.size
        self._scale = min(cw / iw, ch / ih) if iw and ih else 1.0
        self._ox = (cw - iw * self._scale) / 2
        self._oy = (ch - ih * self._scale) / 2
        self._fitted_at = (cw, ch)
        self._render()

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
        """Replace the box with ``roi`` — (x, y, w, h) or None.

        Anything that is not a usable box becomes None. If an ROI is applied,
        the view re-crops to the new box straight away."""
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
            self._render()
        if notify:
            self._emit_roi()

    def arm_roi(self) -> None:
        """The next left-drag draws the ROI instead of panning."""
        if self._roi_applied:
            return              # Clear first: a box drawn on a crop is ambiguous
        self._roi_armed = True
        self.canvas.configure(cursor="crosshair")
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
        self.canvas.configure(cursor="")
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
        """The ROI clamped to an image of ``size``, as a PIL crop box — or
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
            box = self._roi_box(b.size)
            if box is not None:
                self._view = b.crop(box)
                self._crop_at = (box[0], box[1])
        self._sync_roi_controls()

    def _to_image(self, cx, cy):
        """Canvas point -> full-image pixel, through pan, zoom and any crop."""
        s = self._scale or 1.0
        return (self._crop_at[0] + (cx - self._ox) / s,
                self._crop_at[1] + (cy - self._oy) / s)

    def _to_canvas(self, ix, iy):
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
        self._btn_draw.state(["disabled"] if applied else ["!disabled"])
        self._btn_apply.state(["!disabled"] if have and not applied
                              else ["disabled"])
        self._btn_clear.state(["!disabled"] if have else ["disabled"])
        if self._roi_armed:
            text = "Drag a box on the image"
        elif not have:
            text = "No ROI"
        elif applied and self._base is not None and self._view is self._base:
            text = "ROI lies outside this frame"
        else:
            # Report the box as it lands on THIS frame. A box typed larger
            # than the frame is clamped when shown and when saved; quoting the
            # requested size would describe pixels that do not exist.
            x, y, w, h = self._roi
            box = self._roi_box(self._base.size) if self._base is not None else None
            if box is not None:
                x, y, w, h = box[0], box[1], box[2] - box[0], box[3] - box[1]
            text = f"{'Applied' if applied else 'ROI'}: {w} x {h} at ({x}, {y})"
            if box is not None and (w, h) != (self._roi[2], self._roi[3]):
                text += " (clamped)"
        self._roi_note.configure(text=text)

    # -- interaction -------------------------------------------------
    def _press(self, e) -> None:
        if self._roi_armed and self._view is not None:
            self._roi_from = (e.x, e.y)
            self.canvas.delete("roi_band")
            self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                         outline=self.ROI_COLOUR, width=2,
                                         dash=(4, 2), tags="roi_band")
            return
        self._pan_from = (e.x, e.y)

    def _drag(self, e) -> None:
        if self._roi_from is not None:
            x0, y0 = self._roi_from
            self.canvas.coords("roi_band", x0, y0, e.x, e.y)
            return
        self._pan_move(e)

    def _release(self, e) -> None:
        if self._roi_from is None:
            self._pan_from = None
            return
        x0, y0 = self._roi_from
        self._roi_from = None
        self._roi_armed = False
        self.canvas.configure(cursor="")
        self.canvas.delete("roi_band")
        ax, ay = self._to_image(min(x0, e.x), min(y0, e.y))
        bx, by = self._to_image(max(x0, e.x), max(y0, e.y))
        iw, ih = self._base.size
        # Rounded, not truncated: canvas->image is float maths, and int() on
        # 9.9999 would shift the box a pixel left of where it was drawn.
        ax, ay = max(0, int(round(ax))), max(0, int(round(ay)))
        bx, by = min(iw, int(round(bx))), min(ih, int(round(by)))
        drawn = self._valid_roi((ax, ay, bx - ax, by - ay))
        if drawn is not None:
            # A click without a drag must not wipe a box drawn earlier.
            self.set_roi(drawn, notify=True)
        self._sync_roi_controls()
        self._render()

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
        view = self._view
        if view is None:
            if self._message:
                w = max(1, self.canvas.winfo_width())
                h = max(1, self.canvas.winfo_height())
                self.canvas.create_text(w / 2, h / 2, text=self._message,
                                        fill="#f5c2e7", width=max(80, w - 40),
                                        justify="center", tags="message")
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.canvas.create_text(10, 10, anchor="nw", fill="#f38ba8",
                                    text="Pillow is required to show images")
            return
        iw, ih = view.size
        w = max(1, int(iw * self._scale))
        h = max(1, int(ih * self._scale))
        img = view.resize((w, h), Image.NEAREST).convert("RGBA")
        if self.overlay_enabled and self._overlay_img is not None:
            ov = self._overlay_img
            if view is not self._base and ov.size == self._base.size:
                box = self._roi_box(self._base.size)
                if box is not None:
                    ov = ov.crop(box)     # the overlay follows the crop
            ov = ov.resize((w, h), Image.NEAREST).convert("RGBA")
            img = Image.blend(img, ov, max(0.0, min(1.0, self.overlay_alpha)))
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(self._ox, self._oy, anchor="nw",
                                 image=self._photo)
        # The drawn, not-yet-applied box, redrawn on every pan and zoom.
        if self._roi and not self._roi_applied and self._base is not None:
            box = self._roi_box(self._base.size)
            if box is not None:
                x0, y0 = self._to_canvas(box[0], box[1])
                x1, y1 = self._to_canvas(box[2], box[3])
                self.canvas.create_rectangle(x0, y0, x1, y1,
                                             outline=self.ROI_COLOUR, width=2,
                                             dash=(4, 2), tags="roi_box")

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
# ui/ports.py — the typed binding surface
# ============================================================
#
# Emitted verbatim as the first block of the generated `ui/ports.py`, followed
# by a `class Ports` produced by emit_ports(spec) below. Direction is stated
# from app.py's point of view: `in` = the app READS it (user -> app);
# `out` = the app WRITES it (app -> user); `io` = both; `e` = event.
#
# Ports OWN their Tk variables. A tk.StringVar that nothing holds is
# garbage-collected and the widget goes on working while every read through
# the app's reference returns "" forever. Owning them here means the lifetime
# matches the UI's.

PORTS_RUNTIME = '''"""Generated by the GUI Designer. DO NOT EDIT — regeneration
overwrites this file. Behaviour belongs in app.py.

TYPED BINDING SURFACE. app.py talks to self.ports.<name> and never touches a
Tk variable or a widget option, so widget substitutions ripple only through
this file. Every port has:

    .get()        read (raises for `out`-only kinds — see .set)
    .set(v)       write (raises for `in`-only kinds; is a no-op for events)
    .on_change(f) f(new_value) fires on user OR programmatic writes
    .enable(b)    True/False; walks composites when the port declares deep=True
    .widget       the underlying Tk widget, if you really need it

An event port (button/toolbar/menubar) exposes:

    .on_fire(f)   f() runs when the button is pressed
    .fire()       call the handler as if the user pressed it
    .enable(b)

Ports as a whole exposes:

    .read()       {name: value} for every in/inout port
    .apply(d)     write into every out/inout port from {name: value}
    ports[name]   look up by name (also `name in ports`, `for p in ports`)
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk

__all__ = ["Ports", "RENAMED"]


# old name -> current name. Emitted only while hand-written code still uses
# the old one; vanishes on the first regeneration after the last reference is
# gone. Self-expiring, not stored in the manifest. Populated by emit_ports.
RENAMED: dict = {}


def _set_state(widget, on: bool, deep: bool = False) -> None:
    """Enable or disable ``widget``, handling ttk / tk / composite differences.

    ttk widgets have .state(["disabled"]); tk.Text / tk.Listbox / tk.Menu do
    not, and take configure(state="disabled") instead. A composite whose
    outer container is a Frame with an inner Entry needs `deep=True`, because
    disabling the Frame does not reach the Entry inside it — measured, not
    assumed."""
    def apply(w):
        try:
            w.state(["!disabled"] if on else ["disabled"])
            return
        except (AttributeError, tk.TclError):
            pass
        try:
            w.configure(state="normal" if on else "disabled")
        except tk.TclError:
            pass
    apply(widget)
    if deep:
        for kid in widget.winfo_children():
            _set_state(kid, on, deep=True)


class _Port:
    """Base class. Subclasses fill in .get / .set / .on_change."""

    __slots__ = ("name", "widget", "direction", "type", "_deep")

    def __init__(self, name, widget, *, direction, type, deep=False):
        self.name, self.widget = name, widget
        self.direction, self.type = direction, type
        self._deep = deep

    def enable(self, on: bool = True) -> None:
        _set_state(self.widget, bool(on), deep=self._deep)

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
        failed scan is exactly the false answer this exists to prevent. The
        base does nothing; each port type that has an honest empty state
        overrides it, and one that does not keeps its value rather than lie."""


class _VarPort(_Port):
    """A port backed by a Tk variable.

    Handles Entry/Checkbutton/Radiobutton/Combobox/Spinbox/Scale/Progressbar/
    Label plus the composites that expose their own var (FilePicker/Scrubber).
    Reads and writes go through the var; the change hook is trace_add('write')
    which fires on user AND programmatic writes."""

    __slots__ = ("var", "_option", "_choices")

    def __init__(self, name, widget, *, var, option, type, direction,
                 default=None, choices=(), deep=False):
        super().__init__(name, widget, direction=direction, type=type, deep=deep)
        self.var = var
        self._option = option
        self._choices = tuple(choices)
        if option:
            try:
                widget.configure(**{option: var})
            except tk.TclError as exc:
                raise TypeError(
                    f"port {name!r}: widget rejected {option}= "
                    f"({exc}); direction was {direction!r}") from exc
        if default is not None:
            try:
                self.set(default)
            except Exception:
                pass

    def get(self):
        if "i" not in self.direction and self.direction != "io":
            # out-only ports (progressbar) are legal to read but the app usually
            # writes; do not refuse.
            pass
        return _coerce(self.var.get(), self.type)

    def set(self, value) -> None:
        # A COMPOSITE THAT OWNS ITS VAR MUST BE SET THROUGH ITS OWN set().
        # Scrubber keeps three views of one index — the variable, the scale
        # position and the entry text — and only Scrubber.set() syncs them.
        # Writing the variable directly moved the value and left the scale
        # and entry showing the old one: measured as
        #     port.set(50) -> var=50, scale=0.0, entry='0'
        # An adopted port is exactly the case where option is "" (we did not
        # attach the variable, the widget already had it), so that flag is
        # the honest test for "this widget owns more than the variable".
        if not self._option:
            setter = getattr(self.widget, "set", None)
            if callable(setter):
                try:
                    setter(value)
                    return
                except Exception:
                    pass
        self.var.set(value)

    def on_change(self, fn) -> None:
        last = [self.var.get()]
        def _cb(*_a, _fn=fn, _last=last):
            v = self.var.get()
            if v == _last[0]:
                return                                 # value-debounced
            _last[0] = v
            _fn(_coerce(v, self.type))
        self.var.trace_add("write", _cb)

    def clear(self) -> None:
        # A text variable can be genuinely blank, and an empty progress bar
        # claims nothing. A scale, a checkbox or a scrubber has no empty
        # state — any value it shows would read as an answer — so those keep
        # theirs, and Generate warns when one is a script output.
        if isinstance(self.var, tk.StringVar):
            self.var.set("")
        elif isinstance(self.widget, ttk.Progressbar):
            self.var.set(0)


def _coerce(v, t):
    """Nudge a Tk value into the port's declared type. StringVar-backed
    Entries hand back a str even when the port is typed int — coerce once
    here so app.py never writes int(self.ports.n.get()).

    A blank or unreadable number is None, not 0. A count box cleared after a
    failed call (Port.clear) read back as 0 — the same false zero clear()
    exists to keep off the screen, handed to whatever read it next."""
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


class _EventPort(_Port):
    """A port that carries no value — buttons, toolbars, menubars.

    The generated main_ui already binds ``command=self.on_<name>``; this port
    installs its own dispatcher that fans out to every on_fire subscriber AND
    calls the app.py method by late name lookup, so an override in App still
    wins. Late lookup is what preserves the existing regeneration contract."""

    __slots__ = ("_subs", "_ui", "_handler")

    def __init__(self, name, widget, *, ui, handler):
        super().__init__(name, widget, direction="e", type="event")
        self._subs = []
        self._ui, self._handler = ui, handler
        try:
            widget.configure(command=self._dispatch)
        except tk.TclError:
            pass

    def _dispatch(self, *args) -> None:
        for fn in list(self._subs):
            try: fn()
            except Exception as exc:
                print(f"[ports] {self.name}.on_fire callback raised: {exc!r}")
        m = getattr(self._ui, self._handler, None)
        if callable(m):
            try: m(*args)
            except Exception as exc:
                print(f"[ports] {self.name} handler raised: {exc!r}")

    def on_fire(self, fn) -> None:
        self._subs.append(fn)

    def fire(self) -> None:
        self._dispatch()


class _TextPort(_Port):
    """A port over a tk.Text — no variable, only get(1.0, end-1c) and delete/
    insert. `end-1c` trims the trailing newline Tk always appends, or a
    round-trip through set(get()) would grow a newline per pass."""

    __slots__ = ()

    def get(self):
        return self.widget.get("1.0", "end-1c")

    def set(self, value) -> None:
        w = self.widget
        was = str(w.cget("state"))
        if was == "disabled":
            w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", "" if value is None else str(value))
        if was == "disabled":
            w.configure(state="disabled")

    def on_change(self, fn) -> None:
        w = self.widget
        last = [self.get()]
        def _cb(_e=None, _fn=fn, _last=last, _w=w):
            v = _w.get("1.0", "end-1c")
            if v != _last[0]:
                _last[0] = v
                _fn(v)
            try: _w.edit_modified(False)         # re-arm; else fires only once
            except tk.TclError: pass
        w.bind("<<Modified>>", _cb, add=True)

    def clear(self) -> None:
        self.set("")


class _ListPort(_Port):
    """A port over a tk.Listbox — the selection is the value.

    contents live in .items; the port's own .get() returns the selected items.
    A Listbox has no textvariable/variable that returns "the value" (the
    legacy listvariable is bidirectional and wipes the contents on attach)."""

    __slots__ = ()

    def get(self):
        w = self.widget
        return [w.get(i) for i in w.curselection()]

    def items(self):
        w = self.widget
        return list(w.get(0, "end"))

    def set(self, values) -> None:
        w = self.widget
        w.delete(0, "end")
        for v in (values or []):
            w.insert("end", v)

    def on_change(self, fn) -> None:
        self.widget.bind("<<ListboxSelect>>",
                         lambda _e, _fn=fn: _fn(self.get()), add=True)

    def clear(self) -> None:
        self.set([])


class _TablePort(_Port):
    """A port over a ttk.Treeview — selection returns each row's values."""

    __slots__ = ()

    def get(self):
        tv = self.widget
        return [tv.item(iid, "values") for iid in tv.selection()]

    def rows(self):
        tv = self.widget
        return [tv.item(iid, "values") for iid in tv.get_children("")]

    def set(self, rows) -> None:
        tv = self.widget
        for iid in tv.get_children(""):
            tv.delete(iid)
        for row in (rows or []):
            tv.insert("", "end", values=list(row))

    def on_change(self, fn) -> None:
        self.widget.bind("<<TreeviewSelect>>",
                         lambda _e, _fn=fn: _fn(self.get()), add=True)

    def clear(self) -> None:
        self.set([])


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
        # An image panel and a status bar have an honest empty state — a
        # status bar left saying "3 of 5 frames bad" after the next scan
        # failed was the previous folder's answer shown beside this one.
        # A log is the record of what happened, so it is never wiped.
        if self._writer == "set_image":
            self.set(None)
        elif self._writer == "set":
            self.set("")


class _TabPort(_Port):
    """The current-tab port for a ttk.Notebook."""

    __slots__ = ()

    def get(self):
        try: return self.widget.index(self.widget.select())
        except tk.TclError: return 0

    def set(self, value) -> None:
        try: self.widget.select(int(value))
        except (tk.TclError, ValueError, TypeError): pass

    def on_change(self, fn) -> None:
        last = [self.get()]
        def _cb(_e=None, _fn=fn, _last=last):
            v = self.get()
            if v != _last[0]:
                _last[0] = v
                _fn(v)
        self.widget.bind("<<NotebookTabChanged>>", _cb, add=True)


_SEQ_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                 ".webp")


def _natkey(path):
    """Sort key: frame_9 before frame_10, layer_9/ before layer_10/.

    A lexical sort puts 10 before 9, so a layer-wise scan comes out reordered
    and NOTHING LOOKS WRONG — the frames are all there, in a plausible order,
    just not the order they were captured in. Measured on the old code:

        frame_1, frame_10, frame_100, frame_11, frame_2, frame_9

    Applied per PATH SEGMENT so a recursive scan orders directories
    numerically too; keying only the basename would order layer_10/ before
    layer_9/ and reintroduce the same bug one level up.

    The int() is guarded: the split is ASCII-only but str.isdigit() is not, so
    a filename carrying e.g. a superscript would otherwise raise inside the
    scan and take the whole folder down with it.
    """
    import re
    parts = []
    # chr(92) is a backslash. Spelled this way because PORTS_RUNTIME is a
    # non-raw string constant, so a literal one here would have to be written
    # four-deep to survive into the generated file — a maintenance trap that
    # silently produces an unterminated literal.
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

    Wired from a `drives` declaration on the index widget. Talks only to
    PORTS, never to widget attribute names, so renaming a widget that keeps
    its port leaves this intact.

    ONE IMAGE IS DECODED AT A TIME. A capture folder can hold thousands of
    frames; listing them is cheap (names only) but decoding them is not, so
    the file list is held and the pixels are not. Scrubbing decodes the frame
    you land on and drops the one before it.

    BOTH EVENTS ARE COALESCED. A scale fires once per integer crossed, so
    dragging across 5,000 frames asks for ~600 decodes; a Browse entry fires
    once per KEYSTROKE, so typing a path rescans the folder ~60 times. The
    `_last` guard cannot help with either — every intermediate value really is
    a different folder or a different frame. Debouncing is what makes the drag
    smooth instead of a sequence of stalls.
    """

    SUFFIXES = _SEQ_SUFFIXES
    SCAN_MS = 150     # coalesce a typed folder path
    SHOW_MS = 30      # coalesce a scrubber drag

    def __init__(self, folder_port, index_port, target_port, *,
                 suffixes=(), recursive=False, status_port=None,
                 roi_port=None, current_port=None):
        self.folder, self.index, self.target = folder_port, index_port, target_port
        self.status = status_port
        self.roi = roi_port
        # The displayed file's path RELATIVE to the folder (usually just its
        # name). Anything that acts on "this frame" — marking it with a class
        # — reads it here rather than recomputing it from the index, which
        # would need this class's exact sort and suffix rules and would label
        # the wrong file the moment they differed.
        self.current = current_port
        self.suffixes = tuple(s.lower() for s in (suffixes or self.SUFFIXES))
        self.recursive = bool(recursive)
        self.files = []
        self._last = None
        self._scan_tok = None
        self._show_tok = None
        self._roi_syncing = False
        folder_port.on_change(self._folder_changed)
        index_port.on_change(self._index_changed)
        # ROI <-> a text port, both ways. The canvas holds the box; the port
        # is what a script link (e.g. crop-on-save) reads, and typing numbers
        # into it moves the box, so the region can be set precisely too.
        canvas = getattr(target_port, "widget", None)
        hook = getattr(canvas, "on_roi_change", None)
        if roi_port is not None and callable(hook):
            hook(self._roi_drawn)
            roi_port.on_change(self._roi_typed)
        # DEFERRED. __init__ runs inside MainUi._build(), before the window is
        # mapped, so a folder restored from a port default would be scanned
        # and shown into a 1x1 canvas.
        idle = getattr(getattr(self.index, "widget", None), "after_idle", None)
        if callable(idle):
            idle(self.reload)
        else:
            self.reload()

    # -- scheduling ---------------------------------------------------
    def _later(self, token, ms, fn):
        """Replace a pending callback. Returns the new token, or None when Tk
        will not schedule — a destroyed widget during teardown — in which case
        the update is DROPPED rather than run inline into a dead canvas."""
        w = getattr(self.index, "widget", None)
        if token is not None:
            try: w.after_cancel(token)
            except Exception: pass
        try:
            return w.after(ms, fn)
        except Exception:
            return None

    def _cancel(self, attr):
        tok = getattr(self, attr, None)
        if tok is not None:
            try: self.index.widget.after_cancel(tok)
            except Exception: pass
        setattr(self, attr, None)

    def _folder_changed(self, folder=None):
        self._scan_tok = self._later(self._scan_tok, self.SCAN_MS, self.reload)

    def _index_changed(self, i=None):
        self._show_tok = self._later(self._show_tok, self.SHOW_MS, self.show)

    # -- the two events ----------------------------------------------
    def reload(self, folder=None) -> None:
        """A new folder: relist, RESIZE THE INDEX to fit, show the first."""
        import os
        self._cancel("_scan_tok")
        folder = str(folder if folder is not None else self.folder.get() or "")
        folder = folder.strip().strip('"')
        if folder and not os.path.isdir(folder):
            return          # half-typed path: keep what is already loaded
        self._root = folder              # what relative paths are relative TO
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
        # The index widget's range must match the folder, or the slider runs
        # past the end of a short folder and stops short on a long one.
        hi = max(0, len(files) - 1)
        w = getattr(self.index, "widget", None)
        setter = getattr(w, "set_range", None)
        if callable(setter):
            try: setter(0, hi)
            except Exception: pass
        else:
            try: w.configure(from_=0, to=hi)
            except Exception: pass
        try: self.index.set(0)
        except Exception: pass
        # Both writes above trip the index trace and queue a show. Cancel it,
        # or switching folders from a non-zero index decodes twice.
        self._cancel("_show_tok")
        self._last = None
        self.show(0)

    def show(self, i=None) -> None:
        """Display frame ``i``. Decodes exactly one image."""
        import os
        self._show_tok = None
        try:
            i = int(i if i is not None else self.index.get())
        except (TypeError, ValueError):
            return
        if not self.files:
            # CLEAR. Leaving the previous folder's frame on screen under a
            # message saying the folder is empty is worse than showing nothing.
            self._last = None
            try: self.target.set(None)
            except Exception: pass
            self._set_current("")
            folder = str(self.folder.get() or "").strip()
            if folder:
                self._say(f"No images in {folder}", error=True)
            else:
                self._say("Choose a folder of frames to view them",
                          error=True)
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
                    # 16-bit CT / layer scans. ImageCanvas._render does
                    # .convert("RGBA"), which CLAMPS a 16-bit slice to near
                    # white; scaling here is what makes it look like the scan.
                    frame = im.point(lambda v: v * (1.0 / 256)).convert("L")
                else:
                    # copy() forces the decode, so the pixels survive the
                    # `with`. An explicit im.load() would be the obvious way
                    # and is refused by gui_policy — `.load` is denied wherever
                    # it appears, because that is also pickle.load's spelling.
                    frame = im.copy()
            self.target.set(frame)
            self._last = i
            self._set_current(self.files[i])
            self._say(f"{i + 1} / {len(self.files)}  "
                      f"{os.path.basename(self.files[i])}")
        except Exception as exc:
            # Nothing valid is on screen now, so no frame is "last". Leaving
            # _last alone made stepping BACK to the previous frame hit the
            # early return above and keep this message up with no image, and
            # the current-file port kept naming a frame that was not shown.
            self._last = None
            try: self.target.set(None)
            except Exception: pass
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
        there, and "Pillow is not installed" used to go only to a console
        while the panel stayed blank."""
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
        try: return getattr(self, name)
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
                try: out[name] = p.get()
                except Exception: pass
        return out

    def apply(self, values) -> None:
        """Write out/inout ports from {name: value}. Unknown names ignored."""
        for name, value in dict(values or {}).items():
            p = getattr(self, RENAMED.get(name, name), None)
            if p is not None and p.direction in ("o", "io"):
                try: p.set(value)
                except Exception: pass
'''


# The runtime knows all the classes; emit_ports stamps out only the constructor.

def emit_ports(spec: Spec, aliases: Optional[Dict[str, str]] = None) -> str:
    """Generate the ui/ports.py file: PORTS_RUNTIME + a project-specific Ports."""
    aliases = dict(aliases or {})
    L: List[str] = [PORTS_RUNTIME]

    # RENAMED shim: `RENAMED[old] = new`. Included at the top of the file, so
    # `from ui.ports import RENAMED` (or a bare import) sees them. The runtime
    # dict is populated at import time via module-level assignment.
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
    if ports:
        names = [p.name for p in ports]
        L.append(f"    _names = {tuple(names)!r}")
    else:
        L.append("    _names = ()")
    L.append("")
    L.append("    def __init__(self, ui):")
    L.append("        self._ui = ui")
    if not ports:
        L.append("        # no bindable widgets on this window")
        L.append("        pass")
        return "\n".join(L).rstrip() + "\n"

    # by-shape lookup: for radio group members, we need to find the widget
    # name of each member. WidgetSpec.name is the attribute on `ui`.
    by_shape: Dict[str, WidgetSpec] = {w.shape_id: w for w in spec.widgets}

    for p in ports:
        primary = by_shape.get(p.shape_ids[0]) if p.shape_ids else None
        if primary is None:
            continue
        wname = f"ui.{primary.name}"
        default = _py(p.default) if p.default is not None else "None"

        if p.binder == "event":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> event")
            L.append(
                f"        self.{p.name} = _EventPort(\n"
                f"            {_py(p.name)}, {wname}, ui=ui, "
                f"handler={_py(primary.handler or '')})")
        elif p.binder == "text":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> str (accessor pair)")
            L.append(
                f"        self.{p.name} = _TextPort(\n"
                f"            {_py(p.name)}, {wname}, "
                f"direction={_py(p.direction)}, type={_py(p.type)})")
            if p.default is not None:
                L.append(f"        self.{p.name}.set({default})")
        elif p.binder == "list":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> selection")
            L.append(
                f"        self.{p.name} = _ListPort(\n"
                f"            {_py(p.name)}, {wname}, "
                f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "table":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> selection (rows)")
            L.append(
                f"        self.{p.name} = _TablePort(\n"
                f"            {_py(p.name)}, {wname}, "
                f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "tab":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> current tab")
            L.append(
                f"        self.{p.name} = _TabPort(\n"
                f"            {_py(p.name)}, {wname}, "
                f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "proxy":
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> {p.writer}")
            L.append(
                f"        self.{p.name} = _ProxyPort(\n"
                f"            {_py(p.name)}, {wname}, writer={_py(p.writer)}, "
                f"direction={_py(p.direction)}, type={_py(p.type)})")
        elif p.binder == "var":
            # SEED A CAPTION INTO ITS VAR. Attaching a textvariable to a
            # widget that carries text= makes the variable authoritative, so
            # an unseeded var blanks the caption. gui_ports' caption rule
            # keeps most labels port-free; this covers the ones a user
            # deliberately bound, so binding a label does not erase it.
            if (p.default is None and p.tk_option == "textvariable"
                    and _text_of(primary)):
                default = _py(_text_of(primary))
            # Composites that own their own var: adopt it, don't re-attach.
            adopt = p.kind in ("file_picker", "scrubber")
            if adopt:
                var_expr = f"{wname}.var"
                option_expr = _py("")
            else:
                var_expr = f"tk.{p.var_class}()"
                option_expr = _py(p.tk_option)
            L.append(f"        # {p.kind}: {_c(primary.label or p.name)} -> "
                     f"{p.type}, {p.direction}")
            L.append(
                f"        self.{p.name} = _VarPort(\n"
                f"            {_py(p.name)}, {wname}, var={var_expr},\n"
                f"            option={option_expr}, type={_py(p.type)}, "
                f"direction={_py(p.direction)},\n"
                f"            default={default}, deep={_py(bool(p.deep))})")
            if p.kind == "radiobutton":
                # Attach the shared var to every member and set its value=.
                for sid in p.shape_ids:
                    member = by_shape.get(sid)
                    if member is None:
                        continue
                    val = str(member.props.get("value")
                              or _default_radio_value(member))
                    L.append(f"        ui.{member.name}.configure("
                             f"variable=self.{p.name}.var, value={_py(val)})")
            if p.kind == "checkbutton":
                # An unbound ttk.Checkbutton renders as ('alternate',); the
                # onvalue/offvalue pair is what turns "None" into False.
                L.append(f"        ui.{primary.name}.configure("
                         f"onvalue=True, offvalue=False)")
        else:
            L.append(f"        # {p.kind}: unknown binder {p.binder!r} — port dropped")
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
            # gui_spec.validate blocks this before emit is reached; the guard
            # stays so a hand-built Spec cannot emit a NameError.
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
    """Fallback for a radio whose ``value=`` prop is empty. Matches
    gui_ports.default_port_name's slug()."""
    return re.sub(r"[^a-z0-9]+", "_",
                  str(w.label or w.name).strip().lower()).strip("_")


# ============================================================
# Hand-written files (created once, never rewritten)
# ============================================================

def emit_app_py(spec: Spec) -> str:
    return f'''"""Hand-written application code for {spec.project}.

This file is created ONCE and never rewritten by the designer. Put behaviour
here: the generated MainUi builds the widgets and calls self.on_<name>, and
those methods live below.

Regenerating the wireframe rewrites ui/ only. Nothing here is touched.

App inherits HandlerMixin FIRST so the handler bodies in handlers.py win over
the no-op stubs MainUi defines. Without that order the stubs shadow them and
every handler silently does nothing — which is exactly what happened before
this line existed: handlers.py was generated, never wired in, and a declared
script link produced dead code that looked correct.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from handlers import HandlerMixin
from ui.main_ui import MainUi


class App(HandlerMixin, MainUi):
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


def handler_stub(name: str, script: Optional[Dict[str, Any]] = None,
                 title: str = "") -> str:
    """A handler body for handlers.py.

    With no script link this is the TODO stub it always was. With one, it is a
    WORKING call — the declared function, fed the declared input ports, its
    result written to the declared output port.

    FAILURE IS SHOWN, NOT ROUNDED TO ZERO. The function reports failure by
    raising, or by returning a dict with a non-empty "error"; both land in the
    except, which CLEARS every port this call fills and shows the message in
    the window (MainUi.report_error). The old stub printed to a console nobody
    reads and left the ports alone — so a scan that could not run left "0 bad
    frames" on screen, or the previous run's count. The import is inside the
    try too: a linked module missing on this machine is a failure to report,
    not a traceback that escapes the handler.

    ``title`` names the action in the error dialog — the button's label.

    Generated ONCE. handlers.py is append-only and never rewritten, so this is
    a starting point the user owns and edits, not generated code that will be
    clobbered. That is the whole point of routing the link through here rather
    than into ui/: the designer records the wiring, the user keeps the
    behaviour."""
    script = dict(script or {})
    module = str(script.get("module") or "").strip()
    func = str(script.get("function") or "").strip()
    if not (module and func):
        return (f"\n    def {name}(self, *args) -> None:\n"
                f'        """TODO: implement."""\n'
                f"        pass\n")

    inputs = [str(p) for p in (script.get("inputs") or []) if str(p).strip()]
    output = str(script.get("output") or "").strip()
    # `outputs` maps PORT NAME -> RESULT KEY, so one call can fill several
    # widgets. A count on its own tells a user ten frames are bad and leaves
    # them to find which; scanning twice to fill two widgets would double the
    # work for nothing.
    outputs = dict(script.get("outputs") or {})
    args = ", ".join(f"self.ports.{p}.get()" for p in inputs)
    call = f"{func}({args})"

    lines = [
        f"\n    def {name}(self, *args) -> None:",
        f'        """Runs {module}.{func} — wired from the wireframe.',
        "",
        "        Generated once from the widget's script link. handlers.py is",
        "        never rewritten, so edit this freely.",
        '        """',
        "        try:",
        f"            from {module} import {func}",
        f"            result = {call}",
        "            # Failure is raising, or a dict carrying a non-empty 'error'.",
        "            if isinstance(result, dict) and result.get('error'):",
        "                raise RuntimeError(result['error'])",
    ]
    filled = list(outputs) if outputs else ([output] if output else [])
    if outputs:
        for port, key in outputs.items():
            lines.append(f"            self.ports.{port}.set(result[{_py(str(key))}])")
    elif output:
        lines.append(f"            self.ports.{output}.set(result)")
    else:
        lines.append("            print(result)   # no output port declared")
    lines += [
        "        except Exception as exc:",
        "            # Clear what this call fills — a stale or zero value must",
        "            # not sit there looking like an answer — then say why, in",
        "            # the window.",
    ]
    if filled:
        lines.append("            self.clear_ports("
                     + ", ".join(_py(p) for p in filled) + ")")
    lines.append(f"            self.report_error({_py(title or f'{module}.{func}')}, "
                 f"exc)")
    return "\n".join(lines) + "\n"


def _title_for(spec: Spec, handler: str) -> str:
    """The label of the widget that owns ``handler`` — the error dialog's
    title, so it names the button the user pressed."""
    for w in spec.widgets:
        if w.handler == handler:
            return str(w.label or w.name)
    return ""


def _script_for(spec: Spec, handler: str) -> Dict[str, Any]:
    """The script link declared on the widget that owns ``handler``, if any."""
    for w in spec.widgets:
        if w.handler == handler and getattr(w, "script", None):
            return dict(w.script)
    return {}


def _legacy_stubs(name: str, script: Dict[str, Any], title: str) -> List[str]:
    """Every stub an OLDER Council wrote for this handler, byte for byte.

    handlers.py is never rewritten, so a stub generated before failures were
    reported kept its old except — print() to a console — for ever. Once the
    linked functions began raising instead of returning zeros, that turned a
    failed scan from a false "0" into the PREVIOUS folder's count and file
    names, silently (measured on all four of the user's projects). An exact
    match is a stub nobody has touched, so replacing it loses nothing; an
    edited one never matches and is left alone.

      * the TODO stub, from before the button had a script link
      * 7fdafb0 .. 4ffd556: import outside the try, print() on failure
      * 4ffd556 .. now: ports cleared one by one, where a renamed port's
        AttributeError stopped report_error from running"""
    module = str(script.get("module") or "").strip()
    func = str(script.get("function") or "").strip()
    inputs = [str(p) for p in (script.get("inputs") or []) if str(p).strip()]
    output = str(script.get("output") or "").strip()
    outputs = dict(script.get("outputs") or {})
    args = ", ".join(f"self.ports.{p}.get()" for p in inputs)
    head = [f"    def {name}(self, *args) -> None:",
            f'        """Runs {module}.{func} — wired from the wireframe.',
            "",
            "        Generated once from the widget's script link. handlers.py is",
            "        never rewritten, so edit this freely.",
            '        """']
    if outputs:
        sets = [f"            self.ports.{p}.set(result[{_py(str(k))}])"
                for p, k in outputs.items()]
    elif output:
        sets = [f"            self.ports.{output}.set(result)"]
    else:
        sets = ["            print(result)   # no output port declared"]
    printing = head + [
        f"        from {module} import {func}",
        "        try:",
        f"            result = {func}({args})",
    ] + sets + [
        "        except Exception as exc:",
        "            # A analysis script raising must not kill the UI thread;",
        "            # the user sees the failure instead of a frozen window.",
        f'            print(f"{name} failed: {{exc!r}}")']
    filled = list(outputs) if outputs else ([output] if output else [])
    clearing = head + [
        "        try:",
        f"            from {module} import {func}",
        f"            result = {func}({args})",
        "            # Failure is raising, or a dict carrying a non-empty 'error'.",
        "            if isinstance(result, dict) and result.get('error'):",
        "                raise RuntimeError(result['error'])",
    ] + sets + [
        "        except Exception as exc:",
        "            # Clear what this call fills — a stale or zero value must",
        "            # not sit there looking like an answer — then say why, in",
        "            # the window.",
    ] + [f"            self.ports.{p}.clear()" for p in filled] + [
        f"            self.report_error({_py(title or f'{module}.{func}')}, exc)"]
    todo = handler_stub(name).lstrip("\n")
    return [todo, "\n".join(printing) + "\n", "\n".join(clearing) + "\n"]


def upgrade_stubs(src: str, spec: Spec) -> Tuple[str, List[str], List[str]]:
    """(new source, upgraded handlers, handlers left as they are that do not
    report failures) for an existing handlers.py.

    Only a script-linked handler whose WHOLE definition — found by parsing,
    so a line appended to it is part of it — equals a _legacy_stubs text is
    replaced, with the stub this version writes."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src, [], []
    spans: Dict[str, List[Tuple[int, int]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = min([item.lineno] + [d.lineno for d in
                                                 item.decorator_list])
                    spans.setdefault(item.name, []).append(
                        (first, item.end_lineno))
    lines = src.splitlines(keepends=True)
    edits, upgraded, silent = [], [], []
    for h in spec.handlers:
        script = _script_for(spec, h)
        if not script or len(spans.get(h, [])) != 1:
            continue
        a, b = spans[h][0]
        current = "".join(lines[a - 1:b])
        if not current.endswith("\n"):
            current += "\n"
        title = _title_for(spec, h)
        new = handler_stub(h, script, title).lstrip("\n")
        if current == new:
            continue
        if current in _legacy_stubs(h, script, title):
            edits.append((a, b, new))
            upgraded.append(h)
        elif "report_error" not in current:
            silent.append(h)
    for a, b, new in sorted(edits, reverse=True):
        lines[a - 1:b] = [new]
    return "".join(lines), upgraded, silent


ON_CLOSE_STUB = '''
    def on_close(self) -> None:
        """Runs when the app closes — its window's X, the GUI Designer's Stop,
        or the designer exiting. Put hardware cleanup here: stop a camera
        grab, close the device, finish writing a recording. The window closes
        after this returns, and closes anyway if this raises."""
        pass
'''


def emit_handlers_py(spec: Spec) -> str:
    body = "".join(handler_stub(h, _script_for(spec, h), _title_for(spec, h))
                   for h in spec.handlers) + ON_CLOSE_STUB
    return f'''"""Handler stubs for {spec.project}.

APPEND-ONLY. Regeneration adds stubs for NEW widgets to the end of this file
and never rewrites what you have written. (A stub an older version generated
that nobody has edited is upgraded in place, and the Generate log says so.)

Mix HandlerMixin into App (in app.py) if you would rather keep behaviour out of
app.py itself.
"""
from __future__ import annotations


class HandlerMixin:
{body}'''


# A file that exists at the app root and nowhere else, used by a generated
# main.py to recognise the app if it has been moved since generation.
_ROOT_MARKER = "council_gui_engine.py"

LAUNCH_SHIM = '''"""Moved to main.py. Kept so existing shortcuts keep working."""
from main import main

if __name__ == "__main__":
    main()
'''


def _requires_block(requires: Sequence[str]) -> str:
    """Startup check for the project's declared packages.

    Every entry is a STATIC import, so it passes the policy gate on exactly
    the same terms as the app's own imports (a declared package is on this
    project's allowlist) and needs neither importlib nor __import__, both of
    which the gate refuses.

    Launched from the designer, the message goes to stderr, which is the
    designer's log. Launched by hand there is no log to read, so it is also
    shown in a window — the designer marks its launches with
    COUNCIL_PREVIEW_CONTROL, which is how the two are told apart."""
    if not requires:
        return ""
    L = ["# -- declared packages (the project's `requires`) -----------------",
         "# Checked BEFORE any widget exists: the wrong Python fails here with",
         "# a list of what is missing, instead of a blank panel or a traceback",
         "# on the first click.",
         "_MISSING = []"]
    for name in requires:
        L += ["try:",
              f"    import {name}  # noqa: F401",
              "except Exception as _exc:",
              f"    _MISSING.append(({name!r}, '%s: %s' % "
              f"(type(_exc).__name__, _exc)))"]
    L += ["if _MISSING:",
          "    _LINES = ['This app cannot start under ' + sys.executable +",
          "              ' because it is missing:']",
          "    _LINES += ['  - %s (%s)' % _m for _m in _MISSING]",
          "    _LINES.append(\"Choose a Python that has them (the GUI \"",
          "                  \"Designer's 'Run with'), or install them into \"",
          "                  \"this one.\")",
          r'    _MSG = "\n".join(_LINES)',
          "    if sys.stderr is not None:     # None under pythonw",
          r'        sys.stderr.write(_MSG + "\n")',
          "    import os as _os",
          "    if not (_os.environ.get('COUNCIL_PREVIEW_CONTROL')",
          "            or _os.environ.get('COUNCIL_NO_DIALOGS')):",
          "        try:",
          "            import tkinter as _tk",
          "            from tkinter import messagebox as _mb",
          "            _r = _tk.Tk()",
          "            _r.withdraw()",
          "            _mb.showerror('Missing packages', _MSG)",
          "            _r.destroy()",
          "        except Exception:",
          "            pass",
          "    raise SystemExit(3)",
          ""]
    return "\n".join(L)


def emit_main_py(spec: Spec, project_dir: Path) -> str:
    """The entry point: `python main.py`.

    REGENERATED every time, on purpose. The sys.path block below depends on
    the project's mode and on LINKED_ALLOWLIST, so freezing it into a
    hand-edited file would leave it describing an allowlist that has since
    changed. Behaviour goes in app.py, which is written once and never
    rewritten; this file only bootstraps.
    """
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
    # THE APP ROOT IS BAKED IN, not walked to.
    #
    # This used to be `Path(__file__).parent.parent.parent`, which assumed the
    # vault lived INSIDE the repo. The default vault is ~/.council/vault, so
    # three levels up from the project landed on ~/.council and EVERY
    # allowlisted import failed at runtime — measured:
    #     ModuleNotFoundError: No module named 'frame_timing'
    # It looked fine from a shell sitting in the repo, because '' on sys.path
    # covered for it; it failed when launched with cwd set to the project,
    # which is how gui_runner actually starts it.
    app_root = Path(__file__).resolve().parent
    path_block = (
        f"_APP_ROOT = Path({str(app_root)!r})\n"
        f"if not (_APP_ROOT / {_ROOT_MARKER!r}).is_file():\n"
        f"    # the app was moved or copied; find it from here instead\n"
        f"    for _p in Path(__file__).resolve().parents:\n"
        f"        if (_p / {_ROOT_MARKER!r}).is_file():\n"
        f"            _APP_ROOT = _p\n"
        f"            break\n"
        f"if str(_APP_ROOT) not in sys.path:\n"
        f"    sys.path.insert(0, str(_APP_ROOT))\n" if linked else "")
    return f'''"""Entry point for {spec.project}. Generated — run `python main.py`.

Behaviour belongs in app.py, which is created once and never rewritten.
"""
from __future__ import annotations

import sys
from pathlib import Path

{note}
sys.path.insert(0, str(Path(__file__).resolve().parent))
{path_block}
{_requires_block(spec.requires)}
from app import main

if __name__ == "__main__":
    main()
'''


# ============================================================
# emit
# ============================================================

def emit(spec: Spec, project_path: Any, *,
         preserve_regions: Optional[Dict[str, str]] = None,
         aliases: Optional[Dict[str, str]] = None) -> EmitResult:
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
    _write(ui / "ports.py", emit_ports(spec, aliases), res)
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
        # MIGRATION, the one exception to append-only: a stub an older
        # Council wrote and nobody has edited is replaced (upgrade_stubs).
        new_src, upgraded, silent = upgrade_stubs(src, spec)
        if upgraded:
            handlers.write_text(new_src, encoding="utf-8")
            src = new_src
            res.handlers_upgraded.extend(upgraded)
            res.warnings.append(
                "handlers.py: upgraded " + ", ".join(upgraded) + " — unedited "
                "stubs from an older version, which left the previous "
                "results on screen when the call failed")
        for h in silent:
            res.warnings.append(
                f"handlers.py: {h} has been edited, so it was left as it is "
                f"— it does not call report_error, so if its script fails the "
                f"window will not say so")
        missing = [h for h in spec.handlers if f"def {h}(" not in src]
        if missing:
            # APPEND, never rewrite. Existing bodies are untouched.
            with handlers.open("a", encoding="utf-8") as fh:
                fh.write("\n    # --- added by regeneration ---\n")
                for h in missing:
                    fh.write(handler_stub(h, _script_for(spec, h),
                                          _title_for(spec, h)))
            res.handlers_added.extend(missing)
        res.files_skipped.append(str(handlers))
    else:
        _write(handlers, emit_handlers_py(spec), res)
        res.handlers_added.extend(spec.handlers)

    _write(root / "main.py", emit_main_py(spec, root), res)
    import gui_spec as _gsp
    res.warnings.extend(_gsp.script_warnings(spec))

    # MIGRATION. Projects generated before the rename ran from launch.py, and
    # a shortcut or a note may still point at it. Replace it with a shim
    # rather than deleting it — but only where it already exists, so a fresh
    # project does not start life with a vestigial file.
    legacy = root / "launch.py"
    if legacy.exists():
        _write(legacy, LAUNCH_SHIM, res)

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
