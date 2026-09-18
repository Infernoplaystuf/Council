"""
council_core.designer_project — draw a wireframe, get a working app.

WHAT THIS IS
The Designer's project operations, with no toolkit: new, open, save, generate,
detach, and the source-gathering for a Council review. Everything here was
inside `_build_gui_designer_tab`'s helpers, where the biggest of them —
generate — is 110 lines of pipeline living in a method that needs a display to
import.

GENERATE IS THE WHOLE STORY
classify → infer layout → build spec → validate → plan port renames →
orphan-check → back up → emit → policy-gate → save the manifest. Nine steps,
four of which can REFUSE, and each refusal has wording that matters:

    BLOCKED — hand-written code still uses these: ...

is a promise that the user's own app.py will not be broken by a regeneration,
and it is the only thing standing between "I renamed a button" and a traceback
in a file the Designer never wrote. That wording is not decoration, so it lives
here rather than being re-typed per front end.

THE RESULT IS LINES PLUS QUESTIONS, NOT A LOG CALLBACK
The Tk version appends to a list and marshals it back with after(0). Returning
the list instead means a test can assert on what a generation SAID, which is
where all four refusals actually live. The caller decides how lines reach a
widget.

A MODEL IS CONSULTED ONLY WHEN A SHAPE IS UNTYPED
`gui_classify.needs_model` decides that, not this module. An all-typed
wireframe generates with no model call at all, which is why the Designer works
with no model loaded — worth preserving exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

#: The canvas the user actually drags on. NOT gui_projects' 1280x800 default:
#: layout inference measures edge-anchoring against these numbers, so a project
#: created with the default infers anchors for a canvas nobody drew on.
CANVAS_W, CANVAS_H = 1100, 700

#: The classification call, exactly as the Tk shell makes it.
CLASSIFY_TEMPERATURE, CLASSIFY_NUM_PREDICT, CLASSIFY_TIMEOUT = 0.1, 700, 180


def default_model_call(prompt: str) -> str:
    """The classification call. Separated so a test never reaches a model."""
    import council_engine
    return council_engine.local_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=CLASSIFY_TEMPERATURE, num_predict=CLASSIFY_NUM_PREDICT,
        timeout=CLASSIFY_TIMEOUT)


#: Settings for the critique call. Advisory only — a review NEVER edits code.
REVIEW_PROMPT = ("Critique this generated Tkinter UI for LAYOUT, ACCESSIBILITY "
                 "and RESIZE BEHAVIOUR only. Do not rewrite it; describe what "
                 "to change and why.\n\n")
REVIEW_SOURCE_LIMIT = 12000


@dataclass
class GenerateResult:
    """What a generation said, and what it still needs to ask."""
    ok: bool = False
    lines: List[str] = field(default_factory=list)
    #: gui_classify's clarification questions — things the model was unsure of.
    questions: List[Any] = field(default_factory=list)
    #: True when the pipeline refused rather than failed: a validation error, a
    #: port-rename collision, an orphaned reference, or a detached project.
    blocked: bool = False

    def say(self, line: str) -> None:
        self.lines.append(line)


@dataclass
class ProjectResult:
    ok: bool
    message: str = ""
    name: str = ""
    shapes: List[Any] = field(default_factory=list)
    project: Any = None


# ============================================================
# New / open / save
# ============================================================

def create(name: str, mode: str, vault_dir: Any) -> ProjectResult:
    """A new, empty project."""
    import gui_projects

    try:
        gui_projects.create(name, mode, vault_dir=vault_dir)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    return ProjectResult(True, f"created {name} ({mode})", name=name)


def create_from_wizard(result: Any, vault_dir: Any) -> ProjectResult:
    """Apply a finished wizard.

    Always a NEW project, which is what makes the destructive load() and the
    widget-name registry safe here. The wizard itself writes nothing — it hands
    back a layout and this applies it — so cancelling leaves nothing behind.
    """
    import gui_projects

    try:
        gui_projects.create(result.name, result.mode, vault_dir=vault_dir)
        project = gui_projects.open_project(result.name, vault_dir=vault_dir)
        project.window.title = result.title
        project.window.min_w, project.window.min_h = result.min_w, result.min_h
        project.canvas.w, project.canvas.h = CANVAS_W, CANVAS_H
        project.shapes = list(result.shapes)
        gui_projects.save_project(result.name, project, vault_dir=vault_dir)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    return ProjectResult(
        True, f"created {result.name} ({result.mode}) from the wizard",
        name=result.name, shapes=list(result.shapes), project=project)


def open_named(name: str, vault_dir: Any) -> ProjectResult:
    import gui_projects

    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    return ProjectResult(True,
                         f"opened {name} ({len(project.shapes)} shape(s))",
                         name=name, shapes=list(project.shapes),
                         project=project)


def list_names(vault_dir: Any) -> List[str]:
    import gui_projects

    try:
        return list(gui_projects.list_projects(vault_dir=vault_dir))
    except Exception:
        return []


def save(name: str, shapes: Sequence[Any], vault_dir: Any) -> ProjectResult:
    """Write the current shapes back to the .gspec."""
    import gui_projects

    if not name:
        return ProjectResult(False, "No project open.")
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        project.shapes = list(shapes)
        gui_projects.save_project(name, project, vault_dir=vault_dir)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    return ProjectResult(True, f"saved {name}", name=name, project=project)


def apply_window(name: str, values: Dict[str, Any], shapes: Sequence[Any],
                 vault_dir: Any) -> ProjectResult:
    """The window panel's Apply: title, min size, colours, and `requires`.

    Saved through the same helper as everything else so bg / title / min size
    survive a regeneration.
    """
    import gui_policy
    import gui_projects

    if not name:
        return ProjectResult(False, "No project open.")
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        for key, value in values.items():
            if key == "requires":
                # Project-level, but edited in this panel because it is the
                # only place the user ever sees the window's own settings.
                project.requires = gui_policy.parse_requires(value)
            elif hasattr(project.window, key):
                setattr(project.window, key, value)
        project.shapes = list(shapes)
        gui_projects.save_project(name, project, vault_dir=vault_dir)
    except Exception as exc:
        return ProjectResult(False, str(exc))

    notes = [f"! {problem}"
             for problem in gui_policy.check_requires(project.requires)]
    requires = ", ".join(getattr(project, "requires", []) or []) or "(none)"
    notes.append(f"window: bg={project.window.bg or '(none)'} "
                 f"title={project.window.title!r} requires={requires}")
    return ProjectResult(True, "\n".join(notes), name=name, project=project)


def detach(project_dir: Any) -> ProjectResult:
    """Merge ui/ into the project and disable regeneration. ONE WAY.

    The caller must have confirmed. This does not ask, because a core module
    that asked would need a toolkit to ask with.
    """
    import gui_projects

    try:
        merged = gui_projects.detach(Path(project_dir))
    except Exception as exc:
        return ProjectResult(False, str(exc))
    return ProjectResult(True, f"detached — merged into {merged.name}")


# ============================================================
# Generate
# ============================================================

def generate(name: str, shapes: Sequence[Any], project_dir: Any,
             vault_dir: Any, *,
             model_call: Optional[Callable[[str], str]] = None
             ) -> GenerateResult:
    """Classify → spec → validate → orphan-check → back up → emit → policy.

    Long, and deliberately not split: every step reads state the previous one
    wrote, and the four refusals have to be able to return from the middle. A
    version with a function per step would pass the same eight values through
    each one.
    """
    import gui_classify
    import gui_emit
    import gui_layout
    import gui_policy
    import gui_projects
    import gui_spec

    out = GenerateResult()
    project_dir = Path(project_dir)
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        manifest = gui_projects.load_manifest(project_dir)
        if manifest.detached:
            out.say("this project is detached — regeneration is disabled")
            out.blocked = True
            return out

        classifications = []
        if gui_classify.needs_model(shapes):
            # A model is consulted ONLY here, and only when a shape is untyped.
            # An all-typed wireframe generates with no model call at all, which
            # is why the Designer works with no model loaded.
            first_pass = gui_layout.infer(shapes, project.canvas.w,
                                          project.canvas.h)
            classifications, out.questions = gui_classify.classify(
                shapes, first_pass, model_call or default_model_call)
            out.say(f"classified {len(classifications)} untyped shape(s)")
        else:
            out.say("no untyped shapes — generated with no model call")

        tree = gui_layout.infer(shapes, project.canvas.w, project.canvas.h)
        spec = gui_spec.build(
            shapes, tree,
            gui_classify.apply_classifications(classifications),
            registry=manifest.widget_names,
            port_registry=getattr(manifest, "port_names", {}) or {},
            project=name, mode=manifest.mode,
            title=project.window.title,
            min_w=project.window.min_w, min_h=project.window.min_h,
            root_bg=getattr(project.window, "bg", "") or "",
            root_fg=getattr(project.window, "fg", "") or "",
            root_font=getattr(project.window, "font", "") or "",
            requires=getattr(project, "requires", []) or [])
        for warning in tree.warnings:
            out.say(f"warning: {warning}")

        valid, errors = gui_spec.validate(spec)
        if not valid:
            out.lines.extend(f"cannot generate: {e}" for e in errors)
            out.blocked = True
            return out

        # Renames are planned FIRST so an aliased old name is redirected rather
        # than orphaned — otherwise renaming a port and regenerating reports
        # the user's own working code as a dangling reference.
        new_ports = (spec.port_registry()
                     if hasattr(spec, "port_registry") else {})
        plan = gui_projects.plan_ports(
            project_dir, getattr(manifest, "port_names", {}) or {}, new_ports)
        # A sequence link also puts `browse_<index>` on Ports, and path()/
        # count() on it are the natural way to name the frame you are looking
        # at. That attribute is not a PORT, so find_orphans called it dead and
        # BLOCKED Generate forever on any app.py that used it.
        valid_ports = (set(new_ports.values()) | set(plan.aliases)
                       | spec.sequence_attr_names())
        orphans = gui_projects.find_orphans(project_dir, spec.widget_names,
                                            new_port_names=valid_ports)
        if plan.removed or plan.collisions:
            out.say("BLOCKED — port rename cannot proceed:")
            out.lines.extend("  " + o.describe() for o in plan.removed)
            out.lines.extend("  " + c for c in plan.collisions)
            out.blocked = True
            return out
        for old, new in plan.renamed:
            tag = " (aliased for now)" if old in plan.aliases else ""
            out.say(f"port renamed: {old} -> {new}{tag}")

        if orphans:
            out.say("BLOCKED — hand-written code still uses these:")
            out.lines.extend("  " + o.describe() for o in orphans)
            out.blocked = True
            return out

        edited = gui_projects.hand_edited_ui_files(project_dir)
        if edited:
            out.say(f"note: ui/ edits will be overwritten: "
                    f"{', '.join(edited)} (backed up first)")
        gui_projects.backup(project_dir)
        written = gui_emit.emit(spec, project_dir, aliases=plan.aliases)
        out.say(f"wrote {len(written.files_written)} file(s); "
                f"kept {len(written.files_skipped)} hand-written")
        if written.handlers_added:
            out.say("new handler stubs: " + ", ".join(written.handlers_added))
        out.lines.extend(written.warnings)

        # The project's declared `requires` widen its allowlist — a camera
        # app's SDK — and nothing else does. Run applies the same gate, so this
        # message is a promise rather than a warning.
        policy_ok, policy_errors = gui_policy.validate_dir(
            project_dir, manifest.mode, spec.requires)
        out.say("policy: OK" if policy_ok
                else "policy REFUSED — Run will not start it until this is "
                     "fixed:")
        out.lines.extend("  " + e for e in policy_errors)

        manifest.widget_names = spec.name_registry()
        manifest.port_names = new_ports
        manifest.ui_checksums = gui_projects.ui_checksums(project_dir)
        # 'Run with' may have changed while this ran; keep it.
        manifest.python = gui_projects.load_manifest(project_dir).python
        gui_projects.save_manifest(project_dir, manifest)
    except Exception as exc:
        out.say(f"generate failed: {exc!r}")
        return out
    out.ok = True
    return out


def describe_questions(questions: Sequence[Any]) -> List[str]:
    """The clarifications, as the lines the log shows."""
    return [f"?  {q.question}  [{' | '.join(q.options)}]  "
            f"(default: {q.default})" for q in questions]


# ============================================================
# Review
# ============================================================

def review_sources(project_dir: Any) -> str:
    """Every generated ui/*.py, concatenated and labelled.

    Empty when there is nothing generated yet, which is how a caller knows to
    say "Generate the project first" rather than sending a model an empty
    prompt and charging the user a round trip for it.
    """
    ui = Path(project_dir) / "ui"
    if not (ui / "main_ui.py").exists():
        return ""
    return "\n\n".join(
        f"# ---- {path.name} ----\n"
        + path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(ui.glob("*.py")))


def review_prompt(project_dir: Any) -> str:
    """The critique prompt, or "" when there is nothing to critique."""
    sources = review_sources(project_dir)
    if not sources:
        return ""
    return REVIEW_PROMPT + sources[:REVIEW_SOURCE_LIMIT]


# ============================================================
# Which Python runs the preview
# ============================================================
# Per project, in its manifest. A camera app needs its SDK's environment, not
# the Council's own Python — that is the whole reason the setting exists.

def interpreter_of(project_dir: Any) -> str:
    """The project's chosen interpreter spec, or "" for the default.

    Never raises. A manifest that cannot be read means "no choice recorded",
    which is the same thing the default means — and a picker that threw while
    merely DISPLAYING a setting would take the tab down on a project the user
    could otherwise still open.
    """
    if not project_dir:
        return ""
    try:
        import gui_projects
        return gui_projects.load_manifest(Path(project_dir)).python or ""
    except Exception:                                    # noqa: BLE001
        return ""


def set_interpreter(project_dir: Any, spec: str) -> ProjectResult:
    """Record which Python runs this project's preview.

    Refused with no project open, because the choice is SAVED WITH THE PROJECT
    and there is nowhere to put it otherwise. Silently accepting it would look
    like it worked and be gone on the next open.
    """
    if not project_dir:
        return ProjectResult(False, "Open or create a project first — the "
                                    "choice is saved with the project.")
    try:
        import gui_projects
        directory = Path(project_dir)
        manifest = gui_projects.load_manifest(directory)
        manifest.python = spec
        gui_projects.save_manifest(directory, manifest)
    except Exception as exc:                             # noqa: BLE001
        return ProjectResult(False, str(exc))
    return ProjectResult(True, "")
