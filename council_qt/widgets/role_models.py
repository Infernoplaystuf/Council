"""
council_qt.widgets.role_models — which model answers for each role.

The Models tab's second half. A model choice per council role — including
"Docs", the model that answers from a documentation server and writes code
from what it read — the Balanced preset (Phi-4 14B for the thinking roles,
Llama 3.2 3B for the quick ones), "Suggest for this PC" (the best INSTALLED
US-origin models, nothing downloaded), "one model for all", and a line saying
where each model will run — GPU while it fits, CPU after
(council_core.model_slots.plan) — or that Ollama runs it.

TWO KINDS OF MODEL IN ONE LIST
GGUF files in the model folders, and the models a localhost Ollama server
already has (council_core.local_models). On the RTX 4070 Laptop this was built
on, the council env has no llama-cpp-python, so the Ollama models are the
ones that actually answer. Each entry says who made it and whether it is
US-origin; a non-US model is listed as "not US — measure only" and is never
suggested, but the user may still pick it.

WHAT SAVE DOES
Writes vault/model_slots.json, makes the Writer's model the active GGUF (the
"main" slot follows COUNCIL_GGUF_PATH, so the rest of the app — Download &
switch, the title bar, the Tk shell — keeps meaning the same thing), and tells
the engine to drop its loaded models. The next question loads the new map.
When the Writer's choice is an Ollama model there is no file to make active:
main names the Ollama model and COUNCIL_GGUF_PATH is left alone.

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
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QVBoxLayout, QWidget)

from council_core import local_models, model_jobs, model_slots, paths

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


def entry_label(entry: Dict[str, Any],
                names: Optional[Dict[str, str]] = None) -> str:
    """A combo label for a list_local_models() entry: who made it, whether
    it is US-origin, and which runtime serves it."""
    if entry.get("backend") == "gguf" and entry.get("path"):
        base = display_name(Path(entry["path"]), names)
        tail = local_models.origin_label(entry.get("origin") or "unknown")
        return f"{base} — {entry.get('maker', 'unknown')} · {tail} · GGUF"
    return local_models.describe(entry)


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

    def models(self) -> List[Dict[str, Any]]:
        """Every runnable model — GGUF files AND the localhost Ollama
        server's. BLOCKING (a header read per new file, one /api/tags
        request): call it on a worker."""
        cfg = self.config()
        also = [self.main_path()] + [s.path for s in cfg.slots.values()]
        return local_models.list_local_models(also=also)

    def hardware(self) -> Tuple[Optional[float], Optional[float]]:
        """(total VRAM GB, RAM GB) for ranking suggestions. BLOCKING."""
        hw = getattr(model_jobs, "_DETECTED", None)
        if hw is not None and (hw.vram_gb or hw.ram_gb):
            return hw.vram_gb, hw.ram_gb
        try:
            import hardware_detect
            return hardware_detect.quick_memory()
        except Exception:                                 # noqa: BLE001
            return None, None

    def role_files(self) -> Dict[str, str]:
        """Each council role's current model file (or "ollama:<name>")."""
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

    def last_call(self) -> Dict[str, Any]:
        """The engine's last_call_stats(), when the engine is loaded — never
        imported just for this."""
        engine = sys.modules.get("council_engine")
        fn = getattr(engine, "last_call_stats", None) if engine else None
        try:
            return dict(fn() or {}) if callable(fn) else {}
        except Exception:                                 # noqa: BLE001
            return {}

    # -- writing -----------------------------------------------------------
    def save(self, role_files: Dict[str, str]) -> str:
        """Persist the map; returns the status line. BLOCKING (engine)."""
        cfg, main_file = model_slots.from_role_files(role_files)
        model_slots.save(self.vault_dir, cfg)
        # Why the Writer's model was not saved for the next launch, or "".
        # onboarding leaves a backend_settings.json it cannot read as it is
        # rather than replace it (MEASURED 2026-09-29: replacing it dropped
        # gguf_path, clip_path and role_models), and this line is the only
        # place the user would hear of it — "Saved" alone would be false the
        # next time the app starts.
        main_not_saved = ""
        if main_file and not model_slots.is_ollama(main_file):
            import onboarding
            main_not_saved = onboarding.save_gguf_path(self.vault_dir,
                                                       main_file) or ""
        try:
            import council_engine
            council_engine.refresh_backend_config()
        except Exception as exc:                          # noqa: BLE001
            return (f"Saved, but the engine could not reload ({exc}); "
                    "restart the app to use it."
                    + (f" The main model was NOT saved for the next "
                       f"launch: {main_not_saved}" if main_not_saved else ""))
        n = len(cfg.slots)
        if main_not_saved:
            return (f"Saved — {n} model{'s' if n != 1 else ''} for the "
                    "council from the next question, but the Writer's model "
                    f"was NOT saved for the next launch: {main_not_saved}")
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


def stats_line(stats: Dict[str, Any]) -> str:
    """'Last answer: llama3.1:8b via Ollama — 812 tokens at 41.2 tok/s ...'
    from last_call_stats(); "" when nothing has answered yet."""
    if not stats or not stats.get("backend") or stats["backend"] == "unknown":
        return ""
    bits = [f"Last answer: {stats.get('model') or '?'} via "
            f"{'Ollama' if stats['backend'] == 'ollama' else 'GGUF'}"]
    gen, rate = stats.get("gen_tokens"), stats.get("gen_tok_s")
    if gen:
        bits.append(f"{gen} tokens" + (f" at {rate:g} tok/s" if rate else ""))
    prompt, prate = stats.get("prompt_tokens"), stats.get("prompt_tok_s")
    if prompt:
        bits.append(f"prompt {prompt}" + (f" at {prate:g} tok/s"
                                          if prate else ""))
    if stats.get("seconds") is not None:
        bits.append(f"{stats['seconds']:.1f} s")
    if stats.get("constrained"):
        bits.append(f"output constrained ({stats.get('constraint')})")
    if stats.get("schema_valid") is False:
        bits.append("reply did NOT match its schema")
    return " — ".join(bits[:1]) + (" — " + ", ".join(bits[1:])
                                   if len(bits) > 1 else "")


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
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._hw: Tuple[Optional[float], Optional[float]] = (None, None)
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
        self.suggest_btn = self._button(
            row, "💡 Suggest for this PC (installed, US-made)",
            self.on_suggest)
        self.balanced_btn = self._button(
            row, "⚖ Balanced (Phi-4 14B + Llama 3.2 3B, 16 GB card)",
            self.on_balanced)
        self._button(row, "One model for all", self.on_one_model)
        self._button(row, "↻ Rescan", self.reload)
        row.addStretch(1)
        self.save_btn = self._button(row, "💾 Save", self.on_save)
        outer.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

        self.stats_label = QLabel("")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.stats_label)

    # -- reading -----------------------------------------------------------
    def reload(self) -> None:
        """Rescan files and the current map now; Ollama's models, free VRAM
        and the hardware on a worker (each can take a moment)."""
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
            self.status.setText("No GGUF files yet — Ollama models appear "
                                "here as soon as the scan finishes.")
        elif problem:
            self.status.setText(problem)
        self.stats_label.setText(stats_line(self.actions.last_call()))
        self._update_plan()

        def work() -> None:
            free = self.actions.free_vram()
            try:
                models = list(self.actions.models())
            except Exception:                             # noqa: BLE001
                models = []
            try:
                hw = self.actions.hardware()
            except Exception:                             # noqa: BLE001
                hw = (None, None)
            self._to_ui(self._got_scan, free, models, hw)

        threading.Thread(target=work, name="model-roles-vram",
                         daemon=True).start()

    def _got_scan(self, free: Optional[int], models: List[Dict[str, Any]],
                  hw: Tuple[Optional[float], Optional[float]]) -> None:
        self._free = free
        self._hw = hw or (None, None)
        self._entries = {model_slots._key(m["id"]): m for m in models}
        for combo in self.combos.values():
            keep = combo.currentData() or ""
            combo.blockSignals(True)
            have = {model_slots._key(combo.itemData(i))
                    for i in range(combo.count())}
            for m in models:
                key = model_slots._key(m["id"])
                if key not in have:
                    combo.addItem(entry_label(m, self._names), m["id"])
                    have.add(key)
            # Relabel what the file scan added, now that origin is known.
            for i in range(combo.count()):
                e = self._entries.get(model_slots._key(combo.itemData(i)))
                if e is not None:
                    combo.setItemText(i, entry_label(e, self._names))
            self._select(combo, keep)
            combo.blockSignals(False)
        n_ollama = sum(1 for m in models if m.get("backend") == "ollama")
        if n_ollama and (not self.status.text()
                         or self.status.text().startswith("No GGUF files")):
            self.status.setText(f"{n_ollama} Ollama model(s) found on this "
                                "PC.")
        self._update_plan()

    def _select(self, combo: QComboBox, path: str) -> None:
        if not path:
            return
        want = model_slots._key(path)
        for i in range(combo.count()):
            if model_slots._key(combo.itemData(i)) == want:
                combo.setCurrentIndex(i)
                return
        if model_slots.is_ollama(path):
            e = self._entries.get(want)
            label = (entry_label(e, self._names) if e else
                     f"{local_models.ollama_name(path)} — Ollama (not "
                     "found yet)")
        else:
            label = display_name(Path(path), self._names)
        combo.addItem(label, path)
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
        ollama_bits: List[str] = []
        warn: List[str] = []
        for name, slot in cfg.slots.items():
            ident = slot.path or main
            if model_slots.is_ollama(ident):
                e = self._entries.get(model_slots._key(ident)) or {}
                where = local_models.fit(e, *self._hw) if e else "unknown"
                size = (e.get("size_bytes") or 0) / GB
                ollama_bits.append(
                    f"{local_models.ollama_name(ident)} → Ollama "
                    + (f"({size:.1f} GB, " if size else "(")
                    + {"gpu": "fits the GPU", "partial": "part GPU, part RAM",
                       "cpu": "CPU", "too big": "too big for this PC",
                       "unknown": "size unknown"}.get(where, where) + ")")
                if e and e.get("origin") != "US":
                    warn.append(f"{e.get('name')} is "
                                f"{local_models.origin_label(e.get('origin'))}")
                continue
            path = Path(ident)
            try:
                sizes[name] = path.stat().st_size
            except OSError:
                continue
            labels[name] = display_name(path, self._names).split("  (")[0]
            e = self._entries.get(model_slots._key(str(path)))
            if e and e.get("origin") != "US":
                warn.append(f"{labels[name]} is "
                            f"{local_models.origin_label(e.get('origin'))}")
        gguf_cfg = model_slots.SlotConfig(
            {n: s for n, s in cfg.slots.items() if n in sizes} or
            {model_slots.MAIN: model_slots.Slot(model_slots.MAIN)},
            {r: s for r, s in cfg.roles.items() if s in sizes})
        placements = model_slots.plan(gguf_cfg, sizes, self._free) \
            if sizes else {}
        line = model_slots.summary(placements, labels, self._free)
        if ollama_bits:
            line = "   ".join(([line] if line else []) + ollama_bits)
            if len({b.split(' → ')[0] for b in ollama_bits}) > 1:
                line += ("   ·   several Ollama models: each switch between "
                         "them reloads weights unless all fit at once")
        if sizes and self._free is None:
            line += "   ·   (free GPU memory not known yet)"
        if warn:
            line += "   ·   " + "; ".join(warn)
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

    def on_suggest(self) -> None:
        """Fill every role from what is INSTALLED: the best US-origin model
        for this PC per role (local_models.rank_for_role — a full GPU fit
        first, native tool calling for Docs). Nothing is downloaded or
        saved; the user reviews and presses Save."""
        models = list(self._entries.values())
        if not models:
            self.status.setText("Still scanning for installed models — try "
                                "again in a moment (or ↻ Rescan).")
            return
        picks = model_slots.suggest_role_models(
            models, vram_gb=self._hw[0], ram_gb=self._hw[1],
            roles=tuple(self.combos))
        if not picks:
            self.status.setText(
                "No installed US-origin model to suggest. Install one "
                "yourself — e.g. `ollama pull llama3.1:8b` — then ↻ Rescan.")
            return
        self.apply_role_files({r: mid for r, (mid, _why) in picks.items()})
        writer_id, why = picks.get("writer") or next(iter(picks.values()))
        docs = picks.get("docs")
        name = (self._entries.get(model_slots._key(writer_id)) or {}).get(
            "name", writer_id)
        line = f"Suggested {name} ({why})"
        if docs and docs[0] != writer_id:
            dname = (self._entries.get(model_slots._key(docs[0])) or {}).get(
                "name", docs[0])
            line += f"; Docs: {dname} ({docs[1]})"
        self.status.setText(line + " — Save to use it.")

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
        self.suggest_btn.setEnabled(not busy)
