"""
gui_wizard.py — a guided on-ramp into the GUI Designer.

The designer is a freeform canvas: powerful once you know it, blank and
unhelpful the first time you open it. This wizard asks five short questions and
hands the answers to that same canvas as ordinary shapes, which the user then
edits normally. It is an ON-RAMP, not a second designer — there is one scene
model, one snap engine, one undo stack, and Generate keeps reading the canvas
it always read.

The layout arithmetic lives in the pure ``gui_templates``; this file is widgets
and step order. That split is what makes the interesting half testable without
a display.

WHAT THE HOST DOES WITH THE RESULT
----------------------------------
``open_wizard(parent, on_done=...)`` calls back with a :class:`WizardResult`.
The host creates the project, applies ``window``/``canvas`` sizing, and puts
``shapes`` on the canvas. The wizard itself creates nothing and writes nothing:
a cancelled wizard must leave no project directory behind, and the only way to
guarantee that is to never have made one.

``transient(parent)`` WITHOUT ``grab_set()`` — matching db_connect_wizard, and
deliberately not onboarding. A modal grab would lock the user out of the
Council transcript while the wizard is open, and there is nothing here worth
that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import gui_templates as _gt
from gui_shapes import PALETTE, Shape

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
# Tk shell
# ============================================================

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    _TK_OK = True
except Exception:                       # pragma: no cover — headless box
    tk = None                                    # type: ignore[assignment]
    ttk = messagebox = None                      # type: ignore[assignment]
    _TK_OK = False

_TkBase = tk.Toplevel if _TK_OK else object

STEPS = ("basics", "layout", "contents", "reserve", "review")
STEP_TITLES = {
    "basics": "1 of 5 — Name and window",
    "layout": "2 of 5 — Starting layout",
    "contents": "3 of 5 — What goes in it",
    "reserve": "4 of 5 — Reserve space for later",
    "review": "5 of 5 — Review",
}


class GuiWizard(_TkBase):
    """Five steps, then a shape list handed back through ``on_done``."""

    def __init__(self, parent,
                 on_done: Optional[Callable[[WizardResult], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 existing: Sequence[str] = ()):
        if not _TK_OK:
            raise RuntimeError("GuiWizard requires tkinter.")
        super().__init__(parent)
        self._on_done = on_done
        self._log = log or (lambda msg: None)
        self._existing = {str(n).strip().lower() for n in existing}
        self._step_idx = 0
        self._closed = False

        self.title("New GUI project")
        self.geometry("640x560")
        try:
            self.transient(parent)
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # -- answers ---------------------------------------------------
        self.v_name = tk.StringVar(value="")
        self.v_mode = tk.StringVar(value="linked")
        self.v_title = tk.StringVar(value="")
        self.v_min_w = tk.StringVar(value=str(DEFAULT_MIN_W))
        self.v_min_h = tk.StringVar(value=str(DEFAULT_MIN_H))
        self.v_template = tk.StringVar(value="form")
        self.v_fields = tk.StringVar(value="3")
        self.v_labels = tk.StringVar(value="")
        self.v_buttons = tk.StringVar(value="OK, Cancel")
        self.v_main = tk.StringVar(value=MAIN_KINDS[0])
        self.v_left = tk.StringVar(value=SIDE_KINDS[0])
        self.v_right = tk.StringVar(value="frame")
        self.v_reserve = tk.StringVar(value="0")
        self.v_res_w = tk.StringVar(value="240")
        self.v_res_h = tk.StringVar(value="160")

        self._build_ui()
        self._render_step()
        self._centre_on(parent)

    # -- chrome --------------------------------------------------------

    def _build_ui(self):
        self.head = ttk.Label(self, text="", font=("Segoe UI", 13, "bold"))
        self.head.pack(anchor="w", padx=18, pady=(14, 2))
        self.hint = ttk.Label(self, text="", wraplength=590, justify="left",
                              foreground="#a6adc8")
        self.hint.pack(anchor="w", padx=18, pady=(0, 8))

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=18, pady=4)

        footer = ttk.Frame(self)
        footer.pack(fill="x", side="bottom", padx=18, pady=12)
        ttk.Button(footer, text="Cancel",
                   command=self._on_cancel).pack(side="left")
        self.btn_next = ttk.Button(footer, text="Next →", command=self._on_next)
        self.btn_next.pack(side="right")
        self.btn_back = ttk.Button(footer, text="← Back", command=self._on_back)
        self.btn_back.pack(side="right", padx=6)

    def _centre_on(self, parent):
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - 640) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 560) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:
            pass

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _row(self, label: str, var, width: int = 28):
        r = ttk.Frame(self.body)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text=label, width=18).pack(side="left")
        e = ttk.Entry(r, textvariable=var, width=width)
        e.pack(side="left")
        return e

    # -- step machine --------------------------------------------------

    def _render_step(self):
        self._clear_body()
        step = STEPS[self._step_idx]
        self.head.configure(text=STEP_TITLES[step])
        getattr(self, f"_render_{step}")()
        self.btn_back.configure(
            state="disabled" if self._step_idx == 0 else "normal")
        self.btn_next.configure(
            text="Finish" if self._step_idx == len(STEPS) - 1 else "Next →")

    def _on_next(self):
        err = self._validate(STEPS[self._step_idx])
        if err:
            messagebox.showwarning("Not yet", err, parent=self)
            return
        if self._step_idx == len(STEPS) - 1:
            self._finish()
            return
        self._step_idx += 1
        self._render_step()

    def _on_back(self):
        if self._step_idx > 0:
            self._step_idx -= 1
            self._render_step()

    def _on_cancel(self):
        """Close without creating anything. Nothing has been written yet, so
        there is nothing to clean up — which is the point of deferring every
        side effect to the host."""
        self._closed = True
        try:
            self.destroy()
        except Exception:
            pass

    # -- steps ---------------------------------------------------------

    def _render_basics(self):
        self.hint.configure(
            text=("A short name for the project folder, and how big the window "
                  "should be allowed to get. Both are editable later."))
        self._row("Project name", self.v_name)
        self._row("Window title", self.v_title)
        self._row("Minimum width", self.v_min_w, width=10)
        self._row("Minimum height", self.v_min_h, width=10)

        m = ttk.LabelFrame(self.body, text="Where it lives")
        m.pack(fill="x", pady=(10, 0))
        ttk.Radiobutton(m, text="Linked — inside the vault, alongside your data",
                        value="linked", variable=self.v_mode).pack(anchor="w")
        ttk.Radiobutton(m, text="Standalone — a self-contained folder",
                        value="standalone", variable=self.v_mode).pack(anchor="w")

    def _render_layout(self):
        self.hint.configure(
            text=("Pick something close to what you want. Everything it "
                  "produces is ordinary shapes you can move, resize or delete "
                  "on the canvas afterwards."))
        for key in _gt.names():
            entry = _gt.TEMPLATES[key]
            ttk.Radiobutton(self.body, text=str(entry["label"]),
                            value=key, variable=self.v_template).pack(anchor="w")
            ttk.Label(self.body, text="      " + str(entry["blurb"]),
                      foreground="#a6adc8").pack(anchor="w", pady=(0, 6))

    def _render_contents(self):
        t = self.v_template.get()
        if t == "form":
            self.hint.configure(
                text=("One row per field. Names are optional — anything you "
                      "leave out becomes Field 1, Field 2 and so on."))
            self._row("How many fields", self.v_fields, width=10)
            self._row("Field names", self.v_labels, width=40)
            self._row("Buttons", self.v_buttons, width=40)
            ttk.Label(self.body, foreground="#a6adc8", wraplength=590,
                      justify="left",
                      text="Separate names with commas.").pack(anchor="w",
                                                               pady=(6, 0))
        elif t == "toolbar_main_status":
            self.hint.configure(text="What fills the middle of the window?")
            for k in MAIN_KINDS:
                ttk.Radiobutton(self.body, text=PALETTE[k]["label"], value=k,
                                variable=self.v_main).pack(anchor="w")
        elif t == "split_view":
            self.hint.configure(text="What goes on each side?")
            ttk.Label(self.body, text="Left").pack(anchor="w")
            for k in SIDE_KINDS:
                ttk.Radiobutton(self.body, text=PALETTE[k]["label"], value=k,
                                variable=self.v_left).pack(anchor="w")
            ttk.Label(self.body, text="Right").pack(anchor="w", pady=(8, 0))
            for k in MAIN_KINDS:
                ttk.Radiobutton(self.body, text=PALETTE[k]["label"], value=k,
                                variable=self.v_right).pack(anchor="w")
        else:
            self.hint.configure(text="Nothing to choose for a blank canvas.")
            ttk.Label(self.body, wraplength=590, justify="left",
                      text=("You will start with an empty canvas and the full "
                            "widget palette.")).pack(anchor="w")

    def _render_reserve(self):
        self.hint.configure(
            text=("A reserved space is an empty frame held open at the size "
                  "you choose, so the room is still there when you come back "
                  "to fill it in. It costs no model call and generates as a "
                  "real, visible frame."))
        self._row("How many", self.v_reserve, width=10)
        self._row("Each one, width", self.v_res_w, width=10)
        self._row("Each one, height", self.v_res_h, width=10)

    def _render_review(self):
        try:
            shapes = self._shapes()
        except Exception as exc:
            ttk.Label(self.body, foreground="#f38ba8", wraplength=590,
                      text=f"Could not build the layout: {exc}").pack(anchor="w")
            return
        kinds: Dict[str, int] = {}
        for s in shapes:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        summary = ", ".join(f"{n}x {PALETTE[k]['label']}"
                            for k, n in sorted(kinds.items())) or "nothing yet"
        self.hint.configure(
            text=f"{len(shapes)} shape(s): {summary}. Finish puts these on the "
                 f"canvas, where you can keep editing them.")
        self._preview(shapes)

    def _preview(self, shapes: Sequence[Shape]):
        """A crude scaled sketch, NOT a DesignerCanvas.

        Embedding the real canvas would give the user a second editable scene
        whose edits go nowhere — every stroke silently discarded on Finish.
        A picture cannot mislead that way."""
        pw, ph = 560, 300
        cv = tk.Canvas(self.body, width=pw, height=ph, bg="#181825",
                       highlightthickness=1, highlightbackground="#45475a")
        cv.pack(pady=(8, 0))
        if not shapes:
            cv.create_text(pw // 2, ph // 2, text="empty canvas",
                           fill="#6c7086")
            return
        scale = min(pw / float(_gt.CANVAS_W), ph / float(_gt.CANVAS_H))
        for s in sorted(shapes, key=lambda s: s.z):
            x1, y1 = s.x * scale, s.y * scale
            x2, y2 = s.x2 * scale, s.y2 * scale
            cv.create_rectangle(x1, y1, x2, y2,
                                outline="#cba6f7", fill="#1e1e2e")
            if s.label and (x2 - x1) > 30:
                cv.create_text(x1 + 4, y1 + 8, text=s.label[:18], anchor="w",
                               fill="#bac2de", font=("Segoe UI", 7))

    # -- answers -> data ----------------------------------------------

    def _int(self, var, fallback: int) -> int:
        try:
            return int(str(var.get()).strip())
        except (TypeError, ValueError):
            return fallback

    def _template_options(self) -> Dict[str, Any]:
        # NOT _options: tkinter.Misc._options(cnf) is called during widget
        # construction, so shadowing it breaks every Toplevel this class makes.
        t = self.v_template.get()
        if t == "form":
            labels = [p.strip() for p in self.v_labels.get().split(",")
                      if p.strip()]
            buttons = [p.strip() for p in self.v_buttons.get().split(",")
                       if p.strip()]
            return {"n_fields": max(0, self._int(self.v_fields, 3)),
                    "labels": labels or None,
                    "buttons": buttons or ("OK",),
                    "title": self.v_title.get().strip() or "Details"}
        if t == "toolbar_main_status":
            return {"main_kind": self.v_main.get()}
        if t == "split_view":
            return {"left_kind": self.v_left.get(),
                    "right_kind": self.v_right.get()}
        return {}

    def _shapes(self) -> List[Shape]:
        return build_shapes(self.v_template.get(), self._template_options(),
                            reserve_n=max(0, self._int(self.v_reserve, 0)),
                            reserve_w=max(24, self._int(self.v_res_w, 240)),
                            reserve_h=max(24, self._int(self.v_res_h, 160)))

    def _validate(self, step: str) -> str:
        if step == "basics":
            name = self.v_name.get().strip()
            if not name:
                return "Give the project a name."
            if name.lower() in self._existing:
                return f"There is already a project called {name!r}."
            if self._int(self.v_min_w, 0) < 200 or self._int(self.v_min_h, 0) < 200:
                return "A minimum window smaller than 200x200 will not be usable."
        if step == "contents" and self.v_template.get() == "form":
            if self._int(self.v_fields, -1) < 0:
                return "Number of fields must be zero or more."
        if step == "reserve":
            if self._int(self.v_reserve, -1) < 0:
                return "Number of reserved spaces must be zero or more."
        return ""

    def _finish(self):
        try:
            shapes = self._shapes()
        except Exception as exc:
            messagebox.showerror("Could not build the layout", str(exc),
                                 parent=self)
            return
        name = self.v_name.get().strip()
        res = WizardResult(
            name=name,
            mode=self.v_mode.get(),
            title=self.v_title.get().strip() or name,
            min_w=max(200, self._int(self.v_min_w, DEFAULT_MIN_W)),
            min_h=max(200, self._int(self.v_min_h, DEFAULT_MIN_H)),
            template=self.v_template.get(),
            shapes=shapes,
        )
        self._closed = True
        try:
            self.destroy()
        except Exception:
            pass
        self._log(f"wizard: {res.template} -> {len(res.shapes)} shape(s)")
        if self._on_done:
            self._on_done(res)


def open_wizard(parent,
                on_done: Optional[Callable[[WizardResult], None]] = None,
                log: Optional[Callable[[str], None]] = None,
                existing: Sequence[str] = ()) -> "GuiWizard":
    return GuiWizard(parent, on_done=on_done, log=log, existing=existing)
