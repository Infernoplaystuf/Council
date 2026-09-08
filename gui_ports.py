"""
gui_ports.py — the typed binding surface, decided per widget kind.

PURE. Stdlib only (dataclasses, keyword, re). Duck-types on .id / .kind / .props
/ .port so it never imports Shape; a fresh Python identifier is not the same
kind of dependency as a class definition, and keeping it structural is what
lets gui_shapes / gui_layout / gui_spec depend on this without a cycle.

WHY THIS MODULE EXISTS
----------------------
Wireframes need to say what each widget PRODUCES for the surrounding code, not
just what it looks like. An Entry is read BY the app; a Label is written BY the
app; a Button is neither — it is an EVENT. The user's own instinct — "buttons
can be like a boolean flicker" — is right that every box should have a named
binding and wrong that the mechanism is a mutable bool: a bool is False at
every moment you could observe it, cannot count two clicks, and TCL raises
`unknown option "-variable"` if you try to attach one to a ttk.Button anyway.
So a button is a first-class EVENT port (`.on_fire`, `.fire`, no `.get`/`.set`)
— and every port, including buttons, gets `.enable(bool)`, which is the honest
boolean a button has: not "was I pressed" but "may I be pressed". If the user
really wants a bool, that widget is a Checkbutton — one palette row up.

WHY THE CATALOGUE IS PINNED
---------------------------
PORT_CAPS is asserted to cover the same key set as gui_shapes.PALETTE. Same
discipline as gui_colors.COLOUR_CAPS: a new widget kind cannot be added without
DECIDING its binding answer. Silence would default to "no port", which looks
like a decision and is not one.

WHY PORT NAMES CARRY NO KIND PREFIX
-----------------------------------
Widget names are `btn_start_scan`. Port names are `start_scan`. Baking the kind
into the identifier that hand-written code depends on turns a routine widget
swap (Button -> Toolbar with one action) into a rename across app.py; the port
name should read as domain vocabulary, and the KIND is already carried by
Ports's declaration site.
"""
from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

# ============================================================
# Grammar
# ============================================================
#
# A closed grammar on purpose. The reserved set is intentionally tiny — only the
# two PUBLIC members Ports itself exposes ("read"/"apply") — because every other
# helper on _Port starts with an ASCII letter and is a legal port name; keeping
# the set small means most domain words are usable verbatim.

RESERVED_PORT_NAMES: FrozenSet[str] = frozenset({"read", "apply"})

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """A label -> an identifier fragment. Same rule as gui_spec.slug."""
    s = _SLUG_RE.sub("_", str(text or "").strip().lower()).strip("_")
    return s


def validate_port_name(name: str, taken: Iterable[str] = ()) -> Tuple[bool, str]:
    """Return (ok, reason) for ``name``.

    A port name is a Python attribute the generated `class Ports` will expose,
    and hand-written app.py will reference it. All four rejection paths matter:

      * not an identifier -> the generated `class Ports` fails to parse
      * a Python keyword  -> same
      * leading underscore -> Ports uses ``__slots__`` and _-prefixed slots
        collide with the private machinery; refusing them keeps the public
        surface unambiguous
      * reserved          -> ``read``/``apply`` are the public helpers on Ports
      * duplicate         -> two ports on one Ports means one silently wins
    """
    s = str(name or "").strip()
    if not s:
        return False, "empty"
    if not _IDENT_RE.match(s):
        return False, f"not a Python identifier: {name!r}"
    if keyword.iskeyword(s):
        return False, f"{name!r} is a Python keyword"
    if s.startswith("_"):
        return False, f"{name!r} starts with '_' (reserved for internals)"
    if s in RESERVED_PORT_NAMES:
        return False, f"{name!r} is reserved (Ports.{s})"
    if s in set(taken):
        return False, f"{name!r} is already in use"
    return True, ""


def default_port_name(kind: str, label: str, group: str = "",
                      taken: Iterable[str] = ()) -> str:
    """A deterministic default derived from the shape.

    For a radio group the port name is the GROUP's slug — one port shared by
    every member, mirroring the single shared tk.Variable. For everything else
    the label is the source; the empty label falls back to the kind. On a
    collision, ``_2`` / ``_3`` are appended in the sort order gui_spec.build
    uses at build time, so re-running the derivation on the same shapes gives
    the same names.
    """
    base = slug(group if kind == "radiobutton" and group else label)
    if not base:
        base = slug(kind) or "value"
    if base[0].isdigit():
        base = f"n{base}"
    if keyword.iskeyword(base):
        base = f"{base}_"
    if base.startswith("_"):
        base = base.lstrip("_") or "value"
    if base in RESERVED_PORT_NAMES:
        base = f"{base}_value"
    used = set(taken)
    if base not in used:
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"


# ============================================================
# Per-kind capability
# ============================================================
#
# ``binder`` tells gui_emit which runtime class to wire up (see PORTS_RUNTIME in
# gui_emit). ``tk_option`` names the Tk widget option the variable attaches to
# — empty means "the widget already owns its own var and we adopt it rather
# than re-attach", used by the composites the emitter builds (FilePicker,
# Scrubber, LogPane, StatusBar, ImageCanvas, ChartPanel).
#
# ``deep=True`` means .enable() has to walk into a composite's children;
# state(["disabled"]) on a ttk.Frame does not reach an Entry inside it, so a
# FilePicker with .enable(False) that leaves its inner field editable is a
# silent lie. Measured, not assumed.

@dataclass(frozen=True)
class PortCap:
    types: Tuple[str, ...] = ()          # first is the default; () = no port
    dirs: Tuple[str, ...] = ()           # first is the default
    binder: str = ""                     # "var" | "text" | "list" | "table"
                                         # | "proxy" | "event" | "tab"
    var_class: str = ""                  # "StringVar" | "BooleanVar"
                                         # | "DoubleVar" | ""
    tk_option: str = ""                  # "textvariable" | "variable" | ""
    writer: str = ""                     # composite method for the writer,
                                         # eg "set_image" / "append"
    classic: bool = False                # emit tk.* instead of ttk.* when
                                         # coloured (from gui_colors)
    deep: bool = False                   # .enable() walks children


_NONE = PortCap()  # no port at all


# WHO MAY PARTICIPATE IN A SEQUENCE LINK (Shape.drives).
#
# Deliberately three frozensets and NOT a fourth column on PortCap: a table
# with one row in each has not earned being a table, and a dataclass field
# defaulting to () would let a newly added kind inherit "cannot drive" in
# silence — the unexamined default this module exists to avoid. Adding a kind
# here is a decision somebody has to type.
SEQUENCE_DRIVERS: FrozenSet[str] = frozenset({"scrubber"})
SEQUENCE_SOURCES: FrozenSet[str] = frozenset({"file_picker"})
SEQUENCE_SINKS: FrozenSet[str] = frozenset({"image_canvas"})


PORT_CAPS: Dict[str, PortCap] = {
    # ---- containers / decoration — no port ----
    "frame": _NONE,
    "labelframe": _NONE,
    "panedwindow": _NONE,
    "freeform": _NONE,
    "separator": _NONE,
    "generic": _NONE,
    # ---- display (app writes) ----
    "label": PortCap(types=("str",), dirs=("o",), binder="var",
                     var_class="StringVar", tk_option="textvariable",
                     classic=True),
    # ---- input (app reads) ----
    "entry": PortCap(types=("str", "int", "float", "path"),
                     dirs=("i", "io"), binder="var",
                     var_class="StringVar", tk_option="textvariable",
                     classic=True),
    "checkbutton": PortCap(types=("bool",), dirs=("i", "io"), binder="var",
                           var_class="BooleanVar", tk_option="variable",
                           classic=True),
    "radiobutton": PortCap(types=("str", "int"), dirs=("i", "io"),
                           binder="var", var_class="StringVar",
                           tk_option="",  # attached member-by-member in emit
                           classic=True),
    "combobox": PortCap(types=("str",), dirs=("i", "io"), binder="var",
                        var_class="StringVar", tk_option="textvariable"),
    "spinbox": PortCap(types=("int", "float", "str"), dirs=("i", "io"),
                       binder="var", var_class="StringVar",
                       tk_option="textvariable", classic=True),
    "scale": PortCap(types=("float", "int"), dirs=("i", "io"), binder="var",
                     var_class="DoubleVar", tk_option="variable",
                     classic=True),
    # ---- data (no Tk var; accessor pair) ----
    "text": PortCap(types=("str",), dirs=("io",), binder="text",
                    classic=True),
    "listbox": PortCap(types=("str",), dirs=("i", "io"), binder="list",
                       classic=True),
    "treeview": PortCap(types=("rows",), dirs=("io",), binder="table"),
    # ---- app writes (progress / progress bar) ----
    "progressbar": PortCap(types=("float",), dirs=("o",), binder="var",
                           var_class="DoubleVar", tk_option="variable"),
    # ---- composites (own their var; adopted, not re-attached) ----
    "file_picker": PortCap(types=("path",), dirs=("i", "io"), binder="var",
                           var_class="StringVar", tk_option="",
                           deep=True),
    "scrubber": PortCap(types=("int",), dirs=("i", "io"), binder="var",
                        var_class="IntVar", tk_option=""),
    "image_canvas": PortCap(types=("image",), dirs=("o",), binder="proxy",
                            writer="set_image"),
    "chart_panel": PortCap(types=("figure",), dirs=("o",), binder="proxy",
                           writer="figure_for_drawing"),
    "log_pane": PortCap(types=("str",), dirs=("o",), binder="proxy",
                        writer="append"),
    "status_bar": PortCap(types=("str",), dirs=("o",), binder="proxy",
                          writer="set"),
    # ---- events (buttons and menu-like things) ----
    "button": PortCap(types=("event",), dirs=("e",), binder="event"),
    "toolbar": PortCap(types=("event",), dirs=("e",), binder="event"),
    "menubar": PortCap(types=("event",), dirs=("e",), binder="event"),
    # ---- notebook — the current tab is a value ----
    "notebook": PortCap(types=("int",), dirs=("io",), binder="tab"),
}


PORT_NOTE: Dict[str, str] = {
    "frame": "A frame is a container; children are its output.",
    "labelframe": "The caption is a static prop, not a binding.",
    "panedwindow": "The sash is presentation, not a value.",
    "freeform": "Free-placed children are the output; there is no widget value.",
    "separator": "A separator is decorative.",
    "generic": "Untyped — no widget yet, so nothing to bind.",
}


def caps(kind: str) -> PortCap:
    return PORT_CAPS.get(kind, _NONE)


def has_port(kind: str) -> bool:
    return bool(caps(kind).types)


def note(kind: str) -> str:
    return PORT_NOTE.get(kind, "")


# ============================================================
# The IR — one PortSpec per bindable widget, produced by build_ports
# ============================================================

@dataclass
class PortSpec:
    """One typed binding: a Ports member the generator will emit."""
    name: str
    kind: str                            # gui_shapes kind of the source widget
    type: str                            # one of caps(kind).types
    direction: str                       # one of caps(kind).dirs
    binder: str                          # from caps().binder
    var_class: str = ""
    tk_option: str = ""
    writer: str = ""
    default: Any = None
    choices: Tuple[str, ...] = ()        # radio group values, in emit order
    group: str = ""                      # radio group id, if any
    deep: bool = False
    # Which shape(s) this port is materialised from. For a radio group this is
    # every member; for everything else exactly one.
    shape_ids: Tuple[str, ...] = ()


def _label_of(props: Dict[str, Any], shape_label: str) -> str:
    return str(shape_label or props.get("text") or "")


def _radio_group_key(parent_id: Optional[str], group: str) -> str:
    """Two radios share a variable IFF they share a container AND a group.

    The parent id is part of the key on purpose: two "size" radio groups in two
    different panels are two independent groups, not one merged group whose
    clicks contradict each other."""
    return f"group:{parent_id or '~'}/{group or 'default'}"


def _bindable(order: Sequence[Any],
              parents: Dict[str, str]) -> List[Tuple[Any, str, PortCap,
                                                     Dict[str, Any],
                                                     Dict[str, Any],
                                                     str, str, bool]]:
    """Every shape that participates in a port, in build order.

    Yields ``(shape, kind, cap, props, overrides, key, group, opens_port)``.
    ``opens_port`` is False only for a radio that EXTENDS a group another
    radio already opened.

    Both passes in build_ports walk this list, so the skip rules — an
    unbindable kind, port.off, the caption rule — are stated ONCE. When the
    reservation pass and the naming pass disagree about which shapes get
    ports, a name gets reserved for a shape that never claims it and the next
    shape is pushed to a `_2` suffix for no visible reason.
    """
    seen_groups: set = set()
    out = []
    for s in order:
        kind = str(getattr(s, "kind", ""))
        cap = caps(kind)
        if not cap.types:
            # Unbindable kind. An explicit port on one is caught by
            # gui_spec.validate; here we simply skip.
            continue

        props = dict(getattr(s, "props", None) or {})
        overrides = dict(getattr(s, "port", None) or {})
        if overrides.get("off"):
            continue

        # THE CAPTION RULE. A Label's port binds `textvariable`, and in Tk a
        # textvariable OVERRIDES text= — so attaching an empty StringVar to a
        # label blanks its caption. A label that already says something is a
        # caption, not a readout, and gets no port unless the user asks for
        # one explicitly.
        #
        # Found by building a GUI whose every label came out invisible: the
        # generated source said text="Exposure (ms)" and the running widget
        # reported text=''. Nothing warned, because the code was correct in
        # isolation and only wrong once the var was attached.
        if (kind == "label" and not overrides
                and str(getattr(s, "label", "") or props.get("text") or "")):
            continue

        if kind == "radiobutton":
            group = str(props.get("group") or "")
            key = _radio_group_key(parents.get(getattr(s, "id", "")), group)
            opens = key not in seen_groups
            seen_groups.add(key)
            out.append((s, kind, cap, props, overrides, key, group, opens))
            continue

        out.append((s, kind, cap, props, overrides,
                    str(getattr(s, "id", "")), "", True))
    return out


def build_ports(shapes: Sequence[Any],
                *,
                parents: Optional[Dict[str, str]] = None,
                registry: Optional[Dict[str, str]] = None,
                ) -> List[PortSpec]:
    """Derive one PortSpec per bindable widget, in deterministic order.

    ``shapes`` are the raw Shape objects (duck-typed on id/kind/label/props/port).
    ``parents`` is the containment map (child_id -> parent_id) — needed only to
    key radio groups so two "size" groups in two frames stay separate.
    ``registry`` is the manifest's port_names (shape id / group key -> name);
    registered names WIN over derivation so retyping a label does not rename a
    port that hand-written code already references.

    ASKED-FOR NAMES ARE RESERVED BEFORE ANY NAME IS DERIVED. Names used to be
    assigned in one pass in (z, id) order, which let a shape that asked for
    nothing consume a name another shape had explicitly declared. Measured:
    two image_canvas shapes, the lower-z one bare and the higher-z one
    declaring port name "image_canvas", produced

        [('image_canvas', ('zzz',)), ('image_canvas_2', ('aaa',))]

    — the shape that asked by name lost it to the shape that did not, purely
    because it was drawn later. That silently re-points every reference made
    BY NAME: a script link's `inputs`, a script link's `outputs`, and a
    scrubber's `drives`. Nothing warned, because both names were valid and
    both ports existed.
    """
    parents = dict(parents or {})
    reg = dict(registry or {})
    order = sorted(shapes, key=lambda s: (getattr(s, "z", 0),
                                          str(getattr(s, "id", ""))))
    cands = _bindable(order, parents)
    taken: List[str] = []
    out: List[PortSpec] = []
    radio_ports: Dict[str, PortSpec] = {}

    # ---- pass 1: reserve the names somebody asked for, in order ----
    # Sequential validation, so two shapes both declaring "foo" still resolve
    # first-come-first-served rather than colliding.
    claimed: Dict[str, str] = {}
    for _s, _k, _c, _p, overrides, key, _g, opens in cands:
        if not opens:
            continue
        wanted = overrides.get("name") or reg.get(key)
        if wanted and validate_port_name(wanted, taken)[0]:
            taken.append(wanted)
            claimed[key] = wanted

    # ---- pass 2: build, deriving only the names nobody claimed ----
    for s, kind, cap, props, port_overrides, key, group, opens in cands:

        # ---- radio: one port per group; extend choices ----
        if kind == "radiobutton":
            value = str(props.get("value")
                        or slug(_label_of(props, getattr(s, "label", ""))))
            if not opens:
                # extend the group. choices preserves DUPLICATES on purpose:
                # two radios with the same value= is a spec bug because
                # var.get() cannot tell them apart, so gui_spec.validate has
                # to see it. Emit iterates shape_ids, not choices, so the
                # duplication does not double-configure the widget.
                existing = radio_ports[key]
                existing.choices = tuple(list(existing.choices) + [value])
                existing.shape_ids = tuple(list(existing.shape_ids) + [
                    str(getattr(s, "id", ""))])
                continue
            name = claimed.get(key)
            if name is None:
                name = default_port_name(
                    kind, _label_of(props, getattr(s, "label", "")),
                    group=group, taken=taken)
                taken.append(name)
            spec = PortSpec(
                name=name, kind=kind,
                type=str(port_overrides.get("type") or cap.types[0]),
                direction=str(port_overrides.get("dir") or cap.dirs[0]),
                binder=cap.binder, var_class=cap.var_class,
                tk_option=cap.tk_option,
                default=port_overrides.get("default", value),
                choices=(value,), group=group,
                shape_ids=(str(getattr(s, "id", "")),),
            )
            radio_ports[key] = spec
            out.append(spec)
            continue

        # ---- everything else ----
        name = claimed.get(key)
        if name is None:
            name = default_port_name(
                kind, _label_of(props, getattr(s, "label", "")),
                taken=taken)
            taken.append(name)
        spec = PortSpec(
            name=name, kind=kind,
            type=str(port_overrides.get("type") or cap.types[0]),
            direction=str(port_overrides.get("dir") or cap.dirs[0]),
            binder=cap.binder, var_class=cap.var_class,
            tk_option=cap.tk_option, writer=cap.writer,
            default=port_overrides.get("default"),
            deep=cap.deep,
            shape_ids=(key,),
        )
        out.append(spec)

    return out


def registry_for(ports: Sequence[PortSpec],
                 parents: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The port_names registry entry to persist in the manifest.

    Keyed by shape id, except for a radio group, whose key is the group id so
    a member added later joins the existing group rather than starting a new
    port. This is the same shape as gui_projects.Manifest.widget_names."""
    parents = dict(parents or {})
    out: Dict[str, str] = {}
    for p in ports:
        if p.kind == "radiobutton" and p.shape_ids:
            key = _radio_group_key(parents.get(p.shape_ids[0]), p.group)
            out[key] = p.name
        else:
            for sid in p.shape_ids:
                out[sid] = p.name
    return out
