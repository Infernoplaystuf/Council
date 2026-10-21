"""
council_qt.tabs.ledger_dialogs — dialogs the Ledger tab opens.

Kept apart from the tab module because the all-tabs harness resolves every
``self.x`` reference against the TAB, and a dialog's own ``self.accept``
is not the tab's. The pattern mirrors ``connections_dialogs.py``.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem,
                               QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from council_core import ollama_install

from ..view import hand_to_qt


class _Signals(QObject):
    progress = Signal(object)
    done = Signal(object, object)       # (final PullProgress or None, error-text or '')


class InstallVisionModelDialog(QDialog):
    """Pick a US-origin vision model from the catalog and install it.

    The dialog never talks to Ollama on the GUI thread — the ``installed``
    probe and the pull both run on a worker, and the progress callback hops
    back through a Qt signal.

    ``on_installed`` is called with the chosen model's name once the pull
    completes successfully (the Ledger tab reacts by refreshing its reader
    dropdown)."""

    def __init__(self, on_installed: Optional[Callable[[str], None]] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install an image reader")
        self.resize(560, 420)
        self._on_installed = on_installed
        self._worker: Optional[threading.Thread] = None
        self._installed_names: List[str] = []
        self.signals = _Signals()
        self.signals.progress.connect(self._on_progress)
        self.signals.done.connect(self._on_done)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Choose a US-origin vision model to install. The Council never "
            "downloads a model on its own; nothing happens until you click "
            "Install.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentItemChanged.connect(self._refresh_details)
        root.addWidget(self.list, 1)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        root.addWidget(self.details)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        progress_row.addWidget(self.progress, 1)
        self.progress_text = QLabel("")
        progress_row.addWidget(self.progress_text)
        root.addLayout(progress_row)

        buttons_row = QHBoxLayout()
        self.install_btn = QPushButton("Install")
        self.install_btn.setEnabled(False)
        self.install_btn.clicked.connect(self.on_install)
        buttons_row.addWidget(self.install_btn)
        buttons_row.addStretch(1)
        root.addLayout(buttons_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
        hand_to_qt(self)          # PySide6 6.12: see view.hand_to_qt

        self._load_catalog()

    # ────────────────────────────────────────────────────────────── catalog

    def _load_catalog(self) -> None:
        try:
            self._installed_names = ollama_install.installed_models()
        except Exception:                                   # noqa: BLE001
            self._installed_names = []
        self.list.clear()
        for m in ollama_install.catalog():
            tag = ollama_install.is_installed(m.name, self._installed_names)
            star = " · recommended" if m.recommended else ""
            marker = "✓ installed" if tag else "not installed"
            item = QListWidgetItem(
                f"{m.label}{star}\n   {marker} · {m.download_gb:g} GB · "
                f"~{m.approx_vram_gb:g} GB VRAM")
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.list.addItem(item)
        self._refresh_details()

    def _refresh_details(self, *_args) -> None:
        model = self._current_model()
        if model is None:
            self.details.setText("")
            self.install_btn.setEnabled(False)
            return
        installed = ollama_install.is_installed(model.name, self._installed_names)
        extra = " (already installed)" if installed else ""
        self.details.setText(f"{model.name}{extra} — {model.description}")
        self.install_btn.setEnabled(
            not installed and self._worker is None)
        self.install_btn.setText("Install" if not installed else "Already installed")

    def _current_model(self) -> Optional[ollama_install.VisionModel]:
        item = self.list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # ────────────────────────────────────────────────────────────── install

    def on_install(self) -> None:
        model = self._current_model()
        if model is None or self._worker is not None:
            return
        name = model.name
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Installing…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)                 # busy until a layer sends total
        self.progress_text.setText("asking Ollama to pull the model…")

        def emit_progress(p):
            self.signals.progress.emit(p)

        def work():
            final = None
            err = ""
            try:
                final = ollama_install.pull(name, on_progress=emit_progress,
                                            timeout=1800)
            except Exception as exc:                        # noqa: BLE001
                err = str(exc)
            self.signals.done.emit(final, err)

        # Prefix must match a TAB_WORKERS entry so CouncilWindow's close
        # drain picks it up — see tests/test_council_qt_foundation.py.
        self._worker = threading.Thread(target=work, name="ledger-ollama-pull",
                                        daemon=True)
        self._worker.start()

    def _on_progress(self, p) -> None:
        if p.fraction is not None:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(100 * p.fraction))
        else:
            self.progress.setRange(0, 0)
        text = p.status or ""
        if p.total:
            mb_done = p.completed / (1024 * 1024)
            mb_total = p.total / (1024 * 1024)
            text += f"   {mb_done:.0f} / {mb_total:.0f} MB"
        self.progress_text.setText(text)

    def _on_done(self, final, err) -> None:
        self._worker = None
        self.progress.setVisible(False)
        if err:
            self.progress_text.setText(f"install failed: {err}")
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Install")
            return
        model = self._current_model()
        if model is None:
            return
        if getattr(final, "status", "").lower() != "success":
            self.progress_text.setText(
                f"install ended with status: {getattr(final, 'status', '?')}")
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Install")
            return
        self.progress_text.setText(f"installed {model.name}.")
        # Refresh install state across the list and tell the Ledger tab.
        self._load_catalog()
        for i in range(self.list.count()):
            item = self.list.item(i)
            m = item.data(Qt.ItemDataRole.UserRole)
            if m is not None and m.name == model.name:
                self.list.setCurrentItem(item)
                break
        if self._on_installed is not None:
            try:
                self._on_installed(model.name)
            except Exception as exc:                        # noqa: BLE001
                print(f"[ledger_dialogs] on_installed hook raised: {exc!r}",
                      flush=True)


class PickModelDialog(QDialog):
    """Which downloaded model answers the Ledger's questions (user,
    2026-10-19: asking failed when no Ollama model was loaded and no GGUF
    was set - ask the user instead, from every model on the computer).

    Lists the GGUF files in Council's model folders and the models this
    PC's Ollama has (council_core.ledger_llm.available_models, on a worker:
    Ollama is never asked on the GUI thread). A non-US-origin model is
    listed but cannot be picked. "Another GGUF file…" picks one anywhere
    on disk. ``model_id`` is the choice: a GGUF path or 'ollama:<name>'."""

    _listed = Signal(object, str)

    def __init__(self, current: str = "", lister: Optional[Callable] = None,
                 browse: Optional[Callable[[], str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Which model should Council use?")
        self.resize(640, 380)
        self.model_id = ""
        self._current = current
        self._browse = browse
        root = QVBoxLayout(self)
        intro = QLabel("Pick a model downloaded on this computer. Only labels and short "
                       "cell text are sent to it, and it runs here (or on your own Pi "
                       "nodes for an Ollama model when Council's remote nodes are on).")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._ok())
        self.list.currentItemChanged.connect(lambda *_a: self._refresh())
        root.addWidget(self.list, 1)
        self.message = QLabel("Looking for models…")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        row = QHBoxLayout()
        self.browse_btn = QPushButton("Another GGUF file…")
        self.browse_btn.clicked.connect(self.on_browse)
        row.addWidget(self.browse_btn)
        row.addStretch(1)
        root.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._listed.connect(self._fill)
        hand_to_qt(self)          # PySide6 6.12: see view.hand_to_qt
        self._refresh()
        lister = lister or self._default_lister

        def work():
            try:
                self._listed.emit(lister(), "")
            except Exception as exc:                        # noqa: BLE001
                self._listed.emit([], f"could not list the models: {exc}")
        threading.Thread(target=work, name="ledger-model-list", daemon=True).start()

    @staticmethod
    def _default_lister():
        from council_core import ledger_llm
        return ledger_llm.available_models()

    def _add(self, model_id: str, label: str, usable: bool = True) -> QListWidgetItem:
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, model_id)
        if not usable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled
                          & ~Qt.ItemFlag.ItemIsSelectable)
        self.list.addItem(item)
        return item

    def _fill(self, models, err: str) -> None:
        self.list.clear()
        first = remembered = None
        for m in models or []:
            usable = bool(m.get("usable", True))
            item = self._add(str(m.get("id") or ""), str(m.get("label") or m.get("id")),
                             usable)
            if usable:
                first = first or item
                if m.get("id") == self._current:
                    remembered = item           # last time's choice, when still here
        if remembered or first:
            self.list.setCurrentItem(remembered or first)
        usable = sum(1 for m in models or [] if m.get("usable", True))
        if err:
            self.message.setText(err)
        elif not models:
            self.message.setText(
                "No downloaded models found: no GGUF files in Council's model folders and "
                "no Ollama models (or Ollama is not running). Download one in the Models "
                "tab, run `ollama pull granite3.3:8b`, or pick a GGUF file below.")
        else:
            self.message.setText(f"{usable} model(s) Council can use.")
        self._refresh()

    def on_browse(self) -> None:
        path = self._browse() if self._browse else ""
        if not path:
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(self, "A GGUF model file", "",
                                                  "GGUF models (*.gguf)")
        if path:
            import os
            item = self._add(path, f"{os.path.basename(path)} — GGUF file you picked")
            self.list.setCurrentItem(item)
            self._refresh()

    def _refresh(self) -> None:
        item = self.list.currentItem()
        ok = item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled) \
            and bool(item.data(Qt.ItemDataRole.UserRole))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _ok(self) -> None:
        item = self.list.currentItem()
        if item is None or not item.data(Qt.ItemDataRole.UserRole) \
                or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        self.model_id = str(item.data(Qt.ItemDataRole.UserRole))
        self.accept()


def pick_model(parent=None) -> str:
    """Ask which model to use (last choice preselected, and remembered);
    '' when the user cancels."""
    from council_core import ledger_llm
    dlg = PickModelDialog(current=ledger_llm.remembered_model(), parent=parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.model_id:
        return ""
    ledger_llm.remember_model(dlg.model_id)
    return dlg.model_id


class PreviewWorkbookDialog(QDialog):
    """Show what the Ledger extractor can see in one source workbook —
    per-sheet candidate part ids, inspectors, confident label→field
    bindings, weak suggestions, and unmatched label cells.

    The expensive work (``source_extract.preview_workbook``) runs on a
    worker thread; the GUI shows a placeholder until it finishes.

    No value extraction happens — this is a *dry run* so the user can
    tell whether a workbook is extraction-ready before kicking off a
    long batch."""

    _done = Signal(object, str)         # (WorkbookPreview or None, error text)
    _llm_done = Signal(object, str, str) # (mapping dict, model name, error)

    def __init__(self, path, schema, aliases=None,
                 pattern_mask: Optional[str] = None,
                 csv_path: Optional[str] = None, parent=None,
                 pick_model: Optional[Callable] = None, ignore=(), part: str = ""):
        super().__init__(parent)
        #: asks which model to use; tests pass their own
        self._pick_model = pick_model or globals()["pick_model"]
        self.setWindowTitle(f"Preview: {path}")
        self.resize(760, 560)
        self._schema = schema
        self._csv_path = csv_path
        self._existing_aliases = dict(aliases or {})
        self._report = None
        self._suggestions: Dict[str, Optional[str]] = {}
        self._model_used = ""
        self._done.connect(self._on_done)
        self._llm_done.connect(self._on_llm_done)

        root = QVBoxLayout(self)
        intro = QLabel(
            f"Dry run against {path}. Nothing is extracted. The report "
            "shows what the pipeline would see — fix the labels / aliases "
            "the matcher misses before running the batch.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(font)
        self.text.setPlainText("Reading the workbook…")
        root.addWidget(self.text, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        root.addWidget(self.progress)

        actions = QHBoxLayout()
        self.ask_btn = QPushButton("Ask Council for mappings")
        self.ask_btn.setToolTip(
            "Send unmatched LABELS (never values) to a model you pick from the "
            "ones downloaded on this computer (GGUF files or Ollama models; "
            "US-origin only). The reply suggests a template field for each "
            "label; nothing is saved on the model's say-so.")
        self.ask_btn.setEnabled(False)
        self.ask_btn.clicked.connect(self.on_ask_llm)
        actions.addWidget(self.ask_btn)
        self.save_btn = QPushButton("Save suggestions as aliases")
        self.save_btn.setToolTip(
            "Write the suggested label → field mappings to this parent "
            "CSV's aliases file. Pre-existing aliases are kept; only the "
            "labels with a non-null suggestion are added.")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.on_save_aliases)
        actions.addWidget(self.save_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
        hand_to_qt(self)          # PySide6 6.12: see view.hand_to_qt

        self._start_worker(path, schema, aliases, pattern_mask, ignore, part)

    def _start_worker(self, path, schema, aliases, pattern_mask, ignore=(),
                      part: str = "") -> None:
        csv_path = self._csv_path

        def work():
            report = None
            err = ""
            try:
                from source_extract import load_patterns, load_taught, preview_workbook
                # the same ignore list, Part, and what Teach taught (the
                # inspectors' and flaw-code labels, the saved pattern when
                # the field is empty) the extract uses
                taught = load_taught(csv_path) if csv_path else None
                mask = pattern_mask or ((load_patterns(csv_path) or [None])[0]
                                        if csv_path else None)
                # the saved pattern is a hint, as Extract reads it
                report = preview_workbook(path, schema, aliases=aliases,
                                          pattern_mask=mask,
                                          ignore=ignore, part=part, taught=taught,
                                          pattern_strict=bool(pattern_mask))
            except Exception as exc:                        # noqa: BLE001
                err = f"preview failed: {exc!r}"
            self._done.emit(report, err)

        threading.Thread(target=work, name="ledger-preview",
                         daemon=True).start()

    def _on_done(self, report, err: str) -> None:
        self.progress.setVisible(False)
        if err:
            self.text.setPlainText(err)
            return
        if report is None:
            self.text.setPlainText("No preview — the reader returned nothing.")
            return
        self._report = report
        self.text.setPlainText(report.describe())
        # Enable "Ask Council" if there are any unmatched labels AND we
        # have a schema that gives the model something to map onto.
        any_unmatched = any(s.unmatched_labels for s in report.sheets)
        self.ask_btn.setEnabled(any_unmatched and self._schema is not None)

    # -- LLM mapping -------------------------------------------------
    def _unmatched_labels(self) -> List[str]:
        if self._report is None:
            return []
        seen: Dict[str, None] = {}
        for s in self._report.sheets:
            for label, _where in s.unmatched_labels:
                seen.setdefault(label, None)
        return list(seen)

    def on_ask_llm(self) -> None:
        if self._report is None:
            return
        labels = self._unmatched_labels()
        if not labels:
            return
        # which model: the user picks one of the models on the computer (it
        # failed when Council had no GGUF set and Ollama none loaded)
        model_id = self._pick_model(self)
        if not model_id:
            return
        self.ask_btn.setEnabled(False)
        self.ask_btn.setText("Asking…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        schema = self._schema

        def work():
            mapping: Dict[str, Optional[str]] = {}
            model = ""
            err = ""
            try:
                from council_core import ledger_llm
                from source_extract import suggest_mappings_for_labels
                mapping, model = suggest_mappings_for_labels(
                    labels, schema, chat=ledger_llm.chat_for(model_id))
            except Exception as exc:                        # noqa: BLE001
                err = str(exc)
            self._llm_done.emit(mapping, model, err)

        threading.Thread(target=work, name="ledger-llm-mapping",
                         daemon=True).start()

    def _on_llm_done(self, mapping, model: str, err: str) -> None:
        self.progress.setVisible(False)
        self.ask_btn.setText("Ask Council for mappings")
        # asked again (another model) after an answer too: it stayed
        # disabled after a good one (review, 2026-10-19)
        self.ask_btn.setEnabled(True)
        if err:
            self.text.appendPlainText("\n--- Council mapping ---\n"
                                       f"skipped: {err}")
            return
        self._suggestions = dict(mapping or {})
        self._model_used = model
        confirmed = {k: v for k, v in self._suggestions.items() if v}
        unknown = sorted(k for k, v in self._suggestions.items() if not v)
        lines = ["", f"--- Council mapping (model: {model}) ---"]
        if confirmed:
            lines.append(f"Suggestions ({len(confirmed)}):")
            for label in sorted(confirmed):
                lines.append(f"  {label!r} → {confirmed[label]}")
        else:
            lines.append("The model offered no mappings.")
        if unknown:
            lines.append(f"No suggestion ({len(unknown)}):")
            for label in unknown[:40]:
                lines.append(f"  {label!r}")
            if len(unknown) > 40:
                lines.append(f"  …and {len(unknown) - 40} more.")
        self.text.appendPlainText("\n".join(lines))
        self.save_btn.setEnabled(bool(confirmed) and bool(self._csv_path))

    def on_save_aliases(self) -> None:
        if not self._suggestions or not self._csv_path:
            return
        confirmed = {k: v for k, v in self._suggestions.items() if v}
        if not confirmed:
            return
        try:
            from source_extract import load_aliases, save_aliases
            existing_map, existing_ignore = load_aliases(self._csv_path)
            merged = dict(existing_map)
            added = 0
            for label, field in confirmed.items():
                if existing_map.get(label) != field:
                    merged[label] = field
                    added += 1
            save_aliases(self._csv_path, merged, existing_ignore)
        except Exception as exc:                            # noqa: BLE001
            self.text.appendPlainText(f"\nSave failed: {exc!r}")
            return
        self.text.appendPlainText(
            f"\nSaved {added} alias{'' if added == 1 else 'es'} to the "
            "parent CSV's aliases file. Rerun the extract to see the "
            "newly-bound columns.")
        self.save_btn.setEnabled(False)
