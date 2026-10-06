"""
council_qt.tabs.council_map — the council as a graph: who talks to whom.

A node per role, helper agent, context supplier, store, model and machine; a
line per link, saying what travels on it. Drawn force-directed, the way
Morphik draws its knowledge graph: drag the background to pan, the wheel to
zoom, drag a node to move it. Hover a node to light up its links; click it
to read every link in and out, with the code it comes from.

"How it works" is what a new user sees first: a step-by-step tour of one
question through the council, how a model is actually called, how the
machines talk to each other and how you talk to all of it, each step lighting
up its part of the map (council_core.council_map.GUIDE).

The colour of a line is its status (see council_core.council_map): grey
live, amber partial, red dashed broken, green dashed proposed — the green
lines are the map's suggestions for what would make the council better.
"What's missing" lists every line that is not live.

Read-only. The topology is in council_core.council_map; the only live reads
are the model-slot file (on open) and, when Refresh is pressed, one probe of
the Ollama hosts on a worker thread.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QSplitter, QVBoxLayout,
                               QWidget)

from council_core import council_map as cm

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.graph_canvas import KIND_COLOURS, GraphCanvas

class CouncilMapTab(ViewHelpers, QWidget):
    """Controls on top, the graph on the left, details on the right."""

    def __init__(self, window=None, probe=None, gather=None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self._tokens = theme.tokens("dark")
        #: Injected by tests; the real ones read the slot file / the hosts.
        self._gather = gather or cm.gather
        self._busy = False
        #: The "How it works" step on show, or None. New users start on it.
        self.guide_step: Optional[int] = 0
        self.map = self._gather(probe=False)
        self._build()
        self.redraw(relayout=True)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        blurb = QLabel(
            "How the council's agents connect: what each one receives, from "
            "whom, and what it passes on. Green dashed lines are links that "
            "do not exist yet and would improve the council; red ones are "
            "wired but never take effect. Click a node "
            "for its links and the code they come from.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.layer_boxes: Dict[str, QCheckBox] = {}
        for layer in cm.LAYERS:
            box = QCheckBox(amp(cm.LAYER_LABELS[layer]))
            box.setChecked(True)
            box.toggled.connect(lambda _c: self.redraw(relayout=False))
            self.layer_boxes[layer] = box
            row.addWidget(box)
        row.addStretch(1)
        outer.addLayout(row)

        row2 = QHBoxLayout()
        self.gaps_only = QCheckBox("Only what is not live")
        self.gaps_only.toggled.connect(lambda _c: self.redraw(relayout=False))
        row2.addWidget(self.gaps_only)
        self._button(row2, "Re-layout", lambda: self.redraw(relayout=True))
        self._button(row2, "Fit", lambda: self.canvas.fit())
        self._button(row2, "How it works", lambda: self.show_guide(0))
        self._button(row2, "What's missing", self.show_gaps)
        self.refresh_btn = self._button(
            row2, "Refresh models & machines", self.refresh)
        self._button(row2, "Placement review…", self.open_placement)
        row2.addStretch(1)
        row2.addWidget(self._legend())
        outer.addLayout(row2)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = GraphCanvas()
        self.canvas.node_clicked.connect(self.show_node)
        split.addWidget(self.canvas)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        self.guide_nav = QWidget()
        nav = QHBoxLayout(self.guide_nav)
        nav.setContentsMargins(0, 0, 0, 0)
        self.guide_back = self._button(nav, "◀ Back", self.guide_prev)
        self.guide_label = QLabel("")
        self.guide_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self.guide_label, 1)
        self.guide_next = self._button(nav, "Next ▶", self.guide_forward)
        sv.addWidget(self.guide_nav)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        sv.addWidget(self.details, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([900, 320])
        outer.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

    def _legend(self) -> QLabel:
        dots = " ".join(
            f"<span style='color:{KIND_COLOURS[k]}'>●</span> "
            f"{cm.KIND_LABELS[k]}" for k in cm.KINDS)
        t = self._tokens
        lines = (f"<span style='color:{t['muted_fg']}'>━ live</span> "
                 f"<span style='color:{t['warning']}'>━ partial</span> "
                 f"<span style='color:{t['error']}'>┅ broken</span> "
                 f"<span style='color:{t['success']}'>┅ proposed</span>")
        label = QLabel(f"{dots}<br>{lines}")
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    # ------------------------------------------------------------------
    def visible_edges(self) -> List[cm.Edge]:
        layers = [k for k, box in self.layer_boxes.items() if box.isChecked()]
        statuses = ([s for s in cm.STATUSES if s != "live"]
                    if self.gaps_only.isChecked() else None)
        return self.map.visible_edges(layers, statuses)

    def redraw(self, relayout: bool = False) -> None:
        edges = self.visible_edges()
        self.canvas.set_graph(self.map, edges, relayout=relayout)
        gaps = self.map.gaps()
        counts = {s: sum(1 for e in gaps if e.status == s)
                  for s in ("broken", "partial", "proposed")}
        summary = (f"{len(self.map.nodes)} nodes, {len(edges)} links shown; "
                   + ", ".join(f"{n} {s}" for s, n in counts.items() if n)
                   + ".")
        notes = " ".join(self.map.notes)
        self.status.setText(summary + (f"  {notes}" if notes else ""))
        if self.guide_step is not None:
            # Re-applied after a refresh: the models and machines it lights
            # up may have changed.
            self.show_guide(self.guide_step)
        elif self.canvas.selected:
            self.show_node(self.canvas.selected)
        elif not self.details.toPlainText():
            self.show_gaps()

    # -- the details panel: a node, the gaps, or the guided tour ---------
    def show_node(self, node_id: str) -> None:
        if not node_id:
            if self.guide_step is not None:
                self.show_guide(self.guide_step)     # a stray click: stay
            else:
                self.show_gaps()
            return
        self._leave_guide()
        self.details.setPlainText(
            cm.describe(self.map, node_id))

    def show_gaps(self) -> None:
        self._leave_guide()
        self.details.setPlainText(
            cm.gaps_report(self.map))

    def show_guide(self, index: int) -> None:
        """One step of "How it works", with its part of the map lit up."""
        index = max(0, min(len(cm.GUIDE) - 1, index))
        self.guide_step = index
        self.canvas.selected = None
        self.canvas.set_highlight(cm.guide_nodes(self.map, cm.GUIDE[index]))
        self.details.setPlainText(cm.guide_text(index))
        self.guide_label.setText(f"{index + 1} / {len(cm.GUIDE)}")
        self.guide_back.setEnabled(index > 0)
        self.guide_next.setEnabled(index < len(cm.GUIDE) - 1)
        self.guide_nav.setVisible(True)

    def guide_prev(self) -> None:
        self.show_guide((self.guide_step or 0) - 1)

    def guide_forward(self) -> None:
        self.show_guide((self.guide_step or 0) + 1)

    def _leave_guide(self) -> None:
        self.guide_step = None
        self.guide_nav.setVisible(False)
        self.canvas.set_highlight(())

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the slots and probe every Ollama host, on a worker."""
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)
        self.status.setText("Asking the machines what they have…")

        def work() -> None:
            try:
                m = self._gather(probe=True)
                self._to_ui(lambda: self._show_map(m))
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="council-map-probe",
                         daemon=True).start()

    def _show_map(self, m: cm.CouncilMap) -> None:
        self.map = m
        self.redraw(relayout=False)

    def _done(self) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)


    def open_placement(self) -> None:
        """The weekly placement review: the controller's proposal, Apply."""
        from ..widgets.placement_review import PlacementReviewDialog
        dialog = PlacementReviewDialog(self.window or self)
        dialog.finished.connect(lambda _r: self._after_placement())
        dialog.open()
        self._placement_dialog = dialog

    def _after_placement(self) -> None:
        # Applied role changes rewrite model_slots.json: show the new roles,
        # without probing the machines again.
        self._show_map(self._gather(probe=False))


def build_council_map(window) -> QWidget:
    """Factory for the tab registry."""
    return CouncilMapTab(window)
