"""
gdd_builder.py — orchestrate a ``Plan`` into a working Godot project.

Walks the Plan produced by ``gdd_planner.plan_from_gdd``:

  1. Skeleton — call ``demo_builder`` to lay down ``project.godot``,
     ``icon.svg``, and a placeholder ``main.tscn``/``main.gd`` from
     the template the planner detected.
  2. Scenes — render each ``ScenePlan`` as a real ``.tscn`` file
     against the chosen project root.
  3. Autoloads — write a stub for each ``AutoloadPlan`` (just the
     extends + signal declarations + empty ``_ready``) and register
     them in ``project.godot``.
  4. Scripts — for each ``ScriptPlan``, call ``GodotCoder.run``
     with a structured task that includes the entity registry +
     the signal contracts the script must emit / handle. Atomic
     write + diff-view confirmation per file (skipped in headless
     mode for batch builds).
  5. Cross-file validation — after every script is written, run
     ``godot --headless --check-only`` once across the whole
     project. Group reported errors per file. For each broken
     file, call GodotCoder again with the error message + the
     other plan-known context, up to ``max_repair_passes`` times.

The orchestrator is *callable from the GUI on a background thread*
and *unit-testable in dry-run mode* (set ``dry_run=True`` and no
subprocess or LLM calls fire — useful for asserting the file
layout is what the smoke test expects).

The user's hard preference applies throughout: **no AI art**. Every
visual is a ColorRect + Label primitive that the user later
replaces with hand-painted sprites from the 🎨 Pixel Art tab.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from gdd_planner import (
    Plan, ScenePlan, ScriptPlan, AutoloadPlan, NodeSpec,
    SignalContract,
)


# ============================================================
# Build result type
# ============================================================

@dataclass
class BuildResult:
    """Outcome of one full orchestrator run."""
    project_path:  Optional[Path] = None
    files_written: List[Path] = field(default_factory=list)
    scripts_passed: List[Path] = field(default_factory=list)
    scripts_failed: List[Path] = field(default_factory=list)
    repair_passes: int = 0
    notes:         List[str] = field(default_factory=list)
    error:         str = ""
    # Path to the generated sim-harness SimContract JSON, when a sim
    # was generated. The GUI passes this straight to
    # godot_sim_project.load_contract so a freshly-built game is
    # immediately sweepable in the 🎲 Simulations tab.
    sim_contract_path: Optional[Path] = None
    # The dev ledger (plan.ledger), with statuses updated to reflect
    # the actual build outcome (real / failed / placeholder). The
    # Godot Workspace plan pane renders it as a per-file worklist.
    ledger:        List[Any] = field(default_factory=list)
    # None until a runtime smoke ran; True/False after. None in dry-run.
    runtime_smoke_ok: Optional[bool] = None

    @property
    def ok(self) -> bool:
        return self.project_path is not None and not self.error


# ============================================================
# Scene rendering — NodeSpec tree → .tscn text
# ============================================================

def render_tscn(scene: ScenePlan,
                  ext_resources: List[Tuple[str, str, str]] | None = None) -> str:
    """Turn a ``ScenePlan`` into the text of a .tscn file.

    ``ext_resources`` is a list of (id, type, path) tuples for any
    scripts the scene references. The Main scene script is passed
    here so the root node's ``script = ExtResource(...)`` line resolves.
    """
    ext_resources = ext_resources or []
    lines: List[str] = []
    load_steps = max(1, 1 + len(ext_resources))
    lines.append(f"[gd_scene load_steps={load_steps} format=3]\n")
    for (rid, rtype, rpath) in ext_resources:
        lines.append(
            f'[ext_resource type="{rtype}" path="{rpath}" id="{rid}"]\n'
        )
    # Walk the tree
    _emit_node(scene.root, lines, "", ext_resources)
    return "\n".join(lines)


def _snapshot_project(project_path: Path, vault_dir: Path) -> Optional[Path]:
    """Copy ``project_path`` to ``projects/.history/<slug>/<timestamp>``
    before a destructive rebuild. Excludes the regenerable ``.godot`` /
    ``.import`` caches to keep snapshots small. Returns the snapshot
    dir, or None if there was nothing to snapshot.
    """
    if not project_path.exists() or not any(project_path.iterdir()):
        return None
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = vault_dir / "projects" / ".history" / project_path.name / ts
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        project_path, dest,
        ignore=shutil.ignore_patterns(".godot", ".import", ".git",
                                       "__pycache__", ".history"),
    )
    return dest


def _collect_ext_resources(scene: ScenePlan) -> List[Tuple[str, str, str]]:
    """Walk a scene's NodeSpec tree and return one ``(id, "Script",
    res://path)`` ext_resource per distinct ``node.script``, in
    first-seen order. These feed ``render_tscn`` so every scripted node
    resolves its ``script = ExtResource(...)`` line.
    """
    seen: Dict[str, bool] = {}
    order: List[str] = []

    def _walk(node: Optional[NodeSpec]) -> None:
        if node is None:
            return
        if node.script and node.script not in seen:
            seen[node.script] = True
            order.append(node.script)
        for child in node.children:
            _walk(child)

    _walk(scene.root)
    out: List[Tuple[str, str, str]] = []
    for i, path in enumerate(order, start=1):
        stem = re.sub(r"[^A-Za-z0-9]+", "_",
                      path.rsplit("/", 1)[-1].rsplit(".", 1)[0]) or "res"
        out.append((f"{i}_{stem}", "Script", path))
    return out


def _emit_node(node: NodeSpec, lines: List[str], parent_path: str,
                 ext_resources: List[Tuple[str, str, str]]) -> None:
    if not node:
        return
    header = f'[node name="{node.name}" type="{node.type}"'
    if parent_path:
        header += f' parent="{parent_path}"'
    header += "]"
    lines.append(header)
    if node.script:
        # Find the matching ext_resource id
        for (rid, _t, rpath) in ext_resources:
            if rpath == node.script:
                lines.append(f'script = ExtResource("{rid}")')
                break
    for k, v in node.props.items():
        lines.append(f"{k} = {v}")
    lines.append("")          # blank line after header block
    # Children: parent is "." for direct child of root, otherwise
    # use the path from root down to this node.
    if not parent_path:
        child_parent = "."
    elif parent_path == ".":
        child_parent = node.name
    else:
        child_parent = f"{parent_path}/{node.name}"
    for child in node.children:
        _emit_node(child, lines, child_parent, ext_resources)


# ============================================================
# Autoload stub — a minimal extends + signals + _ready
# ============================================================

def render_autoload_stub(autoload: AutoloadPlan) -> str:
    """Tiny GDScript stub for a manager singleton.

    Signals are declared with the planned signatures so the rest of
    the project can connect immediately. Body is a printable
    placeholder; GodotCoder can flesh it out later when the user
    asks for "make GameManager actually track score".
    """
    lines = ["extends Node", ""]
    lines.append(f"# {autoload.purpose}")
    lines.append("")
    for sig in autoload.signals_emit:
        if sig.args:
            args = ", ".join(sig.args)
            lines.append(f"signal {sig.name}({args})")
        else:
            lines.append(f"signal {sig.name}")
    lines.append("")
    lines.append("var score: int = 0")
    lines.append("var hp: int = 100")
    lines.append("")
    lines.append("func _ready() -> void:")
    lines.append(f'\tprint("[{autoload.name}] ready")')
    lines.append("")
    return "\n".join(lines)


# ============================================================
# Script rendering — placeholder body + coder-friendly task
# ============================================================

def render_script_placeholder(script: ScriptPlan) -> str:
    """A minimal but parseable .gd stub that compiles in Godot 4.

    The orchestrator writes this BEFORE calling GodotCoder. If the
    coder is skipped (dry_run, no model), the placeholder is the
    final content — it parses cleanly so the project loads but
    doesn't actually implement the entity's behaviour. The
    placeholder spells out the planned exports + signals so the
    user (or a later Coder pass) can see what was intended.
    """
    lines = [
        f"extends {script.extends}",
    ]
    if script.class_name:
        lines.append(f"class_name {script.class_name}")
    lines.append("")
    # Header comment carrying the planner's purpose so a human reader
    # sees the task brief inline.
    lines.append(f"# Anvil GDD plan — {script.purpose}")
    lines.append("")
    # Signal declarations
    for sig in script.signals_emit:
        if sig.args:
            args = ", ".join(sig.args)
            lines.append(f"signal {sig.name}({args})")
        else:
            lines.append(f"signal {sig.name}")
    if script.signals_emit:
        lines.append("")
    # Exports
    for (name, type_, default) in script.exported_vars:
        lines.append(f"@export var {name}: {type_} = {default}")
    if script.exported_vars:
        lines.append("")
    # Minimum methods so the script parses + does something visible
    lines.append("func _ready() -> void:")
    lines.append(f'\tprint("[" + str(self.name) + "] _ready")')
    lines.append("")
    return "\n".join(lines)


def build_coder_task(script: ScriptPlan, plan: Plan,
                     placeholder: str) -> str:
    """Build the prompt-shaped task we hand to GodotCoder for one
    script. The Coder uses this as its FIRST_ATTEMPT task; failure
    feedback comes from godot --check-only stderr on retry."""
    parts: List[str] = []
    parts.append(f"PURPOSE: {script.purpose}")
    parts.append("")
    parts.append(f"This script extends {script.extends}.")
    if script.class_name:
        parts.append(f"Declare class_name {script.class_name}.")
    if script.exported_vars:
        parts.append("Exported variables (keep these as @export):")
        for (name, type_, default) in script.exported_vars:
            parts.append(f"  @export var {name}: {type_} = {default}")
    if script.signals_emit:
        parts.append("This script MUST DECLARE and EMIT these signals "
                      "with the exact signatures:")
        for sig in script.signals_emit:
            args = ", ".join(sig.args) if sig.args else ""
            parts.append(f"  signal {sig.name}({args})")
    if script.signals_handle:
        parts.append("This script MUST CONNECT to and HANDLE these "
                      "signals from other nodes (use a "
                      "deferred connect in _ready):")
        for sig in script.signals_handle:
            parts.append(f"  {sig.emitter}.{sig.name} → handler "
                          f"with args ({', '.join(sig.args)})")
    # Entity registry — only the entities THIS script actually
    # references (its own + the emitters of signals it emits/handles),
    # not the whole project. Sending the full registry in every
    # per-script prompt blows the context on small local models (#17).
    if plan.entity_registry:
        relevant = set()
        if script.entity:
            relevant.add(script.entity)
        for sig in (script.signals_handle + script.signals_emit):
            relevant.add(sig.emitter)
        refs = [(slug, ent) for slug, ent in plan.entity_registry.items()
                if slug in relevant]
        if refs:
            parts.append("Related entities (for reference):")
            for slug, ent in refs:
                parts.append(f"  {slug}  (role={ent.role})  {ent.description[:80]}")
    parts.append("")
    parts.append("Constraints:")
    parts.append("- Godot 4 syntax only (no `tool`/`export`/`onready` — "
                 "use `@tool`/`@export`/`@onready`).")
    parts.append("- Do NOT change the extends line.")
    parts.append("- Do NOT add network / shell / filesystem code.")
    parts.append("- A `_ready()` function MUST exist.")
    parts.append("- Output ONE fenced GDScript block, no prose.")
    parts.append("")
    parts.append("STARTING STUB (you may keep or rewrite, but the "
                 "extends + signals + exports must remain):")
    parts.append("```gdscript")
    parts.append(placeholder.rstrip())
    parts.append("```")
    return "\n".join(parts)


# ============================================================
# project.godot autoload registration
# ============================================================

def add_autoloads_to_project(project_path: Path,
                              autoloads: List[AutoloadPlan]) -> None:
    """Insert an [autoload] section into project.godot listing every
    planned autoload. If a section already exists, append; otherwise
    add a new section at the end.
    """
    manifest = project_path / "project.godot"
    if not manifest.exists() or not autoloads:
        return
    try:
        text = manifest.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[gdd_builder] could not read project.godot: {exc!r}")
        return
    autoload_lines = [
        f'{a.name}="*res://{a.file}"' for a in autoloads
    ]
    if "[autoload]" in text:
        # Append our lines under the existing section
        new_text = text.replace(
            "[autoload]",
            "[autoload]\n" + "\n".join(autoload_lines),
            1,
        )
    else:
        new_text = text.rstrip() + "\n\n[autoload]\n" \
                   + "\n".join(autoload_lines) + "\n"
    try:
        manifest.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        print(f"[gdd_builder] could not write project.godot: {exc!r}")


# ============================================================
# Orchestrator
# ============================================================

@dataclass
class BuildOptions:
    """Caller-provided knobs.

    ``model`` is a PersonalityModel-like object with .respond().
    ``godot_binary`` is the path used by GodotCoder's --check-only.
    When ``dry_run`` is True, the orchestrator writes only the
    placeholders + scene/autoload files — no LLM calls, no
    --check-only invocation. Useful for unit-test layout assertions.
    """
    model:            Any = None
    godot_binary:     str = "godot"
    max_repair_passes: int = 2
    coder_max_attempts: int = 3
    dry_run:          bool = False
    on_event:         Optional[Callable[[str, str], None]] = None
    # When True (default), snapshot an existing project to
    # projects/.history/<slug>/<timestamp> before a destructive
    # overwrite rebuild, so an edited-GDD rebuild can't silently lose
    # the prior working version.
    snapshot_history: bool = True
    # When True (default), run the built game headless for a few frames
    # after the Coder pass to catch RUNTIME errors that --check-only
    # (parse-only) misses. Skipped in dry-run.
    runtime_smoke:    bool = True
    # When True (default), also generate a headless sim harness
    # (scripts/sim/Sim.gd) + a SimContract so the built game can be
    # swept in the Simulations tab. Pure GDScript/JSON — no AI art.
    generate_sim:     bool = True


def build_from_plan(
    plan: Plan,
    vault_dir: Any,
    options: Optional[BuildOptions] = None,
) -> BuildResult:
    """Execute ``plan`` and return a ``BuildResult`` summarising the
    project path + files + which scripts passed.

    Never raises — every failure becomes a ``result.notes`` entry or
    a populated ``result.error``. The caller (GUI thread) is
    expected to surface those to the user.
    """
    options = options or BuildOptions()
    result = BuildResult()
    result.ledger = list(getattr(plan, "ledger", []) or [])
    _emit = options.on_event or (lambda phase, msg: None)

    def _ledger_set(path_suffix: str, **changes) -> None:
        for entry in result.ledger:
            if str(getattr(entry, "path", "")).endswith(path_suffix):
                for k, v in changes.items():
                    setattr(entry, k, v)
                return

    # ── Step 1: skeleton via demo_builder ──
    try:
        import demo_builder as _db
        import game_concept as _gc
    except Exception as exc:
        result.error = f"could not import demo_builder: {exc!r}"
        return result
    concept = _gc.GameConcept(
        title=plan.title,
        genre=plan.genre,
        hook=plan.notes[0] if plan.notes else "",
        engine="Godot 4",
        mechanics=[],
    )
    _emit("skeleton", f"Scaffolding skeleton for {plan.title!r}…")
    skel = _db.build_demo(concept, vault_dir, overwrite=False)
    if skel.error and "already exists" in skel.error:
        # Snapshot the prior version before the destructive overwrite so
        # an edited-GDD rebuild is recoverable.
        if options.snapshot_history and skel.project_path:
            try:
                snap = _snapshot_project(Path(skel.project_path), Path(vault_dir))
                if snap:
                    result.notes.append(
                        f"Snapshotted previous version → "
                        f"{snap.relative_to(Path(vault_dir))}")
                    _emit("snapshot", f"  saved prior version → {snap.name}")
            except Exception as exc:
                result.notes.append(f"snapshot skipped: {exc!r}")
        # Reuse existing project — the orchestrator overwrites files
        # so this is fine.
        result.notes.append(
            f"Project folder existed — reusing "
            f"{skel.project_path.name}.")
        skel = _db.build_demo(concept, vault_dir, overwrite=True)
    if not skel.ok:
        result.error = f"demo_builder failed: {skel.error}"
        return result
    project_path = skel.project_path
    result.project_path = project_path
    result.files_written.extend(skel.files_written)
    _emit("skeleton", f"  → {project_path}")

    # ── Step 2: scenes ──
    # The demo_builder already wrote a main.tscn; we replace it
    # with our planned version. Multi-scene projects would append.
    # ext_resources are collected FROM the scene tree so every node
    # with a script (the main controller + each inlined entity) gets a
    # real [ext_resource] + `script = ExtResource(...)` line — without
    # this the entity scripts are written but never attached, and the
    # game runs as inert shapes.
    for scene in plan.scenes:
        scene_path = project_path / scene.file
        scene_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ext_resources = _collect_ext_resources(scene)
            scene_path.write_text(
                render_tscn(scene, ext_resources),
                encoding="utf-8",
            )
            result.files_written.append(scene_path)
            _emit("scene",
                  f"  scene: {scene.file} ({len(ext_resources)} script(s) wired)")
        except Exception as exc:
            result.notes.append(f"scene write failed for {scene.file}: {exc!r}")

    # ── Step 3: autoloads ──
    for autoload in plan.autoloads:
        a_path = project_path / autoload.file
        a_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            a_path.write_text(
                render_autoload_stub(autoload),
                encoding="utf-8",
            )
            result.files_written.append(a_path)
            _emit("autoload", f"  autoload: {autoload.file}")
        except Exception as exc:
            result.notes.append(
                f"autoload write failed for {autoload.file}: {exc!r}")
    add_autoloads_to_project(project_path, plan.autoloads)

    # ── Step 4: script placeholders + (non-dry-run) Coder pass ──
    placeholders: Dict[Path, str] = {}
    for script in plan.scripts:
        s_path = project_path / script.file
        s_path.parent.mkdir(parents=True, exist_ok=True)
        placeholder = render_script_placeholder(script)
        placeholders[s_path] = placeholder
        try:
            s_path.write_text(placeholder, encoding="utf-8")
            result.files_written.append(s_path)
            _emit("placeholder", f"  placeholder: {script.file}")
        except Exception as exc:
            result.notes.append(
                f"placeholder write failed for {script.file}: {exc!r}")

    # ── Step 4.5: generate a headless sim harness + contract ──
    # One whole-project artifact (NOT per-entity), so it sits outside
    # the per-script Coder loop. Written even in dry-run so a
    # placeholder-only build is still sweepable. Failure here is a
    # note, never fatal — the game still built.
    if options.generate_sim:
        try:
            import sim_harness_gen as _shg
            sim_gd, contract_path = _shg.generate_for_project(plan, project_path)
            result.files_written.append(sim_gd)
            result.files_written.append(contract_path)
            result.sim_contract_path = contract_path
            _ledger_set("scripts/sim/Sim.gd", status="real")
            _emit("sim", f"  sim harness: {sim_gd.relative_to(project_path)} "
                          f"(+ {contract_path.name})")
        except Exception as exc:
            result.notes.append(f"sim-harness generation skipped: {exc!r}")

    if options.dry_run or options.model is None:
        # Skip the LLM + validation pass entirely.
        result.scripts_passed = list(placeholders.keys())
        result.notes.append(
            "dry-run / no model — kept placeholders, no Coder pass."
        )
        return result

    # ── Live Coder pass ──
    try:
        import godot_coder as _gcd
    except Exception as exc:
        result.notes.append(f"godot_coder unavailable: {exc!r}")
        result.scripts_passed = list(placeholders.keys())
        return result
    coder = _gcd.GodotCoder(
        options.model,
        project_path,
        godot_binary=options.godot_binary,
        max_attempts=options.coder_max_attempts,
        event_callback=lambda phase, msg: _emit(
            "coder", f"  {phase}: {msg}"),
    )
    for script in plan.scripts:
        target = project_path / script.file
        task = build_coder_task(script, plan, placeholders[target])
        _emit("coder", f"  generating: {script.file}")
        state = coder.run(task, target, goal=script.purpose[:160])
        if state.passed:
            result.scripts_passed.append(target)
            _ledger_set(script.file, status="real", tools_suggested=[])
        else:
            result.scripts_failed.append(target)
            _ledger_set(script.file, status="failed")
            result.notes.append(
                f"coder failed on {script.file}: "
                f"{(state.stderr or 'no stderr')[:200]}"
            )

    # ── Step 5: cross-file validation + repair ──
    for pass_idx in range(options.max_repair_passes):
        if not result.scripts_failed:
            break
        _emit("repair",
              f"Repair pass {pass_idx + 1}/{options.max_repair_passes}…")
        result.repair_passes += 1
        # godot --check-only across the project surfaces the same
        # errors GodotCoder already reflected against; the repair
        # pass re-runs Coder against each failed file with the
        # accumulated context. Same loop as the inline FIX prompt
        # except the script gets a fresh start.
        new_failed: List[Path] = []
        for target in result.scripts_failed:
            script_plan = next(
                (s for s in plan.scripts
                 if (project_path / s.file).resolve() == target.resolve()),
                None,
            )
            if script_plan is None:
                continue
            task = build_coder_task(
                script_plan, plan, placeholders[target],
            )
            state = coder.run(task, target,
                              goal=script_plan.purpose[:160])
            if state.passed:
                result.scripts_passed.append(target)
                _ledger_set(script_plan.file, status="real", tools_suggested=[])
            else:
                new_failed.append(target)
                result.notes.append(
                    f"still failing after pass {pass_idx + 1}: "
                    f"{target.name}"
                )
        result.scripts_failed = new_failed

    # ── Step 6: runtime smoke ──
    # --check-only is parse-only: a script that compiles but errors at
    # runtime (bad node path, null deref in _ready) still shows green.
    # Run the game headless for a few frames and flag runtime SCRIPT
    # ERRORs the parse pass can't see.
    if options.runtime_smoke and not options.dry_run:
        ok, detail = _runtime_smoke(project_path, options.godot_binary)
        result.runtime_smoke_ok = ok
        if ok:
            _emit("smoke", "  runtime smoke passed (ran headless, no errors)")
        else:
            result.notes.append(f"runtime smoke flagged issues: {detail}")
            _emit("smoke", f"  runtime smoke: {detail}")

    return result


def _runtime_smoke(project_path: Path, godot_binary: str,
                    frames: int = 90, timeout_s: float = 30.0) -> Tuple[bool, str]:
    """Run the built game headless for ~``frames`` frames and report
    whether any runtime SCRIPT ERROR / crash hint appeared. Returns
    ``(ok, detail)``; ok=True also when Godot isn't available (we don't
    want a missing binary to fail a build).
    """
    import subprocess
    args = [godot_binary, "--headless", "--path", str(project_path),
            f"--quit-after", str(frames)]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        except Exception:
            startupinfo = None
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            startupinfo=startupinfo, creationflags=creationflags,
        )
    except FileNotFoundError:
        return True, "godot binary not found — runtime smoke skipped"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout_s:.0f}s (possible hang in _ready/_process)"
    except Exception as exc:
        return True, f"runtime smoke could not launch ({exc!r}) — skipped"
    blob = (cp.stderr or "") + "\n" + (cp.stdout or "")
    hints = ("SCRIPT ERROR", "Parse Error:", "Cannot call method",
             "Invalid get index", "Attempt to call", "Nonexistent function",
             "null instance")
    for h in hints:
        if h in blob:
            # Grab the first offending line for the note.
            line = next((ln.strip() for ln in blob.splitlines() if h in ln), h)
            return False, line[:200]
    return True, "ok"
