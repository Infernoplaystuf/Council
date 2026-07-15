"""
plots_pane.py — a Spyder-style inline plots pane for Tk.

A large render surface for the current figure (with the real matplotlib
navigation toolbar: pan / zoom / save) beside a scrollable rail of thumbnails,
one per figure made this session. Click a thumbnail to bring that figure back;
pop it out into its own window if you want it side-by-side.

Why this exists
---------------
The Grapher's interactive charts were Plotly HTML shown in a tkinterweb frame.
That is broken twice over on the air-gapped machines this app targets: the HTML
loaded plotly.js from a CDN that never resolves offline (a blank chart, with no
error, reported as success), and tkinterweb is a tkhtml3 wrapper with NO
JavaScript engine, so a Plotly chart could not run in it even with the script
inlined.

An Agg-rendered matplotlib Figure embedded via FigureCanvasTkAgg has neither
problem: Tk draws the canvas itself, so there is no browser, no JS, and nothing
to fetch. Pan/zoom still work, because the toolbar drives the canvas directly.

Everything here builds Figure() objects and never calls pyplot, which keeps
figures off pyplot's global registry (a leak across a long session) and stops
any backend from opening a window of its own.
"""
from __future__ import annotations

import io
from typing import List, Optional

import tkinter as tk
from tkinter import ttk

try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
    from matplotlib.figure import Figure
    _MPL_OK = True
except Exception:                                      # pragma: no cover
    _MPL_OK = False

try:
    from PIL import Image, ImageTk
    _PIL_OK = True
except Exception:                                      # pragma: no cover
    _PIL_OK = False

THUMB_W = 180
# Figures are cheap but not free (each holds its own rendered buffers). A long
# session can make hundreds; keep the most recent and let the rest go, rather
# than growing until the app is swapping.
MAX_HISTORY = 60


def figure_to_png_bytes(fig, dpi: int = 72) -> bytes:
    """Render a Figure to PNG bytes. Pure function — no Tk, no pyplot, so the
    thumbnail path is testable headlessly."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()


def figure_to_thumbnail(fig, width: int = THUMB_W):
    """A PIL Image thumbnail of ``fig``, at most ``width`` wide."""
    if not _PIL_OK:
        raise RuntimeError("Pillow is required for plot thumbnails.")
    im = Image.open(io.BytesIO(figure_to_png_bytes(fig)))
    im.thumbnail((width, width * 4))
    return im


class PlotsPane(ttk.Frame):
    """Render surface + thumbnail history rail.

    Use ``add_figure(fig)`` with a fully-built Figure; it becomes the current
    plot and gains a thumbnail. Nothing in here knows what a chart means — it
    only displays Figures — so the registry can grow without touching the pane.
    """

    def __init__(self, master, *, thumb_width: int = THUMB_W,
                 max_history: int = MAX_HISTORY, on_select=None, **kw):
        super().__init__(master, **kw)
        self.thumb_width = thumb_width
        self.max_history = max_history
        self._on_select = on_select

        self._figures: List["Figure"] = []
        self._thumb_imgs: List[object] = []      # refs; Tk won't hold them
        self._thumb_btns: List[tk.Button] = []
        self._active_canvas = None
        self._active_toolbar = None
        self._active_idx: Optional[int] = None

        # ---- left: history rail -------------------------------------------
        rail_wrap = ttk.Frame(self)
        rail_wrap.pack(side="left", fill="y")
        self._rail_canvas = tk.Canvas(rail_wrap, width=self.thumb_width + 26,
                                      highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(rail_wrap, orient="vertical",
                           command=self._rail_canvas.yview)
        self._rail = ttk.Frame(self._rail_canvas)
        self._rail.bind("<Configure>", lambda e: self._rail_canvas.configure(
            scrollregion=self._rail_canvas.bbox("all")))
        self._rail_canvas.create_window((0, 0), window=self._rail, anchor="nw")
        self._rail_canvas.configure(yscrollcommand=sb.set)
        self._rail_canvas.pack(side="left", fill="y", expand=False)
        sb.pack(side="right", fill="y")
        # Wheel scrolling over the rail (Windows/macOS send <MouseWheel>).
        self._rail_canvas.bind("<Enter>", lambda e: self._rail_canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self._rail_canvas.bind("<Leave>", lambda e: self._rail_canvas.unbind_all(
            "<MouseWheel>"))

        # ---- right: render surface ----------------------------------------
        self._surface = ttk.Frame(self)
        self._surface.pack(side="left", fill="both", expand=True)
        ctl = ttk.Frame(self._surface)
        ctl.pack(side="top", fill="x")
        self._count_lbl = ttk.Label(ctl, text="No plots yet")
        self._count_lbl.pack(side="left", padx=6)
        ttk.Button(ctl, text="Clear history",
                   command=self.clear).pack(side="right", padx=2)
        ttk.Button(ctl, text="Pop out",
                   command=self.popout).pack(side="right", padx=2)
        self._holder = ttk.Frame(self._surface)
        self._holder.pack(side="top", fill="both", expand=True)
        self._placeholder = ttk.Label(
            self._holder, anchor="center",
            text="Pick columns, then choose a plot.\n"
                 "Every plot you make lands in the rail on the left.")
        self._placeholder.pack(fill="both", expand=True)

    # ---- public API -------------------------------------------------------

    def add_figure(self, fig) -> int:
        """Add a built Figure, show it, and return its index."""
        if not _MPL_OK:
            raise RuntimeError("matplotlib is required for the plots pane.")
        self._figures.append(fig)
        self._add_thumb(fig, len(self._figures) - 1)
        self._trim_history()
        self.show(len(self._figures) - 1)
        return len(self._figures) - 1

    @property
    def count(self) -> int:
        return len(self._figures)

    def current_figure(self):
        if self._active_idx is None:
            return None
        return self._figures[self._active_idx]

    def show(self, idx: int):
        """Embed figure ``idx`` in the render surface."""
        if not (0 <= idx < len(self._figures)):
            return
        for w in self._holder.winfo_children():
            w.destroy()
        self._active_canvas = None
        self._active_toolbar = None

        fig = self._figures[idx]
        canvas = FigureCanvasTkAgg(fig, master=self._holder)
        canvas.draw()
        toolbar = NavigationToolbar2Tk(canvas, self._holder,
                                       pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        self._active_canvas = canvas
        self._active_toolbar = toolbar
        self._active_idx = idx
        self._highlight(idx)
        self._count_lbl.configure(
            text=f"Plot {idx + 1} of {len(self._figures)}")
        if self._on_select:
            try:
                self._on_select(idx, fig)
            except Exception:
                pass

    def popout(self):
        """Open the current figure in its own interactive window."""
        fig = self.current_figure()
        if fig is None:
            return
        win = tk.Toplevel(self)
        win.title(f"Figure {self._active_idx + 1}")
        c = FigureCanvasTkAgg(fig, master=win)
        c.draw()
        NavigationToolbar2Tk(c, win)
        c.get_tk_widget().pack(fill="both", expand=True)
        return win

    def clear(self):
        """Drop every figure and reset the pane."""
        self._figures.clear()
        self._thumb_imgs.clear()
        self._thumb_btns.clear()
        for w in self._rail.winfo_children():
            w.destroy()
        for w in self._holder.winfo_children():
            w.destroy()
        self._active_canvas = None
        self._active_toolbar = None
        self._active_idx = None
        self._count_lbl.configure(text="No plots yet")
        self._placeholder = ttk.Label(
            self._holder, anchor="center",
            text="Pick columns, then choose a plot.")
        self._placeholder.pack(fill="both", expand=True)

    # ---- internals --------------------------------------------------------

    def _on_wheel(self, event):
        self._rail_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _add_thumb(self, fig, idx: int):
        try:
            im = figure_to_thumbnail(fig, self.thumb_width)
            photo = ImageTk.PhotoImage(im)
        except Exception:
            photo = None
        self._thumb_imgs.append(photo)
        btn = tk.Button(self._rail, image=photo, relief="flat", bd=2,
                        command=lambda i=idx: self.show(i))
        if photo is None:
            btn.configure(image="", text=f"Plot {idx + 1}",
                          width=18, height=4)
        btn.pack(side="top", pady=4, padx=4)
        self._thumb_btns.append(btn)

    def _highlight(self, idx: int):
        for i, b in enumerate(self._thumb_btns):
            try:
                b.configure(relief="solid" if i == idx else "flat")
            except Exception:
                pass

    def _trim_history(self):
        """Drop the oldest figures past the cap, and their thumbnails with
        them — otherwise a long session grows without bound."""
        while len(self._figures) > self.max_history:
            self._figures.pop(0)
            self._thumb_imgs.pop(0)
            btn = self._thumb_btns.pop(0)
            try:
                btn.destroy()
            except Exception:
                pass
            # Every remaining button's captured index is now off by one.
            for i, b in enumerate(self._thumb_btns):
                b.configure(command=lambda idx=i: self.show(idx))
            if self._active_idx is not None:
                self._active_idx = max(0, self._active_idx - 1)
