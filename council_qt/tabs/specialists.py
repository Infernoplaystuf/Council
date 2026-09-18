"""
council_qt.tabs.specialists — named lenses on the shared vault.

A specialist is pure config: an id, an icon, a name, some domain keywords and a
system-prompt overlay. It owns NO data — there is exactly one knowledge pool,
the vault — and at query time the council either honours the Council tab's pin
or keyword-matches up to three and composes their overlays into the extra
context the base personality answers with.

THE ONE THING THAT CANNOT BE TRANSLATED FAITHFULLY
The Tk detail pane is DESTROYED AND REBUILT on every selection and on every
Enabled toggle — and the Enabled checkbox's own handler triggers one of those
rebuilds, so the widget currently executing destroys itself. Tk tolerates that.
Qt does not: deleting a QWidget inside its own signal handler is a
use-after-free.

So the form here is built ONCE and repopulated. Nothing is destroyed, so
nothing can be destroyed from inside its own handler, and the question does not
arise.

AND THE FORM REMEMBERS WHAT YOU TYPED
Selecting another specialist in Tk destroys the form and the unsaved edits with
it, silently. Here the draft is compared against what is stored and the user is
told before anything is lost.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit,
                               QSplitter, QVBoxLayout, QWidget, QCheckBox)
from PySide6.QtCore import Qt

from council_core import specialists_ops as ops
from council_core import paths

from .. import dialogs, theme
from ..view import ViewHelpers


class SpecialistsActions:
    """What the Specialists tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    def load(self):
        return ops.load(self.vault_dir, enabled_only=False)

    def save(self, draft):
        return ops.save_specialist(self.vault_dir, draft)

    def set_enabled(self, specialist_id, enabled):
        return ops.set_enabled(self.vault_dir, specialist_id, enabled)

    def delete(self, specialist_id):
        return ops.delete_specialist(self.vault_dir, specialist_id)

    def pool_counts(self):
        """How many files the shared knowledge pool holds."""
        try:
            import data_index
            in_dir = data_index.input_dir(self.vault_dir)
            return sum(1 for p in in_dir.rglob("*") if p.is_file()), in_dir
        except Exception:                                 # noqa: BLE001
            return 0, self.vault_dir


class SpecialistsTab(ViewHelpers, QWidget):
    """A list of specialists and one form, built once."""

    def __init__(self, window=None, actions=None, on_registry_changed=None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or SpecialistsActions()
        self.on_registry_changed = on_registry_changed
        self._tokens = theme.tokens("dark")
        self._listing = None
        self._current: Optional[ops.SpecialistDraft] = None
        self._loading = False          # suppress handlers while repopulating

        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        title = QLabel("Personal Specialists")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        outer.addWidget(title)
        subtitle = QLabel("Named lenses on your shared data. Auto-summoned "
                          "when your question matches their domain.")
        subtitle.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._list_side())
        split.addWidget(self._form_side())
        split.setSizes([300, 640])
        outer.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)
        outer.addWidget(self._pool_footer())

    def _list_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        heading = QLabel("Active specialists")
        heading.setStyleSheet("font-weight: bold;")
        row.addWidget(heading)
        row.addStretch(1)
        self._button(row, "➕ New", self.on_new)
        layout.addLayout(row)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.on_selected)
        layout.addWidget(self.list, 1)
        return panel

    def _form_side(self) -> QWidget:
        """The form, built ONCE.

        The Tk pane is destroyed and rebuilt per selection, and the Enabled
        checkbox's handler triggers one of those rebuilds — destroying the
        widget currently executing. In Qt that is a use-after-free.
        """
        box = QGroupBox("")
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        self.heading = QLabel("Select a specialist")
        self.heading.setStyleSheet("font-weight: bold; font-size: 14px;")
        header.addWidget(self.heading)
        header.addStretch(1)
        self.enabled = QCheckBox("Enabled")
        self.enabled.toggled.connect(self.on_enabled_toggled)
        header.addWidget(self.enabled)
        layout.addLayout(header)

        layout.addWidget(self._bold("Description"))
        self.description = QLineEdit()
        layout.addWidget(self.description)

        layout.addWidget(self._bold("Domain keywords  (comma-separated)"))
        layout.addWidget(self._hint(
            "Used to auto-summon this specialist when a question mentions one "
            "of these terms."))
        self.keywords = QLineEdit()
        layout.addWidget(self.keywords)

        layout.addWidget(self._bold("Lens / system prompt overlay"))
        layout.addWidget(self._hint(
            "Injected as extra context before the personality answers. Tell "
            "it how to think, not what to know."))
        self.overlay = QPlainTextEdit()
        self.overlay.setMinimumHeight(140)
        layout.addWidget(self.overlay, 1)

        base_row = QHBoxLayout()
        base_row.addWidget(self._bold("Base personality:"))
        self.base = QComboBox()
        self.base.addItems(ops.BASE_PERSONALITIES)
        base_row.addWidget(self.base)
        base_row.addWidget(self._hint("(which personality wears this lens)"))
        base_row.addStretch(1)
        layout.addLayout(base_row)

        buttons = QHBoxLayout()
        self._button(buttons, "💾 Save", self.on_save)
        self._button(buttons, "🗑 Delete", self.on_delete)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._set_form_enabled(False)
        return box

    def _pool_footer(self) -> QWidget:
        box = QGroupBox("Shared knowledge pool")
        row = QHBoxLayout(box)
        self.pool = QLabel("")
        self.pool.setStyleSheet(f"color: {self._tokens['info']};")
        row.addWidget(self.pool)
        row.addStretch(1)
        self._button(row, "📂 Open data_in", lambda: self._open("data_in"))
        self._button(row, "📂 Open data_out", lambda: self._open("data_out"))
        return box

    def _bold(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold;")
        return label

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        return label

    def _open(self, which: str) -> None:
        _count, in_dir = self.actions.pool_counts()
        target = Path(in_dir).parent / which
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not open it: {exc}")

    # ------------------------------------------------------------------
    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in (self.description, self.keywords, self.overlay,
                       self.base, self.enabled):
            widget.setEnabled(enabled)

    def refresh(self) -> None:
        self._listing = self.actions.load()
        self.list.clear()
        for specialist in self._listing.specialists:
            self.list.addItem(ops.list_label(specialist))
        self.status.setText(self._listing.message)
        count, _in_dir = self.actions.pool_counts()
        self.pool.setText(f"{count} file(s) in the shared pool — every "
                          "specialist reads the same vault.")
        if self.on_registry_changed is not None:
            self.on_registry_changed()

    def selected(self):
        row = self.list.currentRow()
        items = (self._listing.specialists if self._listing else [])
        return items[row] if 0 <= row < len(items) else None

    def draft_from_form(self) -> ops.SpecialistDraft:
        base = self._current or ops.SpecialistDraft()
        return ops.SpecialistDraft(
            id=base.id, name=base.name, icon=base.icon,
            description=self.description.text(),
            keywords=ops.parse_keywords(self.keywords.text()),
            overlay=self.overlay.toPlainText(),
            base=self.base.currentText(),
            enabled=self.enabled.isChecked())

    def has_unsaved_edits(self) -> bool:
        """Whether the form differs from what is stored.

        The Tk form has no such notion: selecting another specialist destroys
        it and the edits with it, silently.
        """
        if self._current is None:
            return False
        return self.draft_from_form() != self._current

    def on_selected(self, row: int) -> None:
        if self._loading:
            return
        specialist = self.selected()
        if specialist is None:
            return
        self._show(ops.SpecialistDraft.of(specialist))

    def _show(self, draft: ops.SpecialistDraft) -> None:
        """Repopulate the form. Nothing is created and nothing destroyed."""
        self._loading = True
        try:
            self._current = draft
            self.heading.setText(f"{draft.icon}  {draft.name}")
            self.description.setText(draft.description)
            self.keywords.setText(ops.format_keywords(draft.keywords))
            self.overlay.setPlainText(draft.overlay)
            index = self.base.findText(draft.base)
            self.base.setCurrentIndex(index if index >= 0 else 0)
            self.enabled.setChecked(draft.enabled)
            self._set_form_enabled(True)
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    def on_enabled_toggled(self, on: bool) -> None:
        """Turn one specialist on or off. Nothing else is written.

        The Tk handler saves the WHOLE specialist as the form currently shows
        it, so toggling Enabled commits whatever half-typed edits are in the
        boxes — and then rebuilds the pane, destroying the checkbox that is
        still executing.
        """
        if self._loading or self._current is None:
            return
        result = self.actions.set_enabled(self._current.id, on)
        self.status.setText(result.message)
        if result.ok:
            self._current = ops.SpecialistDraft(
                **{**self._current.__dict__, "enabled": on})
            # Refreshing the LIST is safe; the form is untouched.
            self._refresh_list_row(on)

    def _refresh_list_row(self, enabled: bool) -> None:
        row = self.list.currentRow()
        item = self.list.item(row)
        if item is not None and self._current is not None:
            tag = "✓" if enabled else "(off)"
            item.setText(f"{self._current.icon}  {self._current.name}  {tag}")

    def on_save(self) -> None:
        if self._current is None:
            self.status.setText("Select a specialist first.")
            return
        draft = self.draft_from_form()
        result = self.actions.save(draft)
        self.status.setText(result.message)
        if result.ok:
            self._current = draft
            self.refresh()

    def on_delete(self) -> None:
        if self._current is None:
            self.status.setText("Select a specialist first.")
            return
        if not dialogs.askyesno("Delete specialist",
                                ops.confirm_delete_text(self._current.name),
                                parent=self):
            return
        result = self.actions.delete(self._current.id)
        self.status.setText(result.message)
        if result.ok:
            self._current = None
            self._set_form_enabled(False)
            self.heading.setText("Select a specialist")
            self.refresh()

    def on_new(self) -> None:
        """Start a blank specialist in the form.

        No dialog: the form is already the editor, and a second one would be a
        second place for the field rules to drift.
        """
        self._show(ops.SpecialistDraft(name="New specialist"))
        self._current = ops.SpecialistDraft(name="New specialist")
        self.heading.setText("New specialist")
        self.description.setFocus()


def build_specialists(window) -> QWidget:
    """Factory for the tab registry."""
    return SpecialistsTab(window)
