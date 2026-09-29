"""
council_qt.widgets.role_models — which model answers for each role.

The Models tab's second half. A model choice per council role, the Balanced
preset (Phi-4 14B for the thinking roles, Llama 3.2 3B for the quick ones),
"one model for all", and a line saying where each model will run — GPU while
it fits, CPU after (council_core.model_slots.plan).

WHAT SAVE DOES
Writes vault/model_slots.json, makes the Writer's model the active GGUF (the
"main" slot follows COUNCIL_GGUF_PATH, so the rest of the app — Download &
switch, the title bar, the Tk shell — keeps meaning the same thing), and tells
the engine to drop its loaded models. The next question loads the new map.

THE ESTIMATE ADDS BACK WHAT IS ALREADY LOADED
Free VRAM is read now, while the council's current models may already occupy
the card. Counting that memory as unavailable would say "does not fit" for a
configuration that fits the moment Save releases the old models, so the
weights the engine reports on the GPU are added back.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QVBoxLayout, QWidget)

from council_core import model_jobs, model_slots, paths

from .. import dialogs, theme
from ..view import ViewHelpers, amp

GB = model_slots.GB


def _catalog_names() -> Dict[str, str]:
    """GGUF file name -> the catalog's human name."""
    try:
        import model_catalog
        return {m.hf_file.lower(): m.name for m in model_catalog.MODELS}
    except Exception:                                     # noqa: BLE001
        return {}


def display_name(path: Path, names: Optional[Dict[str, str]] = None) -> str:
    names = names if names is not None else _catalog_names()
    base = names.get(path.name.lower(), path.stem)
    try:
        size = path.stat().st_size / GB
        return f"{base}  ({size:.1f} GB)"
    except OSError:
        return f"{base}  (missing)"


def _nvidia_free_bytes() -> Optional[int]:
    """Free VRAM via nvidia-smi — the engine's fallback probe, without
    importing the engine (seconds) just to draw a label."""
    try:
        import shutil
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        out = subprocess.run(
            [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        first = out.strip().splitlines()[0].strip()
        return int(first) * 1024 * 1024 if first.isdigit() else None
    except Exception:                                     # noqa: BLE001
        return None


class RoleActions:
    """What the Roles section can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    # -- reading ---------------------------------------------------------
    def main_path(self) -> str:
        env = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
        if env:
            return env
        try:
            import onboarding
            return onboarding.load_gguf_path(self.vault_dir) or ""
        except Exception:                                 # noqa: BLE001
            return ""

    def config(self) -> model_slots.SlotConfig:
        return model_slots.load(self.vault_dir)

    def problem(self) -> str:
        return model_slots.problem(self.vault_dir)

    def files(self) -> List[Path]:
        cfg = self.config()
        also = [self.main_path()] + [s.path for s in cfg.slots.values()]
        return model_slots.known_files(also=also)

    def role_files(self) -> Dict[str, str]:
        """Each council role's current model file."""
        cfg, main = self.config(), self.main_path()
        return {role: cfg.slots[cfg.slot_for(role)].resolved_path(main)
                for role in model_slots.COUNCIL_ROLES}

    def free_vram(self) -> Optional[int]:
        """Free VRAM plus the weights the engine already has on the card."""
        free = _nvidia_free_bytes()
        if free is None:
            return None
        engine = sys.modules.get("council_engine")
        if engine is not None:
            try:
                for info in engine.slot_status().values():
                    if info.get("on_gpu"):
                        free += Path(info["path"]).stat().st_size
            except Exception:                             # noqa: BLE001
                pass
        return free

    # -- writing -----------------------------------------------------------
    def save(self, role_files: Dict[str, str]) -> str:
        """Persist the map; returns the status line. BLOCKING (engine)."""
        cfg, main_file = model_slots.from_role_files(role_files)
        model_slots.save(self.vault_dir, cfg)
        if main_file:
            import onboarding
            onboarding.save_gguf_path(self.vault_dir, main_file)
        try:
            import council_engine
            council_engine.refresh_backend_config()
        except Exception as exc:                          # noqa: BLE001
            return (f"Saved, but the engine could not reload ({exc}); "
                    "restart the app to use it.")
        n = len(cfg.slots)
        return (f"Saved — {n} model{'s' if n != 1 else ''} for the council. "
                "It takes effect on the next question.")

    def missing_for(self, preset: dict) -> List[str]:
        return model_slots.preset_missing(preset)

    def download(self, model_id: str, on_progress=None) -> Path:
        spec = model_slots.catalog_spec(model_id)
        return model_jobs.download(spec.hf_repo, spec.hf_file,
                                   on_progress=on_progress,
                                   size_gb=spec.size_gb)

    def preset_role_files(self, preset: dict) -> Dict[str, str]:
        files = {mid: model_slots.find_catalog_file(mid)
                 for mid in [preset["main"]] + list(preset["slots"].values())}
        main = str(files[preset["main"]])
        out = {role: main for role in model_slots.COUNCIL_ROLES}
        for role, slot in preset["roles"].items():
            out[role] = str(files[preset["slots"][slot]])
        return out


class RoleModelsPanel(ViewHelpers, QGroupBox):
    """A model per role, the presets, and where each model will run."""

    def __init__(self, actions: Optional[RoleActions] = None,
                 parent: Optional[QWidget] = None,
                 confirm: Callable[..., bool] = dialogs.askyesno,
                 auto_load: bool = True):
        super().__init__(amp("Which model answers for each role"), parent)
        self.actions = actions or RoleActions()
        self._confirm = confirm
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._free: Optional[int] = None
        self._names = _catalog_names()
        self.combos: Dict[str, QComboBox] = {}
        self._build()
        if auto_load:
            self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        grid = QGridLayout()
        for i, role in enumerate(model_slots.COUNCIL_ROLES):
            row, col = divmod(i, 2)
            grid.addWidget(QLabel(model_slots.ROLE_LABELS.get(role, role)),
                           row, col * 2)
            combo = QComboBox()
            combo.setMinimumWidth(260)
            combo.currentIndexChanged.connect(self._update_plan)
            grid.addWidget(combo, row, col * 2 + 1)
            self.combos[role] = combo
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        outer.addLayout(grid)

        self.plan_label = QLabel("")
        self.plan_label.setWordWrap(True)
        self.plan_label.setStyleSheet(f"color: {self._tokens['info']};")
        outer.addWidget(self.plan_label)

        row = QHBoxLayout()
        self.balanced_btn = self._button(
            row, "⚖ Balanced (Phi-4 14B + Llama 3.2 3B)", self.on_balanced)
        self._button(row, "One model for all", self.on_one_model)
        self._button(row, "↻ Rescan", self.reload)
        row.addStretch(1)
        self.save_btn = self._button(row, "💾 Save", self.on_save)
        outer.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

    # -- reading -----------------------------------------------------------
    def reload(self) -> None:
        """Rescan files and the current map; free VRAM on a worker."""
        files = self.actions.files()
        current = self.actions.role_files()
        for role, combo in self.combos.items():
            combo.blockSignals(True)
            combo.clear()
            for f in files:
                combo.addItem(display_name(f, self._names), str(f))
            self._select(combo, current.get(role, ""))
            combo.blockSignals(False)
        problem = self.actions.problem()
        if not files:
            self.status.setText("No models downloaded yet — use Balanced, "
                                "or download one above.")
        elif problem:
            self.status.setText(problem)
        self._update_plan()

        def work() -> None:
            free = self.actions.free_vram()
            self._to_ui(self._got_free, free)

        threading.Thread(target=work, name="model-roles-vram",
                         daemon=True).start()

    def _got_free(self, free: Optional[int]) -> None:
        self._free = free
        self._update_plan()

    def _select(self, combo: QComboBox, path: str) -> None:
        if not path:
            return
        want = model_slots._key(path)
        for i in range(combo.count()):
            if model_slots._key(combo.itemData(i)) == want:
                combo.setCurrentIndex(i)
                return
        combo.addItem(display_name(Path(path), self._names), path)
        combo.setCurrentIndex(combo.count() - 1)

    def role_files(self) -> Dict[str, str]:
        return {role: combo.currentData() or ""
                for role, combo in self.combos.items()}

    def _update_plan(self, *_args) -> None:
        chosen = self.role_files()
        if not any(chosen.values()):
            self.plan_label.setText("")
            return
        cfg, main = model_slots.from_role_files(chosen)
        sizes, labels = {}, {}
        for name in cfg.slots:
            path = Path(cfg.slots[name].path or main)
            try:
                sizes[name] = path.stat().st_size
            except OSError:
                continue
            labels[name] = display_name(path, self._names).split("  (")[0]
        placements = model_slots.plan(cfg, sizes, self._free)
        line = model_slots.summary(placements, labels, self._free)
        if self._free is None:
            line += "   ·   (free GPU memory not known yet)"
        self.plan_label.setText(line)

    # -- presets -----------------------------------------------------------
    def apply_role_files(self, role_files: Dict[str, str]) -> None:
        for role, path in role_files.items():
            if role in self.combos:
                self._select(self.combos[role], path)
        self._update_plan()

    def on_one_model(self) -> None:
        writer = self.combos["writer"].currentData()
        if writer:
            self.apply_role_files({r: writer for r in self.combos})
            self.status.setText("Every role on the Writer's model — Save to "
                                "use it.")

    def on_balanced(self) -> None:
        preset = model_slots.BALANCED
        missing = self.actions.missing_for(preset)
        if not missing:
            self.apply_role_files(self.actions.preset_role_files(preset))
            self.on_save()
            return
        specs = [model_slots.catalog_spec(m) for m in missing]
        total = sum(s.size_gb for s in specs)
        names = ", ".join(s.name for s in specs)
        if not self._confirm(
                "Download models",
                f"The Balanced preset needs {names} — about {total:.1f} GB "
                "to download. Download now?", parent=self):
            return
        self._download_then_apply(preset, missing)

    def _download_then_apply(self, preset: dict, missing: List[str]) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def work() -> None:
            try:
                for model_id in missing:
                    spec = model_slots.catalog_spec(model_id)

                    def progress(done, total, name=spec.name):
                        line = model_jobs.progress_line(done, total, name)
                        self._to_ui(self.status.setText, line)

                    self.actions.download(model_id, on_progress=progress)
                self._to_ui(self._downloaded, preset, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._downloaded, preset, exc)

        threading.Thread(target=work, name="model-roles-download",
                         daemon=True).start()

    def _downloaded(self, preset: dict, error) -> None:
        self._set_busy(False)
        if error is not None:
            self.status.setText(f"Download failed: {error}")
            return
        self.reload()
        self.apply_role_files(self.actions.preset_role_files(preset))
        self.on_save()

    # -- saving ------------------------------------------------------------
    def on_save(self) -> None:
        chosen = self.role_files()
        if not all(chosen.values()):
            self.status.setText("Pick a model for every role first.")
            return
        if self._busy:
            return
        self._set_busy(True)
        self.status.setText("Saving…")

        def work() -> None:
            try:
                line = self.actions.save(chosen)
            except Exception as exc:                      # noqa: BLE001
                line = f"Could not save: {exc}"
            self._to_ui(self._saved, line)

        threading.Thread(target=work, name="model-roles-save",
                         daemon=True).start()

    def _saved(self, line: str) -> None:
        self._set_busy(False)
        self.status.setText(line)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.save_btn.setEnabled(not busy)
        self.balanced_btn.setEnabled(not busy)
