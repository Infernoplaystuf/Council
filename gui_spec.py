"""
gui_spec.py — the validated intermediate representation.

Shapes + layout tree + classifications in, a Spec out. Everything downstream
(gui_emit) reads ONLY the Spec, never the raw shapes, so there is exactly one
place where "is this buildable?" is answered. Pure data — no Tk, no model.

WHY VALIDATION LIVES HERE AND NOT IN THE EMITTER
------------------------------------------------
The emitter's job is templating. If it also had to decide whether a kind is
real or a prop key is allowed, every template would carry defensive branches and
a bad spec would surface as half-written source — a file that imports, runs, and
is subtly wrong. validate() answers all of it up front and returns EVERY fault
at once, so a user fixing a wireframe sees the whole list rather than
rediscovering one problem per generation.

WHY WIDGET NAMES ARE READ FROM THE REGISTRY FIRST
--------------------------------------------------
main_ui.py assigns self.<name>; app.py, which the generator never rewrites,
references those names by hand. If a name were re-derived from the label on
every generation, retyping a button's caption would rename its attribute and
app.py would keep calling the old one — an AttributeError inside a callback,
surfacing far from the edit that caused it and with nothing pointing at
regeneration as the cause. So a shape that already has a name KEEPS it (spec
7.2), and dropping a widget is caught separately by gui_projects.find_orphans.
"""
from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import gui_ports as _gpo
from gui_ports import PortSpec
from gui_shapes import GENERIC_KIND, PALETTE, RESIZE_MODES, Shape, is_container

# kind -> attribute prefix (spec 7.2). The tuple of these prefixes is mirrored
# in gui_projects.WIDGET_PREFIXES for orphan detection; a test asserts the two
# agree, because a prefix known to one and not the other means a real widget
# reads as an ordinary attribute and its removal stops being caught.
KIND_PREFIX: Dict[str, str] = {
    "frame": "frm", "labelframe": "lfr", "notebook": "nbk",
    "panedwindow": "pnd", "freeform": "frm",
    "label": "lbl", "button": "btn", "entry": "ent", "text": "txt",
    "checkbutton": "chk", "radiobutton": "rad", "combobox": "cmb",
    "listbox": "lst", "spinbox": "spn", "scale": "scl",
    "progressbar": "prg", "separator": "sep",
    "treeview": "tbl",
    "image_canvas": "img", "chart_panel": "cht", "scrubber": "scr",
    "log_pane": "log", "file_picker": "fpk", "status_bar": "sts",
    "toolbar": "tbr", "menubar": "mnu",
    GENERIC_KIND: "lbl",
}

# Kinds that fire a callback, so the emitter binds command=self.on_<name> and a
# stub is appended to handlers.py.
COMMAND_KINDS = frozenset({
    "button", "checkbutton", "radiobutton", "combobox", "spinbox", "scale",
    "scrubber", "file_picker",
})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """A label -> an identifier fragment. 'Start Scan!' -> 'start_scan'."""
    s = _SLUG_RE.sub("_", str(text or "").strip().lower()).strip("_")
    return s or ""


def widget_name(kind: str, label: str, taken: Iterable[str]) -> str:
    """A unique, kind-prefixed attribute name (spec 7.2).

    A leading digit or a Python keyword would produce source that does not
    parse, so both are prefixed away rather than left to fail at emit time."""
    prefix = KIND_PREFIX.get(kind, "wdg")
    base = slug(label) or slug(kind) or "widget"
    if base[0].isdigit():
        base = f"n{base}"
    if keyword.iskeyword(base):
        base = f"{base}_"
    name = f"{prefix}_{base}"
    used = set(taken)
    if name not in used:
        return name
    i = 2
    while f"{name}_{i}" in used:
        i += 1
    return f"{name}_{i}"


# ============================================================
# The IR
# ============================================================

@dataclass
class WidgetSpec:
    shape_id: str
    name: str
    kind: str
    label: str = ""
    note: str = ""
    parent: Optional[str] = None          # parent widget NAME, None = root
    props: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[str] = None         # "on_<name>" when the kind commands

    # placement
    manager: str = "grid"
    row: int = 0
    column: int = 0
    rowspan: int = 1
    columnspan: int = 1
    sticky: str = ""
    padx: int = 0
    pady: int = 0
    relx: float = 0.0
    rely: float = 0.0
    relwidth: float = 0.0
    relheight: float = 0.0

    # container-only
    is_container: bool = False
    children: List[str] = field(default_factory=list)
    row_weights: List[int] = field(default_factory=list)
    col_weights: List[int] = field(default_factory=list)
    row_minsizes: List[int] = field(default_factory=list)
    col_minsizes: List[int] = field(default_factory=list)
    explicit_w: int = 0
    explicit_h: int = 0

    # typed binding, when the kind has one and the widget is not explicitly
    # opted out. Radio group members share the SAME PortSpec instance.
    port: Optional[PortSpec] = None

    # Colour, copied from Shape.bg / Shape.fg. Emitted only for kinds that
    # can honour it (gui_colors.COLOUR_CAPS); the emitter's classic-tk swap
    # is what makes that honouring possible on the default Windows theme.
    bg: str = ""
    fg: str = ""
    # Tk font spec, e.g. "Magneto 18 bold". Shape's own, else Window's.
    font: str = ""
    # A declared link to a Python function. See Shape.script.
    script: Dict[str, Any] = field(default_factory=dict)
    # A declared sequence link. See Shape.drives.
    drives: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Spec:
    project: str = "untitled"
    mode: str = "linked"
    title: str = "Untitled"
    min_w: int = 900
    min_h: int = 600
    # Root/MainUi colour. When bg is set, emit_main_ui swaps MainUi from
    # ttk.Frame to tk.Frame (which honours `background=`) and configures
    # both self and the toplevel with the colour.
    root_bg: str = ""
    root_fg: str = ""
    # Default font for every text-bearing widget that names none.
    root_font: str = ""
    widgets: List[WidgetSpec] = field(default_factory=list)
    root_children: List[str] = field(default_factory=list)
    root_row_weights: List[int] = field(default_factory=list)
    root_col_weights: List[int] = field(default_factory=list)
    root_row_minsizes: List[int] = field(default_factory=list)
    root_col_minsizes: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # Declared packages (Project.requires): checked at startup by the
    # generated main.py, and the project's additions to the policy allowlist.
    requires: List[str] = field(default_factory=list)

    def by_name(self, name: str) -> Optional[WidgetSpec]:
        for w in self.widgets:
            if w.name == name:
                return w
        return None

    def by_shape(self, sid: str) -> Optional[WidgetSpec]:
        for w in self.widgets:
            if w.shape_id == sid:
                return w
        return None

    @property
    def widget_names(self) -> List[str]:
        return [w.name for w in self.widgets]

    def name_registry(self) -> Dict[str, str]:
        """shape id -> widget name, for the manifest."""
        return {w.shape_id: w.name for w in self.widgets}

    def sequence_attr_names(self) -> Set[str]:
        """The non-port attributes a sequence link adds to Ports.

        emit_ports writes `self.browse_<index>` for every `drives`
        declaration, and hand-written code reaches for `.path()` on it to name
        the frame on screen. It is an attribute, not a port, so the orphan
        check has to be told about it explicitly or it reads every use as a
        reference to something the wireframe deleted. Kept HERE, beside the
        spec that decides the name, so the two cannot drift."""
        return {f"browse_{w.port.name}" for w in self.widgets
                if w.drives and w.port}

    @property
    def handlers(self) -> List[str]:
        return sorted({w.handler for w in self.widgets if w.handler})

    @property
    def ports(self) -> List[PortSpec]:
        """The ports, deduplicated. Radio group members share one PortSpec, so
        without dedup a group of three radios would appear three times."""
        seen: List[PortSpec] = []
        seen_ids = set()
        for w in self.widgets:
            if w.port is None or id(w.port) in seen_ids:
                continue
            seen.append(w.port)
            seen_ids.add(id(w.port))
        return seen

    @property
    def port_names(self) -> List[str]:
        return [p.name for p in self.ports]

    def port_registry(self, parents: Optional[Dict[str, str]] = None
                      ) -> Dict[str, str]:
        """The port_names registry entry to persist in the manifest.

        Delegates to gui_ports.registry_for so key formation (per-shape versus
        the ``group:<parent>/<name>`` shape used for radio groups) is decided
        in one place — same discipline as name_registry above."""
        return _gpo.registry_for(self.ports, parents=parents)


# ============================================================
# build
# ============================================================

def build(shapes: Sequence[Shape], layout_tree: Any,
          classifications: Optional[Dict[str, Any]] = None, *,
          registry: Optional[Dict[str, str]] = None,
          port_registry: Optional[Dict[str, str]] = None,
          project: str = "untitled", mode: str = "linked",
          title: str = "Untitled", min_w: int = 900,
          min_h: int = 600,
          root_bg: str = "", root_fg: str = "",
          root_font: str = "",
          requires: Sequence[str] = ()) -> Spec:
    """Assemble the IR.

    ``classifications`` maps shape id -> {"kind", "props"} for shapes the model
    typed; it is applied BEFORE naming so a classified treeview is named tbl_,
    not the lbl_ its generic placeholder would have produced.

    ``registry`` is the manifest's shape id -> name map. Existing names win.
    ``port_registry`` is the manifest's port_names, wired the same way — a
    registered port name wins over a fresh derivation, because hand-written
    app.py references it and rename is a deliberate action, not a side effect
    of retyping a label (§3 in the build spec)."""
    spec = Spec(project=project, mode=mode, title=title,
                min_w=min_w, min_h=min_h,
                root_bg=root_bg, root_fg=root_fg, root_font=root_font,
                requires=[str(r).strip() for r in requires if str(r).strip()])
    spec.warnings.extend(getattr(layout_tree, "warnings", []) or [])
    nodes = getattr(layout_tree, "nodes", {}) or {}
    reg = dict(registry or {})
    cls = dict(classifications or {})

    # Resolve kinds first — naming depends on them.
    kinds: Dict[str, str] = {}
    props: Dict[str, Dict[str, Any]] = {}
    for s in shapes:
        c = cls.get(s.id) or {}
        k = str(c.get("kind") or s.kind)
        if k not in PALETTE:
            spec.warnings.append(
                f"{s.label or s.id}: unknown kind {k!r}; treated as a label")
            k = "label"
        kinds[s.id] = k
        merged = dict(s.props or {})
        merged.update(dict(c.get("props") or {}))
        props[s.id] = merged

    # Names: registry first, in a stable order so a fresh project is
    # deterministic rather than dependent on dict iteration.
    taken: List[str] = []
    names: Dict[str, str] = {}
    ordered = sorted(shapes, key=lambda s: (s.z, s.y, s.x, s.id))
    for s in ordered:
        existing = reg.get(s.id)
        if existing and existing not in taken:
            names[s.id] = existing
        else:
            names[s.id] = widget_name(kinds[s.id], s.label, taken)
        taken.append(names[s.id])

    for s in ordered:
        n = nodes.get(s.id)
        kind = kinds[s.id]
        w = WidgetSpec(
            shape_id=s.id, name=names[s.id], kind=kind, label=s.label,
            note=s.note, props=props[s.id],
            parent=names.get(getattr(n, "parent_id", None) or "") or None,
            is_container=is_container(kind),
            bg=str(getattr(s, "bg", "") or ""),
            fg=str(getattr(s, "fg", "") or ""),
            font=str(getattr(s, "font", "") or ""),
            script=dict(getattr(s, "script", None) or {}),
            drives=dict(getattr(s, "drives", None) or {}),
        )
        if kind in COMMAND_KINDS:
            w.handler = f"on_{w.name}"
        if n is not None:
            for f in ("manager", "row", "column", "rowspan", "columnspan",
                      "sticky", "padx", "pady", "relx", "rely", "relwidth",
                      "relheight", "row_weights", "col_weights",
                      "row_minsizes", "col_minsizes", "explicit_w",
                      "explicit_h"):
                setattr(w, f, getattr(n, f))
            w.children = [names[c] for c in getattr(n, "children", [])
                          if c in names]
        spec.widgets.append(w)

    root = nodes.get("__root__")
    spec.root_children = [names[i] for i in getattr(layout_tree, "roots", [])
                          if i in names]
    if root is not None:
        spec.root_row_weights = list(root.row_weights)
        spec.root_col_weights = list(root.col_weights)
        spec.root_row_minsizes = list(root.row_minsizes)
        spec.root_col_minsizes = list(root.col_minsizes)

    # -- typed bindings ------------------------------------------------
    #
    # Ports are derived from a SHIM view of each shape that carries the
    # classified kind and merged props — gui_ports duck-types on
    # .id/.kind/.label/.props/.port, so the shim only needs those fields.
    # This is the seam that keeps a classified generic Frame from getting a
    # Frame's "no port" answer.
    class _S:
        __slots__ = ("id", "kind", "label", "props", "port", "z")
    shims = []
    for s in shapes:
        sh = _S()
        sh.id = s.id
        sh.kind = kinds[s.id]
        sh.label = s.label
        sh.props = props[s.id]
        sh.port = dict(getattr(s, "port", None) or {})
        sh.z = s.z
        shims.append(sh)
    parents = {c: getattr(n, "parent_id", None) for c in names
               for n in [nodes.get(c)] if n is not None}

    # ---- resolve colour INHERITANCE ---------------------------------
    #
    # A widget with no colour of its own takes its nearest coloured ancestor's
    # background. That is not a nicety: Tk has no transparency, so "a label
    # with a transparent background over a pink panel" IS "a label whose
    # background is that pink". Inheritance produces it with no extra concept,
    # and it also prevents the ugly default: a coloured panel scattered with
    # grey OS-default boxes.
    #
    # resolve_scene refuses to inherit into a kind that cannot honour colour
    # (Notebook, Treeview, Combobox, Progressbar), so the emitter is never
    # handed a colour it would have to drop.
    import gui_colors as _gcol
    _parents_nonnull = {k: v for k, v in parents.items() if v}
    effective = _gcol.resolve_scene(shapes, _parents_nonnull)
    # The window's own background is the root of the inheritance chain: a
    # top-level widget with no colour should sit on the window's colour.
    for w in spec.widgets:
        eff_bg, eff_fg = effective.get(w.shape_id, ("", ""))
        if not eff_bg and root_bg and _gcol.can_colour(w.kind, "bg"):
            try:
                eff_bg = _gcol.normalise(root_bg)
            except ValueError:
                eff_bg = ""
        if not eff_fg and _gcol.can_colour(w.kind, "fg"):
            # An EXPLICIT window foreground beats the derived one. auto_fg
            # picks maximum contrast, which against Barbie pink is near-black
            # — a perfectly reasonable default and the exact opposite of the
            # white lettering a user who set Window.fg asked for. Derived
            # colour is a fallback for when nobody chose, not an override.
            if root_fg:
                try:
                    eff_fg = _gcol.normalise(root_fg)
                except ValueError:
                    eff_fg = _gcol.auto_fg(eff_bg) if eff_bg else ""
            elif eff_bg:
                eff_fg = _gcol.auto_fg(eff_bg)
        w.bg, w.fg = eff_bg, eff_fg
        # Font: the shape's own, else the window's default.
        if not w.font and root_font:
            w.font = root_font

    ports = _gpo.build_ports(shims, parents=parents, registry=port_registry)
    by_sid: Dict[str, PortSpec] = {}
    for p in ports:
        for sid in p.shape_ids:
            by_sid[sid] = p
    for w in spec.widgets:
        w.port = by_sid.get(w.shape_id)
    return spec


# ============================================================
# validate
# ============================================================

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _top_level_defs(module: str, root: Any = None) -> Optional[set]:
    """Function and class names defined at the top of an app-root module, by
    PARSING its source — never importing it. None when the module is not a
    file in ``root`` (default: beside this one) — a vendor package, which is
    then not checked."""
    import ast as _ast
    from pathlib import Path as _Path
    base = _Path(root) if root else _Path(__file__).resolve().parent
    p = base / (module.replace(".", "/") + ".py")
    if not p.is_file():
        return None
    try:
        tree = _ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    out = set()
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, _ast.Assign):     # fn = other_fn aliases
            out.update(t.id for t in node.targets if isinstance(t, _ast.Name))
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            # A module that RE-EXPORTS a function (`from .core import scan`)
            # really does offer it; without this the check would reject a
            # working link as "has no function".
            if any(a.name == "*" for a in node.names):
                return None                     # star import: cannot know
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
    return out


def _script_errors(w, where: str, port_of: Dict[str, Any], spec: "Spec",
                   gpol, cache: Dict[str, Optional[set]]) -> List[str]:
    """Everything wrong with one widget's script link, one message each."""
    sc = dict(getattr(w, "script", None) or {})
    if not sc:
        return []
    errs: List[str] = []
    module = str(sc.get("module") or "").strip()
    func = str(sc.get("function") or "").strip()
    if not module or not all(p.isidentifier() for p in module.split(".")):
        errs.append(f"{where}: script link names no valid module ({module!r})")
        return errs
    if not func.isidentifier():
        errs.append(f"{where}: script link names no valid function ({func!r})")
        return errs
    if module.split(".")[0] not in gpol.allowed_modules(spec.mode,
                                                         spec.requires):
        errs.append(f"{where}: script module {module!r} is not allowed in "
                    f"{spec.mode} mode — add it to the project's requires")
    if module not in cache:
        cache[module] = _top_level_defs(module)
    defs = cache[module]
    if defs is not None and func not in defs:
        errs.append(f"{where}: {module} has no function {func!r}")
    for p in sc.get("inputs") or []:
        if str(p) not in port_of:
            errs.append(f"{where}: script input {p!r} names no port")
    targets = list((sc.get("outputs") or {}).keys())
    if sc.get("output"):
        targets.append(sc["output"])
    for p in targets:
        if str(p) not in port_of:
            errs.append(f"{where}: script output {p!r} names no port")
    return errs


def validate(spec: Spec) -> Tuple[bool, List[str]]:
    """(ok, errors). EVERY fault, not the first.

    A generator that stopped at the first problem would make the user fix one
    thing, regenerate, and discover the next — turning a five-minute correction
    into five rounds."""
    # A declared package widens this project's policy allowlist, so the
    # declaration is gated too: no denied module, no council_engine.
    import gui_policy as _gpol
    errs: List[str] = list(_gpol.check_requires(spec.requires))
    _module_defs: Dict[str, Optional[set]] = {}     # per-validate AST cache
    seen: Dict[str, str] = {}
    seen_ports: Dict[str, str] = {}   # port name -> shape id
    driven_sinks: Dict[str, str] = {}  # target port name -> driving widget
    # Every port in the spec, for resolving cross-references by name.
    port_of: Dict[str, Any] = {w.port.name: w for w in spec.widgets if w.port}

    for w in spec.widgets:
        where = f"{w.label or w.kind} ({w.name})"

        if w.kind not in PALETTE:
            errs.append(f"{where}: unknown widget kind {w.kind!r}")
            continue
        if w.kind == GENERIC_KIND:
            errs.append(
                f"{where}: still untyped — classify it or pick a palette kind")

        if not _IDENT_RE.match(w.name) or keyword.iskeyword(w.name):
            errs.append(f"{where}: {w.name!r} is not a valid Python attribute")
        if w.name in seen and seen[w.name] != w.shape_id:
            errs.append(f"duplicate widget name {w.name!r} — "
                        f"self.{w.name} would be overwritten")
        seen[w.name] = w.shape_id

        schema = PALETTE[w.kind].get("prop_schema") or {}
        for pk, pv in (w.props or {}).items():
            if pk not in schema:
                errs.append(f"{where}: {w.kind} has no property {pk!r} "
                            f"(allowed: {', '.join(sorted(schema)) or 'none'})")
                continue
            choices = schema[pk].get("choices")
            if choices and pv not in choices and pv not in ("", None):
                errs.append(f"{where}: {pk}={pv!r} is not one of "
                            f"{', '.join(map(str, choices))}")

        if w.handler and (not _IDENT_RE.match(w.handler)
                          or not w.handler.startswith("on_")):
            errs.append(f"{where}: handler {w.handler!r} is not a valid "
                        f"on_* method name")
        if w.kind in COMMAND_KINDS and not w.handler:
            errs.append(f"{where}: {w.kind} fires a callback but names no "
                        f"handler")

        if w.manager not in ("grid", "pack", "place"):
            errs.append(f"{where}: unknown geometry manager {w.manager!r}")
        if w.parent and spec.by_name(w.parent) is None:
            errs.append(f"{where}: parent {w.parent!r} is not in the spec")
        if not w.is_container and w.children:
            errs.append(f"{where}: {w.kind} cannot contain children")

        # -- script link --------------------------------------------------
        # Checked HERE, at Generate. It was never checked at all: a wrong
        # module, function or port name surfaced only when the button was
        # pressed, as a print to a console nobody reads.
        errs.extend(_script_errors(w, where, port_of, spec, _gpol,
                                   _module_defs))

        # -- sequence link (drives) --------------------------------------
        #
        # A BROKEN LINK USED TO BE A COMMENT. emit_ports wrote
        # "# scr_frame: sequence link skipped" into ui/ports.py and generation
        # succeeded, so deleting the image canvas — or renaming its port —
        # produced an app that built, ran, and did nothing when you moved the
        # slider. Nothing in the UI said why. These are blocking errors so the
        # break surfaces at Generate, next to the widget that caused it.
        d = dict(getattr(w, "drives", None) or {})
        if d:
            if w.kind not in _gpo.SEQUENCE_DRIVERS:
                errs.append(f"{where}: {w.kind} cannot drive a sequence "
                            f"(only {', '.join(sorted(_gpo.SEQUENCE_DRIVERS))} can)")
            if w.port is None:
                errs.append(f"{where}: drives a sequence but has no port to "
                            f"carry the frame index")
            src_name, snk_name = str(d.get("folder") or ""), str(d.get("target") or "")
            src, snk = port_of.get(src_name), port_of.get(snk_name)
            if src is None:
                errs.append(f"{where}: drives.folder names no port "
                            f"({src_name!r}) — the folder widget was renamed "
                            f"or deleted")
            elif src.kind not in _gpo.SEQUENCE_SOURCES:
                errs.append(f"{where}: {src.kind} cannot be a frame source")
            elif str((src.props or {}).get("mode", "")) != "folder":
                errs.append(
                    f"{where}: {src_name!r} is a file picker set to "
                    f"mode={(src.props or {}).get('mode')!r} — set mode=folder "
                    f"or it can never list frames")
            if snk is None:
                errs.append(f"{where}: drives.target names no port "
                            f"({snk_name!r}) — the display widget was renamed "
                            f"or deleted")
            elif snk.kind not in _gpo.SEQUENCE_SINKS:
                errs.append(f"{where}: {snk.kind} cannot display a frame")
            st_name = str(d.get("status") or "")
            if st_name and st_name not in port_of:
                errs.append(f"{where}: drives.status names no port "
                            f"({st_name!r})")
            # Optional ROI text port, kept in sync with the canvas's box so a
            # script link (crop-on-save) can read it. Both halves have to be
            # declared: a port with no drawable canvas behind it, or a canvas
            # whose box nothing can read, is a link that silently does nothing.
            roi_name = str(d.get("roi") or "")
            if roi_name:
                rp = port_of.get(roi_name)
                if rp is None:
                    errs.append(f"{where}: drives.roi names no port "
                                f"({roi_name!r})")
                elif rp.kind != "entry":
                    errs.append(f"{where}: drives.roi must be an entry, so the "
                                f"box can be read and typed; {roi_name!r} is "
                                f"a {rp.kind}")
                if snk is not None and snk.kind in _gpo.SEQUENCE_SINKS \
                        and not bool((snk.props or {}).get("roi")):
                    errs.append(f"{where}: drives.roi is set but {snk_name!r} "
                                f"has roi off — set its roi prop to true so a "
                                f"box can be drawn")
            # Optional port that shows the displayed file's name — what
            # "this frame" means to a button that marks or predicts it.
            cur_name = str(d.get("current") or "")
            if cur_name:
                cp = port_of.get(cur_name)
                if cp is None:
                    errs.append(f"{where}: drives.current names no port "
                                f"({cur_name!r})")
                elif cp.kind not in ("label", "entry"):
                    errs.append(f"{where}: drives.current must be a label or "
                                f"entry, to show the file name; "
                                f"{cur_name!r} is a {cp.kind}")
            names = [n for n in (src_name, snk_name, roi_name, cur_name,
                                 w.port.name if w.port else None) if n]
            if len(set(names)) != len(names):
                errs.append(f"{where}: a sequence link needs a DIFFERENT "
                            f"port for each role; got {names}")
            if snk_name:
                prior = driven_sinks.setdefault(snk_name, w.name)
                if prior != w.name:
                    errs.append(f"{where}: {snk_name!r} is already driven by "
                                f"{prior!r} — last writer would win invisibly")

        # -- port validation --------------------------------------------
        cap = _gpo.caps(w.kind)
        if w.port is None:
            # An explicit port on a kind with no caps must not just quietly
            # vanish — build_ports drops it, but the AUTHOR asked for it.
            if not cap.types and dict(getattr(spec.by_shape(w.shape_id), "props",
                                              None) or {}).get("port"):
                errs.append(f"{where}: {w.kind} cannot have a port — "
                            f"{_gpo.note(w.kind)}")
            continue
        p = w.port
        ok, why = _gpo.validate_port_name(
            p.name, taken=[n for n in seen_ports if n != p.name])
        if not ok:
            errs.append(f"{where}: port name {why}")
        # A radio group shows this port once per member; skip the seen check
        # for the additional members so we do not report a self-duplicate.
        prior = seen_ports.get(p.name)
        if prior is not None and prior != p.shape_ids[0]:
            errs.append(f"{where}: duplicate port name {p.name!r}")
        else:
            seen_ports.setdefault(p.name, p.shape_ids[0] if p.shape_ids else w.shape_id)
        if cap.types and p.type not in cap.types:
            errs.append(f"{where}: port type {p.type!r} not in "
                        f"{list(cap.types)}")
        if cap.dirs and p.direction not in cap.dirs:
            errs.append(f"{where}: port direction {p.direction!r} not in "
                        f"{list(cap.dirs)}")
        # Radio group: default must be one of the actual member values, else
        # every button silently deselects on init.
        if w.kind == "radiobutton" and p.default is not None:
            if p.default not in p.choices:
                errs.append(f"{where}: radio default {p.default!r} is not "
                            f"one of {list(p.choices)}")
        # Radio group: duplicate value= within one group means var.get() is
        # ambiguous — the widget silently reports whichever button was clicked
        # LAST wrote the shared var.
        if w.kind == "radiobutton" and len(p.choices) != len(set(p.choices)):
            dups = sorted({v for v in p.choices if p.choices.count(v) > 1})
            errs.append(f"{where}: duplicate radio value(s) {dups} in group "
                        f"{p.group!r} — var.get() would be ambiguous")

    # ---- structural traps that emit fine and render as NOTHING ----------
    #
    # Everything above checks that a widget is well-formed. These check that
    # the LAYOUT is one a user could actually see. Both were found by asking
    # a model to design a wireframe: it produced a spec that passed every
    # check above, emitted parseable code, passed the policy gate, and came
    # up as a completely blank window.

    # Two root-level siblings covering the same area. Whichever is emitted
    # last is place()d on top and hides the other. A full-canvas Frame plus a
    # full-canvas Notebook is the exact shape that failed.
    roots = [w for w in spec.widgets if not w.parent]
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            if not (a.manager == b.manager == "place"):
                continue
            same = (abs(a.relx - b.relx) < 1e-6 and abs(a.rely - b.rely) < 1e-6
                    and abs(a.relwidth - b.relwidth) < 1e-6
                    and abs(a.relheight - b.relheight) < 1e-6)
            if same and a.relwidth > 0 and a.relheight > 0:
                errs.append(
                    f"{a.name!r} and {b.name!r} are both placed over the same "
                    f"area with nothing between them — the second one drawn "
                    f"hides the first. Nest one inside the other, or give "
                    f"them different regions of the canvas.")

    # A Notebook with no children shows an empty tab strip; that is a
    # legitimate placeholder. But a Notebook is a container, so anything the
    # user drew inside it becomes a TAB — and if the tabs prop is short, the
    # tab titles fall back to widget labels. Warn rather than error: the
    # emitter now handles it, and a missing title is cosmetic.
    for w in spec.widgets:
        if w.kind != "notebook" or not w.children:
            continue
        tabs = list((w.props or {}).get("tabs") or [])
        if len(tabs) < len(w.children):
            spec.warnings.append(
                f"{w.name}: {len(w.children)} child widget(s) but only "
                f"{len(tabs)} tab title(s) — the rest are named from their "
                f"labels")

    # A window with no elastic axis cannot be resized — gui_layout guarantees
    # against it, so reaching here means the tree was built some other way.
    if spec.widgets and spec.root_col_weights and not any(spec.root_col_weights):
        errs.append("the root window has no weighted column; it will not resize")
    if spec.widgets and spec.root_row_weights and not any(spec.root_row_weights):
        errs.append("the root window has no weighted row; it will not resize")

    return (not errs), errs
