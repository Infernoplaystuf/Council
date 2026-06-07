"""
sim_recorder.py — persistent storage for simulation runs.

Every ``SimRun`` produced by a runner (godot subprocess, in-process
python simulator, future backends) lands here. Records are flat JSON
files under ``vault/simulations/<sim_name>/<run_id>.json`` with a
lightweight index for fast listing.

Why JSON and not pickle / SQLite
--------------------------------
- The records are small (a few KB each at most — events are capped per
  run). JSON is human-inspectable and survives Python upgrades.
- The Sim Analyst (phase 3) reads these as a corpus; JSON keeps the
  read path stdlib-only.
- A SQLite layer can land later if record counts ever push into the
  tens of thousands. Until then, the per-file layout is also
  trivially git-friendly if the user wants to version their sweep
  results.

Concurrency
-----------
Single-writer per ``SimRecorder`` instance, guarded by ``threading.
RLock``. Multiple recorders can target the same directory safely
because each run's filename includes a UUID prefix.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Constants
# ============================================================

SIMS_SUBDIR = "simulations"
INDEX_FILENAME = "_index.json"
INDEX_SCHEMA_VERSION = 1

# Per-run caps so a long-running sim doesn't fill the disk with
# millions of events. The runner is expected to drop events past
# the cap rather than letting the file grow unbounded.
DEFAULT_MAX_EVENTS_PER_RUN = 10_000


# ============================================================
# Record shape
# ============================================================

def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _new_run_id() -> str:
    """Date-prefixed UUID so runs sort chronologically in the file
    system listing as well as in the index."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


@dataclass
class SimEvent:
    """A discrete event emitted during a run (death, level-up,
    spawn, etc.). Free-form payload."""
    t:    float             # seconds since run start
    name: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SimRun:
    """One simulation run's full record."""
    id:           str = field(default_factory=_new_run_id)
    sim_name:     str = ""             # e.g. "jump_curve_test"
    backend:      str = ""             # "godot" | "python"
    started_at:   str = field(default_factory=_now_iso)
    duration_s:   float = 0.0          # wall-clock execution time
    exit_code:    Optional[int] = None # None for python sims that returned
    params:       Dict[str, Any] = field(default_factory=dict)
    metrics:      Dict[str, float] = field(default_factory=dict)
    events:       List[SimEvent] = field(default_factory=list)
    stdout_tail:  str = ""             # last ~4 KB of stdout (Godot only)
    stderr_tail:  str = ""             # last ~4 KB of stderr (Godot only)
    error:        str = ""             # populated on crash / timeout

    @property
    def ok(self) -> bool:
        return not self.error and (self.exit_code in (0, None))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # asdict already handles SimEvent because it's a dataclass.
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SimRun":
        events_raw = d.pop("events", []) if isinstance(d, dict) else []
        events = [
            SimEvent(**e) if isinstance(e, dict) else e
            for e in events_raw
        ]
        valid = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in valid}
        kw["events"] = events
        return cls(**kw)


# ============================================================
# Recorder
# ============================================================

class SimRecorder:
    """Filesystem store for ``SimRun`` records.

    Layout:
      vault/simulations/
        _index.json                  ← lightweight runs index
        <sim_name>/
          <run_id>.json              ← one file per run

    The index keeps just the fields needed to render a results table
    quickly (run_id, sim_name, started_at, duration, ok flag, top-
    level params + metrics). The full record lives in the per-run
    file.
    """

    def __init__(self, vault_dir: Any, *, max_events: int = DEFAULT_MAX_EVENTS_PER_RUN):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.root = self.vault_dir / SIMS_SUBDIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_events = max_events
        self._index_path = self.root / INDEX_FILENAME
        self._index: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._load_index()

    # ----------------------------------------------------------------
    # Index
    # ----------------------------------------------------------------

    def _load_index(self) -> None:
        if not self._index_path.exists():
            self._index = []
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[SimRecorder] could not load index: {exc!r}")
            self._index = []
            return
        if (not isinstance(data, dict)
                or data.get("version") != INDEX_SCHEMA_VERSION):
            self._index = []
            return
        entries = data.get("entries") or []
        if not isinstance(entries, list):
            entries = []
        self._index = [e for e in entries if isinstance(e, dict)]

    def _save_index(self) -> None:
        payload = {
            "version": INDEX_SCHEMA_VERSION,
            "entries": self._index,
        }
        tmp = self._index_path.with_suffix(self._index_path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._index_path)
        except Exception as exc:
            print(f"[SimRecorder] index save failed: {exc!r}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Write
    # ----------------------------------------------------------------

    def record(self, run: SimRun) -> Path:
        """Persist ``run`` and update the index. Returns the on-disk path."""
        if not run.sim_name:
            run.sim_name = "untitled"
        # Cap events
        if len(run.events) > self.max_events:
            run.events = run.events[: self.max_events]
        # Per-sim subdir
        sub = self.root / _safe_dir(run.sim_name)
        sub.mkdir(parents=True, exist_ok=True)
        path = sub / f"{run.id}.json"
        with self._lock:
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(run.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except Exception as exc:
                print(f"[SimRecorder] record save failed: {exc!r}")
                return path
            # Index entry — lightweight summary
            entry = {
                "id":         run.id,
                "sim_name":   run.sim_name,
                "backend":    run.backend,
                "started_at": run.started_at,
                "duration_s": run.duration_s,
                "ok":         run.ok,
                "exit_code":  run.exit_code,
                "params":     dict(run.params),
                "metrics":    dict(run.metrics),
                "n_events":   len(run.events),
                "file":       str(path.relative_to(self.root)),
            }
            # Replace any existing entry for this id
            self._index = [e for e in self._index if e.get("id") != run.id]
            self._index.insert(0, entry)
            self._save_index()
        return path

    # ----------------------------------------------------------------
    # Read
    # ----------------------------------------------------------------

    def list_runs(self, *, sim_name: str = "",
                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return index entries, newest first. Optionally filter by sim."""
        with self._lock:
            out = list(self._index)
        if sim_name:
            out = [e for e in out if e.get("sim_name") == sim_name]
        if limit is not None:
            out = out[: int(limit)]
        return out

    def load_run(self, run_id: str) -> Optional[SimRun]:
        """Load a full ``SimRun`` by id, or None if not found."""
        for entry in self._index:
            if entry.get("id") != run_id:
                continue
            rel = entry.get("file") or ""
            path = self.root / rel
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return SimRun.from_dict(data)
            except Exception as exc:
                print(f"[SimRecorder] load_run({run_id}) failed: {exc!r}")
                return None
        return None

    def delete_run(self, run_id: str) -> bool:
        """Remove a single run + its index entry. Returns True if found."""
        with self._lock:
            for entry in list(self._index):
                if entry.get("id") != run_id:
                    continue
                path = self.root / (entry.get("file") or "")
                try:
                    if path.exists():
                        path.unlink()
                except Exception:
                    pass
                self._index.remove(entry)
                self._save_index()
                return True
        return False

    def sim_names(self) -> List[str]:
        """Distinct sim names that have at least one recorded run."""
        with self._lock:
            return sorted({e.get("sim_name", "") for e in self._index
                            if e.get("sim_name")})

    def count(self) -> int:
        with self._lock:
            return len(self._index)


# ============================================================
# Helpers
# ============================================================

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def _safe_dir(name: str) -> str:
    """Subfolder-safe form of a sim name. Keeps it short and avoids
    path-traversal characters."""
    s = _UNSAFE_RE.sub("_", str(name)).strip("._")
    return (s or "untitled")[:60]
