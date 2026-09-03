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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    widgets: List[WidgetSpec] = field(default_factory=list)
    root_children: List[str] = field(default_factory=list)
    root_row_weights: List[int] = field(default_factory=list)
    root_col_weights: List[int] = field(default_factory=list)
    root_row_minsizes: List[int] = field(default_factory=list)
    root_col_minsizes: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

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
          root_bg: str = "", root_fg: str = "") -> Spec:
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
                root_bg=root_bg, root_fg=root_fg)
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


def validate(spec: Spec) -> Tuple[bool, List[str]]:
    """(ok, errors). EVERY fault, not the first.

    A generator that stopped at the first problem would make the user fix one
    thing, regenerate, and discover the next — turning a five-minute correction
    into five rounds."""
    errs: List[str] = []
    seen: Dict[str, str] = {}
    seen_ports: Dict[str, str] = {}   # port name -> shape id

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

    # A window with no elastic axis cannot be resized — gui_layout guarantees
    # against it, so reaching here means the tree was built some other way.
    if spec.widgets and spec.root_col_weights and not any(spec.root_col_weights):
        errs.append("the root window has no weighted column; it will not resize")
    if spec.widgets and spec.root_row_weights and not any(spec.root_row_weights):
        errs.append("the root window has no weighted row; it will not resize")

    return (not errs), errs
