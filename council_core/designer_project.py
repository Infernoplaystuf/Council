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

#: The design area a NEW project gets, which is what the user drags on until
#: they change it in the window panel. NOT gui_projects' 1280x800 default:
#: layout inference measures edge-anchoring against these numbers, so a project
#: created with the default infers anchors for a canvas nobody drew on. An
#: existing project keeps its own (Typhon's is 1504 x 1016) — the Designer's
#: canvas takes its size from the project, not from here.
CANVAS_W, CANVAS_H = 1100, 700

#: The classification call's defaults. num_predict is only the FLOOR now:
#: gui_classify sizes each call to the boxes it asks about (700 held about
#: nine 32-token ids). `coder` — the role that writes structured output —
#: as Describe uses; unassigned, it is the main model, as before.
CLASSIFY_TEMPERATURE, CLASSIFY_NUM_PREDICT, CLASSIFY_TIMEOUT = 0.1, 700, 180
CLASSIFY_ROLE = "coder"

#: The engine keywords the shared contract adds (json_schema, seed, stop,
#: should_stop). An engine from before the contract has none of them: they
#: are dropped, not sent, and the caller's own parsing does the rest.
CONTRACT_KEYWORDS = ("json_schema", "seed", "stop", "should_stop")


def local_chat(engine: Any, **kwargs: Any) -> str:
    """engine.local_chat(**kwargs), TypeError-safe for the new keywords.

    Read from the signature first, so a contract keyword the engine does not
    take is dropped before the call (no wasted generation); and if it is
    still refused — a wrapper hiding the signature — the call is retried
    once without any of them. A TypeError from INSIDE the engine is not
    mistaken for that: only "unexpected keyword" retries."""
    import inspect
    fn = engine.local_chat
    extra = {k: kwargs.pop(k) for k in CONTRACT_KEYWORDS
             if kwargs.get(k) is not None}
    kwargs = {k: v for k, v in kwargs.items() if k not in CONTRACT_KEYWORDS}
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            extra = {k: v for k, v in extra.items() if k in params}
    except (TypeError, ValueError):
        pass
    if extra:
        try:
            return fn(**kwargs, **extra)
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
    return fn(**kwargs)


def default_model_call(prompt: str, *, json_schema: Any = None,
                       num_predict: Optional[int] = None,
                       temperature: Optional[float] = None,
                       seed: Optional[int] = None) -> str:
    """The classification call. Separated so a test never reaches a model."""
    import council_engine
    return local_chat(
        council_engine,
        messages=[{"role": "user", "content": prompt}],
        temperature=CLASSIFY_TEMPERATURE if temperature is None
        else temperature,
        num_predict=max(CLASSIFY_NUM_PREDICT, int(num_predict or 0)),
        timeout=CLASSIFY_TIMEOUT, role=CLASSIFY_ROLE,
        json_schema=json_schema, seed=seed)


#: The "Describe it" call. Its own numbers, not the classifier's: a whole
#: wireframe is 10-40 shapes of JSON, and 700 tokens cut one off mid-list,
#: which then fails to parse and costs a repair round to say so. `coder` is
#: the role that writes structured output; with no slot assigned it is the
#: main model, which is what every caller got before roles existed.
DESCRIBE_TEMPERATURE, DESCRIBE_NUM_PREDICT, DESCRIBE_ROLE = 0.1, 1800, "coder"
#: Per call. The engine honours it under the shared contract; generous,
#: because a CPU-placed model writing 1800 tokens is minutes, not seconds.
DESCRIBE_TIMEOUT = 300


def describe_model_call(prompt: str, *, json_schema: Any = None,
                        seed: Optional[int] = None,
                        temperature: Optional[float] = None,
                        num_predict: Optional[int] = None,
                        should_stop: Optional[Callable[[], bool]] = None
                        ) -> str:
    """The Describe-it call. Separated so a test never reaches a model.

    Every keyword is optional and gui_describe passes only what its profile
    sets; an engine without the schema contract gets the plain call."""
    import council_engine
    return local_chat(
        council_engine,
        messages=[{"role": "user", "content": prompt}],
        temperature=DESCRIBE_TEMPERATURE if temperature is None
        else temperature,
        num_predict=int(num_predict or DESCRIBE_NUM_PREDICT),
        role=DESCRIBE_ROLE, timeout=DESCRIBE_TIMEOUT,
        json_schema=json_schema, seed=seed, should_stop=should_stop)


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
    #: shape id -> {"kind", "props"}: the classifier's answers this Generate
    #: WROTE BACK into the .gspec, so the next one asks nothing. The Designer
    #: applies the same to its canvas, or its next Save would write the
    #: untyped boxes back over them.
    classified: Dict[str, Dict[str, Any]] = field(default_factory=dict)

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
    """The window panel's Apply: title, min size, colours, `requires`, and
    the canvas — the design area's own size.

    Saved through the same helper as everything else so bg / title / min size
    survive a regeneration.

    A canvas size that would cut off a shape REFUSES THE WHOLE APPLY, with
    nothing written (see designer_geometry.canvas_problem for why refuse
    rather than warn). Whole, not "everything but the canvas": the panel
    keeps what the user typed, so fixing the one number and pressing Apply
    again sends the rest with it — whereas half an Apply that said "refused"
    would leave the user unsure which half landed.
    """
    import gui_policy
    import gui_projects

    from . import designer_geometry as geometry

    if not name:
        return ProjectResult(False, "No project open.")
    try:
        project = gui_projects.open_project(name, vault_dir=vault_dir)
        if any(key in values for key in geometry.CANVAS_KEYS):
            current = (project.canvas.w, project.canvas.h)
            wanted_w = values.get("canvas_w", project.canvas.w)
            wanted_h = values.get("canvas_h", project.canvas.h)
            problem = geometry.canvas_problem(shapes, wanted_w, wanted_h,
                                              current=current)
            if problem:
                return ProjectResult(False, f"canvas not changed — "
                                            f"{problem.rstrip('.')}. Nothing "
                                            f"else was applied.",
                                     name=name, project=project)
            project.canvas.w, project.canvas.h = int(wanted_w), int(wanted_h)
        for key, value in values.items():
            if key in geometry.CANVAS_KEYS:
                continue
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
                 f"title={project.window.title!r} requires={requires} "
                 f"canvas={project.canvas.w} x {project.canvas.h}")
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
            # The real window, so a long list of boxes keeps the props
            # catalogue when it fits — only for the engine's own model.
            window = None if model_call else _role_window(CLASSIFY_ROLE)
            classifications, out.questions = gui_classify.classify(
                shapes, first_pass, model_call or default_model_call,
                n_ctx=window)
            out.say(f"classified {len(classifications)} untyped shape(s)")
            shapes = _persist_classifications(
                name, shapes, classifications, out, vault_dir)
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


def _persist_classifications(name: str, shapes: Sequence[Any],
                             classifications: Sequence[Any],
                             out: GenerateResult, vault_dir: Any
                             ) -> List[Any]:
    """Write the classifier's answers back into the wireframe. Returns the
    shapes Generate should carry on with (copies; the caller's are not
    touched).

    gui_classify's promise is that a box is asked about ONCE — but nothing
    wrote the answer anywhere, so every Generate called the model again, and
    could get a different kind each time: a wireframe that generated a
    treeview yesterday and a listbox today. Now each answered box becomes
    that kind in the .gspec, with the catalogue's default props under the
    model's (what a palette shape gets), and a question the model was unsure
    of is recorded as a clarification, unanswered. A FLAGGED fallback is
    not an answer and is not written: the next Generate asks again.

    A failure to save is a line in the log, never a failed Generate — the
    classifications still apply to THIS generation.

    The answers are MERGED into the .gspec as it is on disk NOW, into the
    shapes there that are still untyped — never by saving the project
    Generate opened BEFORE the model call with ``shapes``: the Designer lets
    the user Save while Generate waits on the model, and that put every
    shape and window setting saved in the meantime back (review finding).
    A caller's unsaved shapes are not saved on its behalf."""
    import copy

    import gui_classify
    from gui_shapes import GENERIC_KIND, Clarification

    answers = gui_classify.persistable(classifications)
    if not answers:
        return list(shapes)
    updated = []
    for s in shapes:
        s = copy.deepcopy(s)
        got = answers.get(s.id)
        if got is not None:
            apply_answer(s, got)
        updated.append(s)
    written: Dict[str, Dict[str, Any]] = {}
    try:
        import gui_projects
        fresh = gui_projects.open_project(name, vault_dir=vault_dir)
        for s in fresh.shapes:
            got = answers.get(s.id)
            if got is not None and s.kind == GENERIC_KIND:
                apply_answer(s, got)
                written[s.id] = got
        if written:
            asked = {c.shape_id for c in fresh.clarifications}
            for q in out.questions:
                if q.shape_id in written and q.shape_id not in asked:
                    fresh.clarifications.append(Clarification(
                        shape_id=q.shape_id, question=q.question))
            gui_projects.save_project(name, fresh, vault_dir=vault_dir)
    except Exception as exc:                             # noqa: BLE001
        out.say(f"note: could not save the classifications ({exc!r}) — the "
                f"next Generate will ask the model again")
        return updated
    if not written:
        return updated
    out.classified = written
    out.say(f"saved {len(written)} classification(s) into the wireframe — "
            f"the next Generate will not ask the model about them again")
    return updated


def apply_answer(shape: Any, answer: Dict[str, Any]) -> None:
    """Make an untyped ``shape`` the kind the classifier answered. In place.

    Props are the catalogue's defaults, then whatever the shape had, then
    the model's — what a palette shape of that kind would carry, so the
    inspector shows its fields and nothing downstream reads a missing key.
    The Designer canvas uses the same function for the same answer, so the
    canvas and the .gspec cannot disagree."""
    from gui_shapes import PALETTE

    kind = str(answer.get("kind") or "")
    if kind not in PALETTE:
        return
    schema = PALETTE[kind].get("prop_schema") or {}
    props = {p: (list(d["default"]) if isinstance(d.get("default"), list)
                 else d["default"])
             for p, d in schema.items() if "default" in d}
    props.update(dict(getattr(shape, "props", None) or {}))
    props.update(dict(answer.get("props") or {}))
    shape.kind, shape.props = kind, props


def described_window(window: Any) -> Dict[str, str]:
    """The window settings a description validated, as apply_window values:
    title, bg, fg, font — each only when set. gui_describe's placeholder
    title "Untitled" is not a setting; applying it would rename a project's
    window to "Untitled" because the model wrote no title."""
    if not isinstance(window, dict):
        return {}
    out = {}
    for key in ("title", "bg", "fg", "font"):
        value = window.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if out.get("title") == "Untitled":
        out.pop("title")
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
             model_call: Optional[Callable[..., str]] = None,
             profile: Any = None,
             should_stop: Optional[Callable[[], bool]] = None) -> Any:
    """Plain English -> a wireframe that will generate, or the reasons not.

    A thin seam over gui_describe, which owns the prompt, the checks and the
    repair rounds. What this adds is what only a PROJECT and the ENGINE
    know: the toolkit to design for, the canvas, the default model call, and
    which model will answer — its size picks the profile (tree mode and
    best-of-N for a small model, pixel mode for a large one) and its window
    the prompt budget.

    The canvas is the PROJECT's. It used to be the fixed 1100 x 700 the
    Designer's widget was, on the grounds that the shapes are about to be
    placed on the canvas the user drags on — and now that the widget takes
    the project's size, the project's size IS that canvas. Describing into
    Typhon's 1504 x 1016 with 1100 x 700 would draw the new wireframe into
    the top-left two-thirds of the window.

    A caller that supplies its OWN model_call gets the plain profile unless
    it passes one: the engine's model is not the one answering, so its size
    says nothing about it.

    Never raises; a failure is a result with ok=False and the reason.
    """
    import gui_describe

    toolkit = "tk"
    canvas_w, canvas_h = CANVAS_W, CANVAS_H
    if project_dir:
        try:
            import gui_projects
            toolkit = gui_projects.toolkit_for(Path(project_dir))
        except Exception:                                # noqa: BLE001
            pass
        canvas_w, canvas_h = canvas_of(project_dir)
    stats: List[Dict[str, Any]] = []
    if model_call is None:
        if profile is None:
            profile = describe_profile(text)
        model_call = _recording(describe_model_call, stats)
    result = gui_describe.describe(
        text, model_call=model_call, canvas_w=canvas_w, canvas_h=canvas_h,
        toolkit=toolkit, profile=profile, should_stop=should_stop)
    line = stats_line(stats)
    if line and hasattr(result, "notes"):
        # FIRST, not last: run_describe_prompts keeps notes[:8], and this is
        # the only way the cost of each call reaches its jsonl — appended,
        # a reply with a few synonyms and moved props pushed it out.
        result.notes.insert(0, line)
    return result


# ---- which model answers, and how big it is --------------------------------

#: Overrides for measuring — the GPU-owning phase runs the graded prompts
#: with each: COUNCIL_DESCRIBE_MODE = pixel | tree | auto,
#: COUNCIL_DESCRIBE_BEST_OF = 1..5, COUNCIL_DESCRIBE_CONSTRAINED = 0 | 1.
ENV_MODE = "COUNCIL_DESCRIBE_MODE"
ENV_BEST_OF = "COUNCIL_DESCRIBE_BEST_OF"
ENV_CONSTRAINED = "COUNCIL_DESCRIBE_CONSTRAINED"


def role_model(role: str = DESCRIBE_ROLE) -> Dict[str, Any]:
    """{"id", "name", "params_b", "n_ctx", "slot"} for the model ``role``
    will be answered by. Never raises, never loads a model: the slot file,
    then the engine's list_local_models() (shared contract) when it has
    one, then the GGUF header, then the file name. Missing pieces are None.
    """
    import os

    info: Dict[str, Any] = {"id": "", "name": "", "params_b": None,
                            "n_ctx": None, "slot": "main"}
    try:
        from . import model_slots
        cfg = model_slots.current()
        slot = cfg.slot_for(role)
        info["slot"] = slot
        s = cfg.slots.get(slot)
        path = (s.path if s is not None else "") or (
            os.environ.get("COUNCIL_GGUF_PATH", "").strip()
            if slot == "main" else "")
        info["id"] = path
    except Exception:                                    # noqa: BLE001
        pass
    model_id = str(info["id"] or "")
    if model_id.startswith("ollama:"):
        info["name"] = model_id.split(":", 1)[1]
    elif model_id:
        info["name"] = Path(model_id).stem
    try:
        import council_engine
    except Exception:                                    # noqa: BLE001
        council_engine = None
    if council_engine is not None and model_id:
        listing = getattr(council_engine, "list_local_models", None)
        if callable(listing):
            try:
                for m in listing() or []:
                    if (_same_ollama(m.get("id"), model_id)
                            if model_id.startswith("ollama:")
                            else str(m.get("id")) == model_id
                            or _same_file(m.get("id"), model_id)):
                        info["params_b"] = m.get("params_b")
                        info["name"] = m.get("name") or info["name"]
                        break
            except Exception:                            # noqa: BLE001
                pass
        if info["params_b"] is None and not model_id.startswith("ollama:"):
            info["params_b"] = _gguf_params_b(council_engine, model_id)
    if info["params_b"] is None and model_id:
        import gui_describe
        info["params_b"] = gui_describe.parse_params_b(info["name"]
                                                       or model_id)
    if council_engine is not None:
        try:
            info["n_ctx"] = int(council_engine.effective_n_ctx(info["slot"]))
        except Exception:                                # noqa: BLE001
            pass
    return info


def _role_window(role: str) -> Optional[int]:
    """The context window ``role``'s model will have, or None. Never
    raises; never loads a model (council_engine.effective_n_ctx)."""
    try:
        from . import model_slots
        import council_engine
        return int(council_engine.effective_n_ctx(
            model_slots.slot_for_role(role)))
    except Exception:                                    # noqa: BLE001
        return None


def _same_ollama(a: Any, b: Any) -> bool:
    """"ollama:phi3.5" and "ollama:phi3.5:latest" are one model — Ollama
    serves an untagged name as :latest, and the engine routes it so, but
    list_local_models' ids always carry the tag (review finding)."""
    def key(s: Any) -> str:
        name = str(s or "").strip().lower()
        name = name[len("ollama:"):] if name.startswith("ollama:") else name
        return name if ":" in name else name + ":latest"
    return str(a or "").lower().startswith("ollama:") and key(a) == key(b)


def _same_file(a: Any, b: Any) -> bool:
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except Exception:                                    # noqa: BLE001
        return False


def _gguf_params_b(engine: Any, path: str) -> Optional[float]:
    """Parameters from the GGUF header: general.size_label ("8B", "3.8B")
    when the file has one, else general.parameter_count. Header only — the
    reader stops before the tensors."""
    try:
        meta = engine.read_gguf_metadata(path) or {}
    except Exception:                                    # noqa: BLE001
        return None
    import gui_describe
    label = meta.get("general.size_label")
    if isinstance(label, str):
        got = gui_describe.parse_params_b(label)
        if got:
            return got
    count = meta.get("general.parameter_count")
    if isinstance(count, (int, float)) and count > 0:
        return round(float(count) / 1e9, 1)
    return gui_describe.parse_params_b(str(meta.get("general.name") or ""))


def describe_profile(text: str = "", role: str = DESCRIBE_ROLE) -> Any:
    """gui_describe.profile_for the model ``role`` uses. Never raises.

    The environment overrides (ENV_MODE / ENV_BEST_OF / ENV_CONSTRAINED)
    exist so the same prompts can be measured each way on the same model."""
    import os

    import gui_describe

    try:
        info = role_model(role)
    except Exception:                                    # noqa: BLE001
        info = {"name": "", "params_b": None, "n_ctx": None}
    mode = os.environ.get(ENV_MODE, "").strip().lower() or None
    if mode == "auto":
        mode = None
    try:
        best = int(os.environ.get(ENV_BEST_OF, "").strip() or 0) or None
    except ValueError:
        best = None
    constrained = os.environ.get(ENV_CONSTRAINED, "1").strip() != "0"
    return gui_describe.profile_for(
        info.get("params_b"), n_ctx=info.get("n_ctx"), text=text,
        model=info.get("name") or "", mode=mode, n_best=best,
        constrained=constrained)


# ---- what each call cost -------------------------------------------------

def _recording(call: Callable[..., str], stats: List[Dict[str, Any]]
               ) -> Callable[..., str]:
    """``call`` that also appends council_engine.last_call_stats() (shared
    contract) after each call, when the engine has it. Keeps the wrapped
    call's keywords visible to gui_describe.call_model's signature check.

    Only stats the call itself RECORDED count: last_call_stats is "the most
    recent call", and a call that raised (a stall timeout, Stop, a dead
    server) may record nothing — reading it regardless counted the previous
    call twice and reported its cost as this one's (review finding)."""
    import functools

    def latest() -> Dict[str, Any]:
        try:
            import council_engine
            fn = getattr(council_engine, "last_call_stats", None)
            got = fn(role=DESCRIBE_ROLE) if callable(fn) else None
        except Exception:                                # noqa: BLE001
            return {}
        return dict(got) if isinstance(got, dict) else {}

    @functools.wraps(call)
    def wrapped(prompt: str, **kwargs: Any) -> str:
        before = latest()
        try:
            reply = call(prompt, **kwargs)
        except BaseException:
            after = latest()
            if after and after != before:   # it did record, e.g. a partial
                stats.append(after)
            raise
        after = latest()
        if after:
            stats.append(after)
        return reply
    return wrapped


def stats_line(stats: Sequence[Dict[str, Any]]) -> str:
    """One log line for the calls a describe made: tokens, seconds, speed,
    and whether the schema actually constrained them. "" with no stats."""
    if not stats:
        return ""
    gen = sum(int(s.get("gen_tokens") or 0) for s in stats)
    prompt = sum(int(s.get("prompt_tokens") or 0) for s in stats)
    secs = sum(float(s.get("seconds") or 0.0) for s in stats)
    constrained = sum(1 for s in stats if s.get("constrained"))
    model = next((str(s.get("model")) for s in stats if s.get("model")), "")
    speed = f", {gen / secs:.1f} tok/s" if secs > 0 and gen else ""
    return (f"model calls: {len(stats)}{' on ' + model if model else ''} — "
            f"{prompt} prompt + {gen} reply tokens in {secs:.1f} s{speed}; "
            f"{constrained} of {len(stats)} schema-constrained")


def canvas_of(project_dir: Any) -> tuple:
    """(w, h) of a project's design area; the new-project size if unreadable.

    Never raises: it is asked while a panel is being DRAWN, and a canvas that
    fails to open because its .gspec is damaged leaves the user nothing to
    repair the file with.
    """
    try:
        import gui_projects
        import gui_shapes
        project = gui_shapes.load_gspec(Path(project_dir)
                                        / gui_projects.GSPEC_NAME)
        return int(project.canvas.w), int(project.canvas.h)
    except Exception:                                    # noqa: BLE001
        return CANVAS_W, CANVAS_H


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
