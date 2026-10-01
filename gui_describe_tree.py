"""
gui_describe_tree.py — "Describe it" for SMALL models: a layout TREE (rows,
columns, containers, widgets) -> pixel shapes on the 8 px grid, placed here,
deterministically. Plus the JSON schemas that constrain a model's reply.

PURE. Stdlib plus gui_shapes, gui_colors, gui_layout. No toolkit, no model, no
council_engine — gui_describe's purity test covers this module too, because
gui_describe imports it.

WHY A TREE
----------
Every Describe failure recorded on a real model was PIXEL ARITHMETIC, not
vocabulary: a frame wrapped around notebook pages, a 1040 px log pane inside a
520 px page, a treeview 24 px past its frame's bottom (tests/data/
describe_phi4_*.json). gui_describe grew one deterministic repair per measured
failure, and each covers exactly the mistake it was measured on — a 3.8B
model makes new ones. So a small model is not asked for pixels at all. It says
what is beside what, and what is inside what:

    {"kind": "column", "children": [
       {"kind": "toolbar", "label": "Tools", "props": {"buttons": ["Open"]}},
       {"kind": "row", "children": [{"kind": "listbox", "label": "Files"},
                                     {"kind": "text", "label": "Notes"}]},
       {"kind": "status_bar", "label": "Status"}]}

and layout() does the arithmetic from the palette's own default sizes. The
geometry failure class is then impossible by construction: children are
inside their containers because they were PLACED there, siblings cannot
overlap because they were placed side by side, and a notebook has one page
per tab because each page is a node.

WHAT COMES OUT IS THE PIXEL PAYLOAD
-----------------------------------
layout() returns {"window": ..., "shapes": [...]} — byte-for-byte the shape of
a pixel-mode reply — and gui_describe takes it through check_reply, the same
authority as ever (schema, prop types, colours, Generate's own gate). Nothing
here decides that a wireframe is acceptable; it only decides where things go.

WHY THE SCHEMAS LIVE HERE
-------------------------
Constrained decoding (llama.cpp grammar / Ollama "format") turns the schema
into the only replies the sampler can produce. Built from PALETTE, so a kind
the catalogue lacks, a prop the kind lacks, a string where a list belongs, a
colour name, or a code fence cannot be generated at all. gui_describe (pixel
mode) and gui_classify reuse prop_json() so all three agree on what a prop is.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gui_colors
from gui_shapes import CONTAINER_KINDS, GENERIC_KIND, PALETTE

# ============================================================
# Constants
# ============================================================

GRID = 8
#: Kept clear at the canvas edge. Twice gui_snap's 8: a tree is laid out from
#: scratch, so there is no reason to crowd the edge.
MARGIN = 16
#: Between siblings. Twice the 8 px the pixel prompt asks for — the minimum
#: is what a model is told not to go under, not what a layout should aim at.
GAP = 16
#: Inside a container, on every side but the top.
INSET = 16
#: Above a container's first child: a labelframe's caption sits on its top
#: border, a notebook's tab strip across its top. Same numbers as the
#: qt_tests wireframes (b_notebook_tabs: notebook y=24, pages y=64).
TOP_INSET = {"labelframe": 24, "notebook": 40}

#: Pseudo-kinds: layout only, never a shape.
ROW, COLUMN, PAGE = "row", "column", "page"
LAYOUT_KINDS = (ROW, COLUMN)

#: Deeper than this is a mistake, not a window. Measured on the qt_tests
#: wireframes, the deepest (b_nested_containers, b_notebook_tabs) need four.
MAX_DEPTH = 6
#: Per list. Bounds the grammar and the reply, not a design limit.
MAX_CHILDREN = 16
MAX_PAGES = 8
#: Shapes a tree may expand to (pages count). gui_describe.MAX_SHAPES is the
#: same number and a test pins the two together.
MAX_SHAPES = 60
#: Rough width of a character in the generated app's default font. Only used
#: to keep a caption from being drawn narrower than its own text.
CHAR_W = 8
#: Captions longer than this wrap rather than widen the window.
MAX_TEXT_W = 480
#: A layout needing more than this much of the window, on either axis, is
#: refused with a fault rather than squeezed: past it, widgets come out a
#: fraction of their natural size and the design is not what was asked for.
MAX_SQUEEZE = 1.35

LABEL_MAX = 60
TITLE_MAX = 80
TEXT_MAX = 120
LIST_MAX = 24
MENUS_MAX = 12
COLOUR_PATTERN = "^#[0-9a-fA-F]{6}$"
#: "<family> <size> [bold] [italic]" — gui_describe._check_font's grammar,
#: narrowed to what a model should write (size 1-99, plain family).
FONT_PATTERN = "^[A-Za-z][A-Za-z ]{0,30} [1-9][0-9]?( bold)?( italic)?$"

#: Kinds a tree may use as a WIDGET (a leaf). The catalogue minus containers
#: and the untyped placeholder, in PALETTE order.
LEAF_KINDS: Tuple[str, ...] = tuple(k for k in PALETTE
                                    if k not in CONTAINER_KINDS
                                    and k != GENERIC_KIND)
#: Containers whose children stack top to bottom.
BOX_KINDS = ("frame", "labelframe", "freeform")

#: Kinds that are one line high and must never be stretched vertically —
#: a 300 px tall entry box is a layout bug, not a "grow".
ONE_LINE = frozenset({
    "label", "button", "entry", "checkbutton", "radiobutton", "combobox",
    "spinbox", "scale", "progressbar", "separator", "scrubber",
    "file_picker", "status_bar", "toolbar", "menubar",
})

#: Natural heights on the grid. The palette's own defaults (26, 30, 36 ...)
#: rounded UP to 8, with the qt_tests wireframes' values where they differ.
_HEIGHT = {"label": 24, "button": 32, "entry": 32, "checkbutton": 24,
           "radiobutton": 24, "combobox": 32, "spinbox": 32, "scale": 32,
           "progressbar": 24, "separator": 8, "scrubber": 40,
           "file_picker": 32, "status_bar": 24, "toolbar": 40,
           "menubar": 24}

_ROW_WORDS = frozenset({"row", "hbox", "horizontal", "hstack", "hlayout",
                        "hsplit"})
_COLUMN_WORDS = frozenset({"column", "col", "vbox", "vertical", "vstack",
                           "vlayout", "stack", "layout", "vsplit"})
_PAGE_WORDS = frozenset({"page", "tab"})
#: What a model calls a widget when it does not use the catalogue's word —
#: the HTML / Qt / everyday names. Mapped with a note rather than sent back:
#: in tree mode the kind is the only thing a node has to get right, and
#: "dropdown" is not ambiguous. Pixel mode keeps its strict check (a fault
#: with a "did you mean"), measured and tested on real replies.
KIND_SYNONYMS = {
    "textbox": "entry", "text_box": "entry", "input": "entry",
    "textfield": "entry", "text_field": "entry", "lineedit": "entry",
    "line_edit": "entry", "textinput": "entry", "text_input": "entry",
    "password": "entry", "textarea": "text", "text_area": "text",
    "textedit": "text", "text_edit": "text", "editor": "text",
    "dropdown": "combobox", "drop_down": "combobox", "select": "combobox",
    "combo": "combobox", "choice": "combobox",
    "slider": "scale", "checkbox": "checkbutton", "check": "checkbutton",
    "toggle": "checkbutton", "switch": "checkbutton",
    "radio": "radiobutton", "radio_button": "radiobutton",
    "table": "treeview", "tree": "treeview", "tableview": "treeview",
    "image": "image_canvas", "canvas": "image_canvas",
    "image_view": "image_canvas", "imageview": "image_canvas",
    "picture": "image_canvas", "preview": "image_canvas",
    "list": "listbox", "list_box": "listbox", "listview": "listbox",
    "progress": "progressbar", "progress_bar": "progressbar",
    "chart": "chart_panel", "plot": "chart_panel", "graph": "chart_panel",
    "log": "log_pane", "console": "log_pane",
    "statusbar": "status_bar", "status": "status_bar",
    "menu": "menubar", "menu_bar": "menubar", "tool_bar": "toolbar",
    "tabs": "notebook", "tabview": "notebook", "tab_widget": "notebook",
    "tabwidget": "notebook", "group": "labelframe", "groupbox": "labelframe",
    "group_box": "labelframe", "fieldset": "labelframe",
    "panel": "frame", "container": "frame", "box": "frame",
    "splitter": "panedwindow", "split": "panedwindow",
    "file": "file_picker", "filepicker": "file_picker",
    "file_chooser": "file_picker", "file_input": "file_picker",
    "folder_picker": "file_picker", "spinner": "spinbox",
    "number": "spinbox", "numeric": "spinbox", "spin_box": "spinbox",
    "caption": "label", "text_label": "label", "heading": "label",
    "title": "label", "push_button": "button", "pushbutton": "button",
    "btn": "button", "divider": "separator", "line": "separator",
    "hr": "separator",
}
#: Where a model puts a node's children. The first list found wins.
CHILD_KEYS = ("children", "pages", "panes", "widgets", "items", "shapes",
              "content", "contents", "elements", "controls")
#: Never applied, whatever a model writes — see gui_describe.REFUSED_KEYS.
_REFUSED = frozenset({"id", "script", "drives", "port", "requires"})
_GEOMETRY = frozenset({"x", "y", "w", "h", "width", "height", "pos",
                       "position", "size", "resize", "min_w", "min_h", "z"})


def _u(px: float) -> int:
    """Pixels -> grid units, rounded UP: a natural size is never shrunk."""
    return max(1, -(-int(px) // GRID))


# ============================================================
# JSON schemas
# ============================================================
# Shared definitions go under "$defs" and are referenced, never inlined: the
# llama.cpp converter names a rule after its PATH, so the same label schema
# inlined under 26 kinds compiles to 26 copies of a 60-deep repetition.

def prop_json(pdef: Dict[str, Any]) -> Dict[str, Any]:
    """One prop_schema entry as JSON Schema. The same TYPES gui_describe's
    _coerce accepts (KNOWN_PROP_TYPES), so a constrained reply never carries
    a prop the type check then refuses."""
    choices = pdef.get("choices")
    if choices:
        return {"enum": list(choices)}
    t = str(pdef.get("type") or "")
    if t == "str":
        return {"type": "string", "maxLength": TEXT_MAX}
    if t == "int":
        # min/max are honoured by jsonschema and by llama.cpp's C++ converter
        # (Ollama); llama-cpp-python's Python converter IGNORES them
        # (llama_grammar.py: "TODO: support minimum, maximum") and caps the
        # integral part at 16 digits instead. gui_describe._coerce refuses
        # anything past int32 either way.
        return {"type": "integer", "minimum": -2 ** 31, "maximum": 2 ** 31 - 1}
    if t == "float":
        return {"type": "number"}
    if t == "bool":
        return {"type": "boolean"}
    if t == "list[str]":
        return {"$ref": "#/$defs/strings"}
    if t == "tree":
        return {"$ref": "#/$defs/menus"}
    return {}


def props_def(kind: str) -> Dict[str, Any]:
    """A kind's props object: its own keys, typed, nothing else. Handler
    props are left out — generation wires every callback itself."""
    schema = PALETTE[kind].get("prop_schema") or {}
    props = {p: prop_json(d) for p, d in schema.items()
             if d.get("type") != "handler"}
    return {"type": "object", "properties": props,
            "additionalProperties": False}


def shared_defs(kinds: Sequence[str]) -> Dict[str, Any]:
    """The "$defs" every schema here shares: label, colour, font, string
    lists, menus, and one props object per kind in ``kinds``."""
    defs: Dict[str, Any] = {
        "label": {"type": "string", "maxLength": LABEL_MAX},
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "colour": {"type": "string", "pattern": COLOUR_PATTERN},
        "font": {"type": "string", "pattern": FONT_PATTERN},
        "strings": {"type": "array", "maxItems": LIST_MAX,
                    "items": {"type": "string", "maxLength": LABEL_MAX}},
        "menus": {"type": "array", "maxItems": MENUS_MAX, "items": {
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 40},
                           "items": {"$ref": "#/$defs/strings"}},
            "required": ["title", "items"], "additionalProperties": False}},
        "window": {"type": "object", "properties": {
            "title": {"$ref": "#/$defs/title"},
            "bg": {"$ref": "#/$defs/colour"},
            "fg": {"$ref": "#/$defs/colour"},
            "font": {"$ref": "#/$defs/font"}},
            "required": ["title"], "additionalProperties": False},
    }
    for k in kinds:
        defs[f"props-{k}"] = props_def(k)
    return defs


def style_props(kind: str) -> Dict[str, Any]:
    """bg / fg / font — only the ones this kind can honour (gui_colors), so a
    colour that would be dropped with a note cannot be generated at all."""
    out: Dict[str, Any] = {}
    for channel in gui_colors.caps(kind):
        out[channel] = {"$ref": "#/$defs/colour"}
    if gui_colors.can_font(kind):
        out["font"] = {"$ref": "#/$defs/font"}
    return out


def _obj(props: Dict[str, Any], required: Sequence[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}


def _array(item_ref: str, max_items: int, min_items: int = 0
           ) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "array", "items": {"$ref": item_ref},
                           "maxItems": max_items}
    if min_items:
        out["minItems"] = min_items
    return out


def tree_schema(max_depth: int = MAX_DEPTH,
                max_children: int = MAX_CHILDREN) -> Dict[str, Any]:
    """The reply schema for tree mode.

    Depth-LIMITED, not recursive: level d's containers hold level d+1 nodes,
    and the last level holds widgets only. A recursive $ref compiles too, but
    an unbounded tree is a reply that can run until num_predict cuts it off,
    and a cut-off object is the one fault a grammar cannot prevent.

    "kind" comes first in every variant — required properties are emitted in
    order — so the sampler commits to a kind before anything that depends on
    it, and every other key is typed for THAT kind."""
    kinds = [k for k in PALETTE if k != GENERIC_KIND]
    defs = shared_defs(kinds)
    leaves = []
    for k in LEAF_KINDS:
        props = {"kind": {"const": k}, "label": {"$ref": "#/$defs/label"},
                 "props": {"$ref": f"#/$defs/props-{k}"},
                 "grow": {"type": "boolean"}}
        props.update(style_props(k))
        leaves.append(_obj(props, ["kind", "label"]))
    defs["leaf"] = {"anyOf": leaves}
    depth = max(1, int(max_depth))
    for d in range(depth):
        name = f"n{d}"
        if d == depth - 1:
            defs[name] = {"$ref": "#/$defs/leaf"}
            continue
        nxt = f"#/$defs/n{d + 1}"
        alts: List[Dict[str, Any]] = [{"$ref": "#/$defs/leaf"}]
        for lay in LAYOUT_KINDS:
            alts.append(_obj({"kind": {"const": lay},
                              "children": _array(nxt, max_children, 1),
                              "grow": {"type": "boolean"}},
                             ["kind", "children"]))
        for k in BOX_KINDS:
            props = {"kind": {"const": k}, "label": {"$ref": "#/$defs/label"},
                     "children": _array(nxt, max_children),
                     "props": {"$ref": f"#/$defs/props-{k}"},
                     "grow": {"type": "boolean"}}
            props.update(style_props(k))
            alts.append(_obj(props, ["kind", "label", "children"]))
        defs[f"page{d}"] = _obj({"kind": {"const": PAGE},
                                 "label": {"$ref": "#/$defs/label"},
                                 "children": _array(nxt, max_children)},
                                ["kind", "label", "children"])
        alts.append(_obj({"kind": {"const": "notebook"},
                          "label": {"$ref": "#/$defs/label"},
                          "children": _array(f"#/$defs/page{d}", MAX_PAGES),
                          "grow": {"type": "boolean"}},
                         ["kind", "label", "children"]))
        pane = {"kind": {"const": "panedwindow"},
                "label": {"$ref": "#/$defs/label"},
                "children": _array(nxt, 4, 2),
                "props": {"$ref": "#/$defs/props-panedwindow"},
                "grow": {"type": "boolean"}}
        pane.update(style_props("panedwindow"))
        alts.append(_obj(pane, ["kind", "label", "children"]))
        defs[name] = {"anyOf": alts}
    return {"$defs": defs, "type": "object",
            "properties": {"window": {"$ref": "#/$defs/window"},
                           "layout": {"$ref": "#/$defs/n0"}},
            "required": ["window", "layout"], "additionalProperties": False}


# ============================================================
# The tree
# ============================================================

@dataclass
class Node:
    """One node of a parsed tree. ``kind`` is a palette kind, or one of the
    pseudo-kinds row / column / page. Values are kept AS WRITTEN — props,
    colours and the label are checked by gui_describe.check_reply after
    layout, with the same messages pixel mode gets."""
    kind: str
    label: Any = ""
    props: Any = None
    style: Dict[str, Any] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    grow: Optional[bool] = None
    path: str = ""
    # Filled by layout(), in GRID UNITS.
    nat_w: int = 0
    nat_h: int = 0
    gx: bool = False
    gy: bool = False
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @property
    def is_layout(self) -> bool:
        return self.kind in LAYOUT_KINDS

    @property
    def is_leaf(self) -> bool:
        return self.kind in LEAF_KINDS


@dataclass
class Parsed:
    root: Optional[Node] = None
    window: Any = None
    faults: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: True when the payload is a TREE at all (has a "layout"-like root).
    #: False lets gui_describe hand a pixel-style reply to the pixel path.
    is_tree: bool = False
    #: Nodes that parsed without a fault — how much of a refused tree was
    #: usable, so the best of several replies is the one with most in it.
    good: int = 0


_ROOT_KEYS = ("layout", "root", "tree", "ui", "body", "main")


def looks_like_tree(payload: Any) -> bool:
    """True for a reply in tree form: a "layout" (or alias) object, or a
    top-level node. A pixel reply has a "shapes" LIST of rows with x/y."""
    if not isinstance(payload, dict):
        return False
    if any(isinstance(payload.get(k), (dict, list)) for k in _ROOT_KEYS):
        return True
    rows = payload.get("shapes")
    if isinstance(rows, list):
        return False
    return "kind" in payload and any(isinstance(payload.get(k), list)
                                     for k in CHILD_KEYS)


def _short(path: str) -> str:
    """The last three steps of a path: enough to find a node, short enough
    that a fault list does not spend its budget on 'layout > column > ...'."""
    parts = path.split(" > ")
    return path if len(parts) <= 3 else "… > " + " > ".join(parts[-3:])


def _name(kind: str, label: Any, index: int) -> str:
    lab = label.strip() if isinstance(label, str) else ""
    if len(lab) > 30:
        lab = lab[:29] + "…"
    return f'{kind} "{lab}"' if lab else f"{kind} {index + 1}"


def _kind_of(raw: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(normalised kind or None, what the model wrote)."""
    k = raw.get("kind")
    if k is None:
        k = raw.get("type")
    if not isinstance(k, str) or not k.strip():
        for key, kind in ((ROW, ROW), (COLUMN, COLUMN)):
            if isinstance(raw.get(key), list):
                return kind, key
        if any(isinstance(raw.get(c), list) for c in CHILD_KEYS):
            return COLUMN, ""
        return None, "" if k is None else str(k)
    word = k.strip().lower().replace("-", "_").replace(" ", "_")
    if word in _ROW_WORDS:
        return ROW, k
    if word in _COLUMN_WORDS:
        return COLUMN, k
    if word in _PAGE_WORDS:
        return PAGE, k
    if word in PALETTE and word != GENERIC_KIND:
        return word, k
    if word in KIND_SYNONYMS:
        return KIND_SYNONYMS[word], k
    return None, k


def _children_of(raw: Dict[str, Any], kind: str) -> Tuple[Optional[str], Any]:
    if kind in (ROW, COLUMN) and isinstance(raw.get(kind), list):
        return kind, raw[kind]
    for key in CHILD_KEYS:
        if isinstance(raw.get(key), list):
            return key, raw[key]
    return None, None


class _Parser:
    def __init__(self) -> None:
        self.faults: List[str] = []
        self.notes: List[str] = []
        self.good = 0

    def node(self, raw: Any, depth: int, path: str, index: int
             ) -> Optional[Node]:
        if isinstance(raw, list):
            raw = {"kind": COLUMN, "children": raw}
        if not isinstance(raw, dict):
            self.faults.append(f"{_short(path)}: expected a node like "
                               f'{{"kind": "button", "label": "Run"}}, got '
                               f"{type(raw).__name__}")
            return None
        kind, wrote = _kind_of(raw)
        if kind is None:
            if wrote:
                kinds = list(LEAF_KINDS) + list(CONTAINER_KINDS) \
                    + [ROW, COLUMN, PAGE]
                near = difflib.get_close_matches(wrote.lower(), kinds, n=1,
                                                 cutoff=0.6)
                self.faults.append(
                    f"{_short(path)}: {wrote!r} is not a widget kind — use "
                    f"one of the listed kinds"
                    + (f' (did you mean "{near[0]}"?)' if near else ""))
            else:
                self.faults.append(f'{_short(path)}: a node has no "kind"')
            return None
        label = raw.get("label")
        if label is None:
            for alias in ("title", "caption", "name"):
                if isinstance(raw.get(alias), str):
                    label = raw[alias]
                    break
        here = f"{path} > {_name(kind, label, index)}" if path else \
            _name(kind, label, index)
        if depth > MAX_DEPTH:
            self.faults.append(f"{_short(here)}: nested more than {MAX_DEPTH}"
                               f" levels deep — flatten it")
            return None

        node = Node(kind=kind, label=label if label is not None else "",
                    path=here)
        props = raw.get("props")
        node.props = dict(props) if isinstance(props, dict) else props
        word = wrote.strip().lower().replace("-", "_").replace(" ", "_") \
            if isinstance(wrote, str) else ""
        if word in KIND_SYNONYMS and KIND_SYNONYMS[word] == kind:
            self.notes.append(f"{_short(here)}: read {wrote!r} as {kind}")
            if word == "password" and isinstance(node.props, (dict,
                                                              type(None))):
                node.props = dict(node.props or {})
                node.props.setdefault("show", "*")
        node.style = {k: raw[k] for k in ("bg", "fg", "font") if k in raw}
        grow = raw.get("grow", raw.get("expand", raw.get("stretch")))
        if isinstance(grow, bool):
            node.grow = grow
        elif grow is not None:
            self.notes.append(f'{_short(here)}: ignored "grow" {grow!r} — it '
                              f"is true or false")

        key, kids = _children_of(raw, kind)
        self._misplaced_props(raw, node, key)
        self._ignored_keys(raw, node, key)

        if node.is_layout or kind == PAGE or kind in CONTAINER_KINDS:
            for i, child in enumerate(kids or []):
                c = self.node(child, depth + 1, here, i)
                if c is not None:
                    node.children.append(c)
        elif kids and not (kind == "toolbar" and all(
                isinstance(x, str) for x in kids)):
            self.faults.append(
                f"{_short(here)}: a {kind} cannot hold other widgets — put "
                f"them beside it in a row or column, or inside a frame")
        else:
            self.good += 1
        if node.is_layout or kind == PAGE or kind in CONTAINER_KINDS:
            self.good += 1
        return node

    def _misplaced_props(self, raw: Dict[str, Any], node: Node,
                         child_key: Optional[str]) -> None:
        """A prop written beside "kind" instead of inside "props" — small
        models do it constantly ("values" on a combobox, "tabs" on a
        notebook). Moved in, with a note; spending a repair round on where a
        key sits would be spending it on nothing."""
        schema = (PALETTE.get(node.kind) or {}).get("prop_schema") or {}
        moved = []
        for k, v in raw.items():
            if k in ("kind", "label", "props", "grow") or k == child_key:
                continue
            if k in schema and schema[k].get("type") != "handler":
                if not isinstance(node.props, dict):
                    node.props = {} if node.props is None else node.props
                if isinstance(node.props, dict) and k not in node.props:
                    node.props[k] = v
                    moved.append(k)
        if node.kind == "toolbar" and child_key and isinstance(
                raw.get(child_key), list) and all(
                isinstance(x, str) for x in raw[child_key]):
            node.props = dict(node.props or {}) if isinstance(
                node.props, (dict, type(None))) else node.props
            if isinstance(node.props, dict):
                node.props.setdefault("buttons", list(raw[child_key]))
                moved.append(child_key)
        if moved:
            self.notes.append(f"{_short(node.path)}: moved "
                              + ", ".join(sorted(set(moved)))
                              + ' into "props"')

    def _ignored_keys(self, raw: Dict[str, Any], node: Node,
                      child_key: Optional[str]) -> None:
        schema = (PALETTE.get(node.kind) or {}).get("prop_schema") or {}
        geo, refused = [], []
        for k in raw:
            if k in ("kind", "type", "label", "title", "caption", "name",
                     "props", "grow", "expand", "stretch", "bg", "fg",
                     "font") or k == child_key or k in schema:
                continue
            if k in _GEOMETRY:
                geo.append(k)
            elif k in _REFUSED:
                refused.append(k)
        if geo:
            self.notes.append(f"{_short(node.path)}: ignored "
                              + ", ".join(geo) + " — the layout places it")
        if refused:
            self.notes.append(f"{_short(node.path)}: ignored "
                              + ", ".join(refused)
                              + " — the user adds those in the designer")


def _normalise(node: Node, notes: List[str], parent: Optional[str] = None
               ) -> Optional[Node]:
    """Structural tidying a model should not have to get right, each with a
    note: a one-child row or column is its child; an empty one is nothing; a
    notebook's non-page child is wrapped in a page titled by its label; a
    page outside a notebook is a frame."""
    node.children = [c for c in (_normalise(c, notes, node.kind)
                                 for c in node.children) if c is not None]
    if node.kind == PAGE and parent != "notebook":
        node.kind = "frame"
        notes.append(f"{_short(node.path)}: a page outside a notebook is "
                     f"drawn as a frame")
    if node.kind == "notebook":
        pages = []
        for i, c in enumerate(node.children):
            if c.kind == PAGE:
                pages.append(c)
                continue
            title = c.label if isinstance(c.label, str) and c.label.strip() \
                else f"Tab {i + 1}"
            if c.kind == "frame":
                c.kind = PAGE
                pages.append(c)
            else:
                pages.append(Node(kind=PAGE, label=title, children=[c],
                                  path=c.path))
            notes.append(f"{_short(c.path)}: made into the notebook page "
                         f"{title!r}")
        node.children = pages
    if node.is_layout:
        if not node.children:
            notes.append(f"{_short(node.path)}: an empty {node.kind} was "
                         f"dropped")
            return None
        if len(node.children) == 1:
            only = node.children[0]
            if node.grow is not None and only.grow is None:
                only.grow = node.grow
            return only
    return node


def parse(payload: Any) -> Parsed:
    """A model's tree reply -> Parsed. Never raises."""
    out = Parsed()
    if not isinstance(payload, dict):
        out.faults.append('the reply was not a JSON object with "window" and '
                          '"layout"')
        return out
    out.is_tree = looks_like_tree(payload)
    if not out.is_tree:
        out.faults.append('the reply has no "layout" — describe the window as'
                          ' {"window": {...}, "layout": {"kind": "column", '
                          '"children": [...]}}')
        return out
    out.window = payload.get("window")
    raw = next((payload[k] for k in _ROOT_KEYS
                if isinstance(payload.get(k), (dict, list))), None)
    if raw is None:
        raw = {k: v for k, v in payload.items() if k != "window"}
    for k in payload:
        if k not in ("window",) + _ROOT_KEYS and k in _REFUSED:
            out.notes.append(f'ignored "{k}" — the user adds that in the '
                             f"designer")
    p = _Parser()
    root = p.node(raw, 0, "", 0)
    out.faults.extend(p.faults)
    out.good = p.good
    out.notes.extend(p.notes)
    if root is not None and not out.faults:
        root = _normalise(root, out.notes)
    if root is None and not out.faults:
        out.faults.append('"layout" is empty — the request needs at least one'
                          ' widget')
    n = _count(root) if root is not None else 0
    if n > MAX_SHAPES:
        out.faults.append(f"{n} widgets is too many (at most {MAX_SHAPES})"
                          f" — merge or drop some")
    out.root = root if not out.faults else None
    return out


def _count(node: Node) -> int:
    own = 0 if node.is_layout else 1
    return own + sum(_count(c) for c in node.children)


# ============================================================
# Layout — all arithmetic in GRID units
# ============================================================

def _text_of(node: Node) -> str:
    if isinstance(node.label, str) and node.label.strip():
        return node.label
    props = node.props if isinstance(node.props, dict) else {}
    text = props.get("text")
    return text if isinstance(text, str) else ""


def _orient(node: Node) -> str:
    props = node.props if isinstance(node.props, dict) else {}
    return "vertical" if props.get("orient") == "vertical" else "horizontal"


def _leaf_px(node: Node) -> Tuple[int, int]:
    """A widget's natural size in pixels: the palette default on the grid,
    widened to its caption where the caption is the widget."""
    pal = PALETTE[node.kind]
    w = int(pal["default_w"])
    h = _HEIGHT.get(node.kind, int(pal["default_h"]))
    text = len(_text_of(node))
    props = node.props if isinstance(node.props, dict) else {}
    if node.kind == "label":
        w = max(w, min(MAX_TEXT_W, text * CHAR_W + 8))
    elif node.kind == "button":
        w = max(112, text * CHAR_W + 32)
    elif node.kind in ("checkbutton", "radiobutton"):
        w = max(144, min(MAX_TEXT_W, text * CHAR_W + 40))
    elif node.kind == "toolbar":
        buttons = props.get("buttons")
        n = len(buttons) if isinstance(buttons, list) else 0
        w = max(w, n * 88 + 16)
    if node.kind in ("separator", "scale", "progressbar") \
            and _orient(node) == "vertical":
        # Turned on its side: the default's long axis becomes the height.
        w, h = (8, 160) if node.kind == "separator" else (h, 200)
    return w, h


def _default_grow(node: Node) -> Tuple[bool, bool]:
    if node.grow is True:
        return True, node.kind not in ONE_LINE
    if node.grow is False:
        return False, False
    mode = str(PALETTE.get(node.kind, {}).get("default_resize") or "fixed")
    gx = mode in ("stretch_h", "stretch_both")
    gy = mode in ("stretch_v", "stretch_both") and node.kind not in ONE_LINE
    return gx, gy


def _top(kind: str) -> int:
    return _u(TOP_INSET.get(kind, INSET))


@dataclass(frozen=True)
class _Ctx:
    """One layout attempt's spacing, in units: the gap between siblings and
    the smallest a widget may be on each axis (see layout())."""
    gap: int
    min_w: int
    min_h: int


def _seq_measure(kids: Sequence[Node], axis: str, ctx: _Ctx
                 ) -> Tuple[int, int]:
    if not kids:
        return 0, 0
    if axis == ROW:
        return (sum(k.nat_w for k in kids) + ctx.gap * (len(kids) - 1),
                max(k.nat_h for k in kids))
    return (max(k.nat_w for k in kids),
            sum(k.nat_h for k in kids) + ctx.gap * (len(kids) - 1))


def _align_form_rows(node: Node, ctx: _Ctx) -> None:
    """Rows of a column that look like a FORM — two or more rows of the same
    length — get the same width per position, so the captions line up and
    gui_layout infers one grid instead of a ragged column of rows."""
    rows = [c for c in node.children if c.kind == ROW and c.children]
    if len(rows) < 2:
        return
    by_len: Dict[int, List[Node]] = {}
    for r in rows:
        by_len.setdefault(len(r.children), []).append(r)
    for n, group in by_len.items():
        if len(group) < 2:
            continue
        for i in range(n):
            cells = [r.children[i] for r in group]
            if any(not c.is_leaf for c in cells):
                continue
            widest = max(c.nat_w for c in cells)
            for c in cells:
                c.nat_w = widest
        for r in group:
            r.nat_w, r.nat_h = _seq_measure(r.children, ROW, ctx)


def _measure(node: Node, ctx: _Ctx) -> None:
    """Natural size (units) and grow flags, bottom up."""
    for c in node.children:
        _measure(c, ctx)
    ins = _u(INSET)
    if node.is_leaf:
        w, h = _leaf_px(node)
        node.nat_w = max(_u(w), ctx.min_w)
        node.nat_h = max(_u(h), ctx.min_h)
        node.gx, node.gy = _default_grow(node)
        return
    if node.is_layout:
        if node.kind == COLUMN:
            _align_form_rows(node, ctx)
        node.nat_w, node.nat_h = _seq_measure(node.children, node.kind, ctx)
        node.gx = any(c.gx for c in node.children)
        node.gy = any(c.gy for c in node.children)
        if node.grow is True:
            node.gx = node.gy = True
        elif node.grow is False:
            node.gx = node.gy = False
        return
    kind = "frame" if node.kind == PAGE else node.kind
    if not node.children:
        pal = PALETTE[kind]
        node.nat_w, node.nat_h = _u(pal["default_w"]), _u(pal["default_h"])
        node.gx = node.gy = node.grow is not False
        return
    if kind == "notebook":
        # Sized for ONE page — the one the running app shows. The designer
        # draws every page side by side inside it (the gate counts them that
        # way), so they are squeezed into its width when placed; sizing the
        # notebook for all of them would refuse a six-tab window as "too
        # wide" when the app it generates is not.
        cw = max(k.nat_w for k in node.children)
        ch = max(k.nat_h for k in node.children)
        node.gx = node.gy = node.grow is not False
    elif kind == "panedwindow":
        axis = COLUMN if _orient(node) == "vertical" else ROW
        cw, ch = _seq_measure(node.children, axis, ctx)
        node.gx = node.gy = node.grow is not False
    else:
        holder = Node(kind=COLUMN, children=node.children)
        _align_form_rows(holder, ctx)
        cw, ch = _seq_measure(node.children, COLUMN, ctx)
        node.gx = any(c.gx for c in node.children)
        node.gy = any(c.gy for c in node.children)
        if node.grow is True:
            node.gx = node.gy = True
        elif node.grow is False:
            node.gx = node.gy = False
    node.nat_w = cw + 2 * ins
    node.nat_h = ch + _top(kind) + ins


def _distribute(nat: Sequence[int], grows: Sequence[bool], avail: int
                ) -> List[int]:
    """Main-axis sizes (units) for children with natural sizes ``nat``.

    Room to spare goes to the children that grow, evenly; none growing, it
    stays at the end. Too little room squeezes every child in proportion to
    its natural size, never below one unit."""
    total = sum(nat)
    if total <= avail:
        sizes = list(nat)
        growers = [i for i, g in enumerate(grows) if g]
        extra = avail - total
        if growers and extra > 0:
            share, rem = divmod(extra, len(growers))
            for n, i in enumerate(growers):
                sizes[i] += share + (1 if n < rem else 0)
        return sizes
    sizes = [max(1, (n * avail) // total) for n in nat]
    over = sum(sizes) - avail
    # Rounding up to one unit can overshoot; take it back from the largest.
    while over > 0:
        i = max(range(len(sizes)), key=lambda j: sizes[j])
        if sizes[i] <= 1:
            break
        sizes[i] -= 1
        over -= 1
    return sizes


class _Placer:
    def __init__(self, ctx: _Ctx) -> None:
        self.ctx = ctx
        self.rows: List[Dict[str, Any]] = []
        self.paths: List[str] = []
        self.notes: List[str] = []

    def place(self, node: Node, x: int, y: int, w: int, h: int) -> None:
        node.x, node.y, node.w, node.h = x, y, max(1, w), max(1, h)
        if node.is_layout:
            self.seq(node, node.children, node.kind, x, y, w, h)
            return
        self.emit(node)
        if not node.children:
            return
        kind = "frame" if node.kind == PAGE else node.kind
        ins, top = _u(INSET), _top(kind)
        ix, iy, iw, ih = x + ins, y + top, w - 2 * ins, h - top - ins
        if kind == "notebook":
            self.seq(node, node.children, ROW, ix, iy, iw, ih, fill=True)
        elif kind == "panedwindow":
            axis = COLUMN if _orient(node) == "vertical" else ROW
            self.seq(node, node.children, axis, ix, iy, iw, ih, fill=True)
        else:
            self.seq(node, node.children, COLUMN, ix, iy, iw, ih)

    def seq(self, owner: Node, kids: Sequence[Node], axis: str, x: int,
            y: int, w: int, h: int, *, fill: bool = False) -> None:
        if not kids:
            return
        gap = self.ctx.gap
        row = axis == ROW
        main = (w if row else h) - gap * (len(kids) - 1)
        nat = [k.nat_w if row else k.nat_h for k in kids]
        grows = [fill or (k.gx if row else k.gy) for k in kids]
        if main < len(kids):
            main = len(kids)
        sizes = _distribute(nat, grows, main)
        pos = x if row else y
        cross = h if row else w
        # In a row, one-line widgets share one height — a 24 px caption
        # beside a 32 px entry reads as a wobbly row, and the caption's text
        # is centred in whatever height it gets.
        line = max([k.nat_h for k in kids
                    if row and k.is_leaf and k.kind in ONE_LINE] or [0])
        for k, size in zip(kids, sizes):
            stretch = fill or (k.gy if row else k.gx)
            natc = k.nat_h if row else k.nat_w
            if row and k.is_leaf and k.kind in ONE_LINE:
                natc = max(natc, line)
            c = cross if stretch else min(natc, cross)
            if row:
                self.place(k, pos, y, size, c)
            else:
                self.place(k, x, pos, c, size)
            pos += size + gap

    def emit(self, node: Node) -> None:
        kind = "frame" if node.kind == PAGE else node.kind
        row: Dict[str, Any] = {"kind": kind, "label": node.label}
        row.update(x=node.x * GRID, y=node.y * GRID, w=node.w * GRID,
                   h=node.h * GRID)
        props = node.props
        if kind == "notebook":
            titles = [c.label if isinstance(c.label, str) else str(c.label)
                      for c in node.children]
            given = props.get("tabs") if isinstance(props, dict) else None
            if given is not None and given != titles:
                self.notes.append(f"{_short(node.path)}: tab titles taken "
                                  f"from its pages")
            props = dict(props) if isinstance(props, dict) else (
                {} if props is None else props)
            if isinstance(props, dict):
                props["tabs"] = titles
        if props is not None:
            row["props"] = props
        row.update(node.style)
        self.rows.append(row)
        self.paths.append(node.path)


@dataclass
class Layout:
    """layout()'s result: the pixel payload, and for each of its shapes the
    TREE path it came from, so a fault check_reply names by position ("shape
    4") can be told back to the model in the terms it wrote."""
    payload: Dict[str, Any] = field(default_factory=dict)
    paths: List[str] = field(default_factory=list)
    faults: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: Layout attempts made (wider gaps each time; see layout()).
    attempts: int = 0


#: Gaps tried in turn until gui_layout reads the placement as a clean grid.
GAPS = (GAP, GAP + 8, GAP + 16)


def _tolerance(canvas_w: int, canvas_h: int) -> Tuple[float, float]:
    """gui_layout's edge-clustering tolerance for the ROOT container: 2 % of
    the canvas, at least 8 px (gui_layout._layout_container). A container's
    own tolerance is 2 % of the container, so never larger."""
    return max(8.0, 0.02 * canvas_w), max(8.0, 0.02 * canvas_h)


def _grid_clean(rows: Sequence[Dict[str, Any]], canvas_w: int,
                canvas_h: int) -> bool:
    """True when gui_layout infers every container as a grid — no "overlap,
    so ... cannot be a grid" warning, which the gate refuses."""
    import gui_layout
    from gui_shapes import Shape
    shapes = [Shape(id=f"n{i}", kind=str(r["kind"]), x=int(r["x"]),
                    y=int(r["y"]), w=int(r["w"]), h=int(r["h"]),
                    label=r["label"] if isinstance(r.get("label"), str)
                    else "")
              for i, r in enumerate(rows)]
    tree = gui_layout.infer(shapes, canvas_w, canvas_h)
    return not any(" overlap, so " in w for w in tree.warnings)


def layout(root: Node, canvas_w: int, canvas_h: int,
           window: Any = None) -> Layout:
    """Place a parsed tree on a canvas. Never raises.

    The root takes the canvas minus MARGIN; a widget or container at the root
    keeps its natural size unless it grows. Everything is on the 8 px grid
    by construction (units), children start INSET inside their containers,
    siblings are a gap apart, and a notebook's pages are drawn side by side
    inside it — exactly the geometry gui_describe's gate checks.

    WHY WIDGETS HAVE A MINIMUM SIZE AND THE GAP CAN GROW: Generate does not
    read this tree; gui_layout re-infers a grid from the pixels by merging
    edges closer than 2 % of the container. MEASURED on random trees: on a
    1504 x 1016 canvas that is 20 px, so an 8 px separator's two edges merged
    with its neighbours' into one grid line, the two "overlapped", and the
    gate refused a layout nobody drew wrong. So every widget is made larger
    than that tolerance on both axes, the placement is checked with
    gui_layout itself, and a placement it cannot read as a grid is redone
    with a wider gap (GAPS) before the layout is refused."""
    out = Layout()
    try:
        cw, ch = int(canvas_w) // GRID, int(canvas_h) // GRID
        m = _u(MARGIN)
        aw, ah = cw - 2 * m, ch - 2 * m
        if aw < 4 or ah < 4:
            out.faults.append(f"the canvas {canvas_w} x {canvas_h} is too "
                              f"small to lay anything out on")
            return out
        tol_x, tol_y = _tolerance(canvas_w, canvas_h)
        need = (0.0, 0.0)
        placer: Optional[_Placer] = None
        for gap in GAPS:
            out.attempts += 1
            ctx = _Ctx(gap=_u(gap), min_w=_u(int(tol_x) + 1),
                       min_h=_u(int(tol_y) + 1))
            _measure(root, ctx)
            need = (root.nat_w / aw, root.nat_h / ah)
            if max(need) > MAX_SQUEEZE:
                break
            placer = _Placer(ctx)
            if root.is_layout:
                placer.place(root, m, m, aw, ah)
            else:
                w = aw if root.gx else min(root.nat_w, aw)
                h = ah if root.gy else min(root.nat_h, ah)
                placer.place(root, m, m, w, h)
            if _grid_clean(placer.rows, canvas_w, canvas_h):
                break
            placer = None
        if max(need) > MAX_SQUEEZE:
            what = []
            if need[0] > MAX_SQUEEZE:
                what.append(f"{root.nat_w * GRID} px wide")
            if need[1] > MAX_SQUEEZE:
                what.append(f"{root.nat_h * GRID} px tall")
            out.faults.append(
                f"the layout is {' and '.join(what)} at its natural size, "
                f"but the window is {canvas_w} x {canvas_h} — put some of it "
                f"in a notebook's pages, or use fewer widgets side by side")
            return out
        if placer is None:
            out.faults.append(
                f"the layout is too crowded for a {canvas_w} x {canvas_h} "
                f"window to be read as a grid — use fewer widgets side by "
                f"side, or put some of them in a notebook's pages")
            return out
        if max(need) > 1.0:
            out.notes.append(f"squeezed the layout to fit the {canvas_w} x "
                             f"{canvas_h} window (it is "
                             f"{root.nat_w * GRID} x {root.nat_h * GRID} at "
                             f"its natural size)")
        out.notes.extend(placer.notes)
        out.payload = {"window": window, "shapes": placer.rows}
        out.paths = list(placer.paths)
    except Exception as exc:                 # never raise out of describe()
        out.faults.append(f"the layout could not be computed: {exc!r}")
    return out


# ============================================================
# Wireframe -> tree (worked examples)
# ============================================================

def _meaningful(kind: str, props: Dict[str, Any]) -> Dict[str, Any]:
    schema = (PALETTE.get(kind) or {}).get("prop_schema") or {}
    out = {}
    for k, v in (props or {}).items():
        d = schema.get(k)
        if d is None or d.get("type") == "handler":
            continue
        if v in ("", None, [], {}) or v == d.get("default"):
            continue
        out[k] = v
    return out


def _bands(items: Sequence[Any], lo, hi) -> List[List[Any]]:
    """Group boxes whose [lo, hi) intervals overlap, in order along the
    axis. Two bands with a gap between them are a cut."""
    ordered = sorted(items, key=lambda s: (lo(s), hi(s)))
    bands: List[List[Any]] = []
    end = None
    for s in ordered:
        if bands and end is not None and lo(s) < end:
            bands[-1].append(s)
            end = max(end, hi(s))
        else:
            bands.append([s])
            end = hi(s)
    return bands


def tree_from_shapes(shapes: Sequence[Any]) -> Dict[str, Any]:
    """A drawn wireframe as the tree a model would write for it.

    Nesting from gui_layout's containment (the parser Generate itself uses);
    order from a recursive XY-cut: a set of boxes with a horizontal gap
    through it is a COLUMN of the bands either side, one with a vertical gap
    a ROW, recursively. Used to show worked examples in tree form — the same
    qt_tests wireframes pixel mode shows, so both modes learn from projects
    that were built and run."""
    import gui_layout
    kids = gui_layout.build_containment_tree(list(shapes), warnings=[])
    by_id = {s.id: s for s in shapes}

    def node_of(s: Any) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": s.kind, "label": s.label or ""}
        props = _meaningful(s.kind, s.props)
        if s.kind == "notebook":
            props.pop("tabs", None)
        if props:
            d["props"] = props
        for k in ("bg", "fg", "font"):
            if getattr(s, k, ""):
                d[k] = getattr(s, k)
        inner = [by_id[i] for i in kids.get(s.id, []) if i in by_id]
        if s.kind == "notebook":
            tabs = list((s.props or {}).get("tabs") or [])
            pages = sorted(inner, key=lambda p: (p.x, p.y))
            d["children"] = [
                {"kind": PAGE,
                 "label": tabs[i] if i < len(tabs) else (p.label or ""),
                 "children": arrange([by_id[j] for j in kids.get(p.id, [])
                                      if j in by_id])}
                for i, p in enumerate(pages)]
        elif s.kind == "panedwindow":
            vertical = (s.props or {}).get("orient") == "vertical"
            panes = sorted(inner, key=(lambda p: (p.y, p.x)) if vertical
                           else (lambda p: (p.x, p.y)))
            d["children"] = [node_of(p) for p in panes]
        elif s.kind in CONTAINER_KINDS:
            d["children"] = arrange(inner)
        return d

    def arrange(boxes: List[Any]) -> List[Dict[str, Any]]:
        """Column items for ``boxes`` (a container's children)."""
        if not boxes:
            return []
        if len(boxes) == 1:
            return [node_of(boxes[0])]
        rows = _bands(boxes, lambda s: s.y, lambda s: s.y + s.h)
        if len(rows) > 1:
            return [band_node(b, ROW) for b in rows]
        cols = _bands(boxes, lambda s: s.x, lambda s: s.x + s.w)
        if len(cols) > 1:
            return [{"kind": ROW,
                     "children": [band_node(b, COLUMN) for b in cols]}]
        return [node_of(b) for b in sorted(boxes, key=lambda s: (s.y, s.x))]

    def band_node(band: List[Any], inner: str) -> Dict[str, Any]:
        if len(band) == 1:
            return node_of(band[0])
        items = arrange(band)
        if inner == COLUMN:
            return items[0] if len(items) == 1 else {"kind": COLUMN,
                                                     "children": items}
        # A horizontal band of several boxes: a row (arrange found the
        # vertical cuts) or, if it could not split, a column of them.
        if len(items) == 1:
            return items[0]
        return {"kind": COLUMN, "children": items}

    roots = arrange([by_id[i] for i in kids.get(None, []) if i in by_id])
    if len(roots) == 1:
        return roots[0]
    return {"kind": COLUMN, "children": roots}


# ============================================================
# Rendering (prompts)
# ============================================================

def render(tree: Any, indent: int = 0) -> str:
    """One widget per line, containers opened and closed on their own lines.
    About a third of indent=2 JSON and still nested visibly — which matters
    in a worked example and when a model is shown its own rejected tree."""
    pad = " " * indent
    if isinstance(tree, dict) and isinstance(tree.get("children"), list) \
            and tree["children"]:
        head = {k: v for k, v in tree.items() if k != "children"}
        opening = json.dumps(head, ensure_ascii=False)[:-1] \
            + ', "children": ['
        lines = [pad + opening]
        n = len(tree["children"])
        for i, c in enumerate(tree["children"]):
            lines.append(render(c, indent + 1) + ("," if i < n - 1 else ""))
        lines.append(pad + "]}")
        return "\n".join(lines)
    return pad + json.dumps(tree, ensure_ascii=False)


def render_reply(window: Any, tree: Any) -> str:
    """A whole tree reply: {"window": ..., "layout": ...}."""
    return ("{\"window\": " + json.dumps(window or {}, ensure_ascii=False)
            + ",\n\"layout\":\n" + render(tree, 1) + "\n}")


def count_shapes(tree: Any) -> int:
    """Shapes a tree expands to (rows and columns are not shapes)."""
    if isinstance(tree, list):
        return sum(count_shapes(t) for t in tree)
    if not isinstance(tree, dict):
        return 0
    own = 0 if tree.get("kind") in LAYOUT_KINDS else 1
    return own + sum(count_shapes(c) for c in tree.get("children") or [])
