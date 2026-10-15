"""
council_qt.tabs.var_graph — which variables in a script feed which, drawn.

For a bug hunt: open a script - or a folder of them, such as a GUI and the
scripts it calls - pick the variable whose value is wrong, and see what it was
made from (left of it) and what it goes on to make (right of it), each link
labelled with the file and line that makes it. Click a link or a trace row to
read that line in the source; click a node to make it the centre.

Across files, values are followed through imports and calls between the
scripts, GUI ports (self.ports.x.get() / .set()), subprocess launches into
sys.argv, and Thread(target=..., args=...). Imports of anything that is not
one of the opened scripts (numpy, PySide6, the standard library) are not
followed; a call into one links its arguments to its result.

The analysis is council_core.var_graph (static, `ast` only — the script is
never run or imported). It runs on a worker; the result comes back through the
bridge. Nothing here writes to disk.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QTextCursor, QTextFormat)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGraphicsPathItem,
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
ROLE_FILE = Qt.ItemDataRole.UserRole + 2     # the file that line is in

#: Most nodes drawn in one column before the rest are summarised.
COLUMN_CAP = 18
NODE_W, NODE_H, COL_GAP, ROW_GAP = 190, 30, 90, 12

KIND_COLOUR = {"variable": "#5b7fa6", "param": "#7ea16d", "attribute": "#a07cc0",
               "return": "#c9a227", "import": "#7a7575", "global": "#5b7fa6",
               "port": "#d32f2f", "argv": "#e0884a"}

#: A link between two different files is drawn in this colour.
CROSS_FILE = "#e0884a"

#: Paths in the path box are separated by this.
SEP = " ; "


class VarGraphActions:
    """What the tab asks of the application; a test can swap any of it."""

    def analyze(self, paths: List[str], conditions: bool) -> vgm.VarGraph:
        return vgm.analyze_paths(paths, conditions)

    def pick_files(self, parent) -> List[str]:
        from .. import dialogs
        return dialogs.askopenfilenames("Open Python scripts",
                                        filetypes=[("Python", "*.py *.pyw")],
                                        parent=parent)

    def pick_export(self, parent, suggested: str) -> str:
        from .. import dialogs
        return dialogs.asksaveasfilename(
            "Export the graph", initialfile=suggested, defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("SVG drawing", "*.svg")],
            parent=parent)

    def pick_folder(self, parent) -> str:
        from .. import dialogs
        return dialogs.askdirectory("Open a folder of scripts", parent=parent)


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
        label = var.name if var.kind != "return" else f"{var.local_scope}() ⏎"
        if var.module and var.kind != "port":
            label += f" · {var.module.rsplit('.', 1)[-1]}"
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
        self.setToolTip(f"{var.label}\n{var.kind}"
                        + (f" in {Path(var.file).name}" if var.file else "")
                        + f"\nwritten on {vgm._lines(var.defined)}"
                        f"\nread on {vgm._lines(var.used)}"
                        + (f"\nalso: {', '.join(var.elsewhere[:8])}"
                           if var.elsewhere else ""))

    def mousePressEvent(self, event):                      # noqa: N802
        self._on_click(self.var.id)
        event.accept()


class _Link(QGraphicsPathItem):
    def __init__(self, path: QPainterPath, edges: List[vgm.Edge], on_click,
                 cross: bool = False):
        super().__init__(path)
        self.edges = edges
        self._on_click = on_click
        dashed = all(e.kind == "condition" for e in edges)
        pen = QPen(QColor(CROSS_FILE if cross else "#9a9090"), 2 if cross else 1.4)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(-1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("\n".join(f"{Path(e.file).name}:{e.line} {e.kind}: {e.code}"
                                  for e in edges[:6]))

    def shape(self):                                       # wider hit area
        s = QPainterPathStroker()
        s.setWidth(8)
        return s.createStroke(self.path())

    def mousePressEvent(self, event):                      # noqa: N802
        self._on_click(self.edges[0].line, self.edges[0].file)
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
        self.on_line = lambda line, file=None: None
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
            cross = bool(g.modules) and g.vars[s].file != g.vars[d].file \
                and g.vars[s].kind != "port" and g.vars[d].kind != "port"
            scene.addItem(_Link(path, edges, self.on_line,
                                cross or any(e.kind in ("argv", "import") for e in edges)))
        rect = scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        scene.setSceneRect(rect)
        self.resetTransform()
        vp = self.viewport()
        fit = min(vp.width() / rect.width(), vp.height() / rect.height(), 1.0)
        # Shrink to fit, but never past legible; past that, scroll.
        f = max(fit, 0.6)
        self.scale(f, f)
        self.centerOn(self.nodes[focus])


#: Pixels per scene unit in an exported PNG: sharp on a high-DPI screen or
#: pasted into a document, without a huge file.
EXPORT_SCALE = 2.0
#: Room above the graph for its caption, and around everything.
CAPTION_H, EXPORT_MARGIN = 34, 24


def export_graph(view: "GraphView", path: str, caption: str = "") -> Path:
    """Write what the graph shows to `path`: PNG, or SVG when the name ends
    in .svg. The whole graph, not just the part scrolled into view, on the
    tab's own background, with `caption` (what was traced) above it. Written
    to a temp file in the same folder and swapped in, so a failed export
    never leaves half an image where a good one was."""
    scene = view.scene()
    if not view.nodes:
        raise ValueError("there is no graph to export yet")
    out = Path(path)
    svg = out.suffix.lower() == ".svg"
    if not svg and out.suffix.lower() != ".png":
        out = out.with_suffix(out.suffix + ".png") if out.suffix else out.with_suffix(".png")
    source = scene.itemsBoundingRect()
    top = CAPTION_H if caption else 0
    w = source.width() + 2 * EXPORT_MARGIN
    h = source.height() + 2 * EXPORT_MARGIN + top
    tokens = theme.tokens("dark")

    def paint(painter: QPainter, scale: float) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(QRectF(0, 0, w * scale, h * scale), QColor(tokens["bg"]))
        if caption:
            f = QFont()
            f.setPixelSize(int(15 * scale))
            painter.setFont(f)
            painter.setPen(QColor(tokens["fg"]))
            painter.drawText(QRectF(EXPORT_MARGIN * scale, EXPORT_MARGIN * scale / 2,
                                    (w - 2 * EXPORT_MARGIN) * scale, top * scale),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             caption)
        target = QRectF(EXPORT_MARGIN * scale, (EXPORT_MARGIN + top) * scale,
                        source.width() * scale, source.height() * scale)
        scene.render(painter, target, source)

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".")
    os.close(fd)
    try:
        if svg:
            # Drawn into memory, then written here: a QSvgGenerator given a
            # file name keeps that file open until the generator is
            # destroyed, and Windows will not rename an open file — every
            # SVG export failed there ("being used by another process") and
            # left its temp file behind.
            from PySide6.QtCore import QBuffer, QIODevice
            from PySide6.QtSvg import QSvgGenerator
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            gen = QSvgGenerator()
            gen.setOutputDevice(buffer)
            gen.setSize(QSize(int(w), int(h)))
            gen.setViewBox(QRectF(0, 0, w, h))
            gen.setTitle(caption or "Variable Map")
            gen.setDescription("Exported from the Council's Variable Map")
            painter = QPainter(gen)
            paint(painter, 1.0)
            painter.end()
            buffer.close()
            Path(tmp).write_bytes(bytes(buffer.data()))
        else:
            image = QImage(int(w * EXPORT_SCALE), int(h * EXPORT_SCALE),
                           QImage.Format.Format_ARGB32)
            image.fill(QColor(tokens["bg"]))
            painter = QPainter(image)
            paint(painter, EXPORT_SCALE)
            painter.end()
            if not image.save(tmp, "PNG"):
                raise OSError(f"could not write {out}")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def suggested_name(var: vgm.Var) -> str:
    """'total_variable_map.png', from the traced variable's name."""
    base = var.name if var.kind != "return" else f"{var.local_scope}_return"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._") or "graph"
    return f"{base}_variable_map.png"


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
                                        f"{vgm._at(g, e)} {e.kind}", e.code])
                item.setData(0, ROLE_VAR, nxt)
                item.setData(0, ROLE_LINE, e.line)
                item.setData(0, ROLE_FILE, e.file)
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
    state: Dict[str, object] = {"graph": None, "focus": None, "file": None}
    layout = QVBoxLayout(page)

    row = QHBoxLayout()
    path_edit = QLineEdit(page)
    path_edit.setPlaceholderText("A .py script, or a folder of scripts "
                                 f"(several: separate with '{SEP.strip()}')")
    path_edit.setToolTip("Every script in a folder is read together, so a "
                         "value can be followed from a GUI into the scripts "
                         "it calls. Outside packages are not followed.")
    row.addWidget(path_edit, 1)
    pick_files = QPushButton(amp("📄 Scripts…"), page)
    pick_files.setToolTip("Choose one or more .py files")
    row.addWidget(pick_files)
    pick_folder = QPushButton(amp("📁 Folder…"), page)
    pick_folder.setToolTip("Choose a folder: every .py in it, and below it")
    row.addWidget(pick_folder)
    add_more = QPushButton(amp("➕ Add"), page)
    add_more.setToolTip("Add more scripts to the ones already chosen - e.g. a "
                        "script the GUI calls that lives somewhere else")
    row.addWidget(add_more)
    run = QPushButton(amp("🔗 Map variables"), page)
    run.setObjectName("analyze")
    row.addWidget(run)
    export = QPushButton(amp("🖼 Export image…"), page)
    export.setObjectName("export")
    export.setToolTip("Save the graph as a PNG or SVG image - all of it, not "
                      "only the part on screen")
    export.setEnabled(False)
    row.addWidget(export)
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

    status = QLabel("Open a script, or a folder of scripts, to see how their "
                    "variables feed each other.", page)
    status.setStyleSheet(f"color: {tokens['muted_fg']};")
    status.setWordWrap(True)
    layout.addWidget(status)

    split = QSplitter(Qt.Orientation.Horizontal, page)
    layout.addWidget(split, 1)

    # left: file filter + search + variables + findings
    left = QWidget(split)
    lv = QVBoxLayout(left)
    lv.setContentsMargins(0, 0, 0, 0)
    file_filter = QComboBox(left)
    file_filter.setObjectName("files")
    file_filter.setToolTip("Show the variables of one file only")
    file_filter.addItem("All files", None)
    file_filter.setVisible(False)
    lv.addWidget(file_filter)
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
                        "replaced before use, scripts that do not parse.")
    lv.addWidget(findings, 1)

    # middle: graph over trace
    mid = QSplitter(Qt.Orientation.Vertical, split)
    view = GraphView(mid)
    tabs = QTabWidget(mid)
    trace = QTreeWidget(tabs)
    trace.setObjectName("trace")
    trace.setHeaderLabels(["Variable", "How", "Line"])
    trace.setColumnWidth(0, 300)
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
    split.setSizes([300, 1100])

    page.view, page.trace, page.code = view, trace, code
    page.var_list, page.findings, page.status = var_list, findings, status
    page.path_edit, page.conditions, page.depth = path_edit, conds, depth
    page.file_filter, page.search = file_filter, search
    page.export_button = export

    # -- behaviour -------------------------------------------------------
    def open_file(file: Optional[str]) -> None:
        """Put `file`'s source in the Source tab, if it is not there."""
        g = state["graph"]
        if g is None or not file or file == state["file"]:
            return
        code.setPlainText(g.source_of(file) or g.source)
        state["file"] = file
        tabs.setTabText(tabs.indexOf(code), f"Source — {Path(file).name}")

    def show_line(line: int, file: Optional[str] = None) -> None:
        """Highlight `line` of `file` (the one shown, if None) and scroll
        to it."""
        open_file(file)
        sels = []
        block = code.document().findBlockByNumber(max(line - 1, 0))
        if block.isValid() and line > 0:
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(tokens["selection_bg"]))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            sels.append(sel)
            code.setTextCursor(QTextCursor(block))
            code.centerCursor()
        code.setExtraSelections(sels)
        tabs.setCurrentWidget(code)

    def focus_on(vid: str) -> None:
        g = state["graph"]
        if g is None or vid not in g.vars:
            return
        state["focus"] = vid
        c = conds.isChecked()
        view.show_graph(g, vid, depth.value(), c)
        export.setEnabled(True)
        fill_trace(trace, g, vid, depth.value(), c)
        tabs.setCurrentWidget(trace)
        v = g.vars[vid]
        up = g.upstream(vid, None, c)
        down = g.downstream(vid, None, c)
        files = {s.var.file for s in up + down if s.var.file} - {v.file}
        across = f" across {len(files) + 1} files" if files else ""
        where = f" in {Path(v.file).name}" if g.modules and v.file else ""
        others = sorted(dict.fromkeys(v.elsewhere), key=lambda x: x.startswith("read"))
        lines = [f"written on {vgm._lines(v.defined)}"] if v.defined else []
        lines += [f"read on {vgm._lines(v.used)}"] if v.used else []
        lines += [", ".join(others[:6]) + ("…" if len(others) > 6 else "")] if others else []
        status.setText(f"{v.label} — {v.kind}{where}"
                       + (f"; {'; '.join(lines)}" if lines else "")
                       + f". {len(up)} variables can affect it; it can "
                       f"affect {len(down)}{across}.")
        for i in range(var_list.count()):
            if var_list.item(i).data(ROLE_VAR) == vid:
                var_list.blockSignals(True)
                var_list.setCurrentRow(i)
                var_list.blockSignals(False)
                break
        open_file(v.file)
        code.setExtraSelections([])

    def fill_list() -> None:
        g = state["graph"]
        var_list.clear()
        if g is None:
            return
        text = search.text().strip()
        only = file_filter.currentData()
        vs = g.find(text) if text else sorted(
            g.vars.values(), key=lambda v: (v.kind != "port", v.module,
                                            v.local_scope != vgm.MODULE,
                                            v.local_scope, v.name))
        for v in vs:
            if only and v.file != only:
                continue
            item = QListWidgetItem(v.label)
            item.setData(ROLE_VAR, v.id)
            item.setForeground(QColor(KIND_COLOUR.get(v.kind, tokens["fg"])).lighter(140))
            item.setToolTip(f"{v.kind}"
                            + (f" in {Path(v.file).name}" if v.file else "")
                            + f"; written on {vgm._lines(v.defined)}; "
                            f"read on {vgm._lines(v.used)}"
                            + (f"; also {', '.join(v.elsewhere[:6])}"
                               if v.elsewhere else ""))
            var_list.addItem(item)

    def deliver(g: Optional[vgm.VarGraph], error: str) -> None:
        run.setEnabled(True)
        if g is None:
            status.setText(error)
            window.set_status("Variable map failed")
            return
        state["graph"] = g
        state["file"] = None
        open_file(g.files[0] if g.files else None)
        if not g.files:
            code.setPlainText(g.source)
        file_filter.blockSignals(True)
        file_filter.clear()
        file_filter.addItem("All files", None)
        for mod in sorted(g.modules):
            file_filter.addItem(f"{mod}  ({Path(g.modules[mod]).name})", g.modules[mod])
        file_filter.setVisible(bool(g.modules))
        file_filter.blockSignals(False)
        fill_list()
        findings.clear()
        for f in g.findings:
            at = f"{Path(f.file).name}:{f.line}" if g.modules else f"L{f.line}"
            item = QListWidgetItem(f"{at}  {f.message}")
            item.setData(ROLE_LINE, f.line)
            item.setData(ROLE_FILE, f.file)
            item.setData(ROLE_VAR, f"{f.scope}::{f.name}")
            findings.addItem(item)
        what = f"{len(g.modules)} files" if g.modules else Path(g.filename).name
        status.setText(f"{what}: {vgm.summary(g)}. Pick a variable to trace it.")
        window.set_status("Variable map ready")
        prev = state["focus"]
        if isinstance(prev, str) and prev in g.vars:
            focus_on(prev)
        else:
            view.scene().clear()
            view.nodes = {}
            trace.clear()
            export.setEnabled(False)

    def chosen_paths() -> List[str]:
        return [p.strip().strip('"') for p in path_edit.text().split(SEP.strip())
                if p.strip().strip('"')]

    def analyze() -> None:
        paths = chosen_paths()
        if not paths:
            status.setText("Choose a script or a folder first.")
            return
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            status.setText(f"No such file or folder: {missing[0]}")
            return
        run.setEnabled(False)
        status.setText("Reading the scripts…")
        window.set_status("Mapping variables …")
        c = conds.isChecked()

        def work() -> None:
            try:
                g, err = actions.analyze(paths, c), ""
            except SyntaxError as exc:
                g, err = None, (f"{Path(exc.filename or '').name or 'The script'} "
                                f"does not parse (line {exc.lineno}): {exc.msg}")
            except FileNotFoundError as exc:
                g, err = None, str(exc)
            except Exception as exc:                       # noqa: BLE001
                g, err = None, f"Could not read it: {exc}"
            window.bridge.call_on_ui(lambda: deliver(g, err))

        threading.Thread(target=work, name="var-graph", daemon=True).start()

    def set_paths(paths: List[str], add: bool = False) -> None:
        if not paths:
            return
        current = chosen_paths() if add else []
        merged = current + [p for p in paths if p not in current]
        path_edit.setText(SEP.join(merged))
        if not add:
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
        show_line(item.data(ROLE_LINE), item.data(ROLE_FILE))

    def on_trace(item: QTreeWidgetItem, _col: int) -> None:
        line = item.data(0, ROLE_LINE)
        if line:
            show_line(line, item.data(0, ROLE_FILE))

    def on_trace_double(item: QTreeWidgetItem, _col: int) -> None:
        vid = item.data(0, ROLE_VAR)
        if vid:
            focus_on(vid)

    def refocus() -> None:
        if isinstance(state["focus"], str):
            focus_on(state["focus"])

    def on_export() -> None:
        g, vid = state["graph"], state["focus"]
        if g is None or not isinstance(vid, str) or vid not in g.vars:
            return
        v = g.vars[vid]
        path = actions.pick_export(page, suggested_name(v))
        if not path:
            return
        where = (f" in {Path(v.file).name}" if g.modules and v.file else
                 f" in {Path(g.filename).name}" if not g.modules else "")
        caption = (f"{v.label}{where} — what feeds it (left) and what it "
                   f"feeds (right), {depth.value()} "
                   f"hop{'s' if depth.value() != 1 else ''}")
        try:
            out = export_graph(view, path, caption)
        except Exception as exc:                           # noqa: BLE001
            status.setText(f"Could not export the graph: {exc}")
            window.set_status("Export failed")
            return
        status.setText(f"Saved the graph to {out}")
        window.set_status(f"Exported {out.name}")

    def on_conditions() -> None:
        # Condition edges are only recorded when asked for: re-read.
        if state["graph"] is not None:
            analyze()

    view.on_node = focus_on
    view.on_line = show_line
    pick_files.clicked.connect(lambda: set_paths(actions.pick_files(page)))
    pick_folder.clicked.connect(
        lambda: set_paths([p] if (p := actions.pick_folder(page)) else []))
    add_more.clicked.connect(lambda: set_paths(actions.pick_files(page), add=True))
    run.clicked.connect(analyze)
    export.clicked.connect(on_export)
    path_edit.returnPressed.connect(analyze)
    search.textChanged.connect(lambda _t: fill_list())
    file_filter.currentIndexChanged.connect(lambda _i: fill_list())
    var_list.currentItemChanged.connect(lambda cur, _prev: on_var(cur))
    findings.itemClicked.connect(on_finding)
    trace.itemClicked.connect(on_trace)
    trace.itemDoubleClicked.connect(on_trace_double)
    depth.valueChanged.connect(lambda _v: refocus())
    conds.toggled.connect(lambda _on: on_conditions())

    page.analyze = analyze
    page.set_paths = set_paths
    page.focus_on = focus_on
    page.export = on_export
    page.show_line = show_line
    page.state = state
    return page
