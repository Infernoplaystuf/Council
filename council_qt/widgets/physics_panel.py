"""
council_qt.widgets.physics_panel — the Build tab's Model view and its
"New physics model…" window (council_core.physics_project).

THE MODEL VIEW (a "Model" page beside "Code" when the folder is a model)
    Inputs      a box per model input (a pick list, a number with its unit,
                or text), kept in physics.json between sessions
    Run model   Council reads the connected data, the model runs in the
                Build tab's sandbox with "Run with"'s Python, and the result
                comes back as: Results (values and notes), a page per table
                (the design sheet first), Charts
    Data        what the model is connected to: a folder, a saved Ledger or
                a Connections graph (with what to ask it). "Read the data"
                shows what Council found there.
    Calibrate   one of the model's fits, a connected table, its x and y
                columns: the fitted values (with their 95 % range) and a
                chart; "Use these values" puts them into the inputs and runs
                the model again.
    Export…     the result as a workbook in the model's results/ folder

NEW PHYSICS MODEL…
Start from one of the worked examples (PVC pipe organ, radio antenna, eddy
currents, water erosion) or from a goal. A goal makes the blank model and
asks the Physics modeller in Agent mode to plan it: the plan card shows what
it will build, and nothing is built until Go.

Everything that asks the user (a folder, a Ledger, a graph, a question) is
an injectable callable on PhysicsActions, so the tests never open a dialog.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from council_core import physics_project as pp

from ..view import ViewHelpers


def _no_dialogs() -> bool:
    return os.environ.get("COUNCIL_NO_DIALOGS") == "1"


class PhysicsActions:
    """What the Model view asks of the user. Tests pass their own."""

    def __init__(self, ask_folder: Optional[Callable[[str], str]] = None,
                 pick_ledger: Optional[Callable[[], str]] = None,
                 pick_graph: Optional[Callable[[Path], Optional[tuple]]] = None,
                 ask_new: Optional[Callable[..., Optional[dict]]] = None):
        self.ask_folder = ask_folder or self._ask_folder
        self.pick_ledger = pick_ledger or self._pick_ledger
        self.pick_graph = pick_graph or self._pick_graph
        self.ask_new = ask_new or self._ask_new

    @staticmethod
    def _ask_folder(start: str) -> str:
        if _no_dialogs():
            return ""
        from PySide6.QtWidgets import QFileDialog
        return QFileDialog.getExistingDirectory(None, "Connect a folder of data", start)

    @staticmethod
    def _pick_ledger() -> str:
        """A saved Ledger's CSV (from the Ledger tab's list), or one picked."""
        if _no_dialogs():
            return ""
        from PySide6.QtWidgets import QFileDialog, QInputDialog
        try:
            from council_core import ledger_saved
            saved = ledger_saved.listed()
        except Exception:                                 # noqa: BLE001
            saved = []
        if saved:
            names = [f"{s.title}  ({s.csv})" for s in saved] + ["Another Ledger CSV…"]
            pick, ok = QInputDialog.getItem(None, "Connect a Ledger", "Ledger:", names, 0, False)
            if not ok:
                return ""
            if pick != names[-1]:
                return saved[names.index(pick)].csv
        path, _ = QFileDialog.getOpenFileName(None, "A Ledger's CSV", "", "Ledger CSV (*.csv)")
        return path

    @staticmethod
    def _pick_graph(vault: Path) -> Optional[tuple]:
        """(graph id, its name, what to ask it about)."""
        if _no_dialogs():
            return None
        from PySide6.QtWidgets import QInputDialog
        try:
            from council_core import kg_graphs
            graphs = kg_graphs.GraphList(vault).all()
        except Exception:                                 # noqa: BLE001
            graphs = []
        names = [g.name for g in graphs] or ["Main graph"]
        pick, ok = QInputDialog.getItem(None, "Connect Connections knowledge", "Graph:", names, 0,
                                        False)
        if not ok:
            return None
        gid = graphs[names.index(pick)].id if graphs else ""
        q, ok = QInputDialog.getText(None, "Connect Connections knowledge",
                                     "What should the model ask it about? (for example: "
                                     "conductivity of our aluminium alloys)")
        if not ok:
            return None
        return gid, pick, q.strip()

    @staticmethod
    def _ask_new(vault: Path, parent=None) -> Optional[dict]:
        if _no_dialogs():
            return None
        dlg = NewModelDialog(vault, parent)
        return dlg.result_dict() if dlg.exec() == QDialog.DialogCode.Accepted else None


# ── the new-model window ────────────────────────────────────────────────
class NewModelDialog(QDialog):
    """Pick an example or write a goal; where it goes; data to connect."""

    def __init__(self, vault: Path, parent=None):
        super().__init__(parent)
        self.vault = Path(vault)
        self.setWindowTitle("New physics model")
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Start from</b>"))
        self.choices: Dict[str, QRadioButton] = {}
        for key, (title, line) in pp.EXAMPLES.items():
            rb = QRadioButton(f"{title}: {line}")
            rb.toggled.connect(self._changed)
            self.choices[key] = rb
            lay.addWidget(rb)
        self.goal_rb = QRadioButton("My own goal: the Physics modeller plans it in Agent "
                                    "mode, and you approve the plan")
        self.goal_rb.toggled.connect(self._changed)
        lay.addWidget(self.goal_rb)
        self.goal = QPlainTextEdit()
        self.goal.setPlaceholderText("For example: the notes a set of steel bars makes when "
                                     "struck, and what lengths make a pentatonic scale")
        self.goal.setMaximumHeight(90)
        lay.addWidget(self.goal)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.textEdited.connect(lambda _t: setattr(self, "_named", True))
        form.addRow("Name:", self.name)
        self.where = QLabel()
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Folder:", self.where)
        lay.addLayout(form)
        self.name.textChanged.connect(self._where)
        note = QLabel("Connect data (a folder, a Ledger, a Connections graph) from the Model "
                      "view's Data page once it's made.")
        note.setWordWrap(True)
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._named = False
        next(iter(self.choices.values())).setChecked(True)

    def _example(self) -> str:
        return next((k for k, rb in self.choices.items() if rb.isChecked()), "")

    def _changed(self, *_a) -> None:
        self.goal.setEnabled(self.goal_rb.isChecked())
        if not self._named:
            ex = self._example()
            self.name.setText(pp.EXAMPLES[ex][0] if ex else "My physics model")
        self._where()

    def _where(self, *_a) -> None:
        self.where.setText(str(self.dest()))

    def dest(self) -> Path:
        base = pp.default_parent(self.vault) / pp.slug(self.name.text())
        p, i = base, 2
        while p.exists() and any(p.iterdir()):
            p, i = base.with_name(f"{base.name}_{i}"), i + 1
        return p

    def result_dict(self) -> dict:
        return {"dest": str(self.dest()), "example": self._example(),
                "goal": self.goal.toPlainText().strip() if self.goal_rb.isChecked() else "",
                "title": self.name.text().strip()}


# ── helpers ─────────────────────────────────────────────────────────────
def _table(rows: List[Dict[str, Any]]) -> QTableWidget:
    t = QTableWidget()
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    cols: List[str] = []
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    t.setColumnCount(len(cols))
    t.setRowCount(len(rows))
    t.setHorizontalHeaderLabels(cols)
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            v = r.get(c, "")
            it = QTableWidgetItem()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                it.setData(Qt.ItemDataRole.DisplayRole, v)
            else:
                it.setText("" if v is None else str(v))
            t.setItem(i, j, it)
    t.resizeColumnsToContents()
    return t


def chart_figure(c: Dict[str, Any]):
    """A physics_kit Chart (as JSON) as a matplotlib Figure (no pyplot)."""
    from matplotlib.figure import Figure
    fig = Figure(figsize=(6.4, 3.8), dpi=100)
    polar = c.get("kind") == "polar"
    ax = fig.add_subplot(111, projection="polar" if polar else None)
    import math
    xs = c.get("x") or []
    for name, ys in (c.get("series") or {}).items():
        pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
        if not pts:
            continue
        px, py = zip(*pts)
        if polar:
            floor = max(py) - 30
            ax.plot([math.radians(x) for x in px], [max(y, floor) for y in py], label=name)
        elif c.get("kind") == "scatter":
            ax.plot(px, py, "o-", markersize=4, linewidth=1, label=name)
        else:
            ax.plot(px, py, linewidth=1.4, label=name)
    if not polar:
        if c.get("log_x"):
            ax.set_xscale("log")
        if c.get("log_y"):
            ax.set_yscale("log")
        ax.set_xlabel(c.get("x_label") or "", fontsize=8)
        ax.set_ylabel(c.get("y_label") or "", fontsize=8)
        ax.grid(True, alpha=0.25)
    if len(c.get("series") or {}) > 1:
        ax.legend(fontsize=7, frameon=False)
    ax.set_title(c.get("title") or "", fontsize=9)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    return fig


def _canvas(fig):
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    canvas = FigureCanvasQTAgg(fig)
    canvas.setMinimumHeight(300)
    return canvas


# ── the Model view ──────────────────────────────────────────────────────
class ModelPanel(ViewHelpers, QWidget):
    """One physics model folder: inputs, run, results, data, calibration."""

    def __init__(self, host=None, actions: Optional[PhysicsActions] = None,
                 python: Optional[Callable[[], Optional[str]]] = None,
                 vault_dir: Optional[Path] = None):
        super().__init__()
        self.host = host
        self.bridge = getattr(host, "bridge", None)
        self.actions = actions or PhysicsActions()
        self._python = python or (lambda: host._python() if host is not None else None)
        self.vault_dir = Path(vault_dir) if vault_dir else (
            host.actions.vault_dir if host is not None else Path.cwd())
        self.folder: Optional[Path] = None
        self.described: Dict[str, Any] = {}
        self.result: Dict[str, Any] = {}
        self.gathered: Optional[pp.Gathered] = None
        self.boxes: Dict[str, QWidget] = {}
        self.calibration: Optional[pp.Calibration] = None
        self._busy = False
        self._stop: Optional[threading.Event] = None
        self._figs: List[Any] = []
        self._build()

    # -- layout --------------------------------------------------------------
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.title = QLabel("")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        row = QHBoxLayout()
        self.run_btn = self._button(row, "Run model", self.on_run)
        self.reload_btn = self._button(row, "Reload model", self.reload)
        self.reload_btn.setToolTip("Read model.py's inputs again (after you or the agent "
                                   "changed it).")
        self.export_btn = self._button(row, "Export…", self.on_export)
        self.export_btn.setEnabled(False)
        row.addStretch(1)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        self.form = QFormLayout(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        split.addWidget(scroll)
        self.pages = QTabWidget()
        # Results
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        self.values = QTableWidget()
        rl.addWidget(self.values, 2)
        self.notes = QTextBrowser()
        rl.addWidget(self.notes, 1)
        self.pages.addTab(res, "Results")
        # Charts
        self.charts_box = QWidget()
        self.charts_lay = QVBoxLayout(self.charts_box)
        cs = QScrollArea()
        cs.setWidgetResizable(True)
        cs.setWidget(self.charts_box)
        self.pages.addTab(cs, "Charts")
        # Data
        data = QWidget()
        dl = QVBoxLayout(data)
        self.data_help = QLabel("")
        self.data_help.setWordWrap(True)
        dl.addWidget(self.data_help)
        self.links_list = QListWidget()
        dl.addWidget(self.links_list, 1)
        drow = QHBoxLayout()
        self._button(drow, "Add folder…", self.add_folder)
        self._button(drow, "Add Ledger…", self.add_ledger)
        self._button(drow, "Add Connections…", self.add_knowledge)
        self._button(drow, "Remove", self.remove_link)
        drow.addStretch(1)
        self._button(drow, "Read the data", self.read_data)
        dl.addLayout(drow)
        self.data_view = QTextBrowser()
        dl.addWidget(self.data_view, 1)
        self.pages.addTab(data, "Data")
        # Calibrate
        cal = QWidget()
        cl = QVBoxLayout(cal)
        cf = QFormLayout()
        self.fit_box = QComboBox()
        self.fit_box.currentIndexChanged.connect(self._fit_changed)
        cf.addRow("Fit:", self.fit_box)
        self.fit_help = QLabel("")
        self.fit_help.setWordWrap(True)
        cf.addRow("", self.fit_help)
        self.table_box = QComboBox()
        self.table_box.currentIndexChanged.connect(self._table_changed)
        cf.addRow("Measured data:", self.table_box)
        self.x_box, self.y_box = QComboBox(), QComboBox()
        self.x_box.currentIndexChanged.connect(self._x_changed)
        cf.addRow("x column:", self.x_box)
        self.x_unit = QComboBox()
        self.x_unit.addItems(list(pp.X_UNITS))
        cf.addRow("x is in:", self.x_unit)
        cf.addRow("y column:", self.y_box)
        cl.addLayout(cf)
        crow = QHBoxLayout()
        self.fit_btn = self._button(crow, "Fit", self.on_fit)
        self.use_btn = self._button(crow, "Use these values", self.use_fit)
        self.use_btn.setEnabled(False)
        crow.addStretch(1)
        cl.addLayout(crow)
        self.fit_text = QLabel("")
        self.fit_text.setWordWrap(True)
        self.fit_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.fit_text)
        self.fit_chart = QVBoxLayout()
        cl.addLayout(self.fit_chart, 1)
        self.pages.addTab(cal, "Calibrate")
        # About
        self.about = QTextBrowser()
        self.pages.addTab(self.about, "About")
        split.addWidget(self.pages)
        split.setSizes([360, 640])
        lay.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def say(self, text: str) -> None:
        self.status.setText(text)

    # -- the folder ------------------------------------------------------------
    def set_folder(self, folder: Optional[Path]) -> bool:
        """Show ``folder``'s model; False (and empty) when it isn't one."""
        if folder is None or not pp.is_model(folder):
            self.folder = None
            return False
        same = self.folder is not None and Path(folder) == self.folder
        self.folder = Path(folder)
        self._show_links()
        if not same:
            self.result = {}
            self.reload()
        return True

    def python(self) -> Optional[str]:
        return self._python()

    def reload(self) -> None:
        """Describe the model (its inputs and fits) in the sandbox."""
        if self.folder is None or self._busy:
            return
        python = self.python()
        if not python:
            self.say("Pick a Python under \"Run with\" first.")
            return
        folder = self.folder
        self._set_busy(True, "Reading the model…")

        def work() -> None:
            res = pp.describe(folder, python)
            self._to_ui(self._described, folder, res)

        threading.Thread(target=work, name="physics-describe", daemon=True).start()

    def _described(self, folder: Path, res: pp.RunResult) -> None:
        self._set_busy(False)
        if folder != self.folder:
            return
        if not res.ok:
            self.say(f"The model didn't load: {res.error}. \"Fix this\" can send it to the chat.")
            self._report(res)
            return
        self.described = res.payload
        d = res.payload
        self.title.setText(f"<b>{d.get('name', '')}</b>: {d.get('goal', '')}")
        self.data_help.setText(d.get("data_help") or
                               "Connect a folder, a Ledger or a Connections graph whose data "
                               "should inform the model.")
        about = ["<h3>Assumptions</h3><ul>"] + [f"<li>{a}</li>" for a in d.get("assumptions") or []]
        about += ["</ul><h3>Where it stops being trustworthy</h3><ul>"]
        about += [f"<li>{a}</li>" for a in d.get("limits") or []] + ["</ul>"]
        self.about.setHtml("".join(about))
        self._fill_form(d.get("inputs") or [])
        self._fill_fits()
        self.say(f"{len(d.get('inputs') or [])} input(s). Set them and Run model.")

    def _report(self, res: pp.RunResult) -> None:
        """A failure into the Build tab's output, for Fix this."""
        host = self.host
        if host is None:
            return
        for line in (res.output or res.error).splitlines()[-60:]:
            host.append(line)
        host._last = {"kind": "run", "name": "The physics model", "failed": True,
                      "output": res.output or res.error, "headline": res.error}
        host._run_lines = (res.output or res.error).splitlines()
        host.fix_btn.setEnabled(True)
        host.chat_panel.use_output.setChecked(True)

    # -- inputs ------------------------------------------------------------------
    def _saved_inputs(self) -> Dict[str, Any]:
        return dict(pp.load_manifest(self.folder).get("inputs") or {}) if self.folder else {}

    def _fill_form(self, inputs: List[Dict[str, Any]]) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.boxes = {}
        saved = self._saved_inputs()
        for spec in inputs:
            name = spec["name"]
            value = saved.get(name, spec.get("default"))
            unit = spec.get("unit") or ""
            if spec.get("choices"):
                box = QComboBox()
                box.addItems([str(c) for c in spec["choices"]])
                box.setCurrentText(str(value))
            elif isinstance(spec.get("default"), bool):
                box = QComboBox()
                box.addItems(["yes", "no"])
                box.setCurrentText("yes" if value else "no")
            elif isinstance(spec.get("default"), int):
                box = QSpinBox()
                box.setRange(int(spec.get("low") if spec.get("low") is not None else -10 ** 9),
                             int(spec.get("high") if spec.get("high") is not None else 10 ** 9))
                box.setValue(int(value))
                if unit:
                    box.setSuffix(f" {unit}")
            elif isinstance(spec.get("default"), float):
                box = QDoubleSpinBox()
                box.setDecimals(6 if abs(float(value or 0)) < 0.01 else 4)
                box.setRange(float(spec.get("low") if spec.get("low") is not None else -1e12),
                             float(spec.get("high") if spec.get("high") is not None else 1e12))
                box.setValue(float(value))
                if unit:
                    box.setSuffix(f" {unit}")
            else:
                box = QLineEdit(str(value))
            box.setToolTip(spec.get("help") or "")
            self.boxes[name] = box
            self.form.addRow(spec.get("label") or name, box)

    def inputs(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, box in self.boxes.items():
            if isinstance(box, QComboBox):
                t = box.currentText()
                out[name] = {"yes": True, "no": False}.get(t, t) if box.count() == 2 and \
                    {box.itemText(0), box.itemText(1)} == {"yes", "no"} else t
            elif isinstance(box, (QSpinBox, QDoubleSpinBox)):
                out[name] = box.value()
            else:
                out[name] = box.text()
        return out

    def set_inputs(self, values: Dict[str, Any]) -> None:
        for name, v in values.items():
            box = self.boxes.get(name)
            if isinstance(box, QDoubleSpinBox):
                # a fitted value keeps its digits (a 4-decimal box would round it)
                box.setDecimals(max(box.decimals(), 8 if abs(v) < 0.01 else 6))
                box.setValue(float(v))
            elif isinstance(box, QSpinBox):
                box.setValue(int(round(v)))
            elif isinstance(box, QComboBox):
                box.setCurrentText(str(v))
            elif isinstance(box, QLineEdit):
                box.setText(str(v))

    def _keep_inputs(self) -> None:
        if self.folder is None:
            return
        m = pp.load_manifest(self.folder)
        m["inputs"] = self.inputs()
        pp._write_json(self.folder / pp.MANIFEST, m)       # noqa: SLF001

    # -- running ---------------------------------------------------------------
    def _set_busy(self, on: bool, text: str = "") -> None:
        self._busy = on
        for b in (self.run_btn, self.reload_btn, self.fit_btn):
            b.setEnabled(not on)
        if text:
            self.say(text)

    def on_run(self) -> None:
        if self.folder is None or self._busy:
            return
        python = self.python()
        if not python:
            self.say("Pick a Python under \"Run with\" first.")
            return
        if self.host is not None and getattr(self.host, "unsaved", None) and self.host.unsaved():
            self.host.save_all()
        values = self.inputs()
        self._keep_inputs()
        folder, vault = self.folder, self.vault_dir
        self._set_busy(True, "Reading the connected data and running the model…")

        def work() -> None:
            g = pp.gather(folder, vault)
            res = pp.run(folder, python, values, g)
            self._to_ui(self._ran, folder, g, res)

        threading.Thread(target=work, name="physics-run", daemon=True).start()

    def _ran(self, folder: Path, gathered: pp.Gathered, res: pp.RunResult) -> None:
        self._set_busy(False)
        if folder != self.folder:
            return
        self.gathered = gathered
        self._fill_tables_box()
        if not res.ok:
            self.say(f"The model failed: {res.error}. \"Fix this\" sends it to the chat.")
            self._report(res)
            return
        self.result = res.payload
        self.show_result(res.payload)
        n = len(gathered.tables) + len(gathered.texts)
        self.say("Done." + (f" Informed by {n} connected source(s)." if n else ""))
        self.export_btn.setEnabled(True)

    def show_result(self, r: Dict[str, Any]) -> None:
        vals = [{"Result": k, "Value": v.get("value"), "Unit": v.get("unit", "")}
                for k, v in (r.get("values") or {}).items()]
        t = _table(vals)
        self.values.setColumnCount(t.columnCount())
        self.values.setRowCount(t.rowCount())
        self.values.setHorizontalHeaderLabels(["Result", "Value", "Unit"][:t.columnCount()])
        for i in range(t.rowCount()):
            for j in range(t.columnCount()):
                self.values.setItem(i, j, t.takeItem(i, j))
        self.values.resizeColumnsToContents()
        notes = r.get("notes") or []
        self.notes.setHtml("<ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")
        # a page per table, after Results and before Charts
        while self.pages.count() > 5:
            self.pages.removeTab(1)
        for i, (name, rows) in enumerate((r.get("tables") or {}).items()):
            self.pages.insertTab(1 + i, _table(rows), name)
        # charts
        while self.charts_lay.count():
            w = self.charts_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._figs = []
        for c in r.get("charts") or []:
            fig = chart_figure(c)
            self._figs.append(fig)
            self.charts_lay.addWidget(_canvas(fig))
        self.pages.setCurrentIndex(0)

    # -- data links -------------------------------------------------------------
    def _show_links(self) -> None:
        self.links_list.clear()
        if self.folder is None:
            return
        for link in pp.links(self.folder):
            it = QListWidgetItem(link.label())
            it.setToolTip(link.target)
            self.links_list.addItem(it)

    def _add(self, link: pp.Link) -> None:
        if self.folder is None:
            return
        items = pp.links(self.folder) + [link]
        pp.save_links(self.folder, items)
        self._show_links()
        self.say(f"Connected {link.label()}. Run model to use it.")

    def add_folder(self) -> None:
        path = self.actions.ask_folder(str(self.folder or ""))
        if path:
            self._add(pp.Link(pp.FOLDER, path, Path(path).name))

    def add_ledger(self) -> None:
        csv = self.actions.pick_ledger()
        if csv:
            self._add(pp.Link(pp.LEDGER, csv, Path(csv).stem))

    def add_knowledge(self) -> None:
        got = self.actions.pick_graph(self.vault_dir)
        if got:
            gid, name, question = got
            self._add(pp.Link(pp.KNOWLEDGE, gid, name, question))

    def remove_link(self) -> None:
        row = self.links_list.currentRow()
        if self.folder is None or row < 0:
            return
        items = pp.links(self.folder)
        if row < len(items):
            gone = items.pop(row)
            pp.save_links(self.folder, items)
            self._show_links()
            self.say(f"Disconnected {gone.label()}.")

    def read_data(self) -> None:
        if self.folder is None or self._busy:
            return
        folder, vault = self.folder, self.vault_dir
        self._set_busy(True, "Reading the connected data…")

        def work() -> None:
            g = pp.gather(folder, vault)
            self._to_ui(self._data_read, folder, g)

        threading.Thread(target=work, name="physics-data", daemon=True).start()

    def _data_read(self, folder: Path, g: pp.Gathered) -> None:
        self._set_busy(False)
        if folder != self.folder:
            return
        self.gathered = g
        self._fill_tables_box()
        parts = []
        for name, t in g.tables.items():
            cols = ", ".join(t["columns"][:12]) + (" …" if len(t["columns"]) > 12 else "")
            parts.append(f"<p><b>{name}</b>: {len(t['rows'])} row(s); columns {cols}</p>")
        for name, text in g.texts.items():
            parts.append(f"<p><b>{name}</b> (text): {text[:300]}…</p>")
        parts += [f"<p><i>{n}</i></p>" for n in g.notes]
        self.data_view.setHtml("".join(parts) or "<p>Nothing connected yet.</p>")
        self.say(f"{len(g.tables)} table(s), {len(g.texts)} text(s) found.")

    # -- calibrate --------------------------------------------------------------
    def _fill_fits(self) -> None:
        self.fit_box.blockSignals(True)
        self.fit_box.clear()
        for f in pp.fits_for(self.described, self.inputs()) if self.boxes else \
                (self.described.get("fits") or []):
            self.fit_box.addItem(f["name"], f)
        self.fit_box.blockSignals(False)
        self._fit_changed()

    def _fit(self) -> Optional[Dict[str, Any]]:
        return self.fit_box.currentData()

    def _fit_changed(self, *_a) -> None:
        f = self._fit()
        self.fit_help.setText(f"{f['equation']}. {f.get('help', '')}" if f else
                              "This model has no fits for these inputs.")
        if f:
            self.x_unit.setCurrentText(f.get("x_unit") or "m")
        self._table_changed()

    def _fill_tables_box(self) -> None:
        keep = self.table_box.currentText()
        self.table_box.blockSignals(True)
        self.table_box.clear()
        for name in (self.gathered.tables if self.gathered else {}):
            self.table_box.addItem(name)
        if keep:
            self.table_box.setCurrentText(keep)
        self.table_box.blockSignals(False)
        self._fill_fits()

    def _table_changed(self, *_a) -> None:
        t = (self.gathered.tables.get(self.table_box.currentText()) if self.gathered else None)
        cols = t["columns"] if t else []
        f = self._fit() or {}
        for box, hint in ((self.x_box, f.get("x_hint", "")), (self.y_box, f.get("y_hint", ""))):
            box.blockSignals(True)
            box.clear()
            box.addItems(cols)
            if cols:
                box.setCurrentText(pp.guess_column(cols, hint))
            box.blockSignals(False)
        self._x_changed()

    def _x_changed(self, *_a) -> None:
        t = (self.gathered.tables.get(self.table_box.currentText()) if self.gathered else None)
        unit = (t or {}).get("units", {}).get(self.x_box.currentText(), "")
        if unit in pp.X_UNITS:
            self.x_unit.setCurrentText(unit)

    def on_fit(self) -> None:
        f = self._fit()
        if not f:
            self.fit_text.setText("No fit to run: this model has none for these inputs.")
            return
        if not self.result:
            self.fit_text.setText("Run the model first: the fit uses values from its run "
                                  "(the speed of sound, the pipe's radius, …).")
            return
        t = self.gathered.tables.get(self.table_box.currentText()) if self.gathered else None
        if not t:
            self.fit_text.setText("Connect a table of measurements on the Data page, then Run model.")
            return
        from physics_kit import to_number
        xc, yc = self.x_box.currentText(), self.y_box.currentText()
        pts = [(to_number(r.get(xc)), to_number(r.get(yc))) for r in t["rows"]]
        pts = [(x, y) for x, y in pts if x is not None and y is not None]
        now = self.inputs()
        # the model's current values are the fit's starting point (0 means "from the table")
        guesses = {p: float(now[i]) for p, i in (f.get("sets") or {}).items()
                   if isinstance(now.get(i), (int, float)) and now.get(i)}
        try:
            cal = pp.calibrate(f, [p[0] for p in pts], [p[1] for p in pts],
                               self.result.get("fit_constants") or {}, self.x_unit.currentText(),
                               guesses)
        except Exception as exc:                          # noqa: BLE001 - said to the user
            self.calibration = None
            self.use_btn.setEnabled(False)
            self.fit_text.setText(str(exc))
            return
        self.calibration = cal
        self.fit_text.setText(cal.text + "  Use these values sets " +
                              ", ".join(f"{k} = {v:.5g}" for k, v in cal.sets.items()) + ".")
        self.use_btn.setEnabled(True)
        from council_core import physics_fit as pf
        while self.fit_chart.count():
            w = self.fit_chart.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.fit_chart.addWidget(_canvas(pf.fit_figure(cal.fit)))

    def use_fit(self) -> None:
        if self.calibration is None:
            return
        self.set_inputs(self.calibration.sets)
        self.use_btn.setEnabled(False)
        self.on_run()

    # -- export ------------------------------------------------------------------
    def on_export(self) -> Optional[Path]:
        if self.folder is None or not self.result:
            return None
        import pandas as pd
        out_dir = self.folder / "results"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"results_{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
        with pd.ExcelWriter(out) as xw:
            pd.DataFrame([{"Input": k, "Value": v} for k, v in self.inputs().items()]).to_excel(
                xw, sheet_name="Inputs", index=False)
            pd.DataFrame([{"Result": k, "Value": v.get("value"), "Unit": v.get("unit")}
                          for k, v in (self.result.get("values") or {}).items()]).to_excel(
                xw, sheet_name="Results", index=False)
            for name, rows in (self.result.get("tables") or {}).items():
                safe = "".join(ch for ch in name if ch not in "[]:*?/\\")[:31] or "Table"
                pd.DataFrame(rows).to_excel(xw, sheet_name=safe, index=False)
            pd.DataFrame({"Notes": self.result.get("notes") or []}).to_excel(
                xw, sheet_name="Notes", index=False)
        self.say(f"Written: {out}")
        return out
