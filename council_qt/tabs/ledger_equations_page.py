"""
The Analyze window's Equations page (council_core.ledger_equations): pick a
result and its inputs, fit or compare equation kinds tested on unseen
parts, predict from the equation (an input outside the data is said so),
and graph it (measured vs predicted, a response curve, a heatmap or contour
over two inputs); save it beside the Ledger and export the graph and its
values. From Andy's LMM Analysis Studio script (2026-11-13).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
                               QVBoxLayout, QWidget)

from council_core import ledger_analytics as la
from council_core import ledger_equations as le
from sample_ledger import LedgerError

MEASURED, CURVE, HEATMAP, CONTOUR = ("Measured vs predicted", "Response curve",
                                     "Heatmap (two inputs)", "Contour (two inputs)")
GRAPHS = [MEASURED, CURVE, HEATMAP, CONTOUR]


def _spin(value: float = 0.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setDecimals(6)
    s.setRange(-1e12, 1e12)
    s.setValue(value)
    return s


class EquationsPage(QWidget):
    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        #: the Analyze window: its data, group and per, and _swap / fill_table
        self.dlg = dialog
        self.eq: Optional[le.Equation] = None
        self.canvas = None
        self.graph_values = None          # the last graph's values (a DataFrame)
        self.figure = None
        self.inputs_at: Dict[str, QDoubleSpinBox] = {}
        self._suggested: List[str] = []
        self._build()
        self.reload()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)
        left = QWidget()
        ll = QVBoxLayout(left)
        form = QFormLayout()
        self.target = QComboBox()
        form.addRow("Result:", self.target)
        self.kind = QComboBox()
        self.kind.addItems(le.KINDS)
        for i, k in enumerate(le.KINDS):
            self.kind.setItemData(i, le.KIND_HELP[k], Qt.ToolTipRole)
        form.addRow("Kind:", self.kind)
        ll.addLayout(form)
        ll.addWidget(QLabel("Inputs (tick them):"))
        self.inputs = QListWidget()
        ll.addWidget(self.inputs, 1)
        row = QHBoxLayout()
        self.suggest_n = QSpinBox()
        self.suggest_n.setRange(1, 10)
        self.suggest_n.setValue(3)
        self.suggest_btn = QPushButton("Suggest")
        self.suggest_btn.setToolTip(
            "Tick the fields most correlated with the result. Fitting inputs picked this "
            "way flatters the score a little: the description says so.")
        row.addWidget(QLabel("Suggest"))
        row.addWidget(self.suggest_n)
        row.addWidget(self.suggest_btn)
        row.addStretch(1)
        ll.addLayout(row)
        row = QHBoxLayout()
        self.fit_btn = QPushButton("Fit")
        self.compare_btn = QPushButton("Compare kinds")
        self.compare_btn.setToolTip("Fit every kind to the same rows; best on unseen parts first.")
        row.addWidget(self.fit_btn)
        row.addWidget(self.compare_btn)
        ll.addLayout(row)
        row = QHBoxLayout()
        self.save_btn = QPushButton("Save equation")
        self.saved_choice = QComboBox()
        self.saved_choice.setToolTip("Equations saved for this Ledger")
        self.load_btn = QPushButton("Load")
        row.addWidget(self.save_btn)
        row.addWidget(self.saved_choice, 1)
        row.addWidget(self.load_btn)
        ll.addLayout(row)
        ll.addWidget(QLabel("Predict (inputs start at their medians):"))
        self.predict_form = QFormLayout()
        pw = QWidget()
        pw.setLayout(self.predict_form)
        ll.addWidget(pw)
        row = QHBoxLayout()
        self.predict_btn = QPushButton("Predict")
        self.predict_out = QLabel()
        self.predict_out.setWordWrap(True)
        row.addWidget(self.predict_btn)
        row.addWidget(self.predict_out, 1)
        ll.addLayout(row)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumHeight(120)
        rl.addWidget(self.result)
        self.compare_table = QTableWidget()
        self.compare_table.setMaximumHeight(130)
        self.compare_table.setVisible(False)
        rl.addWidget(self.compare_table)
        row = QHBoxLayout()
        row.addWidget(QLabel("Graph:"))
        self.graph = QComboBox()
        self.graph.addItems(GRAPHS)
        row.addWidget(self.graph)
        row.addWidget(QLabel("X:"))
        self.gx = QComboBox()
        row.addWidget(self.gx, 1)
        self.x_lo, self.x_hi = _spin(), _spin()
        row.addWidget(self.x_lo)
        row.addWidget(QLabel("to"))
        row.addWidget(self.x_hi)
        rl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Y:"))
        self.gy = QComboBox()
        row.addWidget(self.gy, 1)
        self.y_lo, self.y_hi = _spin(), _spin()
        row.addWidget(self.y_lo)
        row.addWidget(QLabel("to"))
        row.addWidget(self.y_hi)
        self.measured = QCheckBox("Show measured")
        self.measured.setChecked(True)
        row.addWidget(self.measured)
        self.draw_btn = QPushButton("Draw")
        row.addWidget(self.draw_btn)
        rl.addLayout(row)
        self.graph_box = QVBoxLayout()
        rl.addLayout(self.graph_box, 1)
        row = QHBoxLayout()
        self.export_fig_btn = QPushButton("Export graph…")
        self.export_csv_btn = QPushButton("Export values (CSV)…")
        row.addStretch(1)
        row.addWidget(self.export_fig_btn)
        row.addWidget(self.export_csv_btn)
        rl.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        rl.addWidget(self.status)
        split.addWidget(right)
        split.setSizes([360, 760])

        self.suggest_btn.clicked.connect(self.on_suggest)
        self.fit_btn.clicked.connect(self.on_fit)
        self.compare_btn.clicked.connect(self.on_compare)
        self.save_btn.clicked.connect(self.on_save)
        self.load_btn.clicked.connect(self.on_load)
        self.predict_btn.clicked.connect(self.on_predict)
        self.draw_btn.clicked.connect(self.draw)
        self.graph.currentTextChanged.connect(lambda _t: self._graph_controls())
        self.gx.currentTextChanged.connect(lambda _t: self._limits(self.gx, self.x_lo,
                                                                   self.x_hi))
        self.gy.currentTextChanged.connect(lambda _t: self._limits(self.gy, self.y_lo,
                                                                   self.y_hi))
        self.export_fig_btn.clicked.connect(self.on_export_figure)
        self.export_csv_btn.clicked.connect(self.on_export_values)
        self.target.currentTextChanged.connect(lambda _t: self._fill_inputs())
        self._enable()

    # ── what is offered ─────────────────────────────────────────────────
    @property
    def data(self) -> la.Data:
        return self.dlg.data

    def reload(self) -> None:
        """The fields of the window's data (again, after it is read again)."""
        fields = le.number_fields(self.data, self.dlg.group)
        keep = self.target.currentText()
        self.target.blockSignals(True)
        self.target.clear()
        self.target.addItems(fields)
        if keep in fields:
            self.target.setCurrentText(keep)
        self.target.blockSignals(False)
        self._fill_inputs()
        self._fill_saved()

    def _fill_inputs(self, ticked: Optional[List[str]] = None) -> None:
        was = set(ticked if ticked is not None else self.ticked())
        target = self.target.currentText()
        self.inputs.clear()
        for f in le.number_fields(self.data, self.dlg.group):
            if f == target:
                continue
            it = QListWidgetItem(f)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if f in was else Qt.Unchecked)
            self.inputs.addItem(it)

    def ticked(self) -> List[str]:
        return [self.inputs.item(i).text() for i in range(self.inputs.count())
                if self.inputs.item(i).checkState() == Qt.Checked]

    def _fill_saved(self) -> None:
        self.saved_choice.clear()
        for i, e in enumerate(le.saved(self.data.csv)):
            self.saved_choice.addItem(f"{i + 1}. {e.kind}: {la.label(e.target, '', 40)} "
                                      f"(R² {e.scores.get('cv_r2', float('nan')):.3f})")
        self.load_btn.setEnabled(self.saved_choice.count() > 0)

    def _enable(self) -> None:
        has = self.eq is not None
        for w in (self.save_btn, self.predict_btn, self.draw_btn, self.export_fig_btn,
                  self.export_csv_btn):
            w.setEnabled(has)

    # ── actions ─────────────────────────────────────────────────────────
    def on_suggest(self) -> None:
        target = self.target.currentText()
        if not target:
            return
        pick = le.suggest_inputs(self.data, target, self.suggest_n.value(), self.dlg.group,
                                 self.dlg.per)
        self._suggested = pick
        self._fill_inputs(pick)
        self.status.setText("Suggested: " + (", ".join(pick) if pick else
                                             "no field has enough values beside it."))

    def on_fit(self) -> Optional[le.Equation]:
        try:
            inputs = self.ticked()
            eq = le.fit(self.data, self.target.currentText(), inputs,
                        self.kind.currentText(), self.dlg.group, self.dlg.per,
                        suggested=bool(self._suggested) and set(inputs) == set(self._suggested))
        except LedgerError as e:
            self.status.setText(str(e))
            return None
        self.compare_table.setVisible(False)
        self._show(eq)
        return eq

    def on_compare(self):
        from .ledger_analytics_dialog import fill_table
        inputs = self.ticked()
        target = self.target.currentText()
        if not target or not inputs:
            self.status.setText("Pick a result and one or more inputs.")
            return None
        res = le.compare(self.data, target, inputs, self.dlg.group, self.dlg.per,
                         suggested=bool(self._suggested) and set(inputs) == set(self._suggested))
        fill_table(self.compare_table, res)
        self.compare_table.setVisible(True)
        best = res.iloc[0]
        if best["Why not"]:
            self.status.setText(f"No kind could be fitted: {best['Why not']}")
            return res
        self.kind.setCurrentText(best["Kind"])
        self.on_fit()
        self.compare_table.setVisible(True)
        self.status.setText(f"Best on unseen parts: {best['Kind']} (shown). Picking the best "
                            "of several flatters its score a little.")
        return res

    def _show(self, eq: le.Equation) -> None:
        self.eq = eq
        self.result.setPlainText(le.describe(eq))
        # the predict inputs, at their medians
        while self.predict_form.rowCount():
            self.predict_form.removeRow(0)
        self.inputs_at = {}
        for f in eq.inputs:
            s = _spin(eq.ranges[f]["median"])
            r = eq.ranges[f]
            s.setToolTip(f"measured {r['min']:g} to {r['max']:g}")
            self.inputs_at[f] = s
            self.predict_form.addRow(la.label(f, eq.units.get(f, ""), 34) + ":", s)
        self.predict_out.setText("")
        for box in (self.gx, self.gy):
            box.blockSignals(True)
            box.clear()
            box.addItems(eq.inputs)
            box.blockSignals(False)
        if len(eq.inputs) > 1:
            self.gy.setCurrentIndex(1)
        self._limits(self.gx, self.x_lo, self.x_hi)
        self._limits(self.gy, self.y_lo, self.y_hi)
        self._enable()
        self.graph.setCurrentText(MEASURED if eq.actual is not None else CURVE)
        self._graph_controls()
        self.draw()

    def _limits(self, box, lo, hi) -> None:
        if self.eq is None or box.currentText() not in self.eq.ranges:
            return
        r = self.eq.ranges[box.currentText()]
        lo.setValue(r["min"])
        hi.setValue(r["max"])

    def _graph_controls(self) -> None:
        g = self.graph.currentText()
        two = g in (HEATMAP, CONTOUR)
        for w in (self.gx, self.x_lo, self.x_hi):
            w.setEnabled(g != MEASURED)
        for w in (self.gy, self.y_lo, self.y_hi):
            w.setEnabled(two)
        self.measured.setEnabled(g != MEASURED)

    def held(self) -> Dict[str, float]:
        return {f: s.value() for f, s in self.inputs_at.items()}

    def draw(self) -> None:
        if self.eq is None:
            return
        eq, g = self.eq, self.graph.currentText()
        try:
            if g == MEASURED:
                fig = le.actual_figure(eq)
                vals = None
                if eq.actual is not None:
                    import pandas as pd
                    vals = pd.DataFrame({f"Measured {eq.target}": eq.actual,
                                         "Predicted for an unseen part": eq.predicted})
            elif g == CURVE:
                fig, vals = le.response_figure(eq, self.data, self.gx.currentText(),
                                               self.x_lo.value(), self.x_hi.value(),
                                               self.held(), self.measured.isChecked())
            else:
                if len(eq.inputs) < 2:
                    raise LedgerError("a heatmap or contour needs an equation of two or "
                                      "more inputs")
                fig, vals = le.surface_figure(
                    eq, self.data, self.gx.currentText(), self.gy.currentText(),
                    (self.x_lo.value(), self.x_hi.value()),
                    (self.y_lo.value(), self.y_hi.value()), self.held(),
                    contour=g == CONTOUR, measured=self.measured.isChecked())
        except LedgerError as e:
            self.status.setText(str(e))
            return
        self.figure, self.graph_values = fig, vals
        self.canvas = self.dlg._swap(self.graph_box, self.canvas, fig)
        held = [f for f in eq.inputs if f not in (self.gx.currentText(), self.gy.currentText())]
        self.status.setText(
            "" if g == MEASURED or not held else
            "Held at the Predict values: " + ", ".join(
                f"{la.label(f, '', 30)} = {self.inputs_at[f].value():g}" for f in held))

    def on_predict(self):
        if self.eq is None:
            return None
        try:
            y, out = le.predict(self.eq, self.held())
        except LedgerError as e:
            self.predict_out.setText(str(e))
            return None
        unit = self.eq.units.get(self.eq.target, "")
        text = (f"{la.label(self.eq.target, '', 40)} ≈ {y:.5g}{' ' + unit if unit else ''} "
                f"(9 in 10 within ±{self.eq.scores['within_p90']:.3g})")
        if out:
            text += ". Outside the data, the equation is guessing: " + "; ".join(out)
        self.predict_out.setText(text)
        return y, out

    def on_save(self) -> None:
        if self.eq is None:
            return
        p = le.save(self.eq, self.data.csv)
        self._fill_saved()
        self.saved_choice.setCurrentIndex(self.saved_choice.count() - 1)
        self.status.setText(f"Saved in {p.name}")

    def on_load(self) -> None:
        i = self.saved_choice.currentIndex()
        eqs = le.saved(self.data.csv)
        if not 0 <= i < len(eqs):
            return
        eq = eqs[i]
        self.target.setCurrentText(eq.target)
        self.kind.setCurrentText(eq.kind)
        self._fill_inputs(eq.asked)
        self._show(eq)
        self.status.setText("Loaded. Fit again to see measured against predicted.")

    def _ask_path(self, title: str, name: str, kinds: str) -> str:
        import os
        if os.environ.get("COUNCIL_NO_DIALOGS"):
            return str(Path(self.data.csv).with_name(name))
        return QFileDialog.getSaveFileName(self, title,
                                           str(Path(self.data.csv).with_name(name)), kinds)[0]

    def on_export_figure(self) -> Optional[str]:
        if self.figure is None:
            return None
        path = self._ask_path("Export the graph", f"{Path(self.data.csv).stem} equation.png",
                              "PNG (*.png);;SVG (*.svg);;PDF (*.pdf)")
        if not path:
            return None
        self.figure.savefig(path, dpi=200, bbox_inches="tight")
        self.status.setText(f"Graph written: {path}")
        return path

    def on_export_values(self) -> Optional[str]:
        if self.graph_values is None:
            return None
        path = self._ask_path("Export the graph's values",
                              f"{Path(self.data.csv).stem} equation values.csv", "CSV (*.csv)")
        if not path:
            return None
        self.graph_values.to_csv(path, index=False, encoding="utf-8-sig")
        self.status.setText(f"Values written: {path}")
        return path
