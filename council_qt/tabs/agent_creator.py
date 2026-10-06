"""
council_qt.tabs.agent_creator — make an agent: a model, connected roles, tools.

Left: your agents. Middle: the selected agent — its name, which model answers
for it (by role), which roles are connected to it (they review the tools the
council builds for it), its read-only built-in tools, the council-made tools
attached to it, a step budget and instructions. Right: ask the council for a
tool in plain words, follow the council's work, and approve or reject what it
built; then try the agent on a goal.

The setting at the top decides what happens when the council finishes a tool:
"Wait for my approval" (the default) or "Add automatically when every
reviewer approves". Automatic never attaches a tool a reviewer objected to or
whose sandbox test failed — those always wait.

All logic is council_core.agent_profiles; this view collects choices and runs
the council and the agent on worker threads ("agent-creator-*").
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget)

from council_core import agent_profiles as ap
from council_core import paths
from council_core.model_slots import COUNCIL_ROLES, ROLE_LABELS

from .. import dialogs, theme
from ..view import ViewHelpers, amp

ATTACH_CHOICES = (("approve", "Wait for my approval"),
                  ("automatic", "Add automatically when every reviewer approves"))
STATUS_TEXT = {"drafting": "being written", "reviewing": "being reviewed",
               "waiting": "waiting for you", "attached": "attached",
               "rejected": "rejected", "failed": "failed"}


def role_label(role: str) -> str:
    lab = ROLE_LABELS.get(role)
    return f"{role} — {lab}" if lab else role


class AgentCreatorActions:
    """What the tab asks of the application; tests pass a fake council."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 chat: Optional[Callable] = None, runner_factory=None,
                 confirm: Optional[Callable[[str, str], bool]] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.store = ap.ProfileStore(self.vault_dir)
        self.chat = chat or ap.default_chat
        self.runner_factory = runner_factory
        self._confirm = confirm

    def confirm(self, title: str, message: str) -> bool:
        if self._confirm is not None:
            return self._confirm(title, message)
        if dialogs.disabled():
            return False
        return dialogs.askyesno(title, message)

    def build_tool(self, profile_id, description, on_progress):
        return ap.build_tool(self.store, profile_id, description, chat=self.chat,
                             on_progress=on_progress)

    def run(self, profile_id, goal, on_step=None):
        runner = self.runner_factory() if self.runner_factory else None
        return ap.run_profile(self.store, profile_id, goal, runner=runner, on_step=on_step)


class AgentCreatorTab(ViewHelpers, QWidget):

    def __init__(self, window=None, actions: Optional[AgentCreatorActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or AgentCreatorActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._pid: Optional[str] = None
        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        top.addWidget(QLabel("When the council finishes a tool:"))
        self.attach_box = QComboBox()
        for _k, text in ATTACH_CHOICES:
            self.attach_box.addItem(text)
        self.attach_box.currentIndexChanged.connect(self.on_attach_mode)
        top.addWidget(self.attach_box)
        top.addStretch(1)
        self.status = QLabel("")
        top.addWidget(self.status)
        outer.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        # left: agents
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.profiles = QListWidget()
        self.profiles.currentItemChanged.connect(self._on_profile)
        ll.addWidget(self.profiles, 1)
        row = QHBoxLayout()
        self._button(row, "+ New agent", self.on_new)
        self.delete_btn = self._button(row, "Delete", self.on_delete)
        ll.addLayout(row)
        split.addWidget(left)

        # middle: the agent
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow("Name", self.name)
        self.description = QLineEdit()
        form.addRow("What it is for", self.description)
        self.model_role = QComboBox()
        for r in COUNCIL_ROLES:
            self.model_role.addItem(role_label(r), r)
        form.addRow("Model (answers as)", self.model_role)
        self.steps = QSpinBox()
        self.steps.setRange(1, 20)
        form.addRow("Step budget", self.steps)
        ml.addLayout(form)

        roles_box = QGroupBox("Connected roles (they review the tools the council builds)")
        rl = QHBoxLayout(roles_box)
        self.role_checks = {}
        for r in COUNCIL_ROLES:
            if r == ap.DRAFTER_ROLE:
                continue
            cb = QCheckBox(r)
            cb.setToolTip(ROLE_LABELS.get(r, r))
            self.role_checks[r] = cb
            rl.addWidget(cb)
        ml.addWidget(roles_box)

        tools_box = QGroupBox("Built-in tools (read-only)")
        tl = QVBoxLayout(tools_box)
        self.tool_checks = {}
        for name, desc in ap.BUILTIN_TOOLS.items():
            cb = QCheckBox(amp(f"{name} — {desc}"))
            self.tool_checks[name] = cb
            tl.addWidget(cb)
        ml.addWidget(tools_box)

        att_box = QGroupBox("Council-made tools attached to this agent")
        al = QVBoxLayout(att_box)
        self.attached = QListWidget()
        self.attached.setMaximumHeight(110)
        al.addWidget(self.attached)
        arow = QHBoxLayout()
        self.detach_btn = self._button(arow, "Detach", self.on_detach)
        arow.addStretch(1)
        al.addLayout(arow)
        ml.addWidget(att_box)

        ml.addWidget(QLabel("Instructions"))
        self.instructions = QPlainTextEdit()
        self.instructions.setMaximumHeight(90)
        ml.addWidget(self.instructions)
        srow = QHBoxLayout()
        self.save_btn = self._button(srow, "Save agent", self.on_save)
        srow.addStretch(1)
        ml.addLayout(srow)
        split.addWidget(mid)

        # right: council tools + try it
        right = QWidget()
        rl2 = QVBoxLayout(right)
        rl2.setContentsMargins(0, 0, 0, 0)
        rl2.addWidget(QLabel("Ask the council for a tool"))
        self.tool_request = QPlainTextEdit()
        self.tool_request.setPlaceholderText(
            "e.g. Count how many work orders are On Hold for each part number")
        self.tool_request.setMaximumHeight(70)
        rl2.addWidget(self.tool_request)
        brow = QHBoxLayout()
        self.build_btn = self._button(brow, "Build it", self.on_build_tool)
        self.progress = QLabel("")
        self.progress.setWordWrap(True)
        brow.addWidget(self.progress, 1)
        rl2.addLayout(brow)
        self.requests = QListWidget()
        self.requests.setMaximumHeight(120)
        self.requests.currentItemChanged.connect(self._on_request)
        rl2.addWidget(self.requests)
        self.request_view = QPlainTextEdit()
        self.request_view.setReadOnly(True)
        rl2.addWidget(self.request_view, 1)
        drow = QHBoxLayout()
        self.approve_btn = self._button(drow, "✓ Approve and attach", self.on_approve)
        self.reject_btn = self._button(drow, "✗ Reject", self.on_reject)
        drow.addStretch(1)
        rl2.addLayout(drow)
        rl2.addWidget(QLabel("Try this agent"))
        trow = QHBoxLayout()
        self.goal = QLineEdit()
        self.goal.setPlaceholderText("A goal for the agent…")
        trow.addWidget(self.goal, 1)
        self.run_btn = self._button(trow, "Run", self.on_run)
        rl2.addLayout(trow)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(140)
        rl2.addWidget(self.output)
        split.addWidget(right)
        split.setSizes([200, 470, 470])
        outer.addWidget(split, 1)

    # ------------------------------------------------------------------
    def reload(self, select: Optional[str] = None) -> None:
        try:
            mode = self.actions.store.tool_attach_mode()
            profs = self.actions.store.profiles()
        except ap.ProfileStoreDamaged as exc:
            self.status.setText(str(exc))
            self.setEnabled(False)
            return
        self.attach_box.blockSignals(True)
        self.attach_box.setCurrentIndex([k for k, _t in ATTACH_CHOICES].index(mode))
        self.attach_box.blockSignals(False)
        want = select or self._pid
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for p in sorted(profs, key=lambda p: p.name.lower()):
            item = QListWidgetItem(p.name)
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.profiles.addItem(item)
            if p.id == want:
                self.profiles.setCurrentItem(item)
        self.profiles.blockSignals(False)
        cur = self.profiles.currentItem()
        self._show(cur.data(Qt.ItemDataRole.UserRole) if cur else None)

    def _on_profile(self, item, _prev=None) -> None:
        self._show(item.data(Qt.ItemDataRole.UserRole) if item else None)

    def _show(self, pid: Optional[str]) -> None:
        self._pid = pid
        prof = self.actions.store.get(pid) if pid else None
        has = prof is not None
        for w in (self.save_btn, self.delete_btn, self.build_btn, self.run_btn,
                  self.detach_btn):
            w.setEnabled(has and not self._busy)
        if not has:
            self.name.clear()
            self.description.clear()
            self.instructions.clear()
            self.attached.clear()
            self.requests.clear()
            self.request_view.clear()
            self._enable_decision(None)
            return
        self.name.setText(prof.name)
        self.description.setText(prof.description)
        idx = self.model_role.findData(prof.model_role)
        self.model_role.setCurrentIndex(max(0, idx))
        self.steps.setValue(int(prof.max_steps))
        for r, cb in self.role_checks.items():
            cb.setChecked(r in prof.connected_roles)
        for t, cb in self.tool_checks.items():
            cb.setChecked(t in prof.builtin_tools)
        self.instructions.setPlainText(prof.instructions)
        self.attached.clear()
        for t in prof.tools:
            item = QListWidgetItem(f"{t.name} — {t.description} "
                                   f"(approved by {t.approved_by})")
            item.setData(Qt.ItemDataRole.UserRole, t.name)
            self.attached.addItem(item)
        self._fill_requests()

    def _fill_requests(self, select: Optional[str] = None) -> None:
        self.requests.clear()
        for r in sorted(self.actions.store.requests(self._pid),
                        key=lambda r: -r.created_ts):
            item = QListWidgetItem(f"[{STATUS_TEXT.get(r.status, r.status)}] "
                                   f"{r.description[:70]}")
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            self.requests.addItem(item)
            if r.id == select:
                self.requests.setCurrentItem(item)
        if select is None:
            self.request_view.clear()
            self._enable_decision(None)

    def _on_request(self, item, _prev=None) -> None:
        req = self.actions.store.get_request(item.data(Qt.ItemDataRole.UserRole)) \
            if item else None
        self._enable_decision(req)
        if req is None:
            self.request_view.clear()
            return
        lines = [f"Request: {req.description}",
                 f"Status: {STATUS_TEXT.get(req.status, req.status)}"
                 + (f" (by {req.decided_by})" if req.decided_by else ""),
                 f"Tool: {req.tool_name or '—'}   Sandbox test: {req.test or '—'}",
                 f"Message: {req.message}", ""]
        for rv in req.reviews:
            verdict = "approves" if rv.approve and not rv.error else "objects"
            lines.append(f"Round {rv.round} · {rv.role} {verdict}"
                         + (f" — {rv.error}" if rv.error else ""))
            lines.extend(f"    - {c}" for c in rv.concerns)
        lines += ["", "Code (model-written; read it before approving):", "", req.code]
        self.request_view.setPlainText("\n".join(lines))

    def _enable_decision(self, req) -> None:
        waiting = req is not None and req.status == "waiting" and not self._busy
        self.approve_btn.setEnabled(bool(waiting and req.code))
        self.reject_btn.setEnabled(bool(waiting))
        self._req = req

    # ------------------------------------------------------------------
    def on_attach_mode(self, idx: int) -> None:
        mode = ATTACH_CHOICES[idx][0]
        self.actions.store.set_tool_attach_mode(mode)
        self.status.setText("Council-made tools will "
                            + ("be added automatically when every reviewer approves."
                               if mode == "automatic" else "wait for your approval."))

    def on_new(self) -> None:
        prof = self.actions.store.create("New agent")
        self.reload(select=prof.id)
        self.name.setFocus()
        self.name.selectAll()

    def on_save(self) -> None:
        prof = self.actions.store.get(self._pid) if self._pid else None
        if prof is None:
            return
        prof.name = self.name.text().strip() or prof.name
        prof.description = self.description.text().strip()
        prof.model_role = self.model_role.currentData()
        prof.max_steps = int(self.steps.value())
        prof.connected_roles = [r for r, cb in self.role_checks.items() if cb.isChecked()]
        prof.builtin_tools = [t for t, cb in self.tool_checks.items() if cb.isChecked()]
        prof.instructions = self.instructions.toPlainText().strip()
        self.actions.store.save(prof)
        self.status.setText(f"Saved '{prof.name}'.")
        self.reload(select=prof.id)

    def on_delete(self) -> None:
        prof = self.actions.store.get(self._pid) if self._pid else None
        if prof is None:
            return
        if not self.actions.confirm("Delete agent",
                                    f"Delete the agent '{prof.name}'? Its tools stay "
                                    "in Tool Creation; only this agent is removed."):
            return
        self.actions.store.delete_profile(prof.id)
        self._pid = None
        self.reload()

    def on_detach(self) -> None:
        item = self.attached.currentItem()
        if item is None or not self._pid:
            return
        self.actions.store.detach_tool(self._pid, item.data(Qt.ItemDataRole.UserRole))
        self._show(self._pid)

    def on_approve(self) -> None:
        req = getattr(self, "_req", None)
        if req is None:
            return
        try:
            self.actions.store.approve(req.id)
            self.status.setText(f"'{req.tool_name}' is attached.")
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not attach: {exc}")
        self._show(self._pid)
        self._fill_requests(select=req.id)

    def on_reject(self) -> None:
        req = getattr(self, "_req", None)
        if req is None:
            return
        self.actions.store.reject(req.id)
        self.status.setText("Rejected. The tool stays in Tool Creation, unattached.")
        self._fill_requests(select=req.id)

    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._show(self._pid)

    def on_build_tool(self) -> None:
        text = self.tool_request.toPlainText().strip()
        if self._busy or not self._pid or not text:
            if not text:
                self.progress.setText("Describe the tool you want first.")
            return
        pid = self._pid
        self._set_busy(True)
        self.progress.setText("Asking the council…")

        def work() -> None:
            try:
                req = self.actions.build_tool(
                    pid, text, lambda m: self._to_ui(self.progress.setText, m))
                self._to_ui(self._built, req, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._built, None, exc)

        threading.Thread(target=work, name="agent-creator-build", daemon=True).start()

    def _built(self, req, exc) -> None:
        self._set_busy(False)
        if exc is not None:
            self.progress.setText(f"The council could not build it: {exc}")
            return
        self.progress.setText(f"Done: {STATUS_TEXT.get(req.status, req.status)}. "
                              + (req.message or ""))
        self.tool_request.clear()
        self._fill_requests(select=req.id)

    def on_run(self) -> None:
        goal = self.goal.text().strip()
        if self._busy or not self._pid or not goal:
            return
        pid = self._pid
        self._set_busy(True)
        self.output.setPlainText("Running…")

        def step(ev, _run) -> None:
            what = (f"step {ev.step}: {ev.tool}({ev.args})" if ev.action == "tool"
                    else f"step {ev.step}: answer")
            self._to_ui(self.output.appendPlainText, what)

        def work() -> None:
            try:
                run = self.actions.run(pid, goal, on_step=step)
                self._to_ui(self._ran, run, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._ran, None, exc)

        threading.Thread(target=work, name="agent-creator-run", daemon=True).start()

    def _ran(self, run, exc) -> None:
        self._set_busy(False)
        if exc is not None:
            self.output.appendPlainText(f"Failed: {exc}")
            return
        self.output.appendPlainText(f"\n{run.final_answer}\n\n(stopped: "
                                    f"{run.stopped_reason}; tools used: "
                                    f"{', '.join(run.tools_used) or 'none'}"
                                    + (f"; asked for tools it does not have: "
                                       f"{', '.join(run.tools_missing)}"
                                       if run.tools_missing else "") + ")")


def build_agent_creator(window) -> QWidget:
    """Factory for the tab registry."""
    return AgentCreatorTab(window)
