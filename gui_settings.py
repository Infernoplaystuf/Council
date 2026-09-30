"""
gui_settings.py — a Settings menu for a generated GUI, and the list of every
Python file behind the app, for debugging.

WHY THIS MODULE EXISTS
When a generated app misbehaves, the first question is "which file does
that?" — and a Designer project is spread over three kinds of file (rewritten
by every Generate, written once, yours to edit) plus the Council modules it
links to, which live in another folder entirely. Nothing in the app said
which was which. Settings -> Python Scripts does: every Python file the app
runs or can reach, where it is, what it is for and whether it has been loaded
yet, with the interpreter at the top and a Copy all for a bug report.

A LINKED MODULE, LIKE frame_camera
Any Designer project can put a Settings button anywhere and wire it to
settings_menu(): this module is on gui_policy.LINKED_MODULES, so the Wiring
editor offers it and the gate admits it. It takes no inputs and fills no
ports — the menu finds the button that was pressed by itself, because a
script link cannot pass a widget.

NOTHING IS IMPORTED TO BE DESCRIBED
The list is built by PARSING (ast) the app's own files and following their
imports into the Council's folder — never by importing a module to read its
docstring: frame_camera reaches for camera SDKs, and describing it must not
open one. "Loaded" comes from sys.modules, which costs nothing. The parse is
cached per file (size and mtime), so a second look costs a stat per file.

QT ONLY WHERE IT IS NEEDED
scripts_in_use() is plain Python and testable with no display. The menu and
the window live in council_qt/widgets/python_scripts.py and are imported
inside the functions that show them, so importing this module costs a Tk app
nothing — and with no QApplication running, the functions say so instead of
raising.

NOTHING HERE WRITES
It reads files and lists them. The clipboard is the only thing Copy all
touches.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: The Council's own folder — where the linked modules, and everything they
#: import, live. A generated main.py puts it on sys.path for the same reason.
COUNCIL_ROOT = Path(__file__).resolve().parent

#: The one item the Settings menu has, for now.
PYTHON_SCRIPTS = "Python Scripts"

#: The three groups, in the order the window lists them.
APP, LINKED, COUNCIL = "app", "linked", "council"
GROUP_TITLES = {
    APP: "This app's own files",
    LINKED: "Council modules the app links to",
    COUNCIL: "Other Council modules they use",
}

#: What the Designer's own files are for, in fixed words. Their docstrings
#: are written for the generator ("Generated — run `python main.py`"); a user
#: debugging the app needs to know which of them are theirs.
_UI_NOTE = "Rewritten by every Generate — don't edit."
GENERATED_ROLES = {
    "main.py": ("Starts the app: checks the packages it needs are installed, "
                "then opens the window. Rewritten by every Generate."),
    "launch.py": ("The old entry point, kept so old shortcuts still work — "
                  "it just runs main.py."),
    "app.py": ("The app's window and its start-up code. Written once when "
               "the project was made; Generate never touches it."),
    "handlers.py": ("What each button and control does, one method per "
                    "widget. Yours to edit — Generate only adds new stubs and "
                    "rewrites stubs nobody has touched."),
    "ui/__init__.py": f"Makes ui/ a package. {_UI_NOTE}",
    "ui/main_ui.py": ("The generated layout: every widget, where it sits and "
                      f"how it resizes. {_UI_NOTE}"),
    "ui/ports.py": ("How handlers read and set each widget's value "
                    f"(self.ports.<name>). {_UI_NOTE}"),
    "ui/widgets.py": ("The app's composite widgets (picture, frame slider, "
                      f"folder picker). {_UI_NOTE}"),
}
_UI_DEFAULT = f"Generated layout code. {_UI_NOTE}"

#: The app's own files, in the order a person meets them.
_APP_ORDER = ("main.py", "launch.py", "app.py", "handlers.py")

#: Longest description shown. The first sentence, cut here with an ellipsis.
SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class Script:
    """One Python file the app runs or can reach."""
    #: "handlers.py", "ui/main_ui.py" for the app's files; the dotted module
    #: name ("frame_camera", "council_core.capture") for everything else.
    name: str
    path: Path
    #: What it does, in one sentence.
    what: str
    #: APP, LINKED or COUNCIL.
    group: str
    #: Already imported in this process.
    loaded: bool

    def line(self) -> str:
        """One tab-separated line, for Copy all."""
        state = "loaded" if self.loaded else "not loaded yet"
        return f"{self.name}\t{self.path}\t{state}\t{self.what}"


# ======================================================================
# Script-linkable — what a Settings button calls
# ======================================================================
def settings_menu() -> Dict[str, Any]:
    """Drop the Settings menu down under the button that was pressed.

    Script-linkable with no inputs. The menu is a non-blocking popup: the
    app keeps running (a camera keeps capturing) while it is open. Its one
    item, Python Scripts, opens the window show_python_scripts opens.
    """
    qt = _qt_app()
    if qt is None:
        return {"summary": "The Settings menu needs a Qt app — this one has "
                           "no Qt window to drop it from."}
    from council_qt.widgets import python_scripts as view

    menu, where = view.settings_menu(
        items=[(PYTHON_SCRIPTS, show_python_scripts)])
    old = _HELD.get("menu")
    if old is not None and view.alive(old):
        old.deleteLater()
    _HELD["menu"] = menu
    _warm_up()
    if _dialogs_disabled():
        return {"summary": f"Settings: {PYTHON_SCRIPTS} (not shown — "
                           f"dialogs are disabled)."}
    menu.popup(where)
    return {"summary": f"Settings: {PYTHON_SCRIPTS}"}


def show_python_scripts(parent: Any = None) -> Dict[str, Any]:
    """Open the Python Scripts window, or bring it forward if it is open.

    Script-linkable too, for a project that wants its own button for it.
    Non-modal and kept: it stays open beside the app, and a second press
    refreshes it rather than opening another.
    """
    qt = _qt_app()
    if qt is None:
        rows = scripts_in_use()
        return {"summary": f"{len(rows)} Python files — there is no Qt "
                           f"window to list them in."}
    from council_qt.widgets import python_scripts as view

    window = _HELD.get("window")
    if window is not None and not view.alive(window):
        window = None
    if window is None:
        window = view.ScriptsWindow(
            load=scripts_in_use, header=interpreter_lines,
            titles=GROUP_TITLES, parent=parent or qt.activeWindow())
        _HELD["window"] = window
    else:
        window.refresh()
    if not _dialogs_disabled():
        window.show()
        window.raise_()
        window.activateWindow()
    return {"summary": f"{PYTHON_SCRIPTS}: {window.count} files"}


def python_scripts() -> Dict[str, Any]:
    """The same list as rows, for a listbox port — one file per row."""
    rows = scripts_in_use()
    return {"rows": [f"{s.name} — {s.what} ({s.path})" for s in rows],
            "summary": f"{len(rows)} Python files, "
                       f"{sum(s.loaded for s in rows)} loaded."}


#: The menu and the window, held so they are not garbage-collected shut
#: (the same reason frame_camera holds its pop-out windows).
_HELD: Dict[str, Any] = {}


def _warm_up() -> None:
    """Parse the app's files on a worker while the menu is open.

    The first list reads and parses every file (0.2-0.26 s for Typhon's 24,
    measured, nearly all of it ast.parse); a later one only stats them
    (about 30 ms in a running Typhon with 1,400 modules loaded). The user takes longer than that to move to "Python Scripts",
    so parsing while the menu is up makes the window's FIRST open the cheap
    one. Results go into _PARSED only — nothing on the worker touches Qt —
    and a second parse of the same file by the UI thread is merely wasted,
    never wrong.
    """
    if _PARSED or _HELD.get("warming"):
        return
    import threading

    def work() -> None:
        try:
            scripts_in_use()
        except Exception:                                 # noqa: BLE001
            pass
        finally:
            _HELD.pop("warming", None)

    worker = threading.Thread(target=work, name="python-scripts-warm",
                              daemon=True)
    _HELD["warming"] = worker
    worker.start()


def _qt_app() -> Any:
    """The running QApplication, or None — without importing Qt into a
    process that has not already imported it."""
    if "PySide6.QtWidgets" not in sys.modules:
        return None
    from PySide6.QtWidgets import QApplication

    return QApplication.instance()


def _dialogs_disabled() -> bool:
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


# ======================================================================
# The list — no toolkit
# ======================================================================
def interpreter_lines(app_dir: Any = None) -> List[str]:
    """What goes above the table, and at the top of Copy all."""
    folder = Path(app_dir) if app_dir else app_folder()
    version = sys.version.split()[0]
    return [f"Python {version} — {sys.executable}",
            f"App folder: {folder if folder else '(not a generated app)'}",
            f"Council folder: {COUNCIL_ROOT}"]


def app_folder() -> Optional[Path]:
    """The running app's project folder, or None when this is not one.

    Found from the app itself: its `app` and `handlers` modules (main.py
    imports them by those names), then __main__, then argv[0].
    """
    candidates = []
    for name in ("app", "handlers", "__main__"):
        path = getattr(sys.modules.get(name), "__file__", None)
        if path:
            candidates.append(Path(path))
    if sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]))
    for path in candidates:
        try:
            folder = path.resolve().parent
        except OSError:
            continue
        if _looks_like_project(folder):
            return folder
    return None


def _looks_like_project(folder: Path) -> bool:
    return (folder / "handlers.py").is_file() and (folder / "ui").is_dir()


def scripts_in_use(app_dir: Any = None, council_root: Any = None,
                   modules: Optional[Mapping[str, Any]] = None) -> List[Script]:
    """Every Python file the app runs or can reach, and what each is for.

    The app's own files (main.py, app.py, handlers.py, ui/*.py, and any
    module of its own they import), then the Council modules it links to
    (frame_camera, gui_settings, ...), then every Council module THOSE
    import — found by following import statements with ast, including the
    ones inside functions (a handler imports its linked function there).
    Anything already in sys.modules from either folder is added too, so a
    module reached some other way is not missing from the list.

    `modules` stands in for sys.modules (tests); `app_dir` defaults to the
    running app's folder.
    """
    app = Path(app_dir).resolve() if app_dir else app_folder()
    council = Path(council_root).resolve() if council_root else COUNCIL_ROOT
    loaded = _loaded_files(sys.modules if modules is None else modules)
    roots = [r for r in (app, council) if r is not None]
    tops = {root: _top_names(root) for root in roots}
    linked = _linked_names()

    found: Dict[str, Script] = {}
    queue: List[Tuple[Path, str]] = []

    def add(path: Path, name: str, group: str, what: str) -> None:
        key = _key(path)
        if key in found:
            return
        found[key] = Script(name, path, what, group, key in loaded)
        queue.append((path, _module_name(path, roots)))

    if app is not None:
        for path in _app_files(app):
            rel = path.relative_to(app).as_posix()
            role = GENERATED_ROLES.get(rel) or (
                _UI_DEFAULT if rel.startswith("ui/") else "")
            problem = _info(path)[3]
            # A generated file keeps its fixed words, with a parse error in
            # front of them: a handlers.py broken while the app runs is
            # exactly what this window is opened to find.
            what = (f"{problem} {role}".strip() if role else
                    _what(path, _OWN_UNDESCRIBED))
            add(path, rel, APP, what)

    while queue:
        path, name = queue.pop(0)
        for wanted in _info(path)[0]:
            target = _resolve(_absolute(wanted, name, path), roots, tops)
            if target is None:
                continue
            where, dotted = target
            if where[0] == app:
                add(where[1], where[1].relative_to(app).as_posix(), APP,
                    _what(where[1], _OWN_UNDESCRIBED))
            else:
                group = LINKED if dotted.split(".")[0] in linked else COUNCIL
                add(where[1], dotted, group,
                    _what(where[1], _UNDESCRIBED))

    for path in _loaded_under(loaded, roots):
        if _key(path) in found:
            continue
        in_app = app is not None and _inside(path, app)
        dotted = _module_name(path, roots)
        group = APP if in_app else (
            LINKED if dotted.split(".")[0] in linked else COUNCIL)
        name = path.relative_to(app).as_posix() if in_app else dotted
        found[_key(path)] = Script(name, path, _what(path, _UNDESCRIBED),
                                   group, True)
    return _ordered(found.values())


_OWN_UNDESCRIBED = "Your own module (it has no docstring)."
_UNDESCRIBED = "(no description in the file)"


def _app_files(app: Path) -> List[Path]:
    """main.py, launch.py, app.py, handlers.py, then ui/*.py."""
    out = [app / n for n in _APP_ORDER if (app / n).is_file()]
    ui = app / "ui"
    if ui.is_dir():
        out += sorted(ui.glob("*.py"),
                      key=lambda p: (p.name != "__init__.py", p.name))
    return out


def _ordered(scripts: Iterable[Script]) -> List[Script]:
    """App files in the order a person meets them, then linked modules,
    then the rest, each alphabetically."""
    rank = {APP: 0, LINKED: 1, COUNCIL: 2}

    def key(s: Script):
        if s.group == APP:
            fixed = (_APP_ORDER.index(s.name) if s.name in _APP_ORDER
                     else len(_APP_ORDER) + (0 if s.name.startswith("ui/")
                                             else 1))
            return (0, fixed, s.name.lower())
        return (rank[s.group], 0, s.name.lower())
    return sorted(scripts, key=key)


def _linked_names() -> frozenset:
    try:
        import gui_policy
        return frozenset(gui_policy.LINKED_MODULES)
    except Exception:                                     # noqa: BLE001
        return frozenset()


# ----------------------------------------------------------------------
# Reading a file: its imports and its first sentence, cached
# ----------------------------------------------------------------------
#: (imports, summary, is a package marker, why it could not be parsed).
_Info = Tuple[Tuple[Tuple[str, str, int], ...], str, bool, str]

#: path key -> ((size, mtime_ns), _Info).
_PARSED: Dict[str, Tuple[Tuple[int, int], _Info]] = {}


def _info(path: Path) -> _Info:
    """(imports, one-sentence summary, is a package marker, problem) for a
    file.

    Imports are (module, name, level) triples — `from a import b` is
    ("a", "b", 0), `import a.b` is ("a.b", "", 0) — every one in the file,
    inside functions too. A file that cannot be read or parsed has none, and
    says why in `problem` ("Does not parse: invalid syntax (line 3)."): the
    window is for debugging, and a module with a syntax error in it is the
    likeliest thing being debugged — it used to read "it has no docstring".
    """
    key = _key(path)
    try:
        st = path.stat()
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError:
        return (), "", False, ""
    held = _PARSED.get(key)
    if held is not None and held[0] == stamp:
        return held[1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError) as exc:
        if isinstance(exc, SyntaxError):
            where = f" (line {exc.lineno})" if exc.lineno else ""
            problem = f"Does not parse: {exc.msg}{where}."
        else:
            problem = f"Cannot be read: {exc}."
        got: _Info = ((), "", False, problem)
        _PARSED[key] = (stamp, got)
        return got
    imports: List[Tuple[str, str, int]] = []
    for node in _statements(tree.body):
        if isinstance(node, ast.Import):
            imports.extend((a.name, "", 0) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imports.append((node.module or "", a.name, node.level or 0))
    name = path.stem if path.name != "__init__.py" else path.parent.name
    got = (tuple(imports), summarise(ast.get_docstring(tree) or "", name),
           _is_marker(tree), "")
    _PARSED[key] = (stamp, got)
    return got


def _what(path: Path, fallback: str) -> str:
    """What a file is for: its problem first when it has one, else its
    first sentence, else `fallback`."""
    _imports, summary, _marker, problem = _info(path)
    return problem or summary or fallback


#: The fields of a compound statement that hold more statements.
_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")


def _statements(body: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Every statement, nested ones too (inside def, class, if, try, with,
    for, match) — but never an expression. An import is a statement, and
    ast.walk visiting every name and call in the file was 60% of a cold list
    (72,000 nodes for 22 files, measured); this visits a tenth of them."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        for field in _BLOCKS:
            inner = getattr(node, field, None)
            if inner:
                stack.extend(inner)


def _is_marker(tree: ast.Module) -> bool:
    """A package __init__ that only marks a package: a docstring, a
    __future__ import, an __all__. Listing those is noise."""
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(
                getattr(node, "value", None), ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id == "__all__"
                for t in node.targets):
            continue
        return False
    return True


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def summarise(doc: str, name: str = "") -> str:
    """The first sentence of a module docstring, as one clean line.

    The Council's docstrings open "frame_camera.py — a live camera for ...";
    the name before the dash is dropped (the table already says it), the
    lines are joined, and it stops at the first sentence — or the second
    when the first is a fragment ("Typhon's slider.").
    """
    para = str(doc or "").strip().split("\n\n")[0]
    text = " ".join(line.strip() for line in para.splitlines()).strip()
    if not text:
        return ""
    last = name.split(".")[-1].lower() if name else ""
    for sep in (" — ", " -- ", " - ", ": "):
        head, found, rest = text.partition(sep)
        stem = head.strip().lower()
        if found and " " not in stem and last and (
                stem.removesuffix(".py").split(".")[-1] == last):
            text = rest.strip()
            break
    parts = _SENTENCE_END.split(text)
    out = parts[0]
    if len(out) < 30 and len(parts) > 1:
        out = f"{out} {parts[1]}"
    if len(out) > SUMMARY_LIMIT:
        out = out[:SUMMARY_LIMIT - 1].rstrip() + "…"
    return out[:1].upper() + out[1:]


# ----------------------------------------------------------------------
# Resolving an import to a file under the app's folder or the Council's
# ----------------------------------------------------------------------
def _top_names(root: Path) -> frozenset:
    """Names importable from `root`: its .py files and its folders. Asked
    once per root, so `import os` is dismissed without touching the disk."""
    try:
        return frozenset(e.name[:-3] if e.name.endswith(".py") else e.name
                         for e in os.scandir(root)
                         if e.name.endswith(".py") or e.is_dir())
    except OSError:
        return frozenset()


def _absolute(wanted: Tuple[str, str, int], importer: str,
              path: Path) -> List[str]:
    """The dotted names an import could mean, most specific first.

    `from a import b` is module a.b if that is a file, else a (b is a name
    in it). Relative imports are resolved against the importer's package.
    """
    module, name, level = wanted
    if level:
        package = importer.split(".") if path.name == "__init__.py" \
            else importer.split(".")[:-1]
        if level > 1:
            package = package[:len(package) - (level - 1)]
        base = ".".join(package + ([module] if module else []))
    else:
        base = module
    if not base:
        return []
    if name and name != "*":
        return [f"{base}.{name}", base]
    return [base]


def _resolve(names: List[str], roots: List[Path],
             tops: Dict[Path, frozenset]
             ) -> Optional[Tuple[Tuple[Path, Path], str]]:
    """((root, file), dotted name) for the first of `names` that is a file
    under a root — skipping package markers — or None."""
    for dotted in names:
        parts = dotted.split(".")
        for root in roots:
            if parts[0] not in tops.get(root, ()):
                continue
            base = root.joinpath(*parts)
            module = base.with_suffix(".py")
            if module.is_file():
                return (root, module), dotted
            init = base / "__init__.py"
            if init.is_file():
                if _info(init)[2]:
                    # A marker: the import is real, the file is not worth a
                    # row. Stop here rather than fall back to the parent.
                    return None
                return (root, init), dotted
    return None


def _module_name(path: Path, roots: List[Path]) -> str:
    """The dotted name `path` is imported by, from whichever root holds it."""
    for root in roots:
        if _inside(path, root):
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join(parts)
    return path.stem


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _key(path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _loaded_files(modules: Mapping[str, Any]) -> Dict[str, str]:
    """normcased path -> path of every loaded module that is a .py file.

    Plain strings, no Path objects: a running Typhon has ~1,150 of these
    (PySide6, numpy, PIL, sklearn) and only a dozen are under the app's or
    the Council's folder. Measured in an updated Typhon, building a Path for
    each and comparing it with both roots by Path.relative_to was 50-70 ms
    of a 90 ms warm list."""
    out: Dict[str, str] = {}
    for module in list(modules.values()):
        # From the module's own dict: a lazy module's __getattr__ is never
        # asked for a __file__ it does not have.
        found = getattr(module, "__dict__", None)
        path = found.get("__file__") if isinstance(found, dict) else None
        # Absolute only. PySide6's shibokensupport modules carry RELATIVE
        # made-up paths ("shibokensupport/__init__.py"), which abspath
        # resolved against the working directory — measured: a Typhon
        # started from the Council's folder listed eleven of them as
        # Council modules.
        if isinstance(path, str) and path.endswith(".py") \
                and os.path.isabs(path):
            out[_key(path)] = path
    return out


def _loaded_under(loaded: Dict[str, str], roots: List[Path]) -> List[Path]:
    """Loaded files inside a root — not in a hidden folder (.venv, .claude)
    or an installed package, and not a package marker. Matched on the
    normcased keys by prefix, so a module elsewhere costs one startswith."""
    prefixes = [_key(root).rstrip(os.sep) + os.sep for root in roots]
    out = []
    for key, raw in loaded.items():
        prefix = next((p for p in prefixes if key.startswith(p)), None)
        if prefix is None:
            continue
        rel = key[len(prefix):].split(os.sep)
        if any(p.startswith(".") or p in ("site-packages", "__pycache__")
               for p in rel):
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        if path.name == "__init__.py" and _info(path)[2]:
            continue
        out.append(path)
    return sorted(out)
