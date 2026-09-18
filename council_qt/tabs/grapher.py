"""
council_qt.tabs.grapher — pick a file, pick columns, get a chart.

ONE EMBEDDED VIEW, NOT TWO
The Tk tab has an inner notebook: "Plots (offline)" and "Interactive (HTML)".
Its own comment says the HTML pane "can only ever show a static shell" — the
Tk HTML widget has no JavaScript engine, so a Plotly chart cannot run in it
even with the script inlined, and the HTML it renders pulls plotly.js from a
CDN that never resolves on the air-gapped machines this app targets.

So the second tab has never worked, and this does not reproduce it. The offline
pane is the embedded view, and the interactive path opens the system browser —
which is what the Tk build actually does whenever that widget is unavailable,
and the only Plotly route that has ever drawn a chart here.

EVERY RENDERER DRAWS THE SAME FRAME
`Session.working()` applies the transforms, the overlay and the date coercion
once. The Tk tab applies transforms inside its Plotly render method only, so
the offline pane and the export draw the untransformed data — a different chart
from the one the interactive view shows, with nothing to say so.
"""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from council_core import grapher, paths

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.plots_pane import PlotsPane

#: The transforms the Tk tab offers, with the parameters it supplies.
TRANSFORMS = (
    ("normalize", {}),
    ("standardize", {}),
    ("log", {}),
    ("fill_nan", {"value": 0}),
    ("clip_outliers", {"sigma": 3.0}),
)

AGGREGATIONS = ("sum", "mean", "count", "median", "min", "max")


class GrapherActions:
    """What the Grapher tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.session = grapher.Session()

    def files(self) -> List[Path]:
        return grapher.scan(self.vault_dir)

    def load(self, path: Path, sheet: Optional[str] = None):
        return self.session.load(path, sheet=sheet)

    def load_overlay(self, path: Path):
        return self.session.load_overlay(path)

    def working(self, spec: Any = None):
        return self.session.working(spec)

    def describe(self) -> str:
        return self.session.describe()

    def open_in_browser(self, path: Path) -> None:
        webbrowser.open(Path(path).as_uri())


class GrapherTab(ViewHelpers, QWidget):
    """A file list, a column list, a plot picker, and a plots pane."""

    def __init__(self, window=None, actions: Optional[GrapherActions] = None,
                 auto_refresh: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or GrapherActions()
        self._tokens = theme.tokens("dark")
        self._files: List[Path] = []
        self._choices: List[grapher.PlotChoice] = []
        self._busy = False

        self._build()
        if auto_refresh:
            self.refresh_files()

    # ==================================================================
    # Building
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        bar = QHBoxLayout()
        self._button(bar, amp("⟳ Files"), self.refresh_files)
        self.load_btn = self._button(bar, amp("Load"), self.on_load)
        self._button(bar, amp("Overlay a second file"), self.on_overlay)
        self._button(bar, amp("Clear overlay"), self.on_clear_overlay)
        bar.addStretch(1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bar.addWidget(self.status)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._left_panel())
        split.addWidget(self._right_panel())
        split.setSizes([340, 900])
        outer.addWidget(split, 1)

    def _left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        files_box = QGroupBox("Data files")
        files_layout = QVBoxLayout(files_box)
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(lambda _i: self.on_load())
        files_layout.addWidget(self.files)
        layout.addWidget(files_box, 1)

        cols_box = QGroupBox("Columns")
        cols_layout = QVBoxLayout(cols_box)
        self.columns = QListWidget()
        self.columns.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.columns.itemSelectionChanged.connect(self.refresh_choices)
        cols_layout.addWidget(self.columns)
        layout.addWidget(cols_box, 1)

        transform_box = QGroupBox("Transforms")
        transform_layout = QVBoxLayout(transform_box)
        row = QHBoxLayout()
        self.transform_box = QComboBox()
        self.transform_box.addItems([name for name, _ in TRANSFORMS])
        row.addWidget(self.transform_box, 1)
        self._button(row, amp("Add"), self.on_add_transform)
        self._button(row, amp("Clear"), self.on_clear_transforms)
        transform_layout.addLayout(row)
        self.transforms = QListWidget()
        self.transforms.setMaximumHeight(90)
        transform_layout.addWidget(self.transforms)
        layout.addWidget(transform_box)
        return panel

    def _right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Plot:"))
        self.kinds = QComboBox()
        self.kinds.setMinimumWidth(260)
        row.addWidget(self.kinds)
        row.addWidget(QLabel("Agg:"))
        self.agg = QComboBox()
        self.agg.addItems(list(AGGREGATIONS))
        row.addWidget(self.agg)
        self.plot_btn = self._button(row, amp("📈 Plot"), self.on_plot)
        self._button(row, amp("🌐 Interactive (browser)"), self.on_interactive)
        row.addStretch(1)
        layout.addLayout(row)

        self.hint = QLabel("Load a data file.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(self.hint)

        self.plots = PlotsPane()
        layout.addWidget(self.plots, 1)

        stats_box = QGroupBox("Summary")
        stats_layout = QVBoxLayout(stats_box)
        self.stats = QPlainTextEdit()
        self.stats.setReadOnly(True)
        self.stats.setMaximumHeight(120)
        stats_layout.addWidget(self.stats)
        layout.addWidget(stats_box)
        return panel

    # ==================================================================
    # Files
    # ==================================================================
    def refresh_files(self) -> None:
        self._files = self.actions.files()
        self.files.clear()
        root = self.actions.vault_dir
        for path in self._files:
            try:
                label = str(path.relative_to(root))
            except ValueError:
                label = str(path)
            item = QListWidgetItem(label)
            # The PATH travels with the row. The Tk overlay resolver builds its
            # labels data_in-relative and resolves them vault-relative, so the
            # loop falls through and the overlay silently never loads.
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.files.addItem(item)
        self.status.setText(f"{len(self._files)} data file(s)")

    def selected_file(self) -> Optional[Path]:
        item = self.files.currentItem()
        if item is None:
            return None
        stored = item.data(Qt.ItemDataRole.UserRole)
        return Path(stored) if stored else None

    def on_load(self) -> None:
        path = self.selected_file()
        if path is None:
            self.status.setText("Pick a file in the list first.")
            return
        ok, message = self.actions.load(path)
        self.status.setText(message)
        if not ok:
            # The loaded dataset is unchanged, so the column list still
            # describes the file that IS loaded rather than the one that failed.
            return
        self.refresh_columns()
        self.refresh_stats()

    def on_overlay(self) -> None:
        path = self.selected_file()
        if path is None:
            self.status.setText("Pick the overlay file in the list first.")
            return
        _ok, message = self.actions.load_overlay(path)
        self.status.setText(message)

    def on_clear_overlay(self) -> None:
        self.actions.session.clear_overlay()
        self.status.setText("Overlay cleared.")

    # ==================================================================
    # Columns and plots
    # ==================================================================
    def refresh_columns(self) -> None:
        working = self.actions.working()
        frame = working.df
        self.columns.clear()
        if frame is None:
            self.hint.setText("Load a data file.")
            return
        roles = grapher.roles_for(frame)
        for name in frame.columns:
            item = QListWidgetItem(f"{name}   [{roles.get(name, '?')[:4]}]")
            item.setData(Qt.ItemDataRole.UserRole, str(name))
            self.columns.addItem(item)
        self.refresh_choices()

    def selected_columns(self) -> List[str]:
        return [item.data(Qt.ItemDataRole.UserRole)
                for item in self.columns.selectedItems()]

    def refresh_choices(self) -> None:
        frame = self.actions.working().df
        columns = self.selected_columns()
        self._choices = grapher.choices_for(frame, columns)
        self.kinds.clear()
        for choice in self._choices:
            self.kinds.addItem(choice.caption, choice.key)
        self.hint.setText(grapher.hint_for(columns, self._choices))

    def selected_kind(self) -> Optional[str]:
        """The plot key, carried as item data rather than parsed from text."""
        index = self.kinds.currentIndex()
        if index < 0:
            return None
        return self.kinds.itemData(index)

    def on_plot(self) -> None:
        key = self.selected_kind()
        columns = self.selected_columns()
        result = grapher.build_figure(self.actions.working().df, key or "",
                                      columns, agg=self.agg.currentText())
        self.hint.setText(result.message)
        if result.ok:
            self.plots.add_figure(result.figure)

    def refresh_stats(self) -> None:
        """The summary, for the frame that is actually drawn.

        The Tk panel describes the RAW dataset beside a transformed chart.
        """
        def work() -> None:
            text = self.actions.describe()
            self._to_ui(lambda: self.stats.setPlainText(text))

        threading.Thread(target=work, name="grapher-stats",
                         daemon=True).start()

    # ==================================================================
    # Transforms
    # ==================================================================
    def on_add_transform(self) -> None:
        self.on_add_transform_named(self.transform_box.currentText())

    def on_add_transform_named(self, name: str) -> None:
        """Add one transform over the selected columns.

        Named rather than read off the combo, so the operation can be driven
        without a widget — which is what lets the transform-reaches-every-
        renderer behaviour be tested at all.
        """
        params = dict(next((p for n, p in TRANSFORMS if n == name), {}))
        columns = self.selected_columns()
        if not columns:
            self.hint.setText("Select the columns to transform first.")
            return
        self.actions.session.add_transform(name, columns, params)
        self._show_transforms()
        # Columns refresh too: a `derive` adds one, and a picker built before
        # it cannot offer it.
        self.refresh_columns()
        self.refresh_stats()

    def on_clear_transforms(self) -> None:
        self.actions.session.clear_transforms()
        self._show_transforms()
        self.refresh_columns()
        self.refresh_stats()

    def _show_transforms(self) -> None:
        self.transforms.clear()
        for line in self.actions.working().log:
            self.transforms.addItem(line)

    # ==================================================================
    # The browser path
    # ==================================================================
    def on_interactive(self) -> None:
        """Write an interactive chart and open it in the system browser.

        Not embedded. The Tk build's embedded HTML widget has no JavaScript
        engine — its own comment says it "can only ever show a static shell" —
        so the browser is the only route that has ever drawn one here.
        """
        key = self.selected_kind()
        columns = self.selected_columns()
        if self._busy:
            self.hint.setText("Already rendering — wait for it to finish.")
            return
        self._busy = True
        self.hint.setText("Rendering…")
        out_dir = self.actions.vault_dir / "data_out" / "charts"

        def work() -> None:
            result = grapher.render_html(self.actions.session, key or "",
                                         columns, out_dir)

            def show() -> None:
                self._busy = False
                self.hint.setText(result.message)
                if result.ok and result.path is not None:
                    self.actions.open_in_browser(result.path)

            self._to_ui(show)

        threading.Thread(target=work, name="grapher-html",
                         daemon=True).start()


def build_grapher(window) -> QWidget:
    """Factory for the tab registry."""
    return GrapherTab(window)
