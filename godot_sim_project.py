"""
godot_sim_project.py — safe working-copy + GDScript patching for the
external-Godot simulation backend.

This module exists to let Anvil sweep a real Godot game's *values* and
*player behaviors* WITHOUT ever touching the user's original project.
It is the load-bearing implementation of the "never modify the real
folder" rule (the Space_Mining rule): every sim run operates on a fresh
throwaway copy under the vault, and all overrides are applied to that
copy's GDScript source.

Three pieces
------------
  * GodotProjectMaterializer — clones a source project into a work
    directory (keeping ``.godot`` so ``class_name`` globals resolve;
    dropping ``.git``) and cleans copies up afterward. Hard-asserts the
    destination is under the work root and never equal to / inside the
    source, so a coding mistake can't write back into the original.

  * GdConstPatcher — rewrites numeric ``const`` declarations, nested
    dict fields (e.g. ``CHARACTERS.human.max_hp``), and anchored inline
    literals (e.g. the AI brain's ``if bd < 150.0:`` boss-dodge radius)
    in a ``.gd`` file. Every patch reports whether it actually
    substituted, so a missed knob is loud (the runner turns it into a
    ``SimRun.error``) rather than a silent no-op.

  * SimContract + goblin_tide_contract() — a data description of one
    game's sim entry point, output contract, and the map from Anvil
    param keys (``balance.XP_GROWTH``, ``brain.CONTACT_BAND``, …) to the
    concrete patch each one performs. Generic where cheap; Goblin_Tide's
    specifics live in data, not in branchy Python.

Nothing here launches Godot or imports Anvil — it's pure filesystem +
text work, so it is fast and unit-testable in isolation.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Robust removal (Windows / OneDrive read-only attributes)
# ============================================================

def _on_rm_error(func, path, _exc_info):
    """``shutil.rmtree`` error hook: clear a read-only bit and retry.

    Files copied out of a OneDrive-synced source often arrive with the
    read-only attribute set, which makes ``os.rmdir`` / ``os.unlink``
    raise ``PermissionError`` on Windows. Clearing ``S_IWRITE`` and
    retrying handles the common case; anything still failing is
    swallowed so cleanup never crashes a sweep.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def robust_rmtree(target: Any) -> None:
    """Remove a directory tree, tolerating read-only files."""
    p = Path(target)
    if not p.exists():
        return
    # onexc replaces the deprecated onerror in 3.12+; fall back for older.
    try:
        shutil.rmtree(p, onexc=_on_rm_error)
    except TypeError:
        shutil.rmtree(p, onerror=lambda f, pth, ei: _on_rm_error(f, pth, ei))


# ============================================================
# Working-copy materializer
# ============================================================

# Top-level dirs never worth copying into a throwaway sim work dir.
_IGNORE_DIRS = (".git", "__pycache__")

# Inside ``.godot`` we keep ONLY the small caches a headless ``-s
# script`` run needs to resolve ``class_name`` globals and uid:// refs:
#   * global_script_class_cache.cfg — class_name → script path (REQUIRED;
#     without it Balance/SimProbe fail to parse).
#   * uid_cache.bin — uid:// → res:// resolution.
# Everything else under .godot (editor/, imported/, shader_cache/, …) is
# editor/asset state we don't need, and crucially the editor/ folding
# cfgs have very long filenames that blow past Windows MAX_PATH (260)
# when the work dir is itself deep. So we drop those subtrees.
_GODOT_KEEP = {"global_script_class_cache.cfg", "uid_cache.bin"}


def _copy_ignore(directory: str, names: List[str]) -> set:
    """``shutil.copytree`` ignore callback.

    Drops ``.git`` / ``__pycache__`` anywhere, and prunes the heavy
    parts of ``.godot`` while preserving the class/uid caches.
    """
    d = Path(directory)
    ignored: set = set()
    if d.name == ".godot":
        # Keep only the whitelisted cache files; drop all subdirs +
        # other files (editor state, imported assets, shader caches).
        for n in names:
            if n not in _GODOT_KEEP:
                ignored.add(n)
        return ignored
    # Anywhere under .godot that survived (shouldn't, since we prune the
    # whole subtree above) — belt and braces: prune known-heavy subdirs.
    if ".godot" in d.parts:
        return set(names)
    for n in names:
        if n in _IGNORE_DIRS:
            ignored.add(n)
    return ignored


class WorkingCopyError(RuntimeError):
    """Raised when a copy would (or did) violate the no-touch-source rule."""


class GodotProjectMaterializer:
    """Make and dispose of throwaway copies of a Godot project.

    Parameters
    ----------
    source_root :
        The user's real project. Opened READ-ONLY — this class never
        writes anything under here.
    work_root :
        A directory Anvil owns (e.g. ``vault/simulations/_workdirs``)
        under which all copies are created.
    """

    def __init__(self, source_root: Any, work_root: Any):
        self.source_root = Path(source_root).expanduser().resolve()
        self.work_root = Path(work_root).expanduser().resolve()
        if not self.source_root.exists():
            raise WorkingCopyError(f"source project does not exist: {self.source_root}")
        if not (self.source_root / "project.godot").exists():
            raise WorkingCopyError(
                f"not a Godot project (no project.godot): {self.source_root}"
            )
        # Refuse a work root that lives inside the source — that would
        # let a copy nest under the original.
        try:
            self.work_root.relative_to(self.source_root)
            raise WorkingCopyError(
                "work_root must not be inside source_root "
                f"({self.work_root} is under {self.source_root})"
            )
        except ValueError:
            pass  # not relative — good
        self.work_root.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------

    def make_copy(self, run_id: str) -> Path:
        """Clone the source into ``work_root/<run_id>`` and return the
        copy root. Raises before any write if the destination isn't a
        safe, distinct path under the work root.
        """
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id)) or "run"
        dest = (self.work_root / safe_id).resolve()

        # ---- Safety gauntlet: must be under work_root, never the source.
        try:
            dest.relative_to(self.work_root)
        except ValueError:
            raise WorkingCopyError(f"destination escaped work_root: {dest}")
        if dest == self.source_root:
            raise WorkingCopyError("destination equals source_root")
        try:
            self.source_root.relative_to(dest)
            # source is under dest → dest is an ancestor of source. Refuse.
            raise WorkingCopyError("destination is an ancestor of source_root")
        except ValueError:
            pass  # source not under dest — good

        if dest.exists():
            robust_rmtree(dest)
        shutil.copytree(self.source_root, dest, ignore=_copy_ignore)
        return dest

    def cleanup(self, run_id: str) -> None:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id)) or "run"
        robust_rmtree(self.work_root / safe_id)

    def prune(self, keep_last: int = 2) -> None:
        """Keep only the ``keep_last`` most-recently-modified copies."""
        if keep_last < 0:
            return
        try:
            copies = [d for d in self.work_root.iterdir() if d.is_dir()]
        except FileNotFoundError:
            return
        copies.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for stale in copies[keep_last:]:
            robust_rmtree(stale)


# ============================================================
# GDScript text patcher
# ============================================================

class GdConstPatcher:
    """Apply numeric overrides to one ``.gd`` file's source text.

    Construct on a file path (which MUST already be inside a working
    copy — this class does not guard that; the materializer does), call
    the ``set_*`` methods, then ``save()``. Each setter returns True iff
    it actually changed the text, so callers can detect a stale anchor.
    """

    def __init__(self, file_path: Any):
        self.path = Path(file_path)
        self.text = self.path.read_text(encoding="utf-8")
        self._dirty = False

    # ---- primitive setters -----------------------------------------

    def set_const(self, name: str, value: Any) -> bool:
        """Rewrite ``const NAME := <number>`` (or ``= <number>``).

        Preserves the assignment operator (``:=`` or ``=``). Matches an
        int or float literal; the replacement is rendered via
        ``_fmt_num`` so an int stays an int and a float stays a float.
        """
        pat = re.compile(
            r"(?P<head>const\s+" + re.escape(name) + r"\s*:?=\s*)"
            r"(?P<val>-?\d+(?:\.\d+)?)"
        )
        return self._sub_group(pat, value)

    def set_const_str(self, name: str, value: str) -> bool:
        """Rewrite ``const NAME := "<string>"`` (used for OUT_DIR)."""
        pat = re.compile(
            r"(?P<head>const\s+" + re.escape(name) + r'\s*:?=\s*")'
            r'(?P<val>[^"]*)(?P<tail>")'
        )
        new, n = pat.subn(
            lambda m: m.group("head") + str(value) + m.group("tail"),
            self.text,
        )
        if n:
            self.text = new
            self._dirty = True
        return n > 0

    def set_literal(self, regex: str, value: Any) -> bool:
        """Replace the ``(?P<val>...)`` group of a caller-supplied regex.

        Use for unnamed inline literals like ``if bd < 150.0:`` →
        ``r"bd < (?P<val>[0-9.]+)"``. The regex MUST define a ``val``
        named group around the number to replace.
        """
        pat = re.compile(regex)
        return self._sub_group(pat, value)

    def set_export_var(self, name: str, value: Any) -> bool:
        """Rewrite ``@export var NAME[: TYPE] = <number>`` — the form
        the GDD builder emits for tunables (player ``move_speed``,
        enemy ``hp`` / ``speed`` / ``damage``). Lets generated games
        sweep their own balance VALUES, not just the harness consts.
        """
        pat = re.compile(
            r"(?P<head>@export\s+var\s+" + re.escape(name)
            + r"\b\s*(?::\s*[A-Za-z0-9_]+)?\s*=\s*)"
            r"(?P<val>-?\d+(?:\.\d+)?)"
        )
        return self._sub_group(pat, value)

    def set_dict_path(self, path: List[str], value: Any) -> bool:
        """Set a field nested inside dict consts, e.g.
        ``["CHARACTERS", "human", "max_hp"]`` → the ``max_hp`` number
        inside the ``"human"`` sub-dict of ``const CHARACTERS``.

        Brace-aware: descends ``{ ... }`` scopes by key so a field name
        that also appears in a sibling dict isn't clobbered.
        """
        if len(path) < 2:
            return False
        const_name, keys, field_name = path[0], path[1:-1], path[-1]

        lines = self.text.splitlines(keepends=True)
        # Locate the const declaration line.
        start = None
        const_re = re.compile(r"\bconst\s+" + re.escape(const_name) + r"\b")
        for i, ln in enumerate(lines):
            if const_re.search(ln):
                start = i
                break
        if start is None:
            return False

        depth = 0
        seen_open = False
        # Stack of dict keys we are currently inside.
        ctx: List[str] = []
        want_ctx = list(keys)
        field_re = re.compile(
            r'(?P<head>"' + re.escape(field_name) + r'"\s*:\s*)'
            r"(?P<val>-?\d+(?:\.\d+)?)"
        )
        key_open_re = re.compile(r'"(?P<k>[^"]+)"\s*:\s*\{')

        for i in range(start, len(lines)):
            ln = lines[i]
            # If we're in the desired context, try the field replace first.
            if seen_open and ctx == want_ctx:
                m = field_re.search(ln)
                if m:
                    lines[i] = (
                        ln[:m.start()] + m.group("head")
                        + _fmt_num(value, m.group("val")) + ln[m.end():]
                    )
                    self.text = "".join(lines)
                    self._dirty = True
                    return True
            # Track key-scoped dict openings on this line.
            for km in key_open_re.finditer(ln):
                ctx.append(km.group("k"))
            # Update brace depth and pop context on closes.
            for ch in ln:
                if ch == "{":
                    depth += 1
                    seen_open = True
                elif ch == "}":
                    depth -= 1
                    if ctx:
                        ctx.pop()
                    if seen_open and depth <= 0:
                        return False  # walked out of the const without a hit
        return False

    # ---- helpers ----------------------------------------------------

    def _sub_group(self, pat: re.Pattern, value: Any) -> bool:
        def _repl(m: re.Match) -> str:
            return m.group("head") + _fmt_num(value, m.group("val")) \
                if "head" in m.groupdict() and m.group("head") is not None \
                else m.group(0).replace(m.group("val"), _fmt_num(value, m.group("val")))
        new, n = pat.subn(_repl, self.text, count=1)
        if n:
            self.text = new
            self._dirty = True
        return n > 0

    def save(self) -> None:
        if self._dirty:
            self.path.write_text(self.text, encoding="utf-8")
            self._dirty = False


def _fmt_num(value: Any, old: str) -> str:
    """Render ``value`` to match the lexical shape of ``old``.

    If the original literal had a decimal point we keep a float form so
    GDScript's inferred type doesn't flip int↔float (which can change
    division semantics); otherwise we emit an int when value is integral.
    """
    old_is_float = "." in old
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return str(value)
    if old_is_float:
        # Trim trailing zeros but always keep one decimal place.
        s = f"{fv:.6f}".rstrip("0")
        if s.endswith("."):
            s += "0"
        return s
    # old was an int literal
    if fv.is_integer():
        return str(int(fv))
    return str(fv)


# ============================================================
# Sim contract
# ============================================================

@dataclass
class KnobTarget:
    """Describes how to apply one override param to a working copy.

    ``file`` is logical — "balance" or "brain" — resolved to a concrete
    path via the contract's ``balance_file`` / ``brain_file``.
    """
    file: str                       # "balance" | "brain"
    kind: str                       # "const" | "const_str" | "dict_path" | "literal"
    name: str = ""                  # const/const_str name
    path: List[str] = field(default_factory=list)   # dict_path
    regex: str = ""                 # literal anchor (must have (?P<val>...))


@dataclass
class SimContract:
    """A data description of one game's headless sim entry point."""
    name: str
    sim_script: str = "res://scripts/sim/Sim.gd"
    fixed_fps: int = 30
    cap: float = 1320.0
    seeds_per_cell: int = 1
    balance_file: str = "scripts/data/Balance.gd"
    brain_file: str = "scripts/sim/Sim.gd"
    out_dir_const: str = "OUT_DIR"
    csv_name: str = "sim_results.csv"
    csv_columns: List[str] = field(default_factory=list)
    char_arg: str = "chars"
    strategy_arg: str = "strategies"
    # arg key -> default (chars/strategies are single-valued per cell).
    char_default: str = "human"
    strategy_default: str = "balanced"
    value_knobs: Dict[str, KnobTarget] = field(default_factory=dict)
    brain_knobs: Dict[str, KnobTarget] = field(default_factory=dict)

    def resolve_file(self, logical: str) -> str:
        # "balance" / "brain" are logical aliases; anything else is
        # treated as a direct project-relative path (used by per-entity
        # @export-var knobs, which each live in their own script).
        if logical == "balance":
            return self.balance_file
        if logical == "brain":
            return self.brain_file
        return logical

    def all_knobs(self) -> Dict[str, KnobTarget]:
        merged = dict(self.value_knobs)
        merged.update(self.brain_knobs)
        return merged


def goblin_tide_contract() -> SimContract:
    """Built-in contract for the Goblin_Tide prototype.

    Paths and anchors confirmed against the real project:
      * balance consts live in scripts/data/Balance.gd
      * the AI brain + OUT_DIR live in scripts/sim/Sim.gd
      * the headless entry is ``-s res://scripts/sim/Sim.gd -- key=val …``
    """
    B = "balance"
    R = "brain"
    value_knobs: Dict[str, KnobTarget] = {
        # Flat consts in Balance.gd
        "balance.XP_BASE":              KnobTarget(B, "const", name="XP_BASE"),
        "balance.XP_GROWTH":            KnobTarget(B, "const", name="XP_GROWTH"),
        "balance.PICKUP_RADIUS":        KnobTarget(B, "const", name="PICKUP_RADIUS"),
        "balance.MAGNET_RADIUS":        KnobTarget(B, "const", name="MAGNET_RADIUS"),
        "balance.SPAWN_INTERVAL_START": KnobTarget(B, "const", name="SPAWN_INTERVAL_START"),
        "balance.SPAWN_INTERVAL_MIN":   KnobTarget(B, "const", name="SPAWN_INTERVAL_MIN"),
        "balance.SPAWN_BATCH_START":    KnobTarget(B, "const", name="SPAWN_BATCH_START"),
        "balance.SPAWN_BATCH_MAX":      KnobTarget(B, "const", name="SPAWN_BATCH_MAX"),
        "balance.MAX_ALIVE_START":      KnobTarget(B, "const", name="MAX_ALIVE_START"),
        "balance.MAX_ALIVE_END":        KnobTarget(B, "const", name="MAX_ALIVE_END"),
        "balance.COMMON_CHEST_CHANCE":  KnobTarget(B, "const", name="COMMON_CHEST_CHANCE"),
        "balance.ELITE_CHEST_CHANCE":   KnobTarget(B, "const", name="ELITE_CHEST_CHANCE"),
        "balance.TUNNEL_COUNT":         KnobTarget(B, "const", name="TUNNEL_COUNT"),
        # Nested character stats in const CHARACTERS
        "balance.human.max_hp":         KnobTarget(B, "dict_path", path=["CHARACTERS", "human", "max_hp"]),
        "balance.human.speed":          KnobTarget(B, "dict_path", path=["CHARACTERS", "human", "speed"]),
        "balance.human.armor":          KnobTarget(B, "dict_path", path=["CHARACTERS", "human", "armor"]),
        "balance.dwarf.max_hp":         KnobTarget(B, "dict_path", path=["CHARACTERS", "dwarf", "max_hp"]),
        "balance.dwarf.speed":          KnobTarget(B, "dict_path", path=["CHARACTERS", "dwarf", "speed"]),
        "balance.dwarf.armor":          KnobTarget(B, "dict_path", path=["CHARACTERS", "dwarf", "armor"]),
        # difficulty slope: `return 1.0 + t / 240.0` in Balance.difficulty_mult
        "balance.difficulty_slope":     KnobTarget(B, "literal", regex=r"t\s*/\s*(?P<val>\d+(?:\.\d+)?)"),
    }
    brain_knobs: Dict[str, KnobTarget] = {
        # Named function-local consts in _drive_brain
        "brain.CONTACT_BAND": KnobTarget(R, "const", name="CONTACT_BAND"),
        "brain.ENGAGE_BAND":  KnobTarget(R, "const", name="ENGAGE_BAND"),
        # Unnamed inline literals — anchored uniquely.
        "brain.centroid_radius":   KnobTarget(R, "literal", regex=r"d\s*<\s*(?P<val>\d+(?:\.\d+)?)"),
        "brain.boss_dodge_radius": KnobTarget(R, "literal", regex=r"bd\s*<\s*(?P<val>\d+(?:\.\d+)?)"),
        "brain.gem_drift_gate":    KnobTarget(R, "literal", regex=r"nd\s*>\s*(?P<val>\d+(?:\.\d+)?)"),
    }
    return SimContract(
        name="goblin_tide",
        sim_script="res://scripts/sim/Sim.gd",
        fixed_fps=30,
        cap=1320.0,
        seeds_per_cell=1,
        balance_file="scripts/data/Balance.gd",
        brain_file="scripts/sim/Sim.gd",
        out_dir_const="OUT_DIR",
        csv_name="sim_results.csv",
        csv_columns=[
            "char", "strategy", "seed", "died", "survived_s", "victory",
            "kills", "level", "dmg_dealt", "dmg_taken", "dps", "chests",
            "min_hp", "t_chieftain1", "t_lord", "t_chieftain2", "t_king",
        ],
        value_knobs=value_knobs,
        brain_knobs=brain_knobs,
    )


# Registry of built-in contracts by name.
_BUILTIN_CONTRACTS = {
    "goblin_tide": goblin_tide_contract,
}


def load_contract(name_or_path: str) -> SimContract:
    """Resolve a contract by built-in name (e.g. ``"goblin_tide"``).

    A filesystem path to a JSON contract is also accepted for user-
    defined games; the JSON mirrors the SimContract / KnobTarget fields.
    """
    if name_or_path in _BUILTIN_CONTRACTS:
        return _BUILTIN_CONTRACTS[name_or_path]()
    p = Path(name_or_path)
    if p.exists() and p.suffix.lower() == ".json":
        return _contract_from_json(p)
    raise KeyError(f"unknown sim contract: {name_or_path!r}")


def _contract_from_json(path: Path) -> SimContract:
    import json
    data = json.loads(path.read_text(encoding="utf-8"))

    def _knobs(d: Dict[str, Any]) -> Dict[str, KnobTarget]:
        out = {}
        for k, v in (d or {}).items():
            out[k] = KnobTarget(
                file=v.get("file", "balance"),
                kind=v.get("kind", "const"),
                name=v.get("name", ""),
                path=v.get("path", []) or [],
                regex=v.get("regex", ""),
            )
        return out

    return SimContract(
        name=data.get("name", path.stem),
        sim_script=data.get("sim_script", "res://scripts/sim/Sim.gd"),
        fixed_fps=int(data.get("fixed_fps", 30)),
        cap=float(data.get("cap", 1320.0)),
        seeds_per_cell=int(data.get("seeds_per_cell", 1)),
        balance_file=data.get("balance_file", "scripts/data/Balance.gd"),
        brain_file=data.get("brain_file", "scripts/sim/Sim.gd"),
        out_dir_const=data.get("out_dir_const", "OUT_DIR"),
        csv_name=data.get("csv_name", "sim_results.csv"),
        csv_columns=data.get("csv_columns", []) or [],
        char_arg=data.get("char_arg", "chars"),
        strategy_arg=data.get("strategy_arg", "strategies"),
        char_default=data.get("char_default", "human"),
        strategy_default=data.get("strategy_default", "balanced"),
        value_knobs=_knobs(data.get("value_knobs", {})),
        brain_knobs=_knobs(data.get("brain_knobs", {})),
    )
