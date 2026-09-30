"""
council_core.designer_project — draw a wireframe, get a working app.

WHAT THIS IS
The Designer's project operations, with no toolkit: new, open, save, generate,
detach, and the source-gathering for a Council review. Everything here was
inside `_build_gui_designer_tab`'s helpers, where the biggest of them —
generate — is 110 lines of pipeline living in a method that needs a display to
import.

GENERATE IS THE WHOLE STORY
classify → infer layout → build spec → validate → plan handlers.py's own stub
edits → plan port renames → orphan-check → back up → emit → policy-gate →
save the manifest. Ten steps, four of which can REFUSE, and each refusal has
wording that matters:

    BLOCKED — hand-written code still uses these: ...

is a promise that the user's own app.py will not be broken by a regeneration,
and it is the only thing standing between "I renamed a button" and a traceback
in a file the Designer never wrote. That wording is not decoration, so it lives
here rather than being re-typed per front end.

The promise protects code a PERSON wrote. A handler stub exactly as the
emitter wrote it is not that, so a stub whose button was rewired is rewritten,
one whose button was deleted is removed, and an EDITED one whose link changed
is left alone and named in a WARNING line with its file:line
(gui_emit.plan_handlers). Those edits are planned before the checks run, so
the checks judge handlers.py as it will be.

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


#: The "Describe it" call. Its own numbers, not the classifier's: a whole
#: wireframe is 10-40 shapes of JSON, and 700 tokens cut one off mid-list,
#: which then fails to parse and costs a repair round to say so. `coder` is
#: the role that writes structured output; with no slot assigned it is the
#: main model, which is what every caller got before roles existed.
DESCRIBE_TEMPERATURE, DESCRIBE_NUM_PREDICT, DESCRIBE_ROLE = 0.1, 1800, "coder"


def describe_model_call(prompt: str) -> str:
    """The Describe-it call. Separated so a test never reaches a model."""
    import council_engine
    return council_engine.local_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=DESCRIBE_TEMPERATURE, num_predict=DESCRIBE_NUM_PREDICT,
        role=DESCRIBE_ROLE)


#: Settings for the critique call. Advisory only — a review NEVER edits code.
REVIEW_PROMPT = ("Critique this generated Tkinter UI for LAYOUT, ACCESSIBILITY "
                 "and RESIZE BEHAVIOUR only. Do not rewrite it; describe what "
                 "to change and why.\n\n")
#: The same critique for a Qt project. Telling a model it is reading Tkinter
#: while showing it QGridLayout calls gets a review of the wrong toolkit —
#: "use grid_propagate" advice for code that has no such thing.
REVIEW_PROMPT_QT = ("Critique this generated Qt (PySide) UI for LAYOUT, "
                    "ACCESSIBILITY and RESIZE BEHAVIOUR only. Do not rewrite "
                    "it; describe what to change and why.\n\n")
REVIEW_SOURCE_LIMIT = 12000

#: What the user sees for each toolkit. The values are gui_projects.TOOLKITS.
TOOLKIT_LABELS = {"tk": "Tk", "qt": "Qt"}


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

def create(name: str, mode: str, vault_dir: Any,
           toolkit: str = "tk") -> ProjectResult:
    """A new, empty project, generated into `toolkit` ('tk' or 'qt').

    The toolkit is chosen HERE because it cannot be changed later: app.py is
    written once, in one toolkit, and Generate refuses to put the other
    toolkit's ui/ beside it. Asking at the first Generate instead would be
    asking after the user has drawn the whole thing.

    The canvas is set to the one the user drags on. gui_projects' default is
    1280x800, and layout inference measures edge-anchoring against the canvas
    size — so a New project used to infer "anchored right" for a shape the user
    had put 180 px short of the edge they could see.
    """
    import gui_projects

    try:
        gui_projects.create(name, mode, vault_dir=vault_dir, toolkit=toolkit)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        project.canvas.w, project.canvas.h = CANVAS_W, CANVAS_H
        gui_projects.save_project(name, project, vault_dir=vault_dir)
    except Exception as exc:
        _discard_new(name, vault_dir)
        return ProjectResult(False, str(exc))
    return ProjectResult(
        True, f"created {name} ({mode}, {TOOLKIT_LABELS.get(toolkit, toolkit)})",
        name=name)


def create_from_wizard(result: Any, vault_dir: Any) -> ProjectResult:
    """Apply a finished wizard.

    Always a NEW project, which is what makes the destructive load() and the
    widget-name registry safe here. The wizard itself writes nothing — it hands
    back a layout and this applies it — so cancelling leaves nothing behind.

    A failure AFTER the directory exists removes it again. Otherwise the
    retry — same answers, the obvious thing to do — fails with "already
    exists" for a project that was never finished.
    """
    import gui_projects

    toolkit = getattr(result, "toolkit", "") or "tk"
    try:
        gui_projects.create(result.name, result.mode, vault_dir=vault_dir,
                            toolkit=toolkit)
    except Exception as exc:
        return ProjectResult(False, str(exc))
    try:
        project = gui_projects.open_project(result.name, vault_dir=vault_dir)
        project.window.title = result.title
        project.window.min_w, project.window.min_h = result.min_w, result.min_h
        project.canvas.w, project.canvas.h = CANVAS_W, CANVAS_H
        project.shapes = list(result.shapes)
        gui_projects.save_project(result.name, project, vault_dir=vault_dir)
    except Exception as exc:
        _discard_new(result.name, vault_dir)
        return ProjectResult(False, str(exc))
    return ProjectResult(
        True, f"created {result.name} ({result.mode}, "
              f"{TOOLKIT_LABELS.get(toolkit, toolkit)}) from the wizard",
        name=result.name, shapes=list(result.shapes), project=project)


def _discard_new(name: str, vault_dir: Any) -> None:
    """Remove a project this call JUST created and could not finish.

    Only ever reached straight after gui_projects.create succeeded, so the
    directory holds nothing but what create wrote: an empty ui/, an empty
    backups/, a manifest and an empty .gspec. Anything else in it means it is
    not ours, and it is left alone.
    """
    import shutil
    import gui_projects

    try:
        pdir = gui_projects.project_path(name, vault_dir)
        ours = {gui_projects.MANIFEST_NAME, gui_projects.GSPEC_NAME,
                gui_projects.UI_DIRNAME, gui_projects.BACKUPS_DIRNAME}
        entries = list(pdir.iterdir())
        if any(p.name not in ours for p in entries):
            return
        if any(p.is_dir() and any(p.iterdir()) for p in entries):
            return
        shutil.rmtree(pdir)
    except Exception:                                    # noqa: BLE001
        pass


def toolkit_label(project_dir: Any) -> str:
    """"Tk" or "Qt" for a project — what it IS if generated, else what it
    was created as. "" when there is no project to ask about."""
    if not project_dir:
        return ""
    try:
        import gui_projects
        found = gui_projects.toolkit_for(Path(project_dir))
    except Exception:                                    # noqa: BLE001
        return ""
    return TOOLKIT_LABELS.get(found, found)


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


def wiring_context(name: str, vault_dir: Any):
    """(mode, requires, port registry) for the Designer's Wiring group.

    Read once per project open (and again after Generate or a window Apply),
    NOT per selection: it comes off disk, and a selection refresh that read
    two files would be the slowest thing the canvas does. Never raises — a
    project that cannot be read just offers the linked-mode defaults.
    """
    import gui_projects

    if not name:
        return "linked", [], {}
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        manifest = gui_projects.load_manifest(
            gui_projects.project_path(name, vault_dir))
    except Exception:                                    # noqa: BLE001
        return "linked", [], {}
    return (manifest.mode or "linked", list(project.requires or []),
            dict(manifest.port_names or {}))


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

        new_ports = (spec.port_registry()
                     if hasattr(spec, "port_registry") else {})
        old_ports = getattr(manifest, "port_names", {}) or {}
        valid, errors = gui_spec.validate(spec)
        if not valid:
            out.lines.extend(f"cannot generate: {e}" for e in errors)
            out.lines.extend(_rename_hints(errors, old_ports, new_ports))
            out.blocked = True
            return out

        # handlers.py's OWN edits are planned before any check reads it: an
        # untouched stub whose link changed is rewritten, one whose widget is
        # gone is removed (gui_emit.plan_handlers). The checks below then see
        # the file as it WILL be — the generator's own stub for a deleted
        # button no longer BLOCKS the generation that deletes it, and a stub
        # about to be rewritten for a renamed port needs no alias. Nothing is
        # written until every refusal has had its say.
        previous_links = dict(getattr(manifest, "script_links", {}) or {})
        sources, kept_lines = _planned_sources(project_dir, spec,
                                               previous_links)

        # Renames are planned FIRST so an aliased old name is redirected rather
        # than orphaned — otherwise renaming a port and regenerating reports
        # the user's own working code as a dangling reference.
        plan = gui_projects.plan_ports(project_dir, old_ports, new_ports,
                                       sources=sources)
        # A sequence link also puts `browse_<index>` on Ports, and path()/
        # count() on it are the natural way to name the frame you are looking
        # at. That attribute is not a PORT, so find_orphans called it dead and
        # BLOCKED Generate forever on any app.py that used it.
        valid_ports = (set(new_ports.values()) | set(plan.aliases)
                       | spec.sequence_attr_names())
        orphans = gui_projects.find_orphans(project_dir, spec.widget_names,
                                            new_port_names=valid_ports,
                                            sources=sources)
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
            # A stub kept only because hand-written code CALLS it is cited
            # above by its own def; this names the call the user must change.
            out.lines.extend("  " + line for line in kept_lines)
            out.blocked = True
            return out

        edited = gui_projects.hand_edited_ui_files(project_dir)
        if edited:
            out.say(f"note: ui/ edits will be overwritten: "
                    f"{', '.join(edited)} (backed up first)")
        gui_projects.backup(project_dir)
        # WHICH TOOLKIT. app.py is created once and never rewritten, so a
        # project that already HAS one can only be regenerated into the same
        # toolkit — emitting a Qt ui/ beside a Tk app.py would produce a
        # project that cannot start, and the failure would look like a bug in
        # the wireframe. A new project takes the manifest's intent.
        built_as = gui_projects.toolkit_of(project_dir)
        wanted = (getattr(manifest, "toolkit", "") or "tk")
        if built_as and built_as != wanted:
            out.say(f"this project's app.py is written in {built_as}, but its "
                    f"manifest asks for {wanted} — regenerating would leave a "
                    f"ui/ that app.py cannot drive. Create a new project "
                    f"instead.")
            out.blocked = True
            return out
        target = built_as or wanted
        written = gui_emit.emit(spec, project_dir, aliases=plan.aliases,
                                target=target, previous_links=previous_links)
        out.say(f"wrote {len(written.files_written)} file(s); "
                f"kept {len(written.files_skipped)} hand-written")
        if written.handlers_added:
            out.say("new handler stubs: " + ", ".join(written.handlers_added))
        out.lines.extend(written.warnings)

        # The project's declared `requires` widen its allowlist — a camera
        # app's SDK — and nothing else does. Run applies the same gate, so this
        # message is a promise rather than a warning. It is checked as the
        # toolkit just EMITTED: checked as Tk, every correct Qt project said
        # "policy REFUSED — the Qt binding is not on the linked allowlist"
        # here while Run, which passes the toolkit, launched it without
        # complaint.
        policy_ok, policy_errors = gui_policy.validate_dir(
            project_dir, manifest.mode, spec.requires, toolkit=target)
        out.say("policy: OK" if policy_ok
                else "policy REFUSED — Run will not start it until this is "
                     "fixed:")
        out.lines.extend("  " + e for e in policy_errors)

        manifest.widget_names = spec.name_registry()
        manifest.port_names = new_ports
        manifest.script_links = _recorded_links(
            gui_emit.script_links(spec), previous_links,
            written.handlers_stale)
        manifest.ui_checksums = gui_projects.ui_checksums(project_dir)
        # 'Run with' may have changed while this ran; keep it.
        manifest.python = gui_projects.load_manifest(project_dir).python
        gui_projects.save_manifest(project_dir, manifest)
    except Exception as exc:
        out.say(f"generate failed: {exc!r}")
        return out
    out.ok = True
    return out


def _planned_sources(project_dir: Path, spec: Any,
                     previous_links: Dict[str, Any]
                     ) -> Tuple[Dict[str, str], List[str]]:
    """({"handlers.py": its text after this generation's own stub edits},
    the lines naming stubs kept because hand-written code calls them) — or
    ({}, []) when there is no handlers.py yet. Pure — writes nothing; emit
    makes the same plan again and writes it, after every refusal has passed.
    """
    import gui_emit

    handlers = project_dir / "handlers.py"
    if not handlers.is_file():
        return {}, []
    src = handlers.read_text(encoding="utf-8", errors="replace")
    plan = gui_emit.plan_handlers(
        src, spec, previous_links,
        callers=gui_emit.hand_written_callers(project_dir))
    return {"handlers.py": plan.source}, plan.kept_lines()


def _recorded_links(links: Dict[str, Any], previous: Dict[str, Any],
                    stale: Sequence[str]) -> Dict[str, Any]:
    """The manifest's script_links after a Generate.

    Today's link for every handler — EXCEPT one Generate had to leave stale
    (edited by hand, and its link changed under it). That one keeps the
    record of the link it was last generated for, so the next Generate still
    sees the change and names it again, until the user brings the handler in
    line or deletes it. Recording today's link instead would have made the
    WARNING a one-off, and every later Generate a silent "policy: OK" over a
    handler that still calls the old thing. With no old record there is
    nothing to keep, and the handler goes unrecorded (judged from its code).
    """
    out = dict(links)
    for name in stale:
        if name in previous:
            out[name] = previous[name]
        else:
            out.pop(name, None)
    return out


def _rename_hints(errors: Sequence[str], old_ports: Dict[str, str],
                  new_ports: Dict[str, str]) -> List[str]:
    """A line for each refusal that names a port this wireframe RENAMED.

    "frame_camera needs a port named 'live_view'" is true and leaves the user
    to remember what they did. The manifest knows: the shape that had that
    port still exists, under another name — so say which.
    """
    renamed = {old: new_ports[key] for key, old in old_ports.items()
               if key in new_ports and new_ports[key] != old}
    hints = []
    for old, new in sorted(renamed.items()):
        if any(repr(old) in error for error in errors):
            hints.append(f"  note: the port {old!r} was renamed to {new!r} "
                         f"in this wireframe — rename it back, or update "
                         f"what still asks for {old!r}.")
    return hints


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
    """The critique prompt, or "" when there is nothing to critique.

    Worded for the toolkit the project was generated in, and capped the same
    either way: the two headers are within a few characters of each other.
    """
    sources = review_sources(project_dir)
    if not sources:
        return ""
    header = REVIEW_PROMPT
    try:
        import gui_projects
        if gui_projects.toolkit_for(Path(project_dir)) == "qt":
            header = REVIEW_PROMPT_QT
    except Exception:                                    # noqa: BLE001
        pass
    return header + sources[:REVIEW_SOURCE_LIMIT]


#: The review panel: the Tk shell's, exactly. Two voices and one round is
#: enough for advice about padding, and a full deliberation over 12 000
#: characters of source is minutes the user spends waiting for an opinion.
REVIEW_PANEL, REVIEW_SYNTH = ("coder", "writer"), "writer"


def review(prompt: str, models: Any) -> str:
    """Run the critique through a one-round Council. Returns the text to show.

    `models` is a council_turn.Personalities (or anything with the role
    slots as attributes). Blocking, and meant for a worker thread. Raises on
    a model failure so the caller can say "review failed" — the Designer tab
    already turns an exception into exactly that line.

    This is what the Qt Designer's Review button had no backend for: the tab
    looked for `review_with_council` on the window, nothing defined it, and
    every click said "Council review unavailable in this build".
    """
    from .council_turn import AGENT_NAMES, final_answer
    from .deliberation import DeliberationOrchestrator, ModelAgent

    judge = getattr(models, "judge", None)
    if judge is None:
        return "review unavailable: no judge model is loaded"
    agents = {role: ModelAgent(AGENT_NAMES.get(role, role.title()),
                               getattr(models, role), enable_tools=False)
              for role in REVIEW_PANEL if getattr(models, role, None) is not None}
    if REVIEW_SYNTH not in agents:
        return f"review unavailable: no {REVIEW_SYNTH} model is loaded"
    orchestrator = DeliberationOrchestrator(
        judge_model=judge, agents=agents, max_rounds=1, debate_turns=1)
    events = orchestrator.run(prompt, panel=list(agents), synth=REVIEW_SYNTH)
    return final_answer(events or [], REVIEW_SYNTH) or "(no critique returned)"


# ============================================================
# Describe it
# ============================================================

def describe(text: str, project_dir: Any = None, *,
             model_call: Optional[Callable[[str], str]] = None) -> Any:
    """Plain English -> a wireframe that will generate, or the reasons not.

    A thin seam over gui_describe, which owns the prompt, the checks and the
    repair rounds. What this adds is the two things only a PROJECT knows: the
    toolkit to design for, and the default model call. The canvas is always
    the one the user drags on, never the project file's, for the same reason
    `create` sets it — the shapes are about to be placed on that canvas.

    Never raises; a failure is a result with ok=False and the reason.
    """
    import gui_describe

    toolkit = "tk"
    if project_dir:
        try:
            import gui_projects
            toolkit = gui_projects.toolkit_for(Path(project_dir))
        except Exception:                                # noqa: BLE001
            pass
    return gui_describe.describe(
        text, model_call=model_call or describe_model_call,
        canvas_w=CANVAS_W, canvas_h=CANVAS_H, toolkit=toolkit)


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
