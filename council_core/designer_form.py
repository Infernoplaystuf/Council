"""
council_core.designer_form — which fields the property panel shows, and why.

WHERE THE DESIGNER'S TOOLKIT COST ACTUALLY IS
The measurement that made phase 8 look expensive said `gui_canvas.py` has 119
toolkit lines. 76 of them are HERE — the property panel — and the 647-line
drawing engine scores one. The canvas hand-draws; the inspector is an ordinary
form, and an ordinary form is the part that has to be rebuilt per toolkit.

So what moves is everything the form DECIDES: which fields appear for a given
selection, what type each one is, and what a raw string becomes. What stays in
the view is the widget per row.

TWO THINGS A MULTI-SELECTION CHANGES, BOTH EASY TO GET WRONG
The Tk panel hides the binding and colour blocks when more than one shape is
selected, and shows the per-kind property schema only for a single one —
because "port" and "background" are answers about ONE widget and a schema
belongs to ONE kind. `fields_for` encodes that rather than leaving each front
end to rediscover it.

And every field it returns carries the value of the FIRST shape. That is the
Tk behaviour and it is why `Scene.apply_props` must write only what the user
changed: applying every field of the panel to every selected shape would
overwrite the rest with the first one's label.

A BAD NUMBER BECOMES 0 RATHER THAN RAISING
Carried over deliberately. An inspector that throws on a typo loses every
other edit in the same Apply, and the user has no idea which field did it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from gui_shapes import PALETTE, RESIZE_MODES, Shape, is_container

#: Field kinds a front end must know how to render. Deliberately small: the
#: panel has always been text, number, choice and switch, plus the two blocks
#: that are their own shape.
TEXT, NUMBER, CHOICE, SWITCH, LIST = "text", "number", "choice", "switch", "list"

#: A colour row — a palette dropdown and a hex entry, resolved by
#: `resolve_colour`. Its own kind because it is two controls, not one.
COLOUR = "colour"

#: Read-only explanatory text. The Tk panel has three of these and builds each
#: by hand: "this kind cannot be coloured", "this kind has no runtime value",
#: and the value-type line shown when only one type is legal. A front end
#: renders it as a grey label, never as an input.
NOTE = "note_text"


@dataclass
class Field:
    """One row of the property panel."""
    key: str
    label: str
    kind: str
    value: Any = ""
    choices: List[str] = field(default_factory=list)
    #: True when this writes into `shape.props` rather than onto the shape.
    prop: bool = False
    #: A heading rather than an input — the schema block's title.
    heading: bool = False
    #: True when this writes into `shape.port` rather than onto the shape.
    port: bool = False
    #: The DERIVED value this field falls back to. A submitted value equal to
    #: it is NOT persisted — that is how a port name stays derived rather than
    #: being frozen the first time the panel is applied.
    placeholder: str = ""
    #: Display value -> stored value, for a field shown in words and stored
    #: as a code. Direction is the only one: the panel says "user -> app",
    #: the .gspec stores "i".
    encode: Dict[str, str] = field(default_factory=dict)


def cast(kind: str, raw: Any) -> Any:
    """A raw control value as the field's declared type.

    A bad number is dropped to 0 rather than raising: an inspector that throws
    on a typo loses every other edit in the same Apply.
    """
    if kind == SWITCH:
        return bool(raw)
    if kind == NUMBER:
        try:
            return int(str(raw).strip() or 0)
        except (TypeError, ValueError):
            return 0
    if kind == LIST:
        return [part.strip() for part in str(raw or "").split(",")
                if part.strip()]
    return str(raw if raw is not None else "")


def fields_for(shapes: Sequence[Shape]) -> List[Field]:
    """Every row the panel shows for this selection, in order.

    Empty for an empty selection — the caller shows its own "nothing selected"
    state, because what that should say differs between a docked panel and a
    dialog.
    """
    shapes = list(shapes)
    if not shapes:
        return []

    first = shapes[0]
    multi = len(shapes) > 1
    rows: List[Field] = [
        Field("label", "Label", TEXT, first.label),
        Field("note", "Note (hint)", TEXT, first.note),
        Field("resize", "Resize", CHOICE, first.resize, list(RESIZE_MODES)),
        Field("min_w", "Min width", NUMBER, first.min_w),
        Field("min_h", "Min height", NUMBER, first.min_h),
    ]
    if is_container(first.kind):
        rows.append(Field("freeform", "Freeform (place)", SWITCH,
                          first.freeform))

    # The per-kind schema belongs to ONE kind, so a mixed selection has no
    # business showing it.
    if not multi:
        rows.extend(schema_fields(first))
    return rows


def schema_fields(shape: Shape) -> List[Field]:
    """The palette's own declared properties for this widget kind."""
    schema = (PALETTE.get(shape.kind, {}) or {}).get("prop_schema") or {}
    if not schema:
        return []
    rows: List[Field] = [Field("", f"{shape.kind} properties", TEXT,
                               heading=True)]
    for name, spec in schema.items():
        current = shape.props.get(name, spec.get("default"))
        declared = str(spec.get("type") or "")
        if spec.get("choices"):
            rows.append(Field(name, name, CHOICE, current,
                              [str(c) for c in spec["choices"]], prop=True))
        elif declared == "bool":
            rows.append(Field(name, name, SWITCH, bool(current), prop=True))
        elif declared.startswith("list"):
            rows.append(Field(name, name, LIST,
                              ", ".join(map(str, current or [])), prop=True))
        elif declared == "int":
            rows.append(Field(name, name, NUMBER,
                              current if current is not None else 0, prop=True))
        else:
            rows.append(Field(name, name, TEXT,
                              current if current is not None else "",
                              prop=True))
    return rows


def shows_single_shape_blocks(shapes: Sequence[Shape]) -> bool:
    """Whether the binding and colour blocks belong on screen.

    "Which script does this run" and "what colour is it" are answers about ONE
    widget. The Tk panel hides both for a multi-selection and that is right.
    """
    return len(list(shapes)) == 1


def collect(fields: Sequence[Field], values: Dict[str, Any]) -> Dict[str, Any]:
    """Raw control values as the change-set `Scene.apply_props` takes.

    ONLY the keys present in ``values`` are returned. That is what makes
    editing a multi-selection possible: a caller that passes every field would
    overwrite every shape's label with the first one's, which is exactly what
    applying the whole panel does.
    """
    changes: Dict[str, Any] = {}
    props: Dict[str, Any] = {}
    for row in fields:
        # A heading and a NOTE are text, not controls, and both carry an empty
        # key — collecting one writes the panel's explanatory sentence to the
        # attribute named "". A port row belongs under "port", which is
        # `collect_all`'s job; taking it here would write it in BOTH places.
        if row.heading or row.kind == NOTE or not row.key or row.port:
            continue
        if row.key not in values:
            continue
        cast_value = cast(row.kind, values[row.key])
        if row.prop:
            props[row.key] = cast_value
        else:
            changes[row.key] = cast_value
    if props:
        changes["props"] = props
    return changes


# ============================================================
# Colour
# ============================================================
# The Tk colour row has a dropdown of theme names AND a free-text hex entry,
# and its docstring says: a raw hex typed in the entry WINS over the dropdown,
# an empty entry means the dropdown wins, and both empty reverts to inherit.
#
# THE CODE DOES NOT DO THAT. `_colour_field` registers only the hex variable
# into the target dict, so the dropdown's value is read only insofar as
# choosing from it writes into the hex entry — and any path that leaves the
# entry untouched loses the choice silently.
#
# The rule is written here, once, as the documented one. Both front ends call
# it, so the behaviour and the sentence describing it cannot drift apart again.

#: What "" means on a shape's bg/fg: inherit from the ancestor or the OS.
INHERIT = ""


def resolve_colour(typed_hex: str, chosen_name: str,
                   palette: Optional[Dict[str, str]] = None) -> str:
    """The colour a row means, from its two controls.

    A typed hex wins, because typing it is the more specific act. An empty
    entry falls back to the dropdown. Both empty means inherit.
    """
    typed = (typed_hex or "").strip()
    if typed:
        return typed
    chosen = (chosen_name or "").strip()
    if not chosen:
        return INHERIT
    if palette and chosen in palette:
        return palette[chosen]
    return chosen


def is_inherited(value: str) -> bool:
    return not (value or "").strip()


# ============================================================
# The binding block
# ============================================================
# What a widget PRODUCES for the surrounding code: a name, a direction, a value
# type and a default. Every part of it is a decision — which rows exist at all,
# what the direction is called on screen, and whether the name gets persisted.

#: The direction, in words. The .gspec stores the terse code; a panel that
#: showed "io" would be asking the user to learn an encoding.
DIR_LABEL = {"i": "user → app", "o": "app → user",
             "io": "both ways", "e": "event"}
DIR_CODE = {label: code for code, label in DIR_LABEL.items()}


def port_fields(shape: Shape) -> List[Field]:
    """The Binding rows for one shape.

    A kind with no runtime value gets the REASON rather than a dead control —
    the same discipline the colour block follows. That is why this returns a
    NOTE field instead of an empty list: "no rows" and "rows withheld, here is
    why" look identical to a caller otherwise.
    """
    import gui_ports

    cap = gui_ports.caps(shape.kind)
    if not cap.types:
        return [Field("", gui_ports.note(shape.kind)
                      or "This kind has no runtime value.", NOTE)]

    port = dict(shape.port or {})
    derived = gui_ports.default_port_name(
        shape.kind, shape.label, group=str(shape.props.get("group") or ""))
    rows = [
        # The placeholder is what keeps the name DERIVED: submit it unchanged
        # and nothing is written, so renaming the label still renames the port.
        Field("name", "Name (self.ports.<name>)", TEXT,
              port.get("name") or derived, port=True, placeholder=derived),
        Field("dir", "Direction", CHOICE,
              DIR_LABEL.get(port.get("dir") or cap.dirs[0],
                            port.get("dir") or cap.dirs[0]),
              [DIR_LABEL[d] for d in cap.dirs if d in DIR_LABEL],
              port=True, encode=dict(DIR_CODE)),
    ]
    if len(cap.types) > 1:
        rows.append(Field("type", "Value type", CHOICE,
                          port.get("type") or cap.types[0],
                          list(cap.types), port=True))
    else:
        # One legal type. Shown, not offered — a dropdown with a single entry
        # is a control that pretends to do something.
        rows.append(Field("", f"Value type: {cap.types[0]}", NOTE))
    if cap.binder != "event":
        rows.append(Field("default", "Default value", TEXT,
                          "" if port.get("default") is None
                          else str(port.get("default")), port=True))
    return rows


# ============================================================
# The colour block
# ============================================================

def colour_fields(shape: Shape) -> List[Field]:
    """The Colour rows for one shape, or the reason there are none.

    Which channels a kind can honour is gui_colors' answer, not the panel's —
    a Notebook or a Progressbar cannot be coloured through ttk at all, and
    offering the control anyway produces an edit that silently does nothing.
    """
    import gui_colors

    able = gui_colors.caps(shape.kind)
    if not able:
        return [Field("", gui_colors.note(shape.kind)
                      or "This kind cannot be coloured.", NOTE)]
    rows = []
    if "bg" in able:
        rows.append(Field("bg", "Background", COLOUR,
                          _current_hex(getattr(shape, "bg", "")),
                          colour_options()))
    if "fg" in able:
        rows.append(Field("fg", "Text colour", COLOUR,
                          _current_hex(getattr(shape, "fg", "")),
                          colour_options()))
    return rows


def colour_options() -> List[str]:
    """Every palette swatch as a pickable label, "(inherit)" first.

    Inherit leads because it is the REVERT, and a colour picker with no way
    back to "unset" makes the first click permanent.
    """
    import gui_colors

    options = ["(inherit)"]
    for name, colours in gui_colors.PALETTES.items():
        options.extend(f"{name}: {hex_value}" for hex_value in colours)
    return options


def option_hex(option: str) -> str:
    """The hex behind a swatch label. "(inherit)" and junk give ""."""
    if not option or option == "(inherit)" or ":" not in option:
        return ""
    return option.split(":", 1)[1].strip()


def hex_option(value: str) -> str:
    """The swatch label showing this colour, or "" if no palette has it."""
    wanted = _current_hex(value)
    if not wanted:
        return ""
    for option in colour_options()[1:]:
        if option_hex(option).lower() == wanted.lower():
            return option
    return ""


def _current_hex(value: Any) -> str:
    """A stored colour as a hex string, or "" if it is not one.

    Never raises: a .gspec edited by hand can hold anything, and an inspector
    that throws while OPENING leaves the user no way to fix the file from the
    app that wrote it.
    """
    import gui_colors

    try:
        return gui_colors.normalise(value) if value else ""
    except (ValueError, TypeError):
        return ""


# ============================================================
# The window panel
# ============================================================
# The controls that belong to no shape. The Tk panel shows these when NOTHING
# is selected, which is the only place the window's own colour can be edited —
# without it the whole colour story lands for widgets and not for the window
# they sit on.

def window_fields(window: Any, requires: Sequence[str] = ()) -> List[Field]:
    """Title, minimum size, colours, and the packages the app imports."""
    return [
        Field("title", "Title", TEXT, getattr(window, "title", "")),
        Field("min_w", "Min width", NUMBER, getattr(window, "min_w", 0)),
        Field("min_h", "Min height", NUMBER, getattr(window, "min_h", 0)),
        Field("bg", "Background", COLOUR,
              _current_hex(getattr(window, "bg", "")), colour_options()),
        Field("fg", "Text colour", COLOUR,
              _current_hex(getattr(window, "fg", "")), colour_options()),
        # What the app imports beyond the stdlib — a camera SDK, PIL, numpy.
        # Checked in the chosen Python before Run, and the only way a package
        # reaches this project's policy allowlist.
        Field("requires", "Packages it needs (import names, comma-separated)",
              TEXT, ", ".join(requires)),
    ]


# ============================================================
# Collecting, with the blocks
# ============================================================

def collect_all(fields: Sequence[Field],
                values: Dict[str, Any]) -> Dict[str, Any]:
    """The change-set for a panel that includes the binding and colour blocks.

    `collect` handles the plain rows; this adds the port dict and the
    placeholder rule. Kept separate so a caller that shows only the common rows
    does not pay for machinery it has no fields for.
    """
    changes = collect(fields, values)
    ports: Dict[str, Any] = {}
    for row in fields:
        if not row.port or row.key not in values:
            continue
        raw = str(values[row.key] if values[row.key] is not None else "")
        if row.encode:
            raw = row.encode.get(raw, raw)
        if row.placeholder and raw == row.placeholder:
            # Still the derived value — do not freeze it.
            raw = ""
        ports[row.key] = raw
    if ports:
        changes["port"] = ports
    return changes
