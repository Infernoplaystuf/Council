"""
gui_projects.py — project storage for the GUI Designer.

One project is a directory under <vault>/GUI_Projects/<name>/ holding the
.gspec (the source of truth), a manifest, the generated ui/ tree, the
hand-written app.py, and timestamped backups. No Tk, no model — filesystem and
AST only, so it is testable against a tmp_path.

WHY delete() ARCHIVES INSTEAD OF DELETING
-----------------------------------------
A project directory is the user's work: a wireframe they drew and handler code
they wrote by hand. This app's standing rule is that it never destroys user
data, and a name-matched shutil.rmtree is exactly the shape of the bug that
once wiped real files in this repo — a cleanup routine matched on NAME alone
and deleted a file it had not verified. So delete() MOVES the project into
.trash/<name>__<timestamp>/ and returns where it went. Recovery is a rename;
there is no code path here that unlinks a user's directory tree.

WHY THE WIDGET-NAME REGISTRY LIVES IN THE MANIFEST
--------------------------------------------------
main_ui.py assigns self.<widget_name> and app.py references those names by
hand. If regeneration renamed a widget because the user retyped its label, the
generated code and the hand-written code would silently disagree — app.py would
reference an attribute that no longer exists, and the failure would surface at
runtime as an AttributeError in a callback, far from the edit that caused it.
So names are assigned once, recorded here, and reused on every regeneration
(spec 7.2). find_orphans() is the guard for the case where a widget genuinely
goes away.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from gui_shapes import GSPEC_VERSION, Project, load_gspec, save_gspec

PROJECTS_DIRNAME = "GUI_Projects"
MANIFEST_NAME = "manifest.json"
GSPEC_NAME = "project.gspec"
TRASH_DIRNAME = ".trash"
BACKUPS_DIRNAME = ".backups"
UI_DIRNAME = "ui"

# Import modes (spec 8). Recorded at creation, enforced by gui_policy.
MODES = ("linked", "standalone")

# A project name becomes a directory name, so it is restricted to characters
# that cannot escape the projects root. Rejecting rather than sanitising is
# deliberate: silently turning "../../etc" into "etc" would open a project the
# user did not ask for.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


class ProjectError(ValueError):
    """A project operation that cannot proceed, with an actionable reason."""


def resolve_vault_root(vault_dir: Optional[Any] = None) -> Path:
    """The vault root. Explicit arg wins; else COUNCIL_VAULT_ROOT; else the
    default. Mirrors app_built_tools.resolve_vault_root — kept in sync with the
    rest of the app rather than imported, so this module stays loadable on its
    own."""
    if vault_dir:
        return Path(vault_dir).expanduser()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".council" / "vault"


def projects_dir(vault_dir: Optional[Any] = None) -> Path:
    return resolve_vault_root(vault_dir) / PROJECTS_DIRNAME


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _validate_name(name: str) -> str:
    n = str(name or "").strip()
    if not _SAFE_NAME.match(n):
        raise ProjectError(
            f"invalid project name {name!r}: use letters, digits, spaces, "
            f"'.', '_' or '-', starting with a letter or digit (max 64)")
    if n in (".", "..") or n.startswith("."):
        raise ProjectError(f"invalid project name {name!r}")
    return n


def project_path(name: str, vault_dir: Optional[Any] = None) -> Path:
    """The directory for ``name``, guaranteed to sit inside the projects root.

    The containment check is not belt-and-braces: _validate_name already
    rejects traversal, but a resolve() comparison is what actually proves the
    result cannot escape, and it costs nothing."""
    root = projects_dir(vault_dir).resolve()
    p = (root / _validate_name(name)).resolve()
    if root not in p.parents and p != root:
        raise ProjectError(f"{name!r} would resolve outside the projects root")
    return p


# ============================================================
# Manifest
# ============================================================

@dataclass
class Manifest:
    """Everything about a project that is not the wireframe itself."""
    name: str
    mode: str = "linked"
    gspec_version: int = GSPEC_VERSION
    # shape id -> assigned widget name. Stable across regeneration (spec 7.2).
    widget_names: Dict[str, str] = field(default_factory=dict)
    # shape id (or radio group key) -> assigned port name. Same registry-first
    # discipline as widget_names: hand-written app.py references these by name,
    # so retyping a widget's label may not silently rename the port.
    port_names: Dict[str, str] = field(default_factory=dict)
    # ui/<relpath> -> sha256, so a hand-edit of generated code is detectable.
    ui_checksums: Dict[str, str] = field(default_factory=dict)
    detached: bool = False
    created: str = ""
    updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _manifest_path(pdir: Path) -> Path:
    return pdir / MANIFEST_NAME


def load_manifest(pdir: Any) -> Manifest:
    p = _manifest_path(Path(pdir))
    if not p.exists():
        raise ProjectError(f"no manifest at {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectError(f"{p.name} is not valid JSON: {exc}")
    return Manifest(
        name=str(raw.get("name") or Path(pdir).name),
        mode=str(raw.get("mode") or "linked"),
        gspec_version=int(raw.get("gspec_version") or GSPEC_VERSION),
        widget_names=dict(raw.get("widget_names") or {}),
        port_names=dict(raw.get("port_names") or {}),
        ui_checksums=dict(raw.get("ui_checksums") or {}),
        detached=bool(raw.get("detached", False)),
        created=str(raw.get("created") or ""),
        updated=str(raw.get("updated") or ""),
    )


def save_manifest(pdir: Any, m: Manifest) -> None:
    m.updated = _now_stamp()
    d = Path(pdir)
    d.mkdir(parents=True, exist_ok=True)
    _manifest_path(d).write_text(
        json.dumps(m.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


# ============================================================
# Lifecycle
# ============================================================

def create(name: str, mode: str = "linked",
           vault_dir: Optional[Any] = None) -> Path:
    """Create a project directory and return it. Refuses to overwrite."""
    if mode not in MODES:
        raise ProjectError(f"unknown mode {mode!r}; expected one of {MODES}")
    pdir = project_path(name, vault_dir)
    if pdir.exists():
        raise ProjectError(f"project {name!r} already exists at {pdir}")
    (pdir / UI_DIRNAME).mkdir(parents=True)
    (pdir / BACKUPS_DIRNAME).mkdir(exist_ok=True)
    save_manifest(pdir, Manifest(name=_validate_name(name), mode=mode,
                                 created=_now_stamp()))
    save_gspec(pdir / GSPEC_NAME,
               Project(project=_validate_name(name), mode=mode))
    return pdir


def open_project(name: str, vault_dir: Optional[Any] = None) -> Project:
    """Load a project's wireframe.

    Named open_project, not open: shadowing the builtin inside a module that
    does file I/O is a hazard that buys nothing. The brief's `open(name)` is
    this function."""
    pdir = project_path(name, vault_dir)
    g = pdir / GSPEC_NAME
    if not g.exists():
        raise ProjectError(f"project {name!r} has no {GSPEC_NAME}")
    return load_gspec(g)


def save_project(name: str, project: Project,
                 vault_dir: Optional[Any] = None) -> Path:
    pdir = project_path(name, vault_dir)
    if not pdir.exists():
        raise ProjectError(f"no such project: {name!r}")
    out = pdir / GSPEC_NAME
    save_gspec(out, project)
    return out


def list_projects(vault_dir: Optional[Any] = None) -> List[str]:
    """Project names, sorted. Directories starting with '.' (.trash) skipped."""
    root = projects_dir(vault_dir)
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and (d / GSPEC_NAME).exists())


def delete(name: str, vault_dir: Optional[Any] = None) -> Path:
    """ARCHIVE a project into .trash/ and return where it went.

    Deliberately not a deletion. See the module docstring: a project holds
    hand-written handler code, and this app does not destroy user work on a
    name match. Recovery is a rename out of .trash/."""
    pdir = project_path(name, vault_dir)
    if not pdir.exists():
        raise ProjectError(f"no such project: {name!r}")
    trash = projects_dir(vault_dir) / TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{pdir.name}__{_now_stamp()}"
    shutil.move(str(pdir), str(dest))
    return dest


# ============================================================
# Backups + checksums (spec 7.3)
# ============================================================

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def ui_checksums(pdir: Any) -> Dict[str, str]:
    """sha256 of every file under ui/, keyed by POSIX-style relative path.

    Forward slashes regardless of platform so a manifest written on Windows
    still matches on Linux — a checksum map keyed by os.sep would report every
    file as changed after moving a project between machines."""
    d = Path(pdir) / UI_DIRNAME
    out: Dict[str, str] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.rglob("*")):
        if f.is_file():
            out[f.relative_to(d).as_posix()] = _sha256(f)
    return out


def hand_edited_ui_files(pdir: Any) -> List[str]:
    """ui/ files whose content no longer matches the manifest.

    ui/ is 100% generated and overwritten on every regeneration, so an edit
    there is about to be lost. Surfacing it BEFORE regenerating is the whole
    point — the user can move the change into app.py or a sentinel region."""
    try:
        m = load_manifest(pdir)
    except ProjectError:
        return []
    now = ui_checksums(pdir)
    changed = [k for k, v in now.items() if m.ui_checksums.get(k) not in (None, v)]
    gone = [k for k in m.ui_checksums if k not in now]
    return sorted(changed + gone)


def backup(pdir: Any) -> Path:
    """Timestamped copy of ui/ and app.py into .backups/<stamp>/.

    Taken BEFORE any regeneration writes a byte. Cheap insurance: the whole
    point of the two-file split is that regeneration is safe, and a backup is
    what makes that claim testable rather than merely asserted."""
    d = Path(pdir)
    dest = d / BACKUPS_DIRNAME / _now_stamp()
    dest.mkdir(parents=True, exist_ok=True)
    src_ui = d / UI_DIRNAME
    if src_ui.is_dir():
        shutil.copytree(src_ui, dest / UI_DIRNAME, dirs_exist_ok=True)
    for extra in ("app.py", "handlers.py", GSPEC_NAME):
        s = d / extra
        if s.is_file():
            shutil.copy2(s, dest / extra)
    return dest


# ============================================================
# Orphan detection (spec 7.3)
# ============================================================

@dataclass
class Orphan:
    """Something hand-written code depends on that the new spec would remove."""
    kind: str            # "widget" | "handler"
    name: str
    referenced_in: str   # file name
    line: int = 0

    def describe(self) -> str:
        return (f"{self.kind} {self.name!r} is used in {self.referenced_in}"
                f":{self.line} but the new wireframe no longer defines it")


def _self_attrs_and_handlers(src: str) -> tuple:
    """(self.<attr> reads, defined on_* methods) with line numbers.

    An AST walk rather than a regex because `self.btn_go` appears in strings and
    comments too, and a false orphan blocks a legitimate regeneration."""
    attrs: List[tuple] = []
    handlers: List[tuple] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # Hand-written code that does not parse is the user's problem to fix,
        # but it must not crash the orphan check — report nothing rather than
        # blocking regeneration on a file we cannot read.
        return [], []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            attrs.append((node.attr, getattr(node, "lineno", 0)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("on_"):
                handlers.append((node.name, getattr(node, "lineno", 0)))
    return attrs, handlers


def port_references(pdir: Any) -> List[Tuple[str, str, int]]:
    """(file, port_name, line) for every ``self.ports.<name>`` and
    ``self.ports["<name>"]`` in app.py / handlers.py.

    A REAL hole this closes: _self_attrs_and_handlers walks
    ``ast.Attribute`` whose ``.value`` is ``ast.Name("self")``. In
    ``self.ports.scan_folder`` the outer Attribute's ``.value`` is another
    Attribute, not a Name — so the walk sees only the harmless ``ports`` and
    misses every port reference. Port renames and deletes are invisible to
    find_orphans today, and this is the shared AST reader that fixes it.

    An AST walk rather than substring for the same reason as
    _self_attrs_and_handlers: a hit inside a string or comment would report a
    false orphan and block a legitimate regeneration."""
    out: List[Tuple[str, str, int]] = []
    d = Path(pdir)
    for fname in ("app.py", "handlers.py"):
        f = d / fname
        if not f.is_file():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # self.ports.<name>
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "ports"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"):
                out.append((fname, node.attr, getattr(node, "lineno", 0)))
            # self.ports["<name>"]
            elif (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "ports"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"):
                key = _string_key(node.slice)
                if key is not None:
                    out.append((fname, key, getattr(node, "lineno", 0)))
    return out


def _string_key(node) -> Optional[str]:
    """The str inside ``self.ports[<...>]``, or None if it is not a plain str.

    A computed key (``self.ports[some_var]``) is invisible to this scan — the
    same limitation _self_attrs_and_handlers has for ``getattr(self, name)``,
    and stated in port_references' docstring rather than papered over."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Index) and isinstance(node.value, ast.Constant) \
            and isinstance(node.value.value, str):                      # py<3.9
        return node.value.value
    return None


def find_orphans(pdir: Any, new_widget_names: Iterable[str], *,
                 new_port_names: Iterable[str] = ()) -> List[Orphan]:
    """Widgets/handlers/ports hand-written code still uses that the new spec
    drops.

    ``new_widget_names`` is the widget-name registry the next generation
    would produce; ``new_port_names`` is the parallel registry for ports.
    Both must block regeneration when non-empty — silently removing either
    turns a working app into an AttributeError inside a callback.

    ``new_port_names`` is keyword-only so existing single-arg callers keep
    working; step 7 wires the tab through."""
    d = Path(pdir)
    names: Set[str] = {str(n) for n in new_widget_names}
    port_names: Set[str] = {str(n) for n in new_port_names}
    # Handler names the new spec implies: a widget named btn_go binds on_btn_go.
    implied = {f"on_{n}" for n in names}
    out: List[Orphan] = []

    for fname in ("app.py", "handlers.py"):
        f = d / fname
        if not f.is_file():
            continue
        attrs, handlers = _self_attrs_and_handlers(
            f.read_text(encoding="utf-8", errors="replace"))
        seen: Set[str] = set()
        for attr, line in attrs:
            # Only widget-shaped attributes: a known kind prefix. Without this
            # every self.foo in hand-written code reads as a missing widget.
            if not _looks_like_widget(attr) or attr in names or attr in seen:
                continue
            seen.add(attr)
            out.append(Orphan("widget", attr, fname, line))
        for h, line in handlers:
            if h in implied or h in seen:
                continue
            # A handler with no widget is not automatically an orphan — the
            # user may call it themselves — so it is reported only when its
            # name maps onto a widget the spec USED to have and no longer does.
            base = h[3:]
            if base and base not in names and _looks_like_widget(base):
                seen.add(h)
                out.append(Orphan("handler", h, fname, line))

    # Ports live on a different attribute, so they need their own scan.
    seen_ports: Set[str] = set()
    for fname, pname, line in port_references(d):
        if pname in port_names or pname in seen_ports:
            continue
        seen_ports.add(pname)
        out.append(Orphan("port", pname, fname, line))

    return out


# ============================================================
# Port rename planning
# ============================================================
#
# The alias story of the ports plan (§3). We DERIVE aliases from AST evidence
# every regeneration — no manifest bookkeeping to keep in sync — so the alias
# is emitted only while hand-written code still uses the old name and vanishes
# on the first regeneration after the last reference is gone.

@dataclass
class PortPlan:
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)     # old -> new
    removed: List[Orphan] = field(default_factory=list)
    collisions: List[str] = field(default_factory=list)


def plan_ports(pdir: Any, old_registry: Dict[str, str],
               new_registry: Dict[str, str]) -> PortPlan:
    """Compare the manifest's port_names to the newly built one; decide
    which are renames (alias the old name), which are removed (block), and
    which would collide with a still-live port (block, no silent shadow).

    Rename detection is keyed on the shape id (or, for a radio group, the
    ``group:<parent>/<name>`` key gui_spec.port_registry emits) — same key on
    both sides means the port was RENAMED, not deleted-and-re-added, and a
    stable identity is what makes the alias safe."""
    plan = PortPlan()
    d = Path(pdir)
    live_new = set(new_registry.values())
    refs = port_references(d)
    referenced = {pname for _f, pname, _l in refs}

    for key, old_name in old_registry.items():
        new_name = new_registry.get(key)
        if new_name is None:
            # The port is gone from the new spec. Only an orphan if app.py
            # still uses it — otherwise a routine delete.
            for fname, pname, line in refs:
                if pname == old_name:
                    plan.removed.append(Orphan("port", old_name, fname, line))
                    break
            continue
        if new_name == old_name:
            continue
        plan.renamed.append((old_name, new_name))
        # Alias only while the old name is still referenced. That is the
        # self-expiring property — a rename that was already migrated leaves
        # no residue on the next regeneration.
        if old_name in referenced:
            if old_name in live_new:
                plan.collisions.append(
                    f"cannot alias {old_name!r} -> {new_name!r}: "
                    f"another live port is already called {old_name!r}")
            elif old_name in plan.aliases and plan.aliases[old_name] != new_name:
                plan.collisions.append(
                    f"cannot alias {old_name!r}: two ports want it as their "
                    f"old name ({plan.aliases[old_name]!r} and {new_name!r})")
            else:
                plan.aliases[old_name] = new_name
    return plan


# Kind prefixes assigned by spec 7.2. Kept here because orphan detection needs
# to tell a widget attribute from any other instance attribute.
WIDGET_PREFIXES = (
    "btn_", "lbl_", "ent_", "txt_", "chk_", "rad_", "cmb_", "lst_", "spn_",
    "scl_", "prg_", "sep_", "tbl_", "img_", "cht_", "scr_", "log_", "fpk_",
    "sts_", "tbr_", "frm_", "lfr_", "nbk_", "pnd_", "mnu_",
)


def _looks_like_widget(attr: str) -> bool:
    return attr.startswith(WIDGET_PREFIXES)


# ============================================================
# Detach (spec 7.5)
# ============================================================

def detach(pdir: Any) -> Path:
    """Merge ui/ into the project and mark it non-regenerable. ONE WAY.

    The escape hatch for a project that has outgrown the designer. A backup is
    taken first and ui/ is left in place: the manifest flag is what stops
    regeneration, so nothing is destroyed by detaching and a user who changes
    their mind still has every file."""
    d = Path(pdir)
    m = load_manifest(d)
    if m.detached:
        raise ProjectError("project is already detached")
    backup(d)
    merged = d / "detached_ui.py"
    parts: List[str] = [
        "# Detached from the GUI Designer — this project is no longer\n"
        "# regenerable from its .gspec. Edit freely.\n"
    ]
    ui = d / UI_DIRNAME
    if ui.is_dir():
        for f in sorted(ui.rglob("*.py")):
            parts.append(f"\n# ---- from {f.relative_to(d).as_posix()} ----\n")
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
    merged.write_text("".join(parts), encoding="utf-8")
    m.detached = True
    save_manifest(d, m)
    return merged
