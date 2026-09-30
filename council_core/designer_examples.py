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
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

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

    target = Path(dest)
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
