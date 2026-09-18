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
        if row.heading or row.key not in values:
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
