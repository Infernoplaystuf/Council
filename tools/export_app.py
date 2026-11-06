"""
tools/export_app.py — export an app built in the Council's GUI Designer to a
folder of its own, to copy to another machine and run there with no Council
(council_core/app_export.py says what the folder holds).

    python tools/export_app.py example_typhon D:\\exports
    python tools/export_app.py "C:\\...\\GUI_Projects\\my_app" D:\\exports --name Typhon
    python tools/export_app.py example_typhon D:\\exports --with-classifiers

The first argument is a project name in the vault (COUNCIL_VAULT_ROOT, or
the default one) or a project folder. The export is checked afterwards: the
app is started in a separate Python that cannot see the Council (--no-check
skips it). An existing folder is never replaced.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("project", help="a project name in the vault, or "
                        "a project folder")
    parser.add_argument("dest", help="the folder to export into (the app "
                        "gets a new folder inside it)")
    parser.add_argument("--name", default="", help="the exported folder's "
                        "name (default: the project's)")
    parser.add_argument("--with-classifiers", action="store_true",
                        help="copy this PC's classifier store into it")
    parser.add_argument("--no-check", action="store_true")
    args = parser.parse_args(argv)

    from council_core import app_export as ae
    project = Path(args.project)
    if not project.is_dir():
        import gui_projects
        project = gui_projects.project_path(args.project)
    try:
        result = ae.export_app(project, Path(args.dest),
                               name=args.name or None,
                               with_classifiers=args.with_classifiers)
    except ae.ExportError as exc:
        print(f"Not exported: {exc}")
        return 2
    print(result.message)
    if args.no_check:
        return 0
    checked = ae.check(result.folder)
    print(checked.message)
    return 0 if checked.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
