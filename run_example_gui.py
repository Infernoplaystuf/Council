#!/usr/bin/env python
"""
Turn a shipped example wireframe into a running app, in one command.

    python run_example_gui.py                  # list what is available
    python run_example_gui.py barbie_capture   # build it and run it
    python run_example_gui.py image_viewer --no-run   # build only

WHY THIS EXISTS
---------------
`examples/gui/*.gspec` are WIREFRAMES, not applications. The generated app —
main.py, app.py, handlers.py, ui/ — lives under vault/GUI_Projects/, and
vault/ is gitignored (it is the write target for user output, so committing it
would mean committing whatever the user has been working on). So a fresh clone
gets the design and none of the code, and "can I just run the Barbie GUI?" was
answered by a five-step trip through the designer UI.

This does those five steps: load the wireframe, infer the layout, build and
VALIDATE the spec, emit the project, launch it. The result is an ordinary
project directory — you can edit app.py, reopen the wireframe in the designer,
and regenerate, exactly as if you had drawn it yourself.

It writes ONLY inside the vault, like every other generator in this app, and
it refuses to clobber: a project of the same name that already exists is left
alone unless you pass --force, because that directory may contain app.py and
handlers.py you have edited by hand — the two files regeneration never
rewrites, and the two most expensive to lose.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gui_emit as ge          # noqa: E402
import gui_examples as gx      # noqa: E402
import gui_layout as gl        # noqa: E402
import gui_projects as gpj     # noqa: E402
import gui_shapes as gs        # noqa: E402
import gui_spec as gsp         # noqa: E402


def build(name: str, *, project: str = "", force: bool = False,
          vault_dir=None, python: str = "") -> Path:
    """Materialise example ``name`` as a generated project. Returns its dir.

    ``python`` is recorded in the project's manifest (see python_envs), so the
    designer's Run uses the same interpreter afterwards."""
    if name not in gx.names():
        raise SystemExit(f"no such example: {name!r}\n"
                         f"available: {', '.join(gx.names()) or '(none)'}")
    project = project or f"example_{name}"

    src = gx.EXAMPLES_DIR / f"{name}.gspec"
    proj = gs.load_gspec(src)

    pdir = gpj.project_path(project, vault_dir)
    if pdir.exists():
        if not force:
            raise SystemExit(
                f"project {project!r} already exists at\n  {pdir}\n"
                f"Pass --force to replace it, or --project NAME to build "
                f"alongside it.\nNOTE: --force deletes app.py and handlers.py "
                f"too, which regeneration would normally never touch.")
        shutil.rmtree(pdir)
    gpj.create(project, mode="linked", vault_dir=vault_dir)

    # The wireframe goes in first, so the project opens in the designer and
    # can be edited and regenerated like any other.
    proj.project = project
    gpj.save_project(project, proj, vault_dir=vault_dir)

    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    spec = gsp.build(proj.shapes, tree, project=project,
                     title=proj.window.title,
                     root_bg=proj.window.bg, root_fg=proj.window.fg,
                     root_font=proj.window.font,
                     requires=proj.requires)
    ok, errs = gsp.validate(spec)
    if not ok:
        # An example that cannot generate is a bug in the example, and saying
        # so plainly beats emitting a broken project.
        raise SystemExit("this example does not validate:\n  "
                         + "\n  ".join(errs))

    res = ge.emit(spec, pdir)
    man = gpj.load_manifest(pdir)
    man.port_names = spec.port_registry() if hasattr(spec, "port_registry") else {}
    man.widget_names = spec.name_registry()
    man.ui_checksums = gpj.ui_checksums(pdir)
    man.python = str(python or "")
    gpj.save_manifest(pdir, man)

    print(f"built {project!r}")
    for f in res.files_written:
        print(f"  wrote  {Path(f).relative_to(pdir)}")
    for f in res.files_skipped:
        print(f"  kept   {Path(f).relative_to(pdir)}  (never regenerated)")
    return pdir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build and run a shipped example GUI.")
    ap.add_argument("example", nargs="?",
                    help="example name; omit to list them")
    ap.add_argument("--project", default="",
                    help="project name to build into "
                         "(default: example_<name>)")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing project of that name")
    ap.add_argument("--no-run", action="store_true",
                    help="generate the project but do not launch it")
    ap.add_argument("--python", default="", metavar="ENV_OR_PATH",
                    help="the Python that runs the app: a conda env name "
                         "(e.g. pylon) or a path to python.exe. Default: "
                         "this Python. Saved with the project, so the GUI "
                         "Designer's Run uses it too.")
    args = ap.parse_args(argv)

    if not args.example:
        print("examples:")
        for n in gx.names():
            print(f"  {n:16} {gx.NOTES.get(n, '').split('.')[0]}.")
        print("\nrun one with:  python run_example_gui.py <name>")
        return 0

    import python_envs as pe
    res = pe.resolve(args.python)
    if res.error:
        # Refuse BEFORE building: a --force build deletes app.py and
        # handlers.py, and must not do that for an interpreter that is not
        # even there.
        raise SystemExit(f"--python {args.python!r}: {res.error}")
    if pe.looks_like_path(args.python):
        # Saved in the manifest, and the designer's Run resolves it from
        # wherever the Council was started — so never a relative path.
        args.python = str(Path(args.python).expanduser().absolute())

    pdir = build(args.example, project=args.project, force=args.force,
                 python=args.python)
    entry = pdir / "main.py"
    # The same preflight the designer's Run makes — policy gate (this path
    # used to have none at all), interpreter, self-check.
    pf = pe.preflight(pdir, args.python, gpj.load_manifest(pdir).mode,
                      _requires_of(pdir))
    print()
    for line in pf.lines:
        print(line)
    if not pf.ok:
        return 1
    if args.no_run:
        print(f'\nrun it with:  "{pf.python}" "{entry}"')
        return 0

    print(f"\nlaunching {pdir.name} — close the window to return")
    import subprocess
    return subprocess.call([pf.python, str(entry)], cwd=str(pdir))


def _requires_of(pdir: Path):
    """The built project's declared packages ([] until it declares any)."""
    try:
        return list(getattr(gs.load_gspec(pdir / gpj.GSPEC_NAME),
                            "requires", None) or [])
    except Exception:
        return []


if __name__ == "__main__":
    raise SystemExit(main())
