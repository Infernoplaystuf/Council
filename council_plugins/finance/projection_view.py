"""
The Finance tab's Projection page: the settings and accounts on top, a
chart of the balances, and a table with a row a year.

The page holds no plan of its own: FinanceTab hands it the plan to show
(show_plan / refresh) and asks it for its settings (settings()), which go
into plan.json beside the rest under "projection".
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .engine import projection

#: A colour-blind-safe set for the account lines; the total is drawn in
#: the text colour.
SERIES = ("#4269d0", "#efb118", "#ff725c", "#6cc5b0", "#3ca951",
          "#ff8ab7", "#a463f2", "#97bbf5", "#9c6b4e", "#9498a0")

ACCOUNT_COLUMNS = ("Account", "Kind", "Whose", "Balance now", "Growth %",
                   "Extra a year")


def money(value: float) -> str:
    return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"


def short_money(value: float) -> str:
    v = abs(value)
    sign = "-" if value < 0 else ""
    if v >= 1e6:
        return f"{sign}${v / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}k"
    return f"{sign}${v:.0f}"


def nice_top(value: float, steps: int = 4) -> float:
    """The smallest round number at or over `value` that splits into
    `steps` round steps (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6 or 8 times a
    power of ten)."""
    if value <= 0:
        return float(steps)
    raw = value / steps
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * power >= raw:
            return m * power * steps
    return 10 * power * steps


class BalanceChart(QWidget):
    """Each account's balance by year, and the total, as lines."""

    def __init__(self):
        super().__init__()
        self.setObjectName("FinanceChart")
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.years: List[int] = []
        self.series: List[tuple] = []          # (name, colour, values)

    def show_series(self, years: List[int], series: List[tuple]) -> None:
        self.years, self.series = list(years), list(series)
        self.update()

    def paintEvent(self, _event) -> None:                    # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text = pal.color(pal.ColorRole.WindowText)
        grid = QColor(text)
        grid.setAlpha(40)
        rect = QRectF(self.rect()).adjusted(64, 12, -16, -48)
        if not self.years or not self.series or rect.width() < 20:
            p.setPen(text)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Add pay or an account to see it grow.")
            return
        top = max((max(v) for _n, _c, v in self.series if v), default=0.0)
        top = nice_top(top)
        n = len(self.years)

        def at(i: int, value: float) -> QPointF:
            x = rect.left() + (rect.width() * i / (n - 1) if n > 1 else 0)
            return QPointF(x, rect.bottom() - rect.height() * value / top)

        fm = p.fontMetrics()
        for k in range(5):                     # light grid, money on the left
            value = top * k / 4
            y = rect.bottom() - rect.height() * k / 4
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(text)
            label = short_money(value)
            p.drawText(QPointF(rect.left() - 8 - fm.horizontalAdvance(label),
                               y + fm.ascent() / 2 - 1), label)
        step = max(1, round(n / 8))
        for i in range(0, n, step):
            label = str(self.years[i])
            pt = at(i, 0)
            p.drawText(QPointF(pt.x() - fm.horizontalAdvance(label) / 2,
                               rect.bottom() + fm.height()), label)
        for name, colour, values in self.series:
            path = QPainterPath()
            for i, value in enumerate(values):
                pt = at(i, value)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            width = 3 if name == "Total" else 2
            p.setPen(QPen(QColor(colour) if colour else text, width))
            p.drawPath(path)
        # Legend under the axis, wrapping as needed.
        x, y = rect.left(), rect.bottom() + fm.height() * 2 + 4
        for name, colour, _values in self.series:
            w = 14 + 6 + fm.horizontalAdvance(name) + 16
            if x + w > rect.right() and x > rect.left():
                x, y = rect.left(), y + fm.height() + 2
            p.setPen(QPen(QColor(colour) if colour else text, 3))
            p.drawLine(QPointF(x, y - fm.ascent() / 2 + 1),
                       QPointF(x + 14, y - fm.ascent() / 2 + 1))
            p.setPen(text)
            p.drawText(QPointF(x + 20, y), name)
            x += w


class ProjectionPage(QWidget):
    def __init__(self, changed: Callable[[], None]):
        super().__init__()
        self._changed = changed
        self._filling = False
        self._earner_names: List[str] = []
        self.result: Optional[projection.Projection] = None
        self._build()

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        top = QHBoxLayout()
        box = QGroupBox("Assumptions")
        form = QFormLayout(box)
        self.years = QSpinBox()
        self.years.setRange(1, projection.MAX_YEARS)
        self.raise_ = self._pct(-20, 50)
        self.inflation = self._pct(-10, 50)
        self.growth = self._pct(-50, 50)
        for label, w, tip in (
                ("Years to show", self.years, ""),
                ("Raises a year", self.raise_, "Each earner's pay rises "
                 "this much every year until they retire."),
                ("Inflation a year", self.inflation, "Tax brackets, limits "
                 "and the plan's fixed dollar amounts rise with it."),
                ("Growth a year", self.growth, "What an account earns when "
                 "it has no growth of its own.")):
            w.setToolTip(tip)
            form.addRow(label, w)
        for w in (self.years, self.raise_, self.inflation, self.growth):
            w.valueChanged.connect(lambda *_a: self._edited())
        top.addWidget(box)

        acc = QGroupBox("Accounts")
        av = QVBoxLayout(acc)
        hint = QLabel("401(k), Roth 401(k) and HSA money from your pay goes "
                      "in by itself. Add IRAs, brokerage or savings and what "
                      "you put in from take-home pay.")
        hint.setWordWrap(True)
        av.addWidget(hint)
        self.accounts = QTableWidget(0, len(ACCOUNT_COLUMNS))
        self.accounts.setObjectName("FinanceAccounts")
        self.accounts.setHorizontalHeaderLabels(list(ACCOUNT_COLUMNS))
        self.accounts.verticalHeader().setVisible(False)
        self.accounts.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        h = self.accounts.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(ACCOUNT_COLUMNS)):
            h.setSectionResizeMode(col,
                                   QHeaderView.ResizeMode.ResizeToContents)
        av.addWidget(self.accounts, 1)
        row = QHBoxLayout()
        add = QPushButton("+ Add account")
        add.clicked.connect(self.on_add_account)
        remove = QPushButton("− Remove selected")
        remove.clicked.connect(self.on_remove_account)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        self.today = QCheckBox("Show in today's dollars")
        self.today.setToolTip("Take inflation out, so a later dollar is "
                              "shown as what it buys now.")
        self.today.toggled.connect(lambda *_a: self._show_result())
        row.addWidget(self.today)
        av.addLayout(row)
        top.addWidget(acc, 1)
        outer.addLayout(top)

        split = QSplitter(Qt.Orientation.Vertical)
        self.chart = BalanceChart()
        split.addWidget(self.chart)
        self.table = QTableWidget(0, 0)
        self.table.setObjectName("FinanceProjection")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        split.addWidget(self.table)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        outer.addWidget(split, 1)
        self.summary = QLabel("")
        self.summary.setObjectName("FinanceProjectionNotes")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.summary)

    @staticmethod
    def _pct(low: float, high: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(2)
        box.setSuffix(" %")
        box.setSingleStep(0.25)
        return box

    @staticmethod
    def _money_box() -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 100_000_000.0)
        box.setDecimals(0)
        box.setPrefix("$ ")
        box.setGroupSeparatorShown(True)
        box.setSingleStep(500.0)
        return box

    # -- the accounts table ----------------------------------------------
    def _add_row(self, account: Dict[str, Any]) -> None:
        r = self.accounts.rowCount()
        self.accounts.insertRow(r)
        name = QLineEdit(account["name"])
        name.textChanged.connect(lambda *_a: self._edited())
        kind = QComboBox()
        for code, label in projection.KINDS.items():
            kind.addItem(label, code)
        kind.setCurrentIndex(max(kind.findData(account["kind"]), 0))
        kind.currentIndexChanged.connect(lambda *_a: self._edited())
        whose = QComboBox()
        whose.addItem("Household", -1)
        for i, n in enumerate(self._earner_names):
            whose.addItem(n, i)
        whose.setCurrentIndex(max(whose.findData(account["owner"]), 0))
        whose.currentIndexChanged.connect(lambda *_a: self._edited())
        balance = self._money_box()
        balance.setValue(account["balance"])
        balance.valueChanged.connect(lambda *_a: self._edited())
        growth = self._pct(-50, 50)
        growth.setSpecialValueText("default")
        growth.setMinimum(-50.25)              # the bottom step = default
        growth.setValue(-50.25 if account["return_percent"] is None
                        else account["return_percent"])
        growth.valueChanged.connect(lambda *_a: self._edited())
        extra = self._money_box()
        extra.setValue(account["extra_per_year"])
        extra.valueChanged.connect(lambda *_a: self._edited())
        for col, w in enumerate((name, kind, whose, balance, growth, extra)):
            self.accounts.setCellWidget(r, col, w)

    def _accounts(self) -> List[Dict[str, Any]]:
        out = []
        for r in range(self.accounts.rowCount()):
            w = [self.accounts.cellWidget(r, c)
                 for c in range(len(ACCOUNT_COLUMNS))]
            growth = w[4].value()
            out.append({"name": w[0].text(), "kind": w[1].currentData(),
                        "owner": w[2].currentData(),
                        "balance": w[3].value(),
                        "return_percent": None if growth <= -50.25 + 1e-9
                        else growth,
                        "extra_per_year": w[5].value()})
        return out

    def on_add_account(self) -> None:
        self._add_row(projection.default_account(
            "savings", -1, "Savings"))
        self._edited()

    def on_remove_account(self) -> None:
        rows = sorted({i.row() for i in self.accounts.selectedIndexes()},
                      reverse=True)
        if not rows and self.accounts.rowCount():
            rows = [self.accounts.rowCount() - 1]
        for r in rows:
            self.accounts.removeRow(r)
        self._edited()

    # -- with the tab -----------------------------------------------------
    def settings(self) -> Dict[str, Any]:
        return {"years": self.years.value(),
                "raise_percent": self.raise_.value(),
                "inflation_percent": self.inflation.value(),
                "return_percent": self.growth.value(),
                "accounts": self._accounts()}

    def _edited(self) -> None:
        if not self._filling:
            self._changed()

    def show_plan(self, plan: Dict[str, Any]) -> None:
        """Fill the settings and accounts from the plan, then show it."""
        settings = projection.settings_of(plan)
        self._earner_names = [e["name"] for e in plan["earners"]]
        self._filling = True
        try:
            self.years.setValue(settings["years"])
            self.raise_.setValue(settings["raise_percent"])
            self.inflation.setValue(settings["inflation_percent"])
            self.growth.setValue(settings["return_percent"])
            self.accounts.setRowCount(0)
            for a in settings["accounts"]:
                self._add_row(a)
        finally:
            self._filling = False
        self.refresh(plan)

    def refresh(self, plan: Dict[str, Any]) -> None:
        """Work the projection out again. When the plan's earners changed,
        or pay began feeding an account kind not listed, the accounts are
        shown again (with the added ones)."""
        names = [e["name"] for e in plan["earners"]]
        settings = projection.settings_of(plan)
        if names != self._earner_names or \
                len(settings["accounts"]) != self.accounts.rowCount():
            self._earner_names = names
            self._filling = True
            try:
                self.accounts.setRowCount(0)
                for a in settings["accounts"]:
                    self._add_row(a)
            finally:
                self._filling = False
        try:
            self.result = projection.project(plan)
        except Exception as exc:                          # noqa: BLE001
            self.result = None
            self.summary.setText(f"Could not work it out: {exc}")
            return
        self._show_result()

    def _show_result(self) -> None:
        res = self.result
        if res is None:
            return
        today = self.today.isChecked()
        infl = res.settings["inflation_percent"] / 100.0

        def v(row, value):
            return value / (1.0 + infl) ** row.index if today else value

        names = [a["name"] for a in res.accounts]
        heads = ["Year", "Ages", "Gross pay", "Taxes", "Take-home",
                 "You put in (pay)", "Employer put in", "Extra put in",
                 "Left to spend", "Growth"] + names + ["Total"]
        self.table.clear()
        self.table.setColumnCount(len(heads))
        self.table.setHorizontalHeaderLabels(heads)
        self.table.setRowCount(len(res.rows))
        for r, row in enumerate(res.rows):
            ages = ", ".join(f"{a}" + ("" if w else " (retired)")
                             for a, w in zip(row.ages, row.working))
            cells = [str(row.year), ages] + [money(v(row, x)) for x in (
                row.gross, row.taxes, row.take_home, row.from_pay,
                row.employer, row.extra, row.left, row.growth)] + \
                [money(v(row, b)) for b in row.balances] + \
                [money(v(row, row.total))]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        years = [row.year for row in res.rows]
        series = [(name, SERIES[i % len(SERIES)],
                   [v(row, row.balances[i]) for row in res.rows])
                  for i, name in enumerate(names)]
        if len(names) > 1:
            series.append(("Total", "", [v(row, row.total)
                                         for row in res.rows]))
        self.chart.show_series(years, series)
        final = res.final
        lines = []
        if final is not None:
            lines.append(
                f"By the end of {final.year}: {money(final.total)} in all "
                f"({money(final.total_today)} in today's dollars), from "
                f"{money(sum(a['balance'] for a in res.accounts))} now.")
        lines.extend(res.notes)
        self.summary.setText("\n".join(lines))
