"""
council_qt.widgets.plots_pane — the session's plots, in Qt.

A render surface for the current figure with the real matplotlib navigation
toolbar, beside a rail of thumbnails, one per figure made this session. Click a
thumbnail to bring that figure back; pop it out to see two at once.

WHY AN EMBEDDED AGG CANVAS AND NOT A BROWSER
The Grapher's interactive charts were Plotly HTML in an embedded HTML frame,
and that is broken twice over on the air-gapped machines this app targets: the
HTML pulled plotly.js from a CDN that never resolves offline — a blank chart,
no error, reported as a success — and the Tk HTML widget has no JavaScript
engine at all, so a Plotly chart could not run in it even with the script
inlined. An Agg figure on a canvas has neither problem: Qt paints it, so there
is no browser, no JavaScript and nothing to fetch, and pan/zoom still work
because the toolbar drives the canvas directly.

That reasoning survives the port intact. It also means this pane is the
Grapher's ONLY embedded view — the interactive path opens the system browser,
which is what the Tk build actually does today.

THE HISTORY IS NOT HERE
`council_core.plots.FigureHistory` holds it, because the part that goes wrong
is arithmetic: every row in the rail is bound to an index, and dropping the
oldest figure shifts every one of them.
"""
from __future__ import annotations

from typing import Callable, List, Optional

import matplotlib
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from council_core import plots

# Agg, explicitly. The canvas below embeds a rendered figure; letting a GUI
# backend be chosen instead lets matplotlib open a window of its own, which on
# a headless run is a crash and on a user's machine is a stray window.
matplotlib.use("Agg")

from matplotlib.backends.backend_qtagg import (FigureCanvasQTAgg,  # noqa: E402
                                               NavigationToolbar2QT)


class PlotsPane(QWidget):
    """Render surface plus a thumbnail history rail."""

    #: Emitted with (index, figure) when the shown figure changes.
    selected = Signal(int, object)

    def __init__(self, parent: Optional[QWidget] = None, *,
                 thumb_width: int = plots.THUMB_W,
                 max_history: int = plots.MAX_HISTORY):
        super().__init__(parent)
        self.thumb_width = thumb_width
        self.history = plots.FigureHistory(max_history=max_history)
        self._canvas: Optional[FigureCanvasQTAgg] = None
        self._toolbar: Optional[NavigationToolbar2QT] = None
        self._popouts: List[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_rail())
        split.addWidget(self._build_surface())
        split.setStretchFactor(1, 1)
        split.setSizes([thumb_width + 40, 700])
        outer.addWidget(split, 1)

    # ==================================================================
    # Building
    # ==================================================================
    def _build_rail(self) -> QWidget:
        self.rail = QListWidget()
        self.rail.setIconSize(QSize(self.thumb_width, self.thumb_width * 2))
        self.rail.setResizeMode(QListWidget.ResizeMode.Adjust)
        # Whole-row selection with the icon above nothing else: the thumbnail
        # IS the label, so a text column would only ever repeat "Plot N".
        self.rail.setSpacing(4)
        self.rail.currentRowChanged.connect(self._rail_row_changed)
        self.rail.setMinimumWidth(self.thumb_width + 30)
        return self.rail

    def _build_surface(self) -> QWidget:
        surface = QWidget()
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.count_label = QLabel("No plots yet")
        bar.addWidget(self.count_label)
        bar.addStretch(1)
        self.popout_btn = QPushButton("Pop out")
        self.popout_btn.clicked.connect(self.popout)
        self.clear_btn = QPushButton("Clear history")
        self.clear_btn.clicked.connect(self.clear)
        bar.addWidget(self.popout_btn)
        bar.addWidget(self.clear_btn)
        layout.addLayout(bar)

        self.holder = QWidget()
        self.holder_layout = QVBoxLayout(self.holder)
        self.holder_layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel(
            "Pick columns, then choose a plot.\n"
            "Every plot you make lands in the rail on the left.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.holder_layout.addWidget(self.placeholder)
        layout.addWidget(self.holder, 1)
        return surface

    # ==================================================================
    # Public API — the Tk pane's, so a caller needs no branch
    # ==================================================================
    def add_figure(self, figure) -> int:
        """Add a built Figure, show it, and return its index."""
        trim = self.history.add(figure)
        self._add_thumb(figure)
        for _ in range(trim.dropped):
            # Taking from the front keeps the rail's rows in step with the
            # history's indices, which is the whole reason the trim reports a
            # count rather than leaving the view to work it out.
            item = self.rail.takeItem(0)
            del item
        self.show_index(self.history.active or 0)
        return len(self.history) - 1

    @property
    def count(self) -> int:
        return len(self.history)

    def current_figure(self):
        return self.history.current()

    def show_index(self, index: int) -> None:
        """Embed figure `index` in the render surface."""
        if not self.history.select(index):
            return
        figure = self.history.current()
        self._clear_holder()

        self._canvas = FigureCanvasQTAgg(figure)
        self._toolbar = NavigationToolbar2QT(self._canvas, self.holder)
        self.holder_layout.addWidget(self._canvas, 1)
        self.holder_layout.addWidget(self._toolbar)
        self._canvas.draw_idle()

        self.count_label.setText(f"Plot {index + 1} of {len(self.history)}")
        if self.rail.currentRow() != index:
            blocked = self.rail.blockSignals(True)
            self.rail.setCurrentRow(index)
            self.rail.blockSignals(blocked)
        self.selected.emit(index, figure)

    # NOT aliased to `show`. The Tk pane's method is `show(idx)`, but on a
    # QWidget `show()` is Qt's own "make this visible" — aliasing it made
    # `pane.show()` raise "missing 1 required positional argument", so the pane
    # could be built and never displayed. Same class of collision as Qt reading
    # "&" in a caption as a mnemonic: a name that means one thing in Tk and
    # another in Qt.

    def popout(self) -> Optional[QWidget]:
        """The current figure in its own window.

        A NEW canvas rather than reparenting this one: a Figure may be shown by
        several canvases, but a canvas belongs to one parent, and moving it
        would empty the pane the user was looking at.
        """
        figure = self.current_figure()
        if figure is None:
            return None
        window = QWidget()
        window.setWindowTitle(f"Figure {(self.history.active or 0) + 1}")
        layout = QVBoxLayout(window)
        canvas = FigureCanvasQTAgg(figure)
        layout.addWidget(canvas, 1)
        layout.addWidget(NavigationToolbar2QT(canvas, window))
        window.resize(720, 520)
        window.show()
        # Held, or Python collects it the moment this returns and the window
        # closes itself immediately.
        self._popouts.append(window)
        return window

    def clear(self) -> None:
        self.history.clear()
        self.rail.clear()
        self._clear_holder()
        self.placeholder = QLabel("Pick columns, then choose a plot.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.holder_layout.addWidget(self.placeholder)
        self.count_label.setText("No plots yet")

    # ==================================================================
    # Internals
    # ==================================================================
    def _clear_holder(self) -> None:
        while self.holder_layout.count():
            item = self.holder_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._canvas = None
        self._toolbar = None

    def _add_thumb(self, figure) -> None:
        item = QListWidgetItem()
        try:
            pixmap = QPixmap()
            pixmap.loadFromData(plots.thumbnail_png(figure, self.thumb_width))
            item.setIcon(QIcon(pixmap))
            item.setSizeHint(QSize(self.thumb_width + 8,
                                   pixmap.height() + 8 if pixmap.height()
                                   else 120))
        except Exception:                                # noqa: BLE001
            # A figure that will not render a thumbnail still belongs in the
            # rail — it is the figure the user just made, and a rail that
            # silently skipped it would put every later index out of step with
            # the history.
            item.setText(f"Plot {len(self.history)}")
        self.rail.addItem(item)

    def _rail_row_changed(self, row: int) -> None:
        if row >= 0:
            self.show_index(row)
