"""
council_core.designer_examples — the shipped examples, into the Designer and
back out again.

WHY THIS EXISTS
Typhon, the Barbie capture forms and the image viewer live in examples/gui/ as
wireframes. The only way to turn one into a project was a command line
(`run_example_gui.py typhon --target qt`), and the only way to get an edited
design back into examples/gui/ was to copy a file out of the vault by hand. So
"build and edit Typhon entirely inside the Council" stopped at step one.

This is the toolkit-free half of both directions:

    build(...)          an example -> a new project in the vault, the SAME
                        pipeline run_example_gui runs (build_project is that
                        pipeline, and the CLI calls it too)
    export_gspec(...)   the open project -> a .gspec file the user picked, in
                        the canonical format the examples are stored in

A BUILD NEVER REPLACES A PROJECT
The CLI has --force, which deletes the old project first. Nothing here does,
and nothing here can be asked to: a project directory holds app.py and
handlers.py, the two files regeneration never rewrites because the user has
been editing them, and a dialog is exactly where a wrong name gets confirmed
by reflex. A name that is taken is refused, and the refusal says so.

The CLI keeps --force by deleting the old directory ITSELF before calling in —
a decision made where the user typed it, not a mode of this module.

EXPORT WRITES ONLY WHERE IT WAS TOLD TO
There is no default destination that gets written without a choice: the
caller supplies a path the user picked, and the caller confirms an overwrite.
This module refuses nothing on that score because it cannot ask — the same
split designer_project.detach makes.

UPDATE FROM EXAMPLE: A NEWER EXAMPLE INTO AN EXISTING PROJECT
A project built from an example never hears about the example again: Typhon
gained a frame-rate box, then a live FPS box and a Settings menu, and a
Typhon built before that still showed "Frame count". The only way to catch
up was the CLI's --force — which deletes app.py and handlers.py.

    update_from_example(...)   the project's DRAWN LAYOUT (shapes, window,
                               canvas, requires) replaced with the example's
                               current one, then Generate

Shapes the user drew in the Designer themselves are not the example's to
replace: they are kept on top of it (drawn_by_user), so the code written for
them still has its widget. It never touches app.py or handlers.py itself. Generate does what it always
does with them: a handler stub nobody edited is rewritten for its button's
new link, an edited one is kept and named in a WARNING line when its link
changed, and new widgets get new stubs. The old project.gspec is copied
beside the new one first, with a timestamp in its name, so the drawing can
be put back. Asking first is the caller's job, as it is for export.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: What the dialog's toolkit box shows until the user picks one, for an
#: example whose intended toolkit is not recorded. Not a toolkit, so a build
#: with it is refused rather than silently becoming Tk.
TOOLKIT_UNCHOSEN = ""


class ExampleError(ValueError):
    """An example that cannot be built, with a sentence a user can act on."""


@dataclass
class ExampleInfo:
    """One shipped example, as the "New from example" dialog lists it."""
    name: str
    #: What it teaches / what it is for — gui_examples.NOTES.
    note: str
    #: "qt" / "tk" when the example records which toolkit it is meant for,
    #: "" when it does not — and then the user is asked rather than guessed
    #: for, because the toolkit cannot be changed once app.py exists.
    toolkit: str
    #: The project name offered by default: the CLI's, example_<name>.
    default_project: str


@dataclass
class ExampleAnswers:
    """What the dialog collected. Plain data, so a test can supply it."""
    example: str
    project: str
    toolkit: str
    #: "" = the Council's own Python, a conda env name, or a path.
    python: str = ""


@dataclass
class BuiltProject:
    """What build_project wrote. The CLI prints it; the Designer logs it."""
    project_dir: Path
    files_written: List[str] = field(default_factory=list)
    files_skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ExampleBuild:
    """What a Designer build said. Never raises — a refusal is ok=False."""
    ok: bool = False
    lines: List[str] = field(default_factory=list)
    name: str = ""
    project_dir: Optional[Path] = None
    #: Wall-clock time of the build itself, reported so its cost is visible.
    seconds: float = 0.0

    def say(self, line: str) -> None:
        self.lines.append(line)


# ============================================================
# What is offered
# ============================================================

def default_project(example: str) -> str:
    """The name run_example_gui builds into when none is given."""
    return f"example_{example}"


def offered() -> List[ExampleInfo]:
    """Every shipped example, in gui_examples' stable order."""
    import gui_examples as gx

    return [ExampleInfo(name=n, note=gx.NOTES.get(n, ""),
                        toolkit=gx.intended_toolkit(n),
                        default_project=default_project(n))
            for n in gx.names()]


def toolkit_warning(example: str, toolkit: str) -> str:
    """A sentence when `toolkit` is not the one `example` is meant for.

    A warning, not a refusal: the CLI builds any example into either toolkit
    and the Designer should not be stricter than the command it replaces. But
    Typhon built as Tk has no live view — the camera draws through the Qt
    canvas's set_array — and that should be said BEFORE the build, not
    discovered at Run.
    """
    import gui_examples as gx

    meant = gx.intended_toolkit(example)
    if meant and toolkit and toolkit != meant:
        label = {"qt": "Qt", "tk": "Tk"}
        return (f"{example} is meant for {label.get(meant, meant)}; built as "
                f"{label.get(toolkit, toolkit)} it will not have everything "
                f"its notes describe (a camera example loses its live view).")
    return ""


# ============================================================
# Checking the answers
# ============================================================

def check_python(spec: str) -> Tuple[str, str]:
    """(the setting to save, the problem) for a "Run with" answer.

    Exactly what run_example_gui does with --python, moved here so the CLI
    and the dialog cannot disagree: resolve it through python_envs, refuse it
    if it does not resolve, and save a PATH absolute — the Designer's Run
    resolves it later from wherever the Council was started, so a relative
    one would name a different file then. A conda env name is saved as the
    name, because that is portable across a reinstall of the env.
    """
    import python_envs as pe

    raw = str(spec or "")
    res = pe.resolve(raw)
    if res.error:
        return "", res.error
    cleaned = raw.strip().strip('"')
    if pe.looks_like_path(cleaned):
        return str(Path(cleaned).expanduser().absolute()), ""
    return cleaned, ""


def problem(answers: ExampleAnswers, vault_dir: Any) -> str:
    """Why these answers cannot be built, or "" when they can.

    The dialog calls this on every change and again on OK, and the Designer
    calls it once more before starting the worker: a name that was free when
    the dialog opened may not be free when the build starts, and the build
    itself refuses a taken name as the last line of defence.
    """
    import gui_examples as gx
    import gui_projects as gpj

    if answers.example not in gx.names():
        return (f"no such example: {answers.example!r} — "
                f"have {', '.join(gx.names()) or '(none)'}")
    name = str(answers.project or "").strip()
    if not name:
        return "Give the new project a name."
    why = gpj.name_problem(name)
    if why:
        return why
    try:
        taken = gpj.project_path(name, vault_dir).exists()
    except Exception as exc:                             # noqa: BLE001
        return str(exc)
    if taken:
        return (f"a project named {name!r} already exists — pick another "
                f"name. Building from an example never replaces a project: "
                f"its app.py and handlers.py may hold your own code.")
    if answers.toolkit not in gpj.TOOLKITS:
        return ("Choose the toolkit the app is written in (qt = PySide6, "
                "tk = tkinter). It cannot be changed after the project is "
                "built.")
    _spec, why = check_python(answers.python)
    if why:
        return f"Run with: {why}"
    return ""


# ============================================================
# Building — the one pipeline the CLI and the Designer share
# ============================================================

def build_project(example: str, project: str, vault_dir: Any, *,
                  python: str = "", toolkit: str = "tk") -> BuiltProject:
    """Materialise `example` as the NEW project `project`. Raises ExampleError.

    load the wireframe -> infer the layout -> build and VALIDATE the spec ->
    create the project -> save the wireframe into it -> emit -> manifest.

    Validation happens BEFORE the project directory exists. It used to happen
    after, so an example that did not validate left a half-made project
    behind, and the obvious retry then failed with "already exists".

    The spec gets the example's window MINIMUM SIZE. It used to get none, so
    gui_spec's 900x600 default reached app.py and Typhon — drawn for
    1400x820 — opened small enough to crush its own panels.

    `python` is stored as given; check it with check_python first (the CLI
    and the Designer both do). A failure after the directory exists moves
    what was written into .trash/ rather than leaving it to block a retry —
    moved, not deleted, the rule gui_projects.delete keeps.
    """
    import gui_emit as ge
    import gui_examples as gx
    import gui_layout as gl
    import gui_projects as gpj
    import gui_shapes as gs
    import gui_spec as gsp

    if example not in gx.names():
        raise ExampleError(f"no such example: {example!r}\n"
                           f"available: {', '.join(gx.names()) or '(none)'}")
    if toolkit not in gpj.TOOLKITS:
        raise ExampleError(f"unknown toolkit {toolkit!r}; expected one of "
                           f"{gpj.TOOLKITS}")
    try:
        pdir = gpj.project_path(project, vault_dir)
    except gpj.ProjectError as exc:
        raise ExampleError(str(exc))
    if pdir.exists():
        raise ExampleError(f"project {project!r} already exists at\n  {pdir}")

    try:
        proj = gs.load_gspec(gx.EXAMPLES_DIR / f"{example}.gspec")
    except gs.GspecError as exc:
        raise ExampleError(str(exc))
    proj.project = project

    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    spec = gsp.build(proj.shapes, tree, project=project, mode=proj.mode,
                     title=proj.window.title,
                     min_w=proj.window.min_w, min_h=proj.window.min_h,
                     root_bg=proj.window.bg, root_fg=proj.window.fg,
                     root_font=proj.window.font,
                     requires=proj.requires)
    ok, errs = gsp.validate(spec)
    if not ok:
        # An example that cannot generate is a bug in the example, and saying
        # so plainly beats emitting a broken project.
        raise ExampleError("this example does not validate:\n  "
                           + "\n  ".join(errs))

    try:
        gpj.create(project, mode=proj.mode, vault_dir=vault_dir,
                   toolkit=toolkit)
    except gpj.ProjectError as exc:
        # Taken between the check above and here, or a name the platform
        # refuses. Nothing of ours exists yet, so nothing to clean up.
        raise ExampleError(str(exc))
    try:
        # The wireframe goes in first, so the project opens in the designer
        # and can be edited and regenerated like any other.
        gpj.save_project(project, proj, vault_dir=vault_dir)
        res = ge.emit(spec, pdir, target=toolkit)
        man = gpj.load_manifest(pdir)
        man.port_names = (spec.port_registry()
                          if hasattr(spec, "port_registry") else {})
        man.widget_names = spec.name_registry()
        man.ui_checksums = gpj.ui_checksums(pdir)
        man.python = str(python or "")
        man.example = example
        gpj.save_manifest(pdir, man)
    except Exception as exc:
        moved = ""
        try:
            moved = str(gpj.delete(project, vault_dir))
        except Exception:                                # noqa: BLE001
            pass
        raise ExampleError(
            f"building {example!r} into {project!r} failed: {exc!r}"
            + (f"\nwhat was written is in {moved}" if moved else ""))
    return BuiltProject(project_dir=pdir,
                        files_written=list(res.files_written),
                        files_skipped=list(res.files_skipped),
                        warnings=list(res.warnings))


def build(answers: ExampleAnswers, vault_dir: Any) -> ExampleBuild:
    """The Designer's "New from example": check, build, gate. Never raises.

    Blocking — a whole generate — so the Designer runs it on a worker. The
    policy gate is reported the way Generate reports it, because "policy:
    OK" is the promise that Run will start the app.
    """
    import gui_policy
    import gui_projects as gpj

    out = ExampleBuild(name=str(answers.project or "").strip())
    why = problem(answers, vault_dir)
    if why:
        out.say(why)
        return out
    python, _ = check_python(answers.python)
    warning = toolkit_warning(answers.example, answers.toolkit)
    if warning:
        out.say(f"note: {warning}")

    started = time.perf_counter()
    try:
        built = build_project(answers.example, out.name, vault_dir,
                              python=python, toolkit=answers.toolkit)
    except ExampleError as exc:
        out.seconds = time.perf_counter() - started
        out.lines.extend(str(exc).splitlines())
        return out
    except Exception as exc:                             # noqa: BLE001
        out.seconds = time.perf_counter() - started
        out.say(f"building {answers.example!r} failed: {exc!r}")
        return out
    out.seconds = time.perf_counter() - started
    out.project_dir = built.project_dir

    label = {"qt": "Qt", "tk": "Tk"}.get(answers.toolkit, answers.toolkit)
    out.say(f"built {out.name} from example {answers.example} ({label}) in "
            f"{out.seconds:.2f}s — wrote {len(built.files_written)} file(s)")
    out.lines.extend(built.warnings)
    try:
        project = gpj.open_project(out.name, vault_dir=vault_dir)
        mode = gpj.load_manifest(built.project_dir).mode
        policy_ok, policy_errors = gui_policy.validate_dir(
            built.project_dir, mode, list(project.requires or []),
            toolkit=answers.toolkit)
    except Exception as exc:                             # noqa: BLE001
        policy_ok, policy_errors = False, [f"could not check: {exc!r}"]
    out.say("policy: OK" if policy_ok
            else "policy REFUSED — Run will not start it until this is "
                 "fixed:")
    out.lines.extend("  " + e for e in policy_errors)
    if python:
        out.say(f"Run with: {python}")
    out.ok = True
    return out


# ============================================================
# Update from example
# ============================================================

#: At most this many shapes are NAMED per kind of change in the log; the rest
#: are counted. The log is a summary, not a diff.
NAMED_PER_CHANGE = 6


#: The id gui_shapes.new_shape gives a shape drawn in the Designer: a uuid4
#: hex. Every shipped example numbers its own s00, s01, ... — so a shape
#: with a Designer id in a project built from an example is one the USER
#: drew, never one an example shipped (or later dropped).
_DRAWN_ID = re.compile(r"[0-9a-f]{32}")


def drawn_by_user(shapes: Sequence[Any], example: Sequence[Any]) -> List[Any]:
    """The shapes in `shapes` the user drew in the Designer — which an
    update keeps, on top of the example's layout.

    Measured before this: a Typhon with a button of the user's own lost it
    to the update, and Generate then BLOCKED on the handler the user had
    written for it ("hand-written code still uses these") — the project
    replaced but not generated. Their button and their code belong to them,
    as handlers.py does."""
    ids = {s.id for s in example}
    return [s for s in shapes
            if s.id not in ids and _DRAWN_ID.fullmatch(str(s.id or ""))]


@dataclass
class LayoutChanges:
    """What replacing one drawing with another changes, by shape id."""
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    #: The user's own shapes, carried over (drawn_by_user).
    kept: List[str] = field(default_factory=list)
    #: "Start capture (s46): now frame_camera.start(capture_folder, ...)"
    rewired: List[str] = field(default_factory=list)
    #: "s08: port frame_count -> frame_rate"
    ports: List[str] = field(default_factory=list)
    relabelled: List[str] = field(default_factory=list)
    moved: int = 0
    #: Any other difference: props, colours, fonts, resize.
    other: int = 0
    #: Window, canvas and requires, one sentence each.
    window: List[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed or self.rewired or self.ports
                    or self.relabelled or self.moved or self.other
                    or self.window)

    def lines(self) -> List[str]:
        """A short summary: counts, then a few names per kind of change."""
        kept = self._named("kept, drawn by you", self.kept)
        if not self.any:
            return ["  the layout already matches the example — nothing "
                    "changed"] + kept
        counts = [f"{len(self.added)} added", f"{len(self.removed)} removed",
                  f"{len(self.rewired)} rewired",
                  f"{len(self.relabelled)} relabelled",
                  f"{self.moved} moved or resized"]
        if self.other:
            counts.append(f"{self.other} restyled")
        out = ["  shapes: " + ", ".join(counts)]
        for title, items in (("added", self.added), ("removed", self.removed),
                             ("rewired", self.rewired),
                             ("ports", self.ports),
                             ("relabelled", self.relabelled)):
            out.extend(self._named(title, items))
        out.extend(kept)
        out.extend(f"  {line}" for line in self.window)
        return out

    @staticmethod
    def _named(title: str, items: Sequence[str]) -> List[str]:
        """"  title: a; b; … and 3 more", or nothing for no items."""
        if not items:
            return []
        shown = list(items[:NAMED_PER_CHANGE])
        more = len(items) - len(shown)
        return [f"  {title}: " + "; ".join(shown)
                + (f"; … and {more} more" if more else "")]


def _shape_name(shape: Any) -> str:
    label = str(getattr(shape, "label", "") or "").strip()
    return f"{label} ({shape.id})" if label else f"{shape.kind} {shape.id}"


def _link_text(script: Any) -> str:
    """"frame_camera.start(capture_folder, frame_rate)", or "no link"."""
    from . import designer_wiring as wiring

    link = wiring.normalise(script)
    if not link:
        return "no link"
    return f"{link['module']}.{link['function']}({', '.join(link['inputs'])})"


def layout_changes(old: Sequence[Any], new: Sequence[Any]) -> LayoutChanges:
    """How `new` differs from `old`, shape by shape (matched by id).

    Ids are what the manifest keys widget and port names by, so a shape that
    kept its id keeps its widget, its port and its handler — which is what
    makes "rewired" mean "the same button now calls something else"."""
    from . import designer_wiring as wiring

    out = LayoutChanges()
    before = {s.id: s for s in old}
    after = {s.id: s for s in new}
    out.added = [_shape_name(after[k]) for k in after if k not in before]
    out.removed = [_shape_name(before[k]) for k in before if k not in after]
    for key, now in after.items():
        was = before.get(key)
        if was is None:
            continue
        if wiring.normalise(was.script) != wiring.normalise(now.script):
            out.rewired.append(f"{_shape_name(now)}: "
                               f"{_link_text(was.script)} → "
                               f"{_link_text(now.script)}")
        old_port = str((was.port or {}).get("name") or "")
        new_port = str((now.port or {}).get("name") or "")
        if old_port != new_port:
            out.ports.append(f"{key}: port {old_port or '(derived)'} → "
                             f"{new_port or '(derived)'}")
        if (was.label or "") != (now.label or ""):
            out.relabelled.append(f"{key}: {was.label!r} → {now.label!r}")
        if (was.x, was.y, was.w, was.h) != (now.x, now.y, now.w, now.h):
            out.moved += 1
        if any(getattr(was, f) != getattr(now, f)
               for f in ("kind", "props", "bg", "fg", "font", "resize",
                         "min_w", "min_h", "freeform", "drives", "note",
                         "z")) or ({k: v for k, v in (was.port or {}).items()
                                    if k != "name"}
                                   != {k: v for k, v in (now.port or {}).items()
                                       if k != "name"}):
            out.other += 1
    return out


def _window_changes(old: Any, new: Any) -> List[str]:
    """One sentence each for the window, the canvas and the requires."""
    lines = []
    ow, nw = old.window, new.window
    diffs = [f"{f} {getattr(ow, f)!r} → {getattr(nw, f)!r}"
             for f in ("title", "bg", "fg", "font")
             if getattr(ow, f) != getattr(nw, f)]
    if (ow.min_w, ow.min_h) != (nw.min_w, nw.min_h):
        diffs.append(f"minimum size {ow.min_w} x {ow.min_h} → "
                     f"{nw.min_w} x {nw.min_h}")
    if diffs:
        lines.append("window: " + ", ".join(diffs))
    if (old.canvas.w, old.canvas.h) != (new.canvas.w, new.canvas.h):
        lines.append(f"canvas: {old.canvas.w} x {old.canvas.h} → "
                     f"{new.canvas.w} x {new.canvas.h}")
    gained = [r for r in new.requires if r not in old.requires]
    lost = [r for r in old.requires if r not in new.requires]
    if gained or lost:
        lines.append("requires: "
                     + ", ".join([f"+{r}" for r in gained]
                                 + [f"-{r}" for r in lost]))
    return lines


def guess_example(project_dir: Any) -> str:
    """The example a project was built from, "" when it cannot be told.

    The manifest says, for a project built since "New from example" existed.
    An older one is guessed from its name — run_example_gui builds into
    example_<name>, and a name that contains an example's ("my_typhon") is
    most likely that one; the longest such name wins, so barbie_capture_v5
    is not taken for barbie_capture. Only a DEFAULT: the user confirms it.
    """
    import gui_examples as gx

    names = gx.names()
    if not project_dir:
        return ""
    directory = Path(project_dir)
    try:
        recorded = _manifest_example(directory)
    except Exception:                                    # noqa: BLE001
        recorded = ""
    if recorded in names:
        return recorded
    name = directory.name.lower()
    stripped = name[len("example_"):] if name.startswith("example_") else name
    if stripped in names:
        return stripped
    inside = [n for n in names if n.lower() in name]
    return max(inside, key=len) if inside else ""


def _manifest_example(directory: Path) -> str:
    import gui_projects as gpj
    return gpj.load_manifest(directory).example


def recorded_example(project_dir: Any) -> str:
    """The example the manifest RECORDS, "" for none (or not an example)."""
    import gui_examples as gx

    if not project_dir:
        return ""
    try:
        found = _manifest_example(Path(project_dir))
    except Exception:                                    # noqa: BLE001
        return ""
    return found if found in gx.names() else ""


def update_problem(name: str, example: str, vault_dir: Any) -> str:
    """Why `name` cannot be updated from `example`, or "" when it can."""
    import gui_examples as gx
    import gui_projects as gpj

    if not name:
        return "No project open."
    if example not in gx.names():
        return (f"no such example: {example!r} — have "
                f"{', '.join(gx.names()) or '(none)'}")
    try:
        pdir = gpj.project_path(name, vault_dir)
        manifest = gpj.load_manifest(pdir)
    except Exception as exc:                             # noqa: BLE001
        return str(exc)
    if manifest.detached:
        return ("this project is detached — its layout no longer generates, "
                "so there is nothing to update")
    return ""


@dataclass
class ExampleUpdate:
    """What an update said. Never raises — a refusal is ok=False."""
    ok: bool = False
    lines: List[str] = field(default_factory=list)
    name: str = ""
    example: str = ""
    #: The old project.gspec, copied beside the new one.
    backup: Optional[Path] = None
    #: The layout now saved — what the Designer puts on its canvas.
    shapes: List[Any] = field(default_factory=list)
    changes: Optional[LayoutChanges] = None
    #: The layout was replaced (True even when Generate then BLOCKED).
    replaced: bool = False
    seconds: float = 0.0

    def say(self, line: str) -> None:
        self.lines.append(line)


def links_before(old_shapes: Sequence[Any], manifest: Any) -> Dict[str, Any]:
    """The manifest's script_links, completed from the drawing being
    replaced.

    A project built by run_example_gui — the user's Typhon — has no record
    at all (only a Designer Generate writes one), and without it Generate
    cannot tell a hand-edited handler whose link changed under it from one
    the user extended on purpose (gui_emit._stale_reason). So an edited
    Start, whose link just gained frame_rate, passed with no WARNING —
    measured. The old drawing says what each handler was generated for;
    it fills in only handlers with no record, and an existing record (the
    last Generate's) wins.
    """
    import gui_spec

    from . import designer_wiring as wiring

    links = dict(getattr(manifest, "script_links", {}) or {})
    names = dict(getattr(manifest, "widget_names", {}) or {})
    for shape in old_shapes:
        link = wiring.normalise(getattr(shape, "script", None))
        widget = names.get(shape.id)
        if link and widget and shape.kind in gui_spec.COMMAND_KINDS:
            links.setdefault(f"on_{widget}", link)
    return links


def backup_gspec(project_dir: Any, stamp: str = "") -> Path:
    """Copy project.gspec to project.gspec.<stamp>.bak beside it.

    In the project folder, not .backups/, so it is found by anyone looking
    for the file it replaced; a second update in the same second gets _2
    rather than overwriting the first backup."""
    import shutil

    import gui_projects as gpj

    directory = Path(project_dir)
    source = directory / gpj.GSPEC_NAME
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    target = directory / f"{gpj.GSPEC_NAME}.{stamp}.bak"
    n = 1
    while target.exists():
        n += 1
        target = directory / f"{gpj.GSPEC_NAME}.{stamp}_{n}.bak"
    shutil.copy2(source, target)
    return target


def update_from_example(name: str, example: str, vault_dir: Any, *,
                        model_call: Any = None,
                        stamp: str = "") -> ExampleUpdate:
    """Replace project `name`'s layout with `example`'s current one, then
    Generate. Never raises. The caller has asked the user first.

    Kept: the project's name, its mode (the manifest's, which the gate
    enforces), app.py, handlers.py and everything else in the folder — and
    the shapes the user drew in the Designer themselves (drawn_by_user),
    on top of the example's layout, so the code they wrote for them still
    has its widget.
    Replaced: the example's shapes, the window, the canvas, `requires` and
    the clarifications — so the project IS the example again, plus the
    user's own additions. The manifest records the example, so the next
    update needs no guess.

    Blocking — a whole Generate — so the Designer runs it on a worker.
    """
    import gui_examples as gx
    import gui_projects as gpj
    import gui_shapes as gs

    from . import designer_project as dp

    out = ExampleUpdate(name=str(name or ""), example=str(example or ""))
    why = update_problem(out.name, out.example, vault_dir)
    if why:
        out.say(why)
        return out
    started = time.perf_counter()
    try:
        pdir = gpj.project_path(out.name, vault_dir)
        old = gpj.open_project(out.name, vault_dir=vault_dir)
        fresh = gs.load_gspec(gx.EXAMPLES_DIR / f"{out.example}.gspec")
    except Exception as exc:                             # noqa: BLE001
        out.say(f"cannot update {out.name}: {exc}")
        return out

    own = drawn_by_user(old.shapes, fresh.shapes)
    shapes = list(fresh.shapes) + list(own)
    changes = layout_changes(old.shapes, shapes)
    changes.kept = [_shape_name(s) for s in own]
    changes.window = _window_changes(old, fresh)
    out.changes = changes
    try:
        out.backup = backup_gspec(pdir, stamp)
        new = gs.Project(project=old.project or out.name, mode=old.mode,
                         canvas=fresh.canvas, window=fresh.window,
                         shapes=shapes,
                         clarifications=list(fresh.clarifications),
                         requires=list(fresh.requires))
        gpj.save_project(out.name, new, vault_dir=vault_dir)
        manifest = gpj.load_manifest(pdir)
        manifest.example = out.example
        manifest.script_links = links_before(old.shapes, manifest)
        gpj.save_manifest(pdir, manifest)
    except Exception as exc:                             # noqa: BLE001
        kept = (f" — the old drawing is in {out.backup.name}"
                if out.backup is not None else " — nothing was replaced")
        out.say(f"cannot update {out.name}: {exc!r}{kept}")
        return out
    out.replaced = True
    out.shapes = list(new.shapes)
    yours = f" + {len(own)} of your own" if own else ""
    out.say(f"updated {out.name} from example {out.example}: the layout is "
            f"now the example's ({len(fresh.shapes)} shapes{yours}, was "
            f"{len(old.shapes)}); the old one is {out.backup.name}")
    out.lines.extend(changes.lines())
    toolkit = gpj.toolkit_for(pdir)
    warning = toolkit_warning(out.example, toolkit)
    if warning:
        out.say(f"note: {warning}")

    out.say("── Generate ──")
    try:
        generated = dp.generate(out.name, new.shapes, pdir, vault_dir,
                                model_call=model_call)
    except Exception as exc:                             # noqa: BLE001
        generated = dp.GenerateResult(lines=[f"generate failed: {exc!r}"])
    out.lines.extend(generated.lines)
    out.seconds = time.perf_counter() - started
    if not generated.ok:
        out.say(f"The new layout is saved but did not generate. Fix what is "
                f"listed and press Generate — or copy {out.backup.name} back "
                f"over {gpj.GSPEC_NAME} to return to the old drawing.")
        return out
    out.say(f"updated and generated in {out.seconds:.2f}s — handlers.py and "
            f"app.py were kept")
    out.ok = True
    return out


# ============================================================
# Export
# ============================================================

@dataclass
class ExportResult:
    """What an export said."""
    ok: bool
    message: str = ""
    path: Optional[Path] = None
    seconds: float = 0.0


def export_default(project_dir: Any) -> Tuple[str, str]:
    """(folder, file name) to OFFER in the save dialog. Writes nothing.

    A project built from an example offers that example's own file, so an
    edited Typhon goes back to examples/gui/typhon.gspec in two clicks. Any
    other project offers <name>.gspec with no folder, and the dialog starts
    wherever the platform starts it: suggesting the examples folder for the
    user's own design would put it where the model is shown worked examples.
    """
    import gui_examples as gx
    import gui_projects as gpj

    if not project_dir:
        return "", ""
    directory = Path(project_dir)
    try:
        source = gpj.load_manifest(directory).example
    except Exception:                                    # noqa: BLE001
        source = ""
    if source and source in gx.names():
        return str(gx.EXAMPLES_DIR), f"{source}.gspec"
    return "", f"{directory.name}.gspec"


def _spelled_as_on_disk(target: Path) -> Path:
    """`target`, spelled the way the file it names is ALREADY spelled.

    On a case-insensitive file system "Typhon.gspec" names the existing
    typhon.gspec. Writing it in place would keep that spelling; renaming a
    temp file over it (which export does, so a failure cannot half-write a
    shipped example) takes the typed one instead — and gui_examples keys an
    example's notes and toolkit by its file's stem, so Typhon became an
    example with no notes and no Qt default. A name with no existing file, or
    an exact match, is returned as given.
    """
    try:
        if not target.exists():
            return target
        for entry in target.parent.iterdir():
            if entry.name == target.name:
                return target
            if (entry.name.casefold() == target.name.casefold()
                    and os.path.samefile(entry, target)):
                return entry
    except OSError:
        pass
    return target


def export_gspec(name: str, shapes: Sequence[Any], vault_dir: Any,
                 dest: Any) -> ExportResult:
    """Write project `name`, with `shapes`, to `dest` as a .gspec.

    `shapes` are the canvas's — what the user is LOOKING at, saved or not;
    everything else (window, canvas size, requires, mode) comes from the
    project file. The written file's `project` is the destination's stem:
    every shipped example names itself after its file, and an example built
    into example_typhon exported back as typhon.gspec should say "typhon",
    not the vault folder it happened to be edited in.

    The format is gui_shapes.save_gspec's — sorted keys, two-space indent —
    with non-ASCII text written as itself ("µs", "—") rather than escaped,
    which is how the examples are stored; so an unchanged project exports to
    an unchanged file. The write goes to a temporary file first
    and is renamed over the destination, so a failure half way leaves the
    old file whole: the destination is often a shipped example.

    The caller has already asked about overwriting; this does not ask.
    """
    import gui_projects as gpj
    import gui_shapes as gs

    target = _spelled_as_on_disk(Path(dest))
    if not name:
        return ExportResult(False, "No project open.")
    started = time.perf_counter()
    tmp = target.with_name(target.name + ".tmp")
    try:
        project = gpj.open_project(name, vault_dir=vault_dir)
        project.shapes = list(shapes)
        project.project = target.stem
        gs.save_gspec(tmp, project, ascii_only=False)
        os.replace(tmp, target)
    except Exception as exc:                             # noqa: BLE001
        try:
            tmp.unlink()
        except OSError:
            pass
        return ExportResult(False, f"export failed: {exc}")
    seconds = time.perf_counter() - started
    return ExportResult(True, f"exported {name} ({len(project.shapes)} "
                              f"shape(s)) to {target}", path=target,
                        seconds=seconds)
