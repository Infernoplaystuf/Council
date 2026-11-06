"""
The Finance tab: the household and each earner on the left, the paycheck
on the right (each check and the year), and what to look at below it.

Everything typed is kept in the plugin's own data folder as plan.json
(saved half a second after the last change, atomically, through the
context Council hands the plugin). Nothing leaves the PC.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCompleter,
                               QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .engine import paycheck, tables

PLAN_FILE = "plan.json"
SAVE_DELAY_MS = 500

#: (key, label, kind, options). kind: money (a year), check (per check),
#: percent, int, text, coverage, city, jedd.
EARNER_FIELDS: Tuple[Tuple[str, str, str, Any], ...] = (
    ("name", "Name", "text", None),
    ("age", "Age (end of year)", "int", (16, 100)),
    ("salary", "Salary (a year)", "money", None),
    ("bonus", "Bonus (a year)", "money", None),
    ("pay_periods", "Checks a year", "int", (1, 52)),
    ("work_city", "Works in (Ohio city)", "city", None),
    ("work_jedd", "…or in a JEDD / JEDZ", "jedd", None),
    ("medical", "Medical (per check)", "check", None),
    ("dental", "Dental (per check)", "check", None),
    ("vision", "Vision (per check)", "check", None),
    ("other_pretax", "Other pre-tax (per check)", "check", None),
    ("hsa_coverage", "HSA coverage", "coverage", None),
    ("hsa_employee", "HSA, you (per check)", "check", None),
    ("hsa_employer", "HSA, employer (a year)", "money", None),
    ("health_fsa", "Health FSA (a year)", "money", None),
    ("dc_fsa", "Dependent care FSA (a year)", "money", None),
    ("k401_percent", "401(k) traditional %", "percent", None),
    ("roth_percent", "401(k) Roth %", "percent", None),
    ("match_rate", "Employer match: % of yours", "percent", None),
    ("match_up_to", "…on up to % of pay", "percent", None),
    ("after_tax", "After-tax deductions (per check)", "check", None),
)


def _money(maximum: float = 10_000_000.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(0.0, maximum)
    box.setDecimals(2)
    box.setPrefix("$ ")
    box.setGroupSeparatorShown(True)
    box.setSingleStep(50.0)
    return box


def _percent() -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(0.0, 100.0)
    box.setDecimals(2)
    box.setSuffix(" %")
    box.setSingleStep(0.5)
    return box


def _picker(entries: List[Tuple[str, str]], none: str) -> QComboBox:
    """A combo of (label, code) with a blank first row; type to filter."""
    box = QComboBox()
    box.setEditable(True)
    box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    box.addItem(none, "")
    for label, code in entries:
        box.addItem(label, code)
    completer = box.completer()
    if completer is not None:
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion)
    return box


def _set_code(box: QComboBox, code: str) -> None:
    index = box.findData(code or "")
    box.setCurrentIndex(index if index >= 0 else 0)


def _code(box: QComboBox) -> str:
    index = box.findText(box.currentText())
    if index < 0:
        return ""
    return str(box.itemData(index) or "")


def _entries(rows: Dict[str, Dict[str, Any]]) -> List[Tuple[str, str]]:
    """("Name (rate%)", code) for an Ohio table, by name; the rate is
    the current one (a rate change is applied by the engine per year)."""
    out = []
    for code, row in rows.items():
        rate = row.get("current_rate")
        rate_text = f"{float(rate) * 100:.2f}%" if rate is not None \
            else "no rate now"
        out.append((f"{str(row.get('name', code)).title()} ({rate_text})",
                    code))
    return sorted(out, key=lambda x: x[0])


def city_entries() -> List[Tuple[str, str]]:
    return _entries(tables.ohio_cities())


def jedd_entries() -> List[Tuple[str, str]]:
    return _entries(tables.ohio_jedds())


def school_entries() -> List[Tuple[str, str]]:
    return _entries(tables.ohio_school_districts())


class EarnerForm(QWidget):
    """One earner's fields; `changed` is called on any edit."""

    def __init__(self, changed: Callable[[], None], cities, jedds):
        super().__init__()
        self.fields: Dict[str, Tuple[QWidget, Callable, Callable]] = {}
        form = QFormLayout(self)
        for key, label, kind, opt in EARNER_FIELDS:
            widget, get, put, signal = self._make(kind, opt, cities, jedds)
            signal.connect(lambda *_a: changed())
            widget.setObjectName(f"finance_{key}")
            form.addRow(label, widget)
            self.fields[key] = (widget, get, put)

    @staticmethod
    def _make(kind, opt, cities, jedds):
        if kind == "text":
            w = QLineEdit()
            return w, w.text, lambda v: w.setText(str(v)), w.textChanged
        if kind == "int":
            w = QSpinBox()
            w.setRange(*opt)
            return w, w.value, lambda v: w.setValue(int(v)), w.valueChanged
        if kind in ("money", "check"):
            w = _money()
            return w, w.value, lambda v: w.setValue(float(v)), w.valueChanged
        if kind == "percent":
            w = _percent()
            return w, w.value, lambda v: w.setValue(float(v)), w.valueChanged
        if kind == "coverage":
            w = QComboBox()
            for text, code in (("No HSA", "none"), ("Self-only", "self"),
                               ("Family", "family")):
                w.addItem(text, code)
            return (w, lambda: w.currentData(),
                    lambda v: _set_code(w, v), w.currentIndexChanged)
        entries = cities if kind == "city" else jedds
        w = _picker(entries, "(none)")
        return (w, lambda: _code(w), lambda v: _set_code(w, v),
                w.currentTextChanged)

    def value(self) -> Dict[str, Any]:
        return {key: get() for key, (_w, get, _p) in self.fields.items()}

    def set_earner(self, earner: Dict[str, Any]) -> None:
        for key, (_w, _g, put) in self.fields.items():
            if key in earner:
                put(earner[key])


class FinanceTab(QWidget):
    def __init__(self, ctx=None):
        super().__init__()
        self.ctx = ctx
        self._loading = False
        self._cities = city_entries()
        self._jedds = jedd_entries()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.save)
        self.result: Optional[paycheck.Result] = None
        self._build()
        self.load()

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        intro = QLabel("Your pay after tax and deductions, check by check "
                       "and for the year. A planner, not tax filing: it is "
                       "kept on this PC only.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        house = QGroupBox("Household")
        hf = QFormLayout(house)
        self.year = QComboBox()
        for y in tables.years():
            self.year.addItem(str(y), y)
        self.status = QComboBox()
        for code in paycheck.STATUSES:
            self.status.addItem(paycheck.STATUS_NAMES[code], code)
        self.state = QComboBox()
        self.state.addItem("(no state tax)", "")
        try:
            rows = tables.states(tables.years()[-1])
        except (KeyError, IndexError):
            rows = {}
        for abbr, row in sorted(rows.items(), key=lambda kv: kv[1]["state"]):
            self.state.addItem(f"{row['state']} ({abbr})", abbr)
        self.home_city = _picker(self._cities, "(none / not in Ohio)")
        self.credit = _percent()
        self.credit.setToolTip("How much of a work city's tax your home city "
                               "credits. Your city's tax office says; most "
                               "give 100%.")
        self.school = _picker(school_entries(), "(none / not in Ohio)")
        self.kids = QSpinBox()
        self.kids.setRange(0, 20)
        self.others = QSpinBox()
        self.others.setRange(0, 20)
        for label, w in (("Tax year", self.year),
                         ("Filing status", self.status),
                         ("State", self.state),
                         ("Home city (Ohio)", self.home_city),
                         ("Home city credit", self.credit),
                         ("School district (Ohio)", self.school),
                         ("Children under 17", self.kids),
                         ("Other dependents", self.others)):
            hf.addRow(label, w)
        for sig in (self.year.currentIndexChanged,
                    self.status.currentIndexChanged,
                    self.state.currentIndexChanged,
                    self.home_city.currentTextChanged,
                    self.credit.valueChanged,
                    self.school.currentTextChanged,
                    self.kids.valueChanged, self.others.valueChanged):
            sig.connect(lambda *_a: self.changed())
        lv.addWidget(house)

        self.earners = QTabWidget()
        lv.addWidget(self.earners, 1)
        row = QHBoxLayout()
        self.add_button = QPushButton("+ Add earner")
        self.add_button.clicked.connect(self.on_add_earner)
        self.remove_button = QPushButton("− Remove this earner")
        self.remove_button.clicked.connect(self.on_remove_earner)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        lv.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        split.addWidget(scroll)

        right = QWidget()
        rv = QVBoxLayout(right)
        self.table = QTreeWidget()
        self.table.setObjectName("FinancePaycheck")
        self.table.setHeaderLabels(["", "Per check", "A year"])
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        rv.addWidget(self.table, 3)
        self.notes = QLabel("")
        self.notes.setObjectName("FinanceNotes")
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes.setAlignment(Qt.AlignmentFlag.AlignTop)
        notes_scroll = QScrollArea()
        notes_scroll.setWidgetResizable(True)
        notes_scroll.setWidget(self.notes)
        rv.addWidget(notes_scroll, 2)
        self.saved = QLabel("")
        rv.addWidget(self.saved)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

    # -- the plan ---------------------------------------------------------
    def _forms(self) -> List[EarnerForm]:
        return [self.earners.widget(i) for i in range(self.earners.count())]

    def _add_form(self, earner: Dict[str, Any]) -> EarnerForm:
        form = EarnerForm(self.changed, self._cities, self._jedds)
        form.set_earner(earner)
        self.earners.addTab(form, earner.get("name") or "Earner")
        return form

    def plan(self) -> Dict[str, Any]:
        return paycheck.normalized({
            "year": self.year.currentData(),
            "filing_status": self.status.currentData(),
            "state": self.state.currentData(),
            "home_city": _code(self.home_city),
            "home_city_credit": self.credit.value() / 100.0,
            "school_district": _code(self.school),
            "children_under_17": self.kids.value(),
            "other_dependents": self.others.value(),
            "earners": [f.value() for f in self._forms()]})

    def show_plan(self, plan: Dict[str, Any]) -> None:
        plan = paycheck.normalized(plan)
        self._loading = True
        try:
            _set_code(self.year, plan["year"])
            _set_code(self.status, plan["filing_status"])
            _set_code(self.state, plan["state"])
            _set_code(self.home_city, plan["home_city"])
            self.credit.setValue(plan["home_city_credit"] * 100.0)
            _set_code(self.school, plan["school_district"])
            self.kids.setValue(plan["children_under_17"])
            self.others.setValue(plan["other_dependents"])
            while self.earners.count():
                w = self.earners.widget(0)
                self.earners.removeTab(0)
                w.deleteLater()
            for earner in plan["earners"]:
                self._add_form(earner)
        finally:
            self._loading = False
        self.recalculate()

    def load(self) -> None:
        saved = None
        if self.ctx is not None:
            saved = self.ctx.load_json(PLAN_FILE, default=None)
        self.show_plan(saved if isinstance(saved, dict)
                       else paycheck.default_plan())

    def save(self) -> None:
        self._timer.stop()
        if self.ctx is None:
            return
        try:
            self.ctx.save_json(PLAN_FILE, self.plan())
            self.saved.setText("Saved on this PC.")
        except Exception as exc:                          # noqa: BLE001
            self.saved.setText(f"Could not save: {exc}")

    def changed(self) -> None:
        if self._loading:
            return
        for i, form in enumerate(self._forms()):
            self.earners.setTabText(i, form.value()["name"] or "Earner")
        self.recalculate()
        self._timer.start()

    def on_add_earner(self) -> None:
        n = self.earners.count() + 1
        self._add_form(paycheck.default_earner(f"Earner {n}"))
        self.earners.setCurrentIndex(self.earners.count() - 1)
        if n == 2 and self.status.currentData() == "single":
            _set_code(self.status, "married_joint")
        self.changed()

    def on_remove_earner(self) -> None:
        if self.earners.count() <= 1:
            return
        i = self.earners.currentIndex()
        w = self.earners.widget(i)
        self.earners.removeTab(i)
        w.deleteLater()
        self.changed()

    # -- the result -------------------------------------------------------
    def recalculate(self) -> None:
        try:
            self.result = paycheck.compute(self.plan())
        except Exception as exc:                          # noqa: BLE001
            self.result = None
            self.table.clear()
            self.notes.setText(f"Could not work it out: {exc}")
            return
        self._fill(self.result)

    def _fill(self, result: paycheck.Result) -> None:
        self.table.clear()
        many = len(result.earners) > 1
        for earner in result.earners:
            top = QTreeWidgetItem([f"{earner.name} · {earner.pay_periods} "
                                   "checks a year", "", ""])
            self.table.addTopLevelItem(top)
            per = earner.per_check()
            for key, label, sign in paycheck.LINES:
                year = earner.annual[key]
                if not year and key not in ("gross", "net"):
                    continue
                show = -1 if sign < 0 else 1
                item = QTreeWidgetItem([label, _dollars(show * per[key]),
                                        _dollars(show * year)])
                for col in (1, 2):
                    item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight)
                if key in ("gross", "net"):
                    font = item.font(0)
                    font.setBold(True)
                    for col in range(3):
                        item.setFont(col, font)
                top.addChild(item)
            for key, label in (("match", "Employer 401(k) match"),
                               ("hsa", "Employer HSA")):
                if earner.employer.get(key):
                    item = QTreeWidgetItem(
                        [f"{label} (not in your check)",
                         _dollars(earner.employer[key]
                                  / earner.pay_periods),
                         _dollars(earner.employer[key])])
                    for col in (1, 2):
                        item.setTextAlignment(col,
                                              Qt.AlignmentFlag.AlignRight)
                    top.addChild(item)
            top.setExpanded(True)
        if many:
            top = QTreeWidgetItem(["Household", "", ""])
            self.table.addTopLevelItem(top)
            for key, label, sign in paycheck.LINES:
                year = result.household[key]
                if not year and key not in ("gross", "net"):
                    continue
                item = QTreeWidgetItem([label, "",
                                        _dollars((-1 if sign < 0 else 1)
                                                 * year)])
                item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight)
                top.addChild(item)
            top.setExpanded(True)
        self.notes.setText(notes_text(result))


def _dollars(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def notes_text(result: paycheck.Result) -> str:
    d = result.details
    lines = [f"Federal: taxable ${d.get('federal_taxable', 0):,.0f} after "
             f"the standard deduction; credits for dependents "
             f"${d.get('dependent_credits', 0) + d.get('refundable_child_credit', 0):,.0f}."]
    if "state_taxable" in d:
        lines.append(f"State: taxable ${d['state_taxable']:,.0f}.")
    if d.get("additional_medicare"):
        lines.append(f"Additional Medicare Tax: "
                     f"${d['additional_medicare']:,.0f} a year.")
    if result.warnings:
        lines.append("")
        lines.append("Look at:")
        lines.extend(f"• {w}" for w in result.warnings)
    if result.not_modeled:
        lines.append("")
        lines.append("Not modeled:")
        lines.extend(f"• {w}" for w in result.not_modeled)
    return "\n".join(lines)


def build_tab(ctx) -> QWidget:
    """The plugin's entry: Council calls it with the plugin's context."""
    return FinanceTab(ctx)
