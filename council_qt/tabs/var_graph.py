"""
council_qt.tabs.var_graph — which variables in a script feed which, drawn.

For a bug hunt: open a script, pick the variable whose value is wrong, and see
what it was made from (left of it) and what it goes on to make (right of it),
each link labelled with the line that makes it. Click a link or a trace row to
read that line in the source; click a node to make it the centre.

The analysis is council_core.var_graph (static, `ast` only — the script is
never run or imported). It runs on a worker; the result comes back through the
bridge. Nothing here writes to disk.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QTextCursor, QTextFormat)
from PySide6.QtWidgets import (QCheckBox, QGraphicsPathItem,
                               QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QTabWidget, QTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from council_core import var_graph as vgm

from .. import theme
from ..view import amp

ROLE_VAR = Qt.ItemDataRole.UserRole          # a node id
ROLE_LINE = Qt.ItemDataRole.UserRole + 1     # a source line

#: Most nodes drawn in one column before the rest are summarised.
COLUMN_CAP = 18
NODE_W, NODE_H, COL_GAP, ROW_GAP = 190, 30, 90, 12

KIND_COLOUR = {"variable": "#5b7fa6", "param": "#7ea16d", "attribute": "#a07cc0",
               "return": "#c9a227", "import": "#7a7575", "global": "#5b7fa6"}


class VarGraphActions:
    """What the tab asks of the application; a test can swap any of it."""

    def analyze(self, path: str, conditions: bool) -> vgm.VarGraph:
        return vgm.analyze_file(path, conditions)

    def pick_file(self, parent) -> str:
        from .. import dialogs
        return dialogs.askopenfilename("Open a Python script",
                                       filetypes=[("Python", "*.py *.pyw")],
                                       parent=parent)


def neighbourhood(g: vgm.VarGraph, focus: str, depth: int,
                  conditions: bool) -> Dict[str, int]:
    """node id -> column: 0 for the focus, -n for n hops upstream, +n for n
    hops downstream. A node both sides of the focus (a loop) keeps the side it
    was reached on first."""
    cols = {focus: 0}
    for step in g.upstream(focus, depth, conditions):
        cols.setdefault(step.var.id, -step.depth)
    for step in g.downstream(focus, depth, conditions):
        cols.setdefault(step.var.id, step.depth)
    return cols


class _Node(QGraphicsRectItem):
    def __init__(self, var: vgm.Var, focus: bool, on_click):
        super().__init__(0, 0, NODE_W, NODE_H)
        self.var = var
        self._on_click = on_click
        colour = QColor(KIND_COLOUR.get(var.kind, "#5b7fa6"))
        self.setBrush(QBrush(colour.darker(170) if not focus else colour))
        self.setPen(QPen(colour.lighter(130), 3 if focus else 1))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        label = var.name if var.kind != "return" else f"{var.scope}() ⏎"
        text = QGraphicsSimpleTextItem(label, self)
        text.setBrush(QBrush(QColor("#f0f0f0")))
        f = QFont()
        f.setBold(focus)
        text.setFont(f)
        while text.boundingRect().width() > NODE_W - 12 and len(label) > 4:
            label = label[:-2]
            text.setText(label + "…")
        r = text.boundingRect()
        text.setPos((NODE_W - r.width()) / 2, (NODE_H - r.height()) / 2)
        self.setToolTip(f"{var.label}\n{var.kind}\nwritten on "
                        f"{vgm._lines(var.defined)}\nread on {vgm._lines(var.used)}")

    def mousePressEvent(self, event):                      # noqa: N802
        self._on_click(self.var.id)
        event.accept()


class _Link(QGraphicsPathItem):
    def __init__(self, path: QPainterPath, edges: List[vgm.Edge], on_click):
        super().__init__(path)
        self.edges = edges
        self._on_click = on_click
        dashed = all(e.kind == "condition" for e in edges)
        pen = QPen(QColor("#9a9090"), 1.4)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(-1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("\n".join(f"L{e.line} {e.kind}: {e.code}" for e in edges[:6]))

    def shape(self):                                       # wider hit area
        s = QPainterPathStroker()
        s.setWidth(8)
        return s.createStroke(self.path())

    def mousePressEvent(self, event):                      # noqa: N802
        self._on_click(self.edges[0].line)
        event.accept()


class GraphView(QGraphicsView):
    """The focus in the middle, what feeds it to the left, what it feeds to
    the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.on_node = lambda vid: None
        self.on_line = lambda line: None
        self.nodes: Dict[str, _Node] = {}

    def wheelEvent(self, event):                           # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            f = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
        else:
            super().wheelEvent(event)

    def show_graph(self, g: vgm.VarGraph, focus: str, depth: int,
                   conditions: bool) -> None:
        scene = self.scene()
        scene.clear()
        self.nodes = {}
        cols = neighbourhood(g, focus, depth, conditions)
        by_col: Dict[int, List[str]] = {}
        for vid, c in cols.items():
            by_col.setdefault(c, []).append(vid)
        muted = QColor(theme.tokens("dark")["muted_fg"])
        for c, vids in by_col.items():
            vids.sort(key=lambda v: (g.vars[v].scope, g.vars[v].name))
            shown, extra = vids[:COLUMN_CAP], len(vids) - COLUMN_CAP
            height = len(shown) * (NODE_H + ROW_GAP)
            x = c * (NODE_W + COL_GAP)
            for i, vid in enumerate(shown):
                node = _Node(g.vars[vid], vid == focus, self.on_node)
                node.setPos(x, i * (NODE_H + ROW_GAP) - height / 2)
                scene.addItem(node)
                self.nodes[vid] = node
            if extra > 0:
                more = scene.addSimpleText(f"+{extra} more")
                more.setBrush(QBrush(muted))
                more.setPos(x + 4, height / 2)
            if c in (-1, 1):
                head = scene.addSimpleText("what feeds it" if c == -1
                                           else "what it feeds")
                head.setBrush(QBrush(muted))
                head.setPos(x, -height / 2 - 26)
        # One curve per (src, dst) pair, carrying all the lines that link them.
        pairs: Dict[tuple, List[vgm.Edge]] = {}
        for vid in self.nodes:
            for e in g.feeds(vid, conditions):
                if e.dst in self.nodes:
                    pairs.setdefault((e.src, e.dst), []).append(e)
        for (s, d), edges in pairs.items():
            a, b = self.nodes[s], self.nodes[d]
            start = a.pos() + QPointF(NODE_W, NODE_H / 2)
            end = b.pos() + QPointF(0, NODE_H / 2)
            if b.pos().x() <= a.pos().x():                   # back edge: arc over
                start = a.pos() + QPointF(NODE_W / 2, 0)
                end = b.pos() + QPointF(NODE_W / 2, 0)
                lift = 40 + abs(a.pos().x() - b.pos().x()) / 6
                path = QPainterPath(start)
                path.cubicTo(start + QPointF(0, -lift), end + QPointF(0, -lift), end)
            else:
                dx = (end.x() - start.x()) / 2
                path = QPainterPath(start)
                path.cubicTo(start + QPointF(dx, 0), end - QPointF(dx, 0), end)
            # arrowhead
            tip = path.pointAtPercent(1.0)
            back = path.pointAtPercent(0.97)
            v = tip - back
            n = max((v.x() ** 2 + v.y() ** 2) ** 0.5, 1e-6)
            u = QPointF(v.x() / n, v.y() / n)
            p = QPointF(-u.y(), u.x())
            path.moveTo(tip)
            path.lineTo(tip - u * 9 + p * 4)
            path.moveTo(tip)
            path.lineTo(tip - u * 9 - p * 4)
            scene.addItem(_Link(path, edges, self.on_line))
        rect = scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        scene.setSceneRect(rect)
        self.resetTransform()
        vp = self.viewport()
        fit = min(vp.width() / rect.width(), vp.height() / rect.height(), 1.0)
        # Shrink to fit, but never past legible; past that, scroll.
        f = max(fit, 0.6)
        self.scale(f, f)
        self.centerOn(self.nodes[focus])


def fill_trace(tree: QTreeWidget, g: vgm.VarGraph, focus: str, depth: int,
               conditions: bool) -> None:
    """Two branches, 'Fed by' and 'Feeds', each a tree of edges. A node is
    expanded once; later mentions say so instead of repeating it."""
    tree.clear()
    for title, step, other in (("⬅ Fed by (where its value comes from)", g.fed_by, "src"),
                               ("➡ Feeds (where its value goes)", g.feeds, "dst")):
        top = QTreeWidgetItem([title, "", ""])
        tree.addTopLevelItem(top)
        expanded = {focus}

        def rec(parent, vid, d):
            if d >= depth:
                return
            for e in sorted(step(vid, conditions), key=lambda e: e.line):
                nxt = getattr(e, other)
                v = g.vars[nxt]
                again = nxt in expanded
                item = QTreeWidgetItem([v.label + ("   ↑ see above" if again and step(nxt, conditions) else ""),
                                        f"L{e.line} {e.kind}", e.code])
                item.setData(0, ROLE_VAR, nxt)
                item.setData(0, ROLE_LINE, e.line)
                parent.addChild(item)
                if not again:
                    expanded.add(nxt)
                    rec(item, nxt, d + 1)

        rec(top, focus, 0)
        if top.childCount() == 0:
            top.addChild(QTreeWidgetItem(["(nothing)", "", ""]))
    tree.expandAll()
    tree.resizeColumnToContents(1)


def build_var_graph(window, actions: Optional[VarGraphActions] = None) -> QWidget:
    """The tab. ``window`` is the CouncilWindow, for the bridge and status."""
    actions = actions or VarGraphActions()
    tokens = theme.tokens("dark")
    page = QWidget()
    page.actions = actions
    state: Dict[str, object] = {"graph": None, "focus": None}
    layout = QVBoxLayout(page)

    row = QHBoxLayout()
    path_edit = QLineEdit(page)
    path_edit.setPlaceholderText("Path to a .py script")
    row.addWidget(path_edit, 1)
    browse = QPushButton(amp("📂 Browse…"), page)
    row.addWidget(browse)
    run = QPushButton(amp("🔗 Map variables"), page)
    run.setObjectName("analyze")
    row.addWidget(run)
    conds = QCheckBox("Include if/while conditions", page)
    conds.setToolTip("Also link the variables an if/while tests to everything "
                     "assigned inside it. Answers 'why did this branch run?', "
                     "at the cost of a much busier graph.")
    row.addWidget(conds)
    row.addWidget(QLabel("Depth", page))
    depth = QSpinBox(page)
    depth.setRange(1, 50)
    depth.setValue(3)
    depth.setToolTip("How many hops out from the selected variable to show")
    row.addWidget(depth)
    layout.addLayout(row)

    status = QLabel("Open a script to see how its variables feed each other.", page)
    status.setStyleSheet(f"color: {tokens['muted_fg']};")
    layout.addWidget(status)

    split = QSplitter(Qt.Orientation.Horizontal, page)
    layout.addWidget(split, 1)

    # left: search + variables + findings
    left = QWidget(split)
    lv = QVBoxLayout(left)
    lv.setContentsMargins(0, 0, 0, 0)
    search = QLineEdit(left)
    search.setPlaceholderText("Find a variable…")
    lv.addWidget(search)
    var_list = QListWidget(left)
    var_list.setObjectName("variables")
    lv.addWidget(var_list, 3)
    lv.addWidget(QLabel("Worth a look", left))
    findings = QListWidget(left)
    findings.setObjectName("findings")
    findings.setToolTip("Patterns that are often the bug: names read but never "
                        "assigned, values assigned and never read, parameters "
                        "overwritten before use.")
    lv.addWidget(findings, 1)

    # middle: graph over trace
    mid = QSplitter(Qt.Orientation.Vertical, split)
    view = GraphView(mid)
    tabs = QTabWidget(mid)
    trace = QTreeWidget(tabs)
    trace.setObjectName("trace")
    trace.setHeaderLabels(["Variable", "How", "Line"])
    trace.setColumnWidth(0, 260)
    tabs.addTab(trace, "Trace")
    code = QPlainTextEdit(tabs)
    code.setObjectName("source")
    code.setReadOnly(True)
    code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    code.setFont(QFont("Consolas", 10))
    tabs.addTab(code, "Source")
    mid.setStretchFactor(0, 3)
    mid.setStretchFactor(1, 2)
    mid.setSizes([520, 340])
    split.setStretchFactor(0, 1)
    split.setStretchFactor(1, 4)
    split.setSizes([280, 1100])

    page.view, page.trace, page.code = view, trace, code
    page.var_list, page.findings, page.status = var_list, findings, status
    page.path_edit, page.conditions, page.depth = path_edit, conds, depth

    # -- behaviour -------------------------------------------------------
    def show_line(line: int, others: List[int] = ()) -> None:
        """Highlight `line` (and lightly, `others`) in the source and scroll
        to it."""
        sels = []
        for n, colour in [(n, "#3a2f1a") for n in others if n != line] + \
                [(line, tokens["selection_bg"])]:
            block = code.document().findBlockByNumber(n - 1)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(colour))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            sels.append(sel)
        code.setExtraSelections(sels)
        block = code.document().findBlockByNumber(max(line - 1, 0))
        if block.isValid():
            code.setTextCursor(QTextCursor(block))
            code.centerCursor()
        tabs.setCurrentWidget(code)

    def focus_on(vid: str) -> None:
        g = state["graph"]
        if g is None or vid not in g.vars:
            return
        state["focus"] = vid
        c = conds.isChecked()
        view.show_graph(g, vid, depth.value(), c)
        fill_trace(trace, g, vid, depth.value(), c)
        tabs.setCurrentWidget(trace)
        v = g.vars[vid]
        up = len(g.upstream(vid, None, c))
        down = len(g.downstream(vid, None, c))
        status.setText(f"{v.label} — {v.kind}; written on {vgm._lines(v.defined)}, "
                       f"read on {vgm._lines(v.used)}. {up} variables can affect it; "
                       f"it can affect {down}.")
        for i in range(var_list.count()):
            if var_list.item(i).data(ROLE_VAR) == vid:
                var_list.blockSignals(True)
                var_list.setCurrentRow(i)
                var_list.blockSignals(False)
                break
        code.setExtraSelections([])

    def fill_list() -> None:
        g = state["graph"]
        var_list.clear()
        if g is None:
            return
        text = search.text().strip()
        vs = g.find(text) if text else sorted(
            g.vars.values(), key=lambda v: (v.scope != vgm.MODULE, v.scope, v.name))
        for v in vs:
            item = QListWidgetItem(f"{v.label}")
            item.setData(ROLE_VAR, v.id)
            item.setForeground(QColor(KIND_COLOUR.get(v.kind, tokens["fg"])).lighter(140))
            item.setToolTip(f"{v.kind}; written on {vgm._lines(v.defined)}; "
                            f"read on {vgm._lines(v.used)}")
            var_list.addItem(item)

    def deliver(g: Optional[vgm.VarGraph], error: str) -> None:
        run.setEnabled(True)
        if g is None:
            status.setText(error)
            window.set_status("Variable map failed")
            return
        state["graph"] = g
        code.setPlainText(g.source)
        fill_list()
        findings.clear()
        for f in g.findings:
            item = QListWidgetItem(f"L{f.line}  {f.message}")
            item.setData(ROLE_LINE, f.line)
            item.setData(ROLE_VAR, f"{f.scope}::{f.name}")
            findings.addItem(item)
        status.setText(f"{Path(g.filename).name}: {vgm.summary(g)}. "
                       "Pick a variable to trace it.")
        window.set_status("Variable map ready")
        prev = state["focus"]
        if isinstance(prev, str) and prev in g.vars:
            focus_on(prev)
        else:
            view.scene().clear()
            trace.clear()

    def analyze() -> None:
        path = path_edit.text().strip().strip('"')
        if not path:
            status.setText("Choose a script first.")
            return
        if not Path(path).is_file():
            status.setText(f"No such file: {path}")
            return
        run.setEnabled(False)
        status.setText("Reading the script…")
        window.set_status("Mapping variables …")
        c = conds.isChecked()

        def work() -> None:
            try:
                g, err = actions.analyze(path, c), ""
            except SyntaxError as exc:
                g, err = None, f"The script does not parse (line {exc.lineno}): {exc.msg}"
            except Exception as exc:                       # noqa: BLE001
                g, err = None, f"Could not read it: {exc}"
            window.bridge.call_on_ui(lambda: deliver(g, err))

        threading.Thread(target=work, name="var-graph", daemon=True).start()

    def on_browse() -> None:
        path = actions.pick_file(page)
        if path:
            path_edit.setText(path)
            state["focus"] = None
            analyze()

    def on_var(item: Optional[QListWidgetItem]) -> None:
        if item is not None:
            focus_on(item.data(ROLE_VAR))

    def on_finding(item: QListWidgetItem) -> None:
        g = state["graph"]
        vid = item.data(ROLE_VAR)
        if g is not None and vid in g.vars:
            focus_on(vid)
        show_line(item.data(ROLE_LINE))

    def on_trace(item: QTreeWidgetItem, _col: int) -> None:
        line = item.data(0, ROLE_LINE)
        if line:
            show_line(line)

    def on_trace_double(item: QTreeWidgetItem, _col: int) -> None:
        vid = item.data(0, ROLE_VAR)
        if vid:
            focus_on(vid)

    def refocus() -> None:
        if isinstance(state["focus"], str):
            focus_on(state["focus"])

    def on_conditions() -> None:
        # Condition edges are only recorded when asked for: re-read.
        if state["graph"] is not None:
            analyze()

    view.on_node = focus_on
    view.on_line = show_line
    browse.clicked.connect(on_browse)
    run.clicked.connect(analyze)
    path_edit.returnPressed.connect(analyze)
    search.textChanged.connect(lambda _t: fill_list())
    var_list.currentItemChanged.connect(lambda cur, _prev: on_var(cur))
    findings.itemClicked.connect(on_finding)
    trace.itemClicked.connect(on_trace)
    trace.itemDoubleClicked.connect(on_trace_double)
    depth.valueChanged.connect(lambda _v: refocus())
    conds.toggled.connect(lambda _on: on_conditions())

    page.analyze = analyze
    page.focus_on = focus_on
    page.show_line = show_line
    page.state = state
    return page
