"""
The LPBF / Dyndrite tab.

Pages:
  Plan         pick the machine limits profile and a model; describe the
               study in plain English and let the model PROPOSE a spec, or
               edit the spec (JSON) by hand; Check computes the specimens
               and runs the deterministic checks; Save draft; an engineer
               types their name and approves or rejects; an approved plan
               exports its manifest CSV. The model never approves, exports
               or runs anything.
  Experiments  every saved plan with its status, and its audit log.
  Results      import measured results (CSV / Excel) for an approved plan,
               joined on specimen ID: what matched, a summary, correlation
               of each varied input with each response, and a scatter plot.
  Limits       the machine / material profiles (JSON) the checks use.
  Dyndrite     the MCP server: where it is and where its TOKEN is kept (an
               environment variable or a Windows Credential Manager entry —
               Council reads it and sends it; it is never saved here nor
               shown to a model); Check connection lists the tools with a
               rule each (read / change / blocked); Ask uses read tools
               only; a change tool is a request an engineer approves before
               it runs once.

Work that asks a model, reads a file or talks to the server runs on a
worker thread; results return to the GUI thread through a queued signal.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .engine import doe
from .engine import dyndrite as dy
from .engine import limits as lm
from .engine import plan as pl
from .engine import spec as sp
from .engine.store import Store

SETTINGS_FILE = "settings.json"
ROLE = Qt.ItemDataRole.UserRole


def esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


class _Ui(QObject):
    call = Signal(object)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        fn()


def fill_table(table: QTableWidget, rows: List[Dict[str, Any]],
               columns: Optional[List[str]] = None) -> None:
    columns = columns or (list(rows[0]) if rows else [])
    table.clear()
    table.setColumnCount(len(columns))
    table.setRowCount(len(rows))
    table.setHorizontalHeaderLabels([str(c) for c in columns])
    for r, row in enumerate(rows):
        for c, col in enumerate(columns):
            v = row.get(col, "")
            if isinstance(v, float):
                v = f"{v:.6g}"
            table.setItem(r, c, QTableWidgetItem(str(v)))
    table.resizeColumnsToContents()


class LpbfTab(QWidget):
    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.ui = _Ui(self)
        self.store = Store(ctx.data_folder())
        self.profiles = lm.load_profiles(ctx)
        self.settings = ctx.load_json(SETTINGS_FILE, {}) or {}
        self.busy = False
        self.tools: List[Dict[str, Any]] = []

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Model:"))
        self.model = QComboBox()
        self.model.setMinimumWidth(220)
        top.addWidget(self.model)
        top.addStretch(1)
        self.status = QLabel("")
        top.addWidget(self.status)
        layout.addLayout(top)
        self.pages = QTabWidget()
        layout.addWidget(self.pages, 1)
        self.plan_page = self._build_plan()
        self.pages.addTab(self.plan_page, "Plan")
        self.pages.addTab(self._build_experiments(), "Experiments")
        self.pages.addTab(self._build_results(), "Results")
        self.pages.addTab(self._build_limits(), "Limits")
        self.pages.addTab(self._build_dyndrite(), "Dyndrite")
        self.model.currentIndexChanged.connect(self._remember_model)
        self._load_models()
        self.refresh_experiments()

    # ------------------------------------------------------------ helpers
    def run(self, label: str, work: Callable[[], Any],
            done: Callable[[Any], None], *, exclusive: bool = True) -> None:
        """`work` on a worker thread, then `done(result)` on this one. Only
        one exclusive step at a time (a second click is refused); listing
        the models is not exclusive."""
        if exclusive and self.busy:
            self.status.setText("Busy, wait for the current step.")
            return
        if exclusive:
            self.busy = True
            self.status.setText(label)

        def worker():
            try:
                result, error = work(), None
            except Exception as exc:                          # noqa: BLE001
                result, error = None, exc

            def back():
                if exclusive:
                    self.busy = False
                    self.status.setText("")
                if error is not None:
                    self.status.setText(f"Failed: {error}")
                    self.ctx.log(f"{label} failed: {error!r}")
                    return
                done(result)
            self.ui.call.emit(back)
        threading.Thread(target=worker, daemon=True).start()

    def _load_models(self) -> None:
        def work():
            return self.ctx.service("models").available()

        def done(models):
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItem("(choose a model)", "")
            for label, ident in models or []:
                self.model.addItem(label, ident)
            want = self.settings.get("model", "")
            i = self.model.findData(want)
            self.model.setCurrentIndex(i if i >= 0 else 0)
            self.model.blockSignals(False)
        self.run("Listing models…", work, done, exclusive=False)

    def _remember_model(self) -> None:
        self.settings["model"] = self.model.currentData() or ""
        self.ctx.save_json(SETTINGS_FILE, self.settings)

    def chat(self):
        model = self.model.currentData() or ""
        return self.ctx.service("models").chat(model)

    def profile(self) -> Optional[Dict[str, Any]]:
        return lm.by_name(self.profiles, self.profile_box.currentText())

    # --------------------------------------------------------------- Plan
    def _build_plan(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Limits profile:"))
        self.profile_box = QComboBox()
        self._fill_profiles()
        row.addWidget(self.profile_box, 1)
        lay.addLayout(row)
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText(
            "Describe the study, e.g. \"25 specimens varying laser power "
            "150-350 W and scan speed 600-1400 mm/s, hatch 100 µm, layer "
            "30 µm\"")
        self.request.setMaximumHeight(70)
        lay.addWidget(self.request)
        btns = QHBoxLayout()
        self.draft_btn = QPushButton("Draft spec with the model")
        self.draft_btn.clicked.connect(self.draft_with_model)
        self.new_btn = QPushButton("Blank spec")
        self.new_btn.clicked.connect(self.blank_spec)
        self.check_btn = QPushButton("Check")
        self.check_btn.clicked.connect(self.check_plan)
        self.save_btn = QPushButton("Save draft")
        self.save_btn.clicked.connect(self.save_draft)
        for b in (self.draft_btn, self.new_btn, self.check_btn, self.save_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.spec_edit = QPlainTextEdit()
        self.spec_edit.setPlaceholderText("The specification (JSON)")
        split.addWidget(self.spec_edit)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.report = QTextBrowser()
        self.report.setMaximumHeight(160)
        rl.addWidget(self.report)
        self.runs_table = QTableWidget()
        rl.addWidget(self.runs_table, 1)
        split.addWidget(right)
        split.setSizes([380, 620])
        lay.addWidget(split, 1)
        box = QGroupBox("Engineer's decision")
        bl = QHBoxLayout(box)
        self.engineer = QLineEdit()
        self.engineer.setPlaceholderText("Engineer's name")
        self.note = QLineEdit()
        self.note.setPlaceholderText("Note (optional)")
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.clicked.connect(lambda: self.decide(True))
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.clicked.connect(lambda: self.decide(False))
        self.export_btn = QPushButton("Export manifest…")
        self.export_btn.clicked.connect(self.export_manifest)
        for w in (self.engineer, self.note, self.approve_btn, self.reject_btn,
                  self.export_btn):
            bl.addWidget(w)
        lay.addWidget(box)
        self.current_id = ""
        self.current_hash = ""
        self.current_runs: List[Dict[str, Any]] = []
        self.current_issues: List[pl.Issue] = []
        self._decision_buttons()
        return page

    def _fill_profiles(self) -> None:
        current = self.profile_box.currentText() if hasattr(self, "profile_box") else ""
        self.profile_box.clear()
        for p in self.profiles:
            self.profile_box.addItem(p["name"])
        i = self.profile_box.findText(current or self.settings.get("profile", ""))
        if i >= 0:
            self.profile_box.setCurrentIndex(i)

    def _decision_buttons(self) -> None:
        exp = self.store.get(self.current_id) if self.current_id else None
        status = exp["status"] if exp else ""
        can_decide = (status == "draft" and exp["hash"] == self.current_hash)
        self.approve_btn.setEnabled(can_decide and not pl.blocked(self.current_issues))
        self.reject_btn.setEnabled(can_decide)
        self.export_btn.setEnabled(status == "approved")

    def blank_spec(self) -> None:
        s = sp.ExperimentSpec(profile=self.profile_box.currentText(), variables=[
            sp.Variable("laser_power", 150, 350, 3),
            sp.Variable("scan_speed", 600, 1400, 3)],
            fixed=[sp.Fixed("hatch_spacing", 0.1), sp.Fixed("layer_thickness", 0.03)])
        self._show_spec(s, [])

    def _show_spec(self, spec: sp.ExperimentSpec, notes: List[str]) -> None:
        self.current_id = ""
        self.current_hash = ""
        self.current_runs = []
        self.current_issues = []
        self.spec_edit.setPlainText(spec.to_json())
        self.report.setHtml("<br>".join(esc(n) for n in notes)
                            or "Press Check to compute the specimens and run "
                               "the checks.")
        self.runs_table.clear()
        self.runs_table.setRowCount(0)
        self._decision_buttons()

    def draft_with_model(self) -> None:
        text = self.request.toPlainText().strip()
        profile = self.profile()
        if not text:
            self.status.setText("Describe the study first.")
            return
        if not self.model.currentData():
            self.status.setText("Choose a model first.")
            return
        chat = self.chat()

        def work():
            return sp.from_request(text, profile, chat)

        def done(result):
            spec, notes = result
            self._show_spec(spec, ["The model proposed this spec. Nothing is "
                                   "checked yet."] + notes)
        self.run("The model is drafting the spec…", work, done)

    def _spec_from_editor(self) -> Optional[sp.ExperimentSpec]:
        try:
            spec = sp.ExperimentSpec.from_dict(json.loads(self.spec_edit.toPlainText()))
        except (ValueError, TypeError, KeyError) as exc:
            self.report.setHtml(f"<b>The spec cannot be read:</b> {esc(exc)}")
            return None
        spec.profile = self.profile_box.currentText()
        return spec

    def check_plan(self) -> None:
        spec = self._spec_from_editor()
        if spec is None:
            return
        profile = self.profile()
        exp_id = self.current_id
        if not exp_id or (self.store.get(exp_id) or {}).get("status", "draft") != "draft":
            exp_id = self.store.next_id()
        try:
            runs = pl.runs(spec, exp_id) if spec.variables else []
        except doe.DesignError:
            runs = []
        issues = pl.check(spec, profile, runs)
        self.current_id = exp_id
        self.current_runs = runs
        self.current_issues = issues
        self.current_hash = ""
        self._render_report(spec, runs, issues)
        fill_table(self.runs_table, runs, pl.manifest_columns(runs)[1:] if runs else None)
        self._decision_buttons()

    def _render_report(self, spec, runs, issues) -> None:
        head = (f"<b>{esc(self.current_id)}</b>: {len(runs)} specimens, "
                f"{esc(doe.DESIGNS.get(spec.design, spec.design))}.")
        if not issues:
            body = "<br><span style='color:#2e7d32'>All checks passed.</span>"
        else:
            body = "<br>" + "<br>".join(
                f"<span style='color:{'#c62828' if i.level == pl.BLOCK else '#ef6c00'}'>"
                f"{esc(i.line())}</span>" for i in issues)
        tail = ("<br><b>Blocked: fix the spec or the limits, then check "
                "again.</b>" if pl.blocked(issues) else
                "<br>Save the draft, then an engineer can approve it.")
        self.report.setHtml(head + body + tail)

    def save_draft(self) -> None:
        if not self.current_id or not self.current_runs:
            self.status.setText("Check the plan first.")
            return
        spec = self._spec_from_editor()
        if spec is None:
            return
        # the editor may have changed since Check: check again so what is
        # saved is exactly what was checked.
        self.check_plan()
        if not self.current_runs:
            return
        try:
            self.current_hash = self.store.save_draft(
                self.current_id, spec.to_dict(), self.current_runs,
                self.profile(), [i.to_dict() for i in self.current_issues])
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText(f"Saved draft {self.current_id}.")
        self._decision_buttons()
        self.refresh_experiments()

    @staticmethod
    def _recheck(spec_d, profile, runs):
        return pl.check(sp.ExperimentSpec.from_dict(spec_d), profile, runs)

    def decide(self, approve: bool) -> None:
        who = self.engineer.text().strip()
        if approve:
            why = self.store.approve(self.current_id, self.current_hash, who=who,
                                     note=self.note.text(),
                                     current_profile=self.profile(),
                                     check=self._recheck)
        else:
            why = self.store.reject(self.current_id, self.current_hash, who=who,
                                    note=self.note.text())
        self.status.setText(why or f"{self.current_id} "
                            f"{'approved' if approve else 'rejected'}.")
        self._decision_buttons()
        self.refresh_experiments()

    def manifest_text(self, exp_id: str) -> str:
        exp = self.store.get(exp_id)
        if not exp or exp["status"] != "approved":
            raise ValueError("Only an approved plan has a manifest.")
        approval = [a for a in self.store.audit(exp_id) if a["action"] == "approved"][-1]
        return pl.manifest_csv(exp_id, exp["runs"], {
            "experiment": exp_id, "title": exp["title"],
            "profile": exp["profile"], "plan hash": exp["hash"],
            "approved by": approval["who"], "approved at": approval["at"]})

    def export_manifest(self) -> None:
        exp_id = self.current_id
        try:
            text = self.manifest_text(exp_id)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export manifest",
                                              f"{exp_id}_manifest.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        pl.write_text_atomic(Path(path), text)
        self.store.log("exported manifest", experiment_id=exp_id,
                       detail={"file": path})
        self.status.setText(f"Manifest written to {path}.")

    # -------------------------------------------------------- Experiments
    def _build_experiments(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        self.exp_list = QListWidget()
        self.exp_list.currentItemChanged.connect(self._show_experiment)
        lay.addWidget(self.exp_list, 1)
        right = QVBoxLayout()
        self.exp_view = QTextBrowser()
        right.addWidget(self.exp_view, 1)
        open_btn = QPushButton("Open in Plan")
        open_btn.clicked.connect(self.open_selected)
        right.addWidget(open_btn)
        lay.addLayout(right, 2)
        return page

    def refresh_experiments(self) -> None:
        current = self.exp_list.currentItem().data(ROLE) \
            if self.exp_list.currentItem() else ""
        self.exp_list.clear()
        for e in self.store.list():
            item = QListWidgetItem(f"{e['id']}  [{e['status']}]  {e['title']}")
            item.setData(ROLE, e["id"])
            self.exp_list.addItem(item)
            if e["id"] == current:
                self.exp_list.setCurrentItem(item)
        if hasattr(self, "res_exp"):
            self._fill_result_experiments()

    def _show_experiment(self, item) -> None:
        if item is None:
            self.exp_view.clear()
            return
        exp_id = item.data(ROLE)
        exp = self.store.get(exp_id)
        lines = [f"<b>{esc(exp_id)}</b> {esc(exp['title'])}: {esc(exp['status'])}, "
                 f"{len(exp['runs'])} specimens, profile {esc(exp['profile'])}, "
                 f"hash {esc(exp['hash'])}", "<br><b>Audit log</b>"]
        for a in self.store.audit(exp_id):
            lines.append(f"{esc(a['at'])} &nbsp; {esc(a['who'])} &nbsp; "
                         f"<b>{esc(a['action'])}</b> {esc(a['detail'] or '')[:300]}")
        self.exp_view.setHtml("<br>".join(lines))

    def open_selected(self) -> None:
        item = self.exp_list.currentItem()
        if not item:
            return
        exp = self.store.get(item.data(ROLE))
        spec = sp.ExperimentSpec.from_dict(exp["spec"])
        i = self.profile_box.findText(exp["profile"])
        if i >= 0:
            self.profile_box.setCurrentIndex(i)
        self.spec_edit.setPlainText(spec.to_json())
        self.current_id = exp["id"]
        self.current_runs = exp["runs"]
        self.current_hash = exp["hash"]
        self.current_issues = pl.check(spec, exp["profile_json"], exp["runs"])
        self._render_report(spec, exp["runs"], self.current_issues)
        fill_table(self.runs_table, exp["runs"], pl.manifest_columns(exp["runs"])[1:])
        if exp["status"] != "draft":
            self.report.append(f"This plan is {exp['status']}; a change made "
                               f"here is checked as a new draft.")
        self._decision_buttons()
        self.pages.setCurrentWidget(self.plan_page)

    # ------------------------------------------------------------ Results
    def _build_results(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Approved experiment:"))
        self.res_exp = QComboBox()
        row.addWidget(self.res_exp, 1)
        imp = QPushButton("Import results…")
        imp.clicked.connect(self.import_results)
        row.addWidget(imp)
        lay.addLayout(row)
        self.res_report = QTextBrowser()
        self.res_report.setMaximumHeight(110)
        lay.addWidget(self.res_report)
        split = QSplitter(Qt.Orientation.Horizontal)
        tables = QTabWidget()
        self.res_summary = QTableWidget()
        self.res_corr = QTableWidget()
        self.res_table = QTableWidget()
        tables.addTab(self.res_summary, "Summary")
        tables.addTab(self.res_corr, "Correlation (r)")
        tables.addTab(self.res_table, "Joined data")
        split.addWidget(tables)
        plot = QWidget()
        pl_lay = QVBoxLayout(plot)
        pick = QHBoxLayout()
        self.plot_x = QComboBox()
        self.plot_y = QComboBox()
        pick.addWidget(QLabel("x"))
        pick.addWidget(self.plot_x, 1)
        pick.addWidget(QLabel("y"))
        pick.addWidget(self.plot_y, 1)
        pl_lay.addLayout(pick)
        self.canvas = None
        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
            self.figure = Figure(figsize=(4, 3))
            self.canvas = FigureCanvasQTAgg(self.figure)
            pl_lay.addWidget(self.canvas, 1)
        except Exception:                                    # noqa: BLE001
            pl_lay.addWidget(QLabel("Plots need matplotlib."))
        self.plot_x.currentIndexChanged.connect(self.draw_plot)
        self.plot_y.currentIndexChanged.connect(self.draw_plot)
        split.addWidget(plot)
        lay.addWidget(split, 1)
        self.joined = None
        self._fill_result_experiments()
        return page

    def _fill_result_experiments(self) -> None:
        current = self.res_exp.currentData()
        self.res_exp.clear()
        for e in self.store.list():
            if e["status"] == "approved":
                self.res_exp.addItem(f"{e['id']}  {e['title']}", e["id"])
        i = self.res_exp.findData(current)
        if i >= 0:
            self.res_exp.setCurrentIndex(i)

    def import_results(self, path: Optional[str] = None) -> None:
        exp_id = self.res_exp.currentData()
        if not exp_id:
            self.status.setText("Approve a plan first; results join to its "
                                "specimen IDs.")
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import results", "", "Tables (*.csv *.xlsx *.xlsm *.xls)")
        if not path:
            return
        exp = self.store.get(exp_id)

        def work():
            joined = pl.join_results(exp["runs"], pl.read_table(Path(path)))
            self.store.add_results(exp_id, joined["table"][joined["table"]
                                   ["specimen_id"].isin([r["specimen_id"]
                                                         for r in exp["runs"]])],
                                   joined["responses"], str(path))
            return joined
        self.run("Reading results…", work, lambda j: self._show_results(exp, j))

    def _show_results(self, exp, joined) -> None:
        self.joined = joined
        table = joined["table"]
        inputs = [v["key"] for v in exp["spec"]["variables"]]
        derived = [c for c in ("LED_J_per_mm", "VED_J_per_mm3") if c in table.columns]
        responses = joined["responses"]
        msg = [f"{joined['matched']} of {len(exp['runs'])} specimens matched "
               f"(ID column {joined['id_column']!r})."]
        if joined["unmatched_ids"]:
            msg.append(f"Not in this plan: {', '.join(joined['unmatched_ids'][:10])}"
                       + (" …" if len(joined["unmatched_ids"]) > 10 else ""))
        if joined["missing_specimens"]:
            msg.append(f"No results yet for {len(joined['missing_specimens'])} "
                       f"specimen(s).")
        fixed = [f["key"] for f in exp["spec"]["fixed"]]
        if fixed:
            msg.append("Held constant (no effect can be learned from this "
                       "data): " + ", ".join(fixed) + ".")
        self.res_report.setHtml("<br>".join(esc(m) for m in msg))
        summ = pl.summary(table, inputs + derived + responses)
        fill_table(self.res_summary, summ.to_dict("records"))
        corr = pl.correlations(table, inputs + derived, responses)
        rows = [{"input": i, **{r: corr.loc[i, r] for r in corr.columns}}
                for i in corr.index]
        fill_table(self.res_corr, rows, ["input"] + list(corr.columns))
        fill_table(self.res_table, table.to_dict("records"))
        for box, cols in ((self.plot_x, inputs + derived), (self.plot_y, responses)):
            box.blockSignals(True)
            box.clear()
            box.addItems(cols)
            box.blockSignals(False)
        self.draw_plot()

    def draw_plot(self) -> None:
        if self.canvas is None or self.joined is None:
            return
        x, y = self.plot_x.currentText(), self.plot_y.currentText()
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        if x and y:
            import pandas as pd
            t = self.joined["table"]
            ax.scatter(pd.to_numeric(t[x], errors="coerce"),
                       pd.to_numeric(t[y], errors="coerce"), s=18)
            ax.set_xlabel(x)
            ax.set_ylabel(y)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    # ------------------------------------------------------------- Limits
    def _build_limits(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(QLabel(
            "Machine / material profiles the checks use. Base units: W, mm/s, "
            "mm, deg; VED window in J/mm³. The example's numbers are made up: "
            "replace them with your machine's approved limits."))
        self.limits_edit = QPlainTextEdit()
        self.limits_edit.setPlainText(json.dumps({"profiles": self.profiles},
                                                 indent=2))
        lay.addWidget(self.limits_edit, 1)
        row = QHBoxLayout()
        save = QPushButton("Save profiles")
        save.clicked.connect(self.save_limits)
        row.addWidget(save)
        self.limits_msg = QLabel("")
        row.addWidget(self.limits_msg, 1)
        lay.addLayout(row)
        return page

    def save_limits(self) -> None:
        try:
            data = json.loads(self.limits_edit.toPlainText())
            profiles = data["profiles"] if isinstance(data, dict) else data
            lm.save_profiles(self.ctx, list(profiles))
        except (ValueError, KeyError, TypeError) as exc:
            self.limits_msg.setText(f"Not saved: {exc}")
            return
        self.profiles = lm.load_profiles(self.ctx)
        self._fill_profiles()
        self.store.log("saved limits", detail={"profiles": [p["name"] for p in self.profiles]})
        self.limits_msg.setText("Saved. Plans checked before this must be "
                                "checked again.")

    # ----------------------------------------------------------- Dyndrite
    def _build_dyndrite(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        s = dy.load_settings(self.ctx)
        self.rules = dy.load_rules(self.ctx)
        form = QFormLayout()
        self.dy_transport = QComboBox()
        self.dy_transport.addItem("HTTP address (Streamable HTTP)", "http")
        self.dy_transport.addItem("Command Council starts (stdio)", "stdio")
        self.dy_transport.setCurrentIndex(0 if s["transport"] == "http" else 1)
        self.dy_url = QLineEdit(s["url"])
        self.dy_command = QLineEdit(" ".join([s["command"]] + list(s["args"])).strip())
        self.dy_remote = QCheckBox("Allow a server on another computer (what "
                                   "you ask and its answers go over the network)")
        self.dy_remote.setChecked(bool(s["allow_remote"]))
        self.dy_token = QLineEdit(s["token_source"])
        self.dy_token.setPlaceholderText("env:DYNDRITE_TOKEN  or  credential:council/dyndrite")
        self.dy_header = QLineEdit(s["token_header"])
        self.dy_scheme = QLineEdit(s["token_scheme"])
        self.dy_token_env = QLineEdit(s["token_env"])
        form.addRow("Server", self.dy_transport)
        form.addRow("Address", self.dy_url)
        form.addRow("Command", self.dy_command)
        form.addRow("", self.dy_remote)
        form.addRow("Token kept in", self.dy_token)
        form.addRow("Token header / scheme", self._pair(self.dy_header, self.dy_scheme))
        form.addRow("Token variable (stdio)", self.dy_token_env)
        lay.addLayout(form)
        lay.addWidget(QLabel(
            "Council reads the token and sends it; it is never saved in "
            "Council's files or shown to the model. Keep it in a Windows "
            "environment variable, or in the Credential Manager: "
            "cmdkey /generic:council/dyndrite /user:token /pass:&lt;token&gt;"))
        row = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self.save_dyndrite)
        check = QPushButton("Check connection")
        check.clicked.connect(self.check_dyndrite)
        row.addWidget(save)
        row.addWidget(check)
        self.dy_msg = QLabel("")
        self.dy_msg.setWordWrap(True)
        row.addWidget(self.dy_msg, 1)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tool_table = QTableWidget(0, 3)
        self.tool_table.setHorizontalHeaderLabels(["Tool", "Rule", "Description"])
        split.addWidget(self.tool_table)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.question = QLineEdit()
        self.question.setPlaceholderText("Ask Dyndrite (read tools only), e.g. "
                                         "\"How do I set hatch spacing in a recipe?\"")
        ask = QPushButton("Ask")
        ask.clicked.connect(self.ask_dyndrite)
        qrow = QHBoxLayout()
        qrow.addWidget(self.question, 1)
        qrow.addWidget(ask)
        rl.addLayout(qrow)
        self.answer = QTextBrowser()
        rl.addWidget(self.answer, 1)
        req = QGroupBox("Requests for change tools (an engineer approves; runs once)")
        rq = QVBoxLayout(req)
        r1 = QHBoxLayout()
        self.req_tool = QComboBox()
        self.req_args = QLineEdit("{}")
        self.req_reason = QLineEdit()
        self.req_reason.setPlaceholderText("Why")
        file_req = QPushButton("Request")
        file_req.clicked.connect(self.request_tool)
        for w in (self.req_tool, self.req_args, self.req_reason, file_req):
            r1.addWidget(w)
        rq.addLayout(r1)
        self.req_list = QListWidget()
        rq.addWidget(self.req_list)
        r2 = QHBoxLayout()
        self.req_who = QLineEdit()
        self.req_who.setPlaceholderText("Engineer's name")
        ok = QPushButton("Approve")
        ok.clicked.connect(lambda: self.decide_request(True))
        no = QPushButton("Reject")
        no.clicked.connect(lambda: self.decide_request(False))
        go = QPushButton("Run approved")
        go.clicked.connect(self.run_request)
        for w in (self.req_who, ok, no, go):
            r2.addWidget(w)
        rq.addLayout(r2)
        rl.addWidget(req)
        split.addWidget(right)
        lay.addWidget(split, 1)
        self.refresh_requests()
        return page

    @staticmethod
    def _pair(a: QWidget, b: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(a)
        h.addWidget(b)
        return w

    def server_settings(self) -> Dict[str, Any]:
        from council_core.docs_servers import parse_command_line
        s = dict(dy.DEFAULT_SETTINGS)
        s["transport"] = self.dy_transport.currentData()
        s["url"] = self.dy_url.text().strip()
        try:
            cmd, args = parse_command_line(self.dy_command.text()) \
                if self.dy_command.text().strip() else ("", [])
        except ValueError:
            cmd, args = self.dy_command.text().strip(), []
        s["command"], s["args"] = cmd, list(args)
        s["allow_remote"] = self.dy_remote.isChecked()
        s["token_source"] = self.dy_token.text().strip()
        s["token_header"] = self.dy_header.text().strip() or "Authorization"
        s["token_scheme"] = self.dy_scheme.text().strip()
        s["token_env"] = self.dy_token_env.text().strip()
        return s

    def save_dyndrite(self) -> bool:
        s = self.server_settings()
        problem = self.ctx.service("mcp").problem(s)
        if problem:
            self.dy_msg.setText(problem)
            return False
        dy.save(self.ctx, s, self.rules)
        self.dy_msg.setText("Saved.")
        return True

    def check_dyndrite(self) -> None:
        if not self.save_dyndrite():
            return
        s = self.server_settings()
        mcp = self.ctx.service("mcp")
        self.run("Connecting to Dyndrite…", lambda: mcp.check(s), self._show_tools)

    def _show_tools(self, report: Dict[str, Any]) -> None:
        self.dy_msg.setText(report.get("message", ""))
        self.tools = report.get("tools") or []
        self.tool_table.setRowCount(len(self.tools))
        for r, t in enumerate(self.tools):
            name = str(t.get("name", ""))
            self.tool_table.setItem(r, 0, QTableWidgetItem(name))
            box = QComboBox()
            box.addItems(list(dy.RULES))
            box.setCurrentText(dy.rule_for(t, self.rules))
            box.currentTextChanged.connect(
                lambda rule, n=name: self._set_rule(n, rule))
            self.tool_table.setCellWidget(r, 1, box)
            desc = str(t.get("description") or "").strip().split("\n")[0]
            self.tool_table.setItem(r, 2, QTableWidgetItem(desc[:200]))
        self.tool_table.resizeColumnsToContents()
        self._fill_change_tools()

    def _set_rule(self, name: str, rule: str) -> None:
        self.rules[name] = rule
        dy.save(self.ctx, self.server_settings(), self.rules)
        self.store.log("tool rule set", detail={"tool": name, "rule": rule})
        self._fill_change_tools()

    def _fill_change_tools(self) -> None:
        self.req_tool.clear()
        for t in self.tools:
            if dy.rule_for(t, self.rules) == dy.CHANGE:
                self.req_tool.addItem(str(t.get("name")))

    def ask_dyndrite(self) -> None:
        q = self.question.text().strip()
        if not q:
            return
        if not self.tools:
            self.answer.setPlainText("Check the connection first, so Council "
                                     "knows the server's tools.")
            return
        if not self.model.currentData():
            self.answer.setPlainText("Choose a model first.")
            return
        chat = self.chat()
        s = self.server_settings()
        mcp = self.ctx.service("mcp")
        tools, rules = list(self.tools), dict(self.rules)

        def work():
            return dy.ask(q, tools, rules, chat,
                          lambda name, args: mcp.call(s, name, args))

        def done(ans: dy.Answer):
            parts = [f"<p>{esc(ans.answer).replace(chr(10), '<br>')}</p>"]
            if ans.note:
                parts.append(f"<p><i>{esc(ans.note)}</i></p>")
            for st in ans.steps:
                parts.append(f"<p><b>{esc(st.tool)}</b> {esc(json.dumps(st.arguments))}"
                             + (f"<br><span style='color:#c62828'>{esc(st.error)}</span>"
                                if st.error else
                                f"<br><pre>{esc(st.text[:1500])}</pre>") + "</p>")
            self.answer.setHtml("".join(parts))
        self.run("Asking Dyndrite…", work, done)

    def request_tool(self) -> None:
        name = self.req_tool.currentText()
        if not name:
            self.dy_msg.setText("No change tool is listed; check the "
                                "connection first.")
            return
        try:
            args = json.loads(self.req_args.text() or "{}")
        except ValueError as exc:
            self.dy_msg.setText(f"The arguments are not JSON: {exc}")
            return
        tool = next((t for t in self.tools if t.get("name") == name), {})
        problem = dy.argument_problem(tool, args)
        if problem:
            self.dy_msg.setText(problem)
            return
        n = self.store.request_tool(name, args, reason=self.req_reason.text())
        self.dy_msg.setText(f"Request {n} filed; an engineer approves it "
                            f"before it can run.")
        self.refresh_requests()

    def refresh_requests(self) -> None:
        self.req_list.clear()
        for r in self.store.tool_requests():
            item = QListWidgetItem(f"#{r['n']} [{r['status']}] {r['tool']} "
                                   f"{json.dumps(r['arguments'])[:80]}  "
                                   f"({r['who']}: {r.get('reason') or ''})")
            item.setData(ROLE, r["n"])
            self.req_list.addItem(item)

    def _selected_request(self) -> int:
        item = self.req_list.currentItem()
        return int(item.data(ROLE)) if item else 0

    def decide_request(self, approve: bool) -> None:
        n = self._selected_request()
        if not n:
            return
        why = self.store.decide_tool(n, approve, who=self.req_who.text())
        self.dy_msg.setText(why or f"Request {n} {'approved' if approve else 'rejected'}.")
        self.refresh_requests()

    def run_request(self) -> None:
        n = self._selected_request()
        req = self.store.claim_approved(n) if n else None
        if req is None:
            self.dy_msg.setText("Only an approved request that has not run "
                                "yet can be run.")
            return
        tool = next((t for t in self.tools if t.get("name") == req["tool"]), None)
        if tool is None or dy.rule_for(tool, self.rules) == dy.BLOCKED:
            self.store.finish_tool(n, {"is_error": True, "text":
                                       "not run: the tool is blocked or not "
                                       "on the server now"})
            self.refresh_requests()
            return
        s = self.server_settings()
        mcp = self.ctx.service("mcp")

        def work():
            try:
                return mcp.call(s, req["tool"], req["arguments"])
            except Exception as exc:                          # noqa: BLE001
                return {"is_error": True, "text": str(exc)}

        def done(result):
            self.store.finish_tool(n, result)
            self.answer.setPlainText(str(result.get("text", ""))[:5000])
            self.refresh_requests()
        self.run(f"Running request {n}…", work, done)


def build_tab(ctx) -> QWidget:
    """The plugin's entry: Council calls it with the plugin's context."""
    return LpbfTab(ctx)
