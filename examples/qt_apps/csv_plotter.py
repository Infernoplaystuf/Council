"""
csv_plotter.py — plot one CSV, or every CSV in a folder, in the basic chart
types.

WHAT IT DOES
Open a file or a folder (or drop either on the window). Each CSV is a row in
the file list; tick the ones to plot. Pick the X column (or the row number),
tick one or more Y columns, pick a chart type, and the plot redraws as soon
as anything changes. With several files ticked, every series is labelled
"file: column", so the same column from ten runs overlays on one chart.

Chart types: line, scatter, bar, horizontal bar, area, step, histogram, box,
violin, and a correlation heatmap of the ticked columns.

SHOWS
  * matplotlib inside Qt: FigureCanvasQTAgg + NavigationToolbar2QT (zoom,
    pan, save) in a QMainWindow's central widget
  * QDockWidget holding the controls, so the user can float or move them
  * QListWidget with checkable items (ItemIsUserCheckable) for files and
    columns, reacting to itemChanged
  * QComboBox, QSpinBox, QSlider, QCheckBox, QLineEdit wired to one replot
  * a QTimer used as a debounce, so ticking five columns draws once
  * QFileDialog for a file, a folder and Save-as PNG/SVG
  * drag and drop of files and folders onto the window (dragEnterEvent /
    dropEvent with mime URLs)
  * pandas.read_csv with errors reported in the status bar instead of a crash
  * QMessageBox for the one error the user must read

RUN
    python examples/qt_apps/csv_plotter.py [file.csv | folder]

REQUIRES  pandas, numpy, matplotlib
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from matplotlib.backends.backend_qtagg import (FigureCanvasQTAgg,
                                               NavigationToolbar2QT)
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDockWidget, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

CHARTS = ["Line", "Scatter", "Bar", "Horizontal bar", "Area", "Step",
          "Histogram", "Box", "Violin", "Correlation heatmap"]
#: Charts that draw a distribution of each Y column, so X is not used.
NO_X = {"Histogram", "Box", "Violin", "Correlation heatmap"}
ROW_NUMBER = "(row number)"
MAX_BAR_ROWS = 60


def read_csv(path: Path) -> pd.DataFrame:
    """The file as a frame. Separator sniffed, so ; and tab files work."""
    return pd.read_csv(path, sep=None, engine="python")


def numeric_columns(df: pd.DataFrame) -> List[str]:
    return [str(c) for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


class PlotWindow(QMainWindow):
    def __init__(self, start: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("CSV plotter")
        self.resize(1100, 700)
        self.setAcceptDrops(True)
        self.frames: Dict[str, pd.DataFrame] = {}   # path -> data

        # ---- the plot, in the middle ----
        self.figure = Figure(figsize=(6, 4), layout="tight")
        self.canvas = FigureCanvasQTAgg(self.figure)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(NavigationToolbar2QT(self.canvas, self))
        lay.addWidget(self.canvas, 1)
        self.setCentralWidget(central)

        # ---- the controls, in a dock ----
        dock = QDockWidget("Data", self)
        dock.setObjectName("data_dock")
        dock.setWidget(self._controls())
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # One replot for a burst of changes.
        self.debounce = QTimer(self, singleShot=True, interval=150)
        self.debounce.timeout.connect(self.replot)

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Open file…", self.open_file, "Ctrl+O")
        file_menu.addAction("Open folder…", self.open_folder, "Ctrl+Shift+O")
        file_menu.addAction("Save chart…", self.save_chart, "Ctrl+S")
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close, "Ctrl+Q")
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(dock.toggleViewAction())

        self.statusBar().showMessage("Open a CSV file or a folder of them.")
        if start:
            self.load_paths([start])

    # ---------------------------------------------------------- building
    def _controls(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        buttons = QHBoxLayout()
        for text, slot in (("Open file…", self.open_file),
                           ("Open folder…", self.open_folder)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        v.addLayout(buttons)

        v.addWidget(QLabel("Files (tick to plot)"))
        self.files = QListWidget()
        self.files.itemChanged.connect(self._files_changed)
        v.addWidget(self.files, 1)

        form = QFormLayout()
        self.chart = QComboBox()
        self.chart.addItems(CHARTS)
        self.chart.currentTextChanged.connect(self._chart_changed)
        form.addRow("Chart", self.chart)
        self.x_col = QComboBox()
        self.x_col.currentTextChanged.connect(self.schedule)
        form.addRow("X", self.x_col)
        v.addLayout(form)

        v.addWidget(QLabel("Y columns"))
        self.y_cols = QListWidget()
        self.y_cols.itemChanged.connect(self.schedule)
        v.addWidget(self.y_cols, 1)

        opts = QGroupBox("Options")
        of = QFormLayout(opts)
        self.title = QLineEdit(placeholderText="Chart title")
        self.title.textChanged.connect(self.schedule)
        of.addRow("Title", self.title)
        self.bins = QSpinBox(minimum=2, maximum=500, value=30)
        self.bins.valueChanged.connect(self.schedule)
        of.addRow("Histogram bins", self.bins)
        self.alpha = QSlider(Qt.Horizontal, minimum=10, maximum=100, value=80)
        self.alpha.valueChanged.connect(self.schedule)
        of.addRow("Opacity", self.alpha)
        self.grid = QCheckBox("Grid", checked=True)
        self.legend = QCheckBox("Legend", checked=True)
        self.log_y = QCheckBox("Log Y axis")
        for box in (self.grid, self.legend, self.log_y):
            box.toggled.connect(self.schedule)
            of.addRow(box)
        v.addWidget(opts)
        return w

    # ---------------------------------------------------------- loading
    def open_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open CSV", "", "CSV files (*.csv *.tsv *.txt);;All (*)")
        if name:
            self.load_paths([Path(name)])

    def open_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Folder of CSV files")
        if name:
            self.load_paths([Path(name)])

    def load_paths(self, paths: List[Path]) -> None:
        """Every CSV named, or found directly inside a named folder."""
        found: List[Path] = []
        for p in paths:
            if p.is_dir():
                found += sorted(q for q in p.iterdir()
                                if q.suffix.lower() in (".csv", ".tsv"))
            elif p.is_file():
                found.append(p)
        failed = []
        self.files.blockSignals(True)
        for p in found:
            key = str(p)
            if key in self.frames:
                continue
            try:
                self.frames[key] = read_csv(p)
            except Exception as exc:                     # noqa: BLE001
                failed.append(f"{p.name}: {exc}")
                continue
            item = QListWidgetItem(p.name)
            item.setData(Qt.UserRole, key)
            item.setToolTip(f"{key}\n{len(self.frames[key])} rows")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # The first file is ticked so something draws straight away.
            item.setCheckState(Qt.Checked if self.files.count() == 0
                               else Qt.Unchecked)
            self.files.addItem(item)
        self.files.blockSignals(False)
        if failed:
            QMessageBox.warning(self, "Some files could not be read",
                                "\n".join(failed[:10]))
        self._files_changed()

    def ticked_files(self) -> List[str]:
        return [self.files.item(i).data(Qt.UserRole)
                for i in range(self.files.count())
                if self.files.item(i).checkState() == Qt.Checked]

    def _files_changed(self, *_: object) -> None:
        """Columns offered are those of the ticked files, in first-seen
        order; ticks and the X choice survive when the column still exists."""
        keep_x = self.x_col.currentText()
        keep_y = set(self.ticked_columns())
        cols: List[str] = []
        all_cols: List[str] = []
        for key in self.ticked_files():
            df = self.frames[key]
            cols += [c for c in numeric_columns(df) if c not in cols]
            all_cols += [str(c) for c in df.columns if str(c) not in all_cols]

        self.x_col.blockSignals(True)
        self.x_col.clear()
        self.x_col.addItems([ROW_NUMBER] + all_cols)
        if keep_x in all_cols:
            self.x_col.setCurrentText(keep_x)
        self.x_col.blockSignals(False)

        self.y_cols.blockSignals(True)
        self.y_cols.clear()
        for i, c in enumerate(cols):
            item = QListWidgetItem(c)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            on = c in keep_y if keep_y else i == 0
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.y_cols.addItem(item)
        self.y_cols.blockSignals(False)
        rows = sum(len(self.frames[k]) for k in self.ticked_files())
        self.statusBar().showMessage(
            f"{len(self.frames)} file(s) loaded, {len(self.ticked_files())} "
            f"ticked, {rows} rows")
        self.schedule()

    def ticked_columns(self) -> List[str]:
        return [self.y_cols.item(i).text() for i in range(self.y_cols.count())
                if self.y_cols.item(i).checkState() == Qt.Checked]

    def _chart_changed(self, chart: str) -> None:
        self.x_col.setEnabled(chart not in NO_X)
        self.bins.setEnabled(chart == "Histogram")
        self.schedule()

    # ---------------------------------------------------------- drawing
    def schedule(self, *_: object) -> None:
        self.debounce.start()

    def series(self):
        """(label, x, y) for every ticked column of every ticked file."""
        many = len(self.ticked_files()) > 1
        xname = self.x_col.currentText()
        for key in self.ticked_files():
            df = self.frames[key]
            for col in self.ticked_columns():
                if col not in df.columns:
                    continue
                y = pd.to_numeric(df[col], errors="coerce")
                x = (df[xname] if xname in df.columns and xname != ROW_NUMBER
                     else pd.Series(np.arange(len(df))))
                label = f"{Path(key).stem}: {col}" if many else col
                yield label, x, y

    def replot(self) -> None:
        chart = self.chart.currentText()
        alpha = self.alpha.value() / 100
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        data = list(self.series())
        if not data:
            ax.text(0.5, 0.5, "Tick a file and a Y column", ha="center",
                    va="center", transform=ax.transAxes, color="grey")
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        labels = [d[0] for d in data]
        try:
            if chart == "Line":
                for label, x, y in data:
                    ax.plot(x, y, label=label, alpha=alpha)
            elif chart == "Scatter":
                for label, x, y in data:
                    ax.scatter(x, y, label=label, alpha=alpha, s=12)
            elif chart == "Area":
                for label, x, y in data:
                    ax.fill_between(np.asarray(x), np.asarray(y.fillna(0)),
                                    label=label, alpha=alpha * 0.6)
            elif chart == "Step":
                for label, x, y in data:
                    ax.step(x, y, where="mid", label=label, alpha=alpha)
            elif chart in ("Bar", "Horizontal bar"):
                self._bars(ax, data, alpha, horizontal=chart != "Bar")
            elif chart == "Histogram":
                ax.hist([y.dropna() for _, _, y in data], bins=self.bins.value(),
                        label=labels, alpha=alpha, histtype="stepfilled"
                        if len(data) == 1 else "bar")
            elif chart == "Box":
                ax.boxplot([y.dropna() for _, _, y in data],
                           tick_labels=labels, patch_artist=True)
            elif chart == "Violin":
                vals = [y.dropna() for _, _, y in data]
                ax.violinplot(vals, showmedians=True)
                ax.set_xticks(range(1, len(vals) + 1), labels)
            elif chart == "Correlation heatmap":
                self._heatmap(ax, data)
        except Exception as exc:                         # noqa: BLE001
            self.statusBar().showMessage(f"Could not draw {chart}: {exc}")
        if chart not in NO_X:
            ax.set_xlabel(self.x_col.currentText())
        if chart != "Correlation heatmap":
            ax.grid(self.grid.isChecked(), alpha=0.3)
            if self.log_y.isChecked():
                ax.set_yscale("log")
            if self.legend.isChecked() and chart not in ("Box", "Violin"):
                ax.legend(fontsize="small")
        if chart in ("Box", "Violin") and len(labels) > 3:
            ax.tick_params(axis="x", labelrotation=30)
        ax.set_title(self.title.text())
        self.canvas.draw_idle()

    def _bars(self, ax, data, alpha: float, horizontal: bool) -> None:
        """Grouped bars; at most MAX_BAR_ROWS rows, or a bar chart of a long
        file is a black block."""
        n = len(data)
        width = 0.8 / n
        for i, (label, x, y) in enumerate(data):
            x, y = x[:MAX_BAR_ROWS], y[:MAX_BAR_ROWS]
            pos = np.arange(len(y)) + i * width
            draw = ax.barh if horizontal else ax.bar
            draw(pos, y, width, label=label, alpha=alpha)
        ticks = np.arange(len(data[0][1][:MAX_BAR_ROWS])) + width * (n - 1) / 2
        names = [str(v) for v in data[0][1][:MAX_BAR_ROWS]]
        if horizontal:
            ax.set_yticks(ticks, names)
        else:
            ax.set_xticks(ticks, names, rotation=45, ha="right")

    def _heatmap(self, ax, data) -> None:
        frame = pd.DataFrame({label: y.reset_index(drop=True)
                              for label, _, y in data})
        corr = frame.corr().to_numpy()
        im = ax.imshow(corr, vmin=-1, vmax=1, cmap="coolwarm")
        ax.set_xticks(range(len(frame.columns)), frame.columns, rotation=45,
                      ha="right")
        ax.set_yticks(range(len(frame.columns)), frame.columns)
        for (r, c), v in np.ndenumerate(corr):
            ax.text(c, r, f"{v:.2f}", ha="center", va="center", fontsize=8)
        self.figure.colorbar(im, ax=ax)

    def save_chart(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "Save chart", "chart.png", "PNG (*.png);;SVG (*.svg)")
        if name:
            self.figure.savefig(name, dpi=150)
            self.statusBar().showMessage(f"Saved {name}")

    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        self.load_paths([Path(u.toLocalFile())
                         for u in event.mimeData().urls()])


def make_window(argv: Optional[List[str]] = None) -> PlotWindow:
    args = argv if argv is not None else sys.argv[1:]
    return PlotWindow(Path(args[0]) if args else None)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
