"""
godot_sim_runner_csv.py — SimRunner backend for external Godot games
that ship their own headless balance-sim (CSV + ``[SIM]`` stdout),
e.g. the Goblin_Tide prototype.

Why a separate backend
----------------------
``sim_runner.GodotSimRunner`` assumes the Anvil telemetry convention:
it writes ``<project>/anvil_params.json`` and parses ``ANVIL_METRIC:`` /
``ANVIL_EVENT:`` lines. A game like Goblin_Tide instead:
  * reads ``key=value`` args after ``--`` (``OS.get_cmdline_user_args``),
  * writes a ``sim_results.csv`` to a hardcoded path, and
  * prints ``[SIM] N/M char/strat seed=… survived=…`` progress lines.

So this backend speaks that contract instead. Crucially it runs every
sweep cell on a *throwaway working copy* of the project (via
``godot_sim_project.GodotProjectMaterializer``) and applies VALUE
overrides (Balance.gd consts) and BEHAVIOR overrides (AI-brain bands +
card strategy) by patching the copy's GDScript. The user's original
project is never touched — the Space_Mining rule, enforced structurally.

It reuses the proven subprocess scaffolding from ``sim_runner``
(Windows no-window flags, bounded tail buffers, two daemon drain
threads, wait→terminate→kill) but with a CSV-aware ingest body.
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sim_recorder import SimEvent, SimRun
from sim_runner import (
    DEFAULT_TIMEOUT_S, TAIL_BUFFER_BYTES, _GODOT_CRASH_HINTS,
    SimRunner, _TailBuffer, parse_telemetry_line,
)
from godot_sim_project import (
    GdConstPatcher, GodotProjectMaterializer, KnobTarget, SimContract,
    load_contract,
)


# ============================================================
# [SIM] stdout line parser
# ============================================================

# "[SIM] 1/24  human/balanced seed=1000  survived=610s died=true
#   victory=false kills=2781 lvl=31 dps=251"
_SIM_RUN_RE = re.compile(
    r"\[SIM\]\s+(?P<done>\d+)\s*/\s*(?P<total>\d+)\s+"
    r"(?P<char>[A-Za-z0-9_]+)\s*/\s*(?P<strategy>[A-Za-z0-9_]+)\s+"
    r"seed=(?P<seed>\d+)\s+"
    r"survived=(?P<survived>[0-9.]+)s\s+"
    r"died=(?P<died>\w+)\s+"
    r"victory=(?P<victory>\w+)\s+"
    r"kills=(?P<kills>\d+)\s+"
    r"lvl=(?P<level>\d+)\s+"
    r"dps=(?P<dps>[0-9.]+)"
)

# "[SIM] config: chars=[...] strategies=[...] seeds=1 cap=180.0s fps=30 -> 1 runs"
_SIM_CONFIG_RE = re.compile(r"\[SIM\]\s+config:\s+(?P<rest>.*)")

# "[SIM] DONE — 1 runs. ..."  (em-dash or hyphen, encoding-tolerant)
_SIM_DONE_RE = re.compile(r"\[SIM\]\s+DONE\b.*?(?P<n>\d+)\s+runs")


def parse_sim_line(line: str) -> Optional[Dict[str, Any]]:
    """Recognise one Goblin_Tide ``[SIM]`` line.

    Returns ``{'kind': 'event', 'name': 'sim_run'|'sim_config'|
    'sim_done', 'data': {...}}`` mirroring ``parse_telemetry_line``'s
    shape, or None for non-``[SIM]`` lines. Pure / unit-testable.
    """
    if not line or "[SIM]" not in line:
        return None
    m = _SIM_RUN_RE.search(line)
    if m:
        g = m.groupdict()
        return {"kind": "event", "name": "sim_run", "data": {
            "done": int(g["done"]), "total": int(g["total"]),
            "char": g["char"], "strategy": g["strategy"],
            "seed": int(g["seed"]),
            "survived_s": float(g["survived"]),
            "died": g["died"].strip().lower() == "true",
            "victory": g["victory"].strip().lower() == "true",
            "kills": int(g["kills"]), "level": int(g["level"]),
            "dps": float(g["dps"]),
        }}
    m = _SIM_CONFIG_RE.search(line)
    if m:
        return {"kind": "event", "name": "sim_config",
                "data": {"text": m.group("rest").strip()}}
    m = _SIM_DONE_RE.search(line)
    if m:
        return {"kind": "event", "name": "sim_done",
                "data": {"n": int(m.group("n"))}}
    return None


# ============================================================
# Persona → behavior mapping
# ============================================================

# Default translation from Anvil persona weight axes (0..1) to
# Goblin_Tide's concrete behavior surfaces. All formulas clamp into a
# sane band so an extreme persona can't produce a degenerate run. Lives
# as data so it can be tuned against real survived_s spreads without a
# code change.
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def persona_to_behavior(params: Dict[str, Any]) -> Dict[str, Any]:
    """Derive ``brain.*`` band overrides + a card ``strategy`` from the
    persona weights already injected into ``params`` (persona_name +
    persona.<axis> floats).

    Returns only the derived keys. The caller merges these UNDER any
    explicit base params, so a literal swept ``brain.CONTACT_BAND`` or
    forced ``strategy`` always wins (mirrors sim_personas merge rules).
    Returns {} when the run carries no persona.
    """
    has_persona = "persona_name" in params or any(
        str(k).startswith("persona.") for k in params
    )
    if not has_persona:
        return {}

    def axis(name: str, default: float = 0.5) -> float:
        v = params.get("persona." + name, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    aggression = axis("aggression")
    caution = axis("caution")
    risk = axis("risk_tolerance")
    greed = axis("greed")

    # Card strategy by dominant axis.
    if aggression >= 0.7:
        strategy = "glass"
    elif caution >= 0.6 or risk <= 0.35:
        strategy = "tank"
    else:
        strategy = "balanced"

    derived: Dict[str, Any] = {
        "strategy": strategy,
        "brain.CONTACT_BAND": round(_clamp(64.0 * (1 + 0.6 * caution - 0.4 * aggression), 32.0, 110.0), 1),
        "brain.ENGAGE_BAND": round(_clamp(130.0 * (1 - 0.35 * aggression + 0.3 * caution), 70.0, 200.0), 1),
        "brain.boss_dodge_radius": round(_clamp(150.0 * (1 + 0.5 * caution - 0.4 * risk), 90.0, 260.0), 1),
        "brain.gem_drift_gate": round(_clamp(200.0 * (1 - 0.5 * greed), 80.0, 300.0), 1),
    }
    return derived


# ============================================================
# The runner
# ============================================================

class GodotCsvSimRunner(SimRunner):
    """Run an external Godot game's headless CSV sim on a safe working
    copy, applying value/behavior overrides, and ingest the result.

    Parameters
    ----------
    project_root :
        The user's real Godot project (read-only).
    contract :
        A ``SimContract`` or a built-in name / JSON path resolvable via
        ``godot_sim_project.load_contract``.
    godot_binary :
        Path to the Godot executable (the ``_console.exe`` build on
        Windows pipes stdout reliably).
    duration_s :
        Wall-clock timeout per cell. A headless run is ~50× realtime, so
        a full 20-min sim is ~25 s; default leaves generous headroom.
    work_root :
        Directory Anvil owns where throwaway copies are created.
    keep_workdirs :
        Prune all but the N most-recent working copies after each run.
    """

    backend = "godot_csv"

    def __init__(
        self,
        project_root: Any,
        *,
        contract: Any,
        godot_binary: str = "godot",
        duration_s: float = 180.0,
        work_root: Any,
        on_line: Optional[Callable[[str, str], None]] = None,
        keep_workdirs: int = 2,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.contract: SimContract = (
            contract if isinstance(contract, SimContract)
            else load_contract(str(contract))
        )
        self.godot_binary = godot_binary
        self.duration_s = max(5.0, float(duration_s))
        self.work_root = Path(work_root).expanduser().resolve()
        self.on_line = on_line or (lambda stream, text: None)
        self.keep_workdirs = int(keep_workdirs)
        self._materializer = GodotProjectMaterializer(self.project_root, self.work_root)

    # ----------------------------------------------------------------
    # Override application
    # ----------------------------------------------------------------

    def _apply_overrides(
        self, copy_root: Path, params: Dict[str, Any],
    ) -> List[str]:
        """Patch the working copy's GDScript per the contract. Returns a
        list of human-readable errors (missed required knobs); empty on
        success. Always redirects OUT_DIR into the copy.
        """
        errors: List[str] = []
        c = self.contract

        # Group knob params by which file they target so we open each
        # .gd once.
        by_file: Dict[str, List[tuple]] = {}
        knobs = c.all_knobs()
        for key, value in params.items():
            kt = knobs.get(key)
            if kt is None:
                continue  # not a knob (char/strategy/persona/seed echo)
            by_file.setdefault(kt.file, []).append((key, kt, value))

        for logical_file, items in by_file.items():
            rel = c.resolve_file(logical_file)
            fpath = copy_root / rel
            if not fpath.exists():
                errors.append(f"override file missing in copy: {rel}")
                continue
            patcher = GdConstPatcher(fpath)
            for key, kt, value in items:
                ok = self._apply_one(patcher, kt, value)
                if not ok:
                    errors.append(f"knob {key!r} did not patch (stale anchor in {rel})")
            patcher.save()

        # Always redirect OUT_DIR (in the brain file) into the copy so
        # the game writes its CSV somewhere we own — never the original
        # / the user's OneDrive path.
        brain_path = copy_root / c.brain_file
        if brain_path.exists():
            out_patcher = GdConstPatcher(brain_path)
            out_target = str(copy_root).replace("\\", "/").rstrip("/") + "/"
            if not out_patcher.set_const_str(c.out_dir_const, out_target):
                errors.append(
                    f"OUT_DIR redirect failed (const {c.out_dir_const} not found) — "
                    "refusing to run to avoid writing to the original project"
                )
            else:
                out_patcher.save()
        else:
            errors.append(f"brain file missing in copy: {c.brain_file}")

        return errors

    @staticmethod
    def _apply_one(patcher: GdConstPatcher, kt: KnobTarget, value: Any) -> bool:
        if kt.kind == "const":
            return patcher.set_const(kt.name, value)
        if kt.kind == "const_str":
            return patcher.set_const_str(kt.name, str(value))
        if kt.kind == "dict_path":
            return patcher.set_dict_path(list(kt.path), value)
        if kt.kind == "literal":
            return patcher.set_literal(kt.regex, value)
        return False

    # ----------------------------------------------------------------
    # Arg building
    # ----------------------------------------------------------------

    def _build_args(self, char: str, strategy: str) -> List[str]:
        c = self.contract
        return [
            self.godot_binary, "--headless",
            "--fixed-fps", str(c.fixed_fps),
            "-s", c.sim_script,
            "--",
            f"seeds={c.seeds_per_cell}",
            f"cap={c.cap}",
            f"fps={c.fixed_fps}",
            f"{c.char_arg}={char}",
            f"{c.strategy_arg}={strategy}",
        ]

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------

    def run(self, *, params: Dict[str, Any], sim_name: str = "") -> SimRun:
        c = self.contract
        run = SimRun(sim_name=sim_name or c.name,
                     backend=self.backend, params=dict(params))

        # Merge persona-derived behavior UNDER explicit params (explicit wins).
        derived = persona_to_behavior(params)
        eff_params = dict(params)
        for k, v in derived.items():
            eff_params.setdefault(k, v)

        # Resolve this cell's single char + strategy.
        char = str(eff_params.get("char", c.char_default))
        strategy = str(eff_params.get("strategy", c.strategy_default))

        # ---- Materialise a clean working copy --------------------------
        try:
            copy_root = self._materializer.make_copy(run.id)
        except Exception as exc:
            run.error = f"could not materialise working copy: {exc!r}"
            return run

        # Wipe any pre-existing sim outputs in the copy so a fresh CSV is
        # a true positive (the source may ship a committed sim_results.csv).
        for stale in (c.csv_name, "sim_report.md", "sim_run.log"):
            sp = copy_root / stale
            try:
                if sp.exists():
                    sp.unlink()
            except Exception:
                pass

        # ---- Apply overrides + OUT_DIR redirect ------------------------
        override_errors = self._apply_overrides(copy_root, eff_params)
        if override_errors:
            run.error = "; ".join(override_errors)
            self._materializer.prune(self.keep_workdirs)
            return run

        # ---- Launch ----------------------------------------------------
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            except Exception:
                startupinfo = None
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        out_tail = _TailBuffer()
        err_tail = _TailBuffer()
        events: List[SimEvent] = []
        metrics_from_stream: Dict[str, float] = {}
        t0 = time.monotonic()
        launch_wall = time.time()

        args = self._build_args(char, strategy)
        try:
            proc = subprocess.Popen(
                args, cwd=str(copy_root),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True, encoding="utf-8", errors="replace",
                startupinfo=startupinfo, creationflags=creationflags,
            )
        except FileNotFoundError:
            run.error = (
                f"Godot binary not found: {self.godot_binary!r}. "
                "Set it in the Godot Workspace settings."
            )
            return run
        except Exception as exc:
            run.error = f"could not launch godot: {exc!r}"
            return run

        def _drain(stream, label, tail):
            try:
                for raw in iter(stream.readline, ""):
                    if not raw:
                        break
                    text = raw.rstrip("\r\n")
                    tail.append(text)
                    try:
                        self.on_line(label, text)
                    except Exception:
                        pass
                    if label != "stdout":
                        continue
                    parsed = parse_sim_line(text)
                    if parsed is None:
                        # Also honour true-ANVIL lines if a project emits them.
                        parsed = parse_telemetry_line(text)
                    if parsed is None:
                        continue
                    if parsed["kind"] == "event":
                        events.append(SimEvent(
                            t=round(time.monotonic() - t0, 3),
                            name=parsed["name"], data=parsed.get("data", {}),
                        ))
                    elif parsed["kind"] == "metric":
                        v = parsed["value"]
                        if isinstance(v, (int, float)):
                            metrics_from_stream[parsed["name"]] = float(v)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, "stdout", out_tail),
                                 daemon=True, name="anvil-gtcsv-stdout")
        t_err = threading.Thread(target=_drain, args=(proc.stderr, "stderr", err_tail),
                                 daemon=True, name="anvil-gtcsv-stderr")
        t_out.start(); t_err.start()

        try:
            rc = proc.wait(timeout=self.duration_s)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            except Exception:
                pass
            rc = proc.returncode if proc.returncode is not None else -1
            run.error = f"timeout after {self.duration_s:.1f}s — process terminated"
        except Exception as exc:
            run.error = f"wait failed: {exc!r}"
            rc = -1

        for t in (t_out, t_err):
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

        run.exit_code = rc
        run.duration_s = round(time.monotonic() - t0, 3)
        run.events = events
        run.stdout_tail = out_tail.text()
        run.stderr_tail = err_tail.text()

        # ---- Crash detection on stderr ---------------------------------
        if not run.error:
            if rc != 0:
                run.error = f"exit code {rc}"
            else:
                blob = run.stderr_tail
                for hint in _GODOT_CRASH_HINTS:
                    if hint in blob:
                        run.error = f"crash hint in stderr: {hint!r}"
                        break

        # ---- Ingest the CSV --------------------------------------------
        csv_path = copy_root / c.csv_name
        csv_metrics, n_rows = self._ingest_csv(csv_path, char, strategy, launch_wall)
        if csv_metrics is None:
            if not run.error:
                run.error = (
                    f"no fresh {c.csv_name} after clean exit — the patched "
                    "GDScript may have failed to produce output"
                )
        else:
            run.metrics.update(csv_metrics)
            run.metrics.update(metrics_from_stream)  # ANVIL metrics, if any
            run.metrics["n_runs"] = float(n_rows)
            # Echo numeric override params so the analyst can correlate
            # knob value vs outcome.
            for key, value in eff_params.items():
                if key in c.all_knobs() or str(key).startswith("persona."):
                    try:
                        run.params[key] = float(value)
                    except (TypeError, ValueError):
                        pass
            # Echo the effective char + (possibly persona-derived)
            # strategy so the results table and analyst show what
            # actually ran — important for persona sweeps where the
            # strategy is derived, not an explicit axis.
            run.params["char"] = char
            run.params["strategy"] = strategy

        self._materializer.prune(self.keep_workdirs)
        return run

    # ----------------------------------------------------------------
    # CSV ingest
    # ----------------------------------------------------------------

    def _ingest_csv(
        self, csv_path: Path, char: str, strategy: str, launch_wall: float,
    ) -> tuple:
        """Read the cell's rows from the working-copy CSV and aggregate
        to numeric metrics. Returns ``(metrics_dict | None, n_rows)``.

        Requires the file to be fresh (mtime ≥ launch) so a stale
        committed CSV can't masquerade as a result.
        """
        if not csv_path.exists():
            return None, 0
        try:
            if csv_path.stat().st_mtime < launch_wall - 1.0:
                return None, 0  # stale file, not from this run
        except Exception:
            pass
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except Exception:
            return None, 0
        if not rows:
            return None, 0

        # Keep only rows for this cell (defensive — single-cell run
        # should already be just these).
        cell = [r for r in rows
                if r.get("char") == char and r.get("strategy") == strategy]
        if not cell:
            cell = rows  # fall back to whatever the run produced

        # Numeric columns to aggregate (mean). died/victory coerced to 1/0.
        numeric_cols = [
            "survived_s", "kills", "level", "dmg_dealt", "dmg_taken",
            "dps", "chests", "min_hp",
            "t_chieftain1", "t_lord", "t_chieftain2", "t_king",
        ]
        bool_cols = ["died", "victory"]

        agg: Dict[str, float] = {}
        for col in numeric_cols:
            vals = []
            for r in cell:
                try:
                    vals.append(float(r.get(col, "")))
                except (TypeError, ValueError):
                    pass
            if vals:
                agg[col] = round(sum(vals) / len(vals), 3)
        for col in bool_cols:
            vals = []
            for r in cell:
                s = str(r.get(col, "")).strip().lower()
                vals.append(1.0 if s == "true" else 0.0)
            if vals:
                # mean = victory RATE / death RATE across seeds
                agg[col + "_rate"] = round(sum(vals) / len(vals), 3)
                # also a 0/1 for a single-seed cell
                agg[col] = 1.0 if (sum(vals) / len(vals)) >= 0.5 else 0.0
        return agg, len(cell)
