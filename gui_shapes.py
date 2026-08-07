"""
gui_shapes.py — the GUI Designer's data model: shapes, the widget catalogue, and
.gspec (de)serialisation.

Pure data. No Tk, no model, no I/O beyond reading and writing one JSON file, so
every module downstream can be unit-tested with nothing loaded.

WHY THE CATALOGUE IS A CLOSED SET
---------------------------------
`kind` is not free text. It is a key in PALETTE, and that is the whole safety
property of the feature: gui_classify may only ever emit a key that is in here,
and gui_emit has a template per key. A model that invents "fancy_slider"
produces a validation error, not a KeyError at generation time or — far worse —
a plausible-looking widget nobody can render. This mirrors nx_generate, where
the model picks from the installed filter catalogue and every emitted uuid is
checked back against it.

`prop_schema` is the same idea one level down. A kind's props are a closed set
of keys with declared types and allowed values, so "columns" on a treeview is
checked, and a hallucinated "colour_scheme" is rejected by name rather than
silently dropped or passed to a widget constructor that will raise.

WHY resize DEFAULTS LIVE HERE AND NOT IN gui_layout
---------------------------------------------------
A shape's `resize` may be "auto", which gui_layout resolves by heuristic
(spec 6.5). But the per-kind DEFAULT — an image canvas stretches, a button does
not — is a property of the widget, not of the layout, so it belongs with the
catalogue entry. gui_layout reads `default_resize` from here rather than
carrying its own copy, because two copies of that table would drift.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bumped when the on-disk shape changes incompatibly. load_gspec REFUSES a
# version it does not know rather than guessing: a .gspec written by a future
# build could carry kinds or fields this build would silently drop, and silently
# dropping a user's work is the one failure mode a file format must not have.
GSPEC_VERSION = 1

# Resize modes. "auto" is resolved at layout time and never survives into an
# emitted spec (spec 4.1).
RESIZE_MODES = ("auto", "fixed", "stretch_h", "stretch_v", "stretch_both")

# The kinds that may contain other shapes (spec 6.1). Containment is decided
# geometrically, but only a container may BE a parent — a rectangle drawn on top
# of a button is a drawing mistake, not a nesting.
CONTAINER_KINDS = frozenset({
    "frame", "labelframe", "notebook", "panedwindow", "freeform",
})

# The placeholder kind. A shape left as "generic" is the ONLY thing that makes
# gui_classify call a model; a wireframe built entirely from typed palette
# shapes generates with zero model calls (spec 2).
GENERIC_KIND = "generic"


def _schema(**kw: Any) -> Dict[str, Any]:
    """Sugar for a prop_schema entry so the PALETTE below stays readable."""
    return kw


# ============================================================
# The widget catalogue (spec 5) — LOCKED for v1
# ============================================================
#
# Each entry:
#   label          human name shown in the palette strip
#   is_container   may own children (see CONTAINER_KINDS)
#   default_w/h    size used when the user clicks rather than drags
#   default_resize the kind's natural resize behaviour, used by gui_layout when
#                  the shape says "auto" (spec 6.5)
#   prop_schema    {prop_name: {"type": ..., "choices"/"default": ...}} — the
#                  closed set of props gui_classify may emit for this kind
PALETTE: Dict[str, Dict[str, Any]] = {
    # ---- containers -------------------------------------------------
    "frame": {
        "label": "Frame", "is_container": True,
        "default_w": 240, "default_h": 160, "default_resize": "stretch_both",
        "prop_schema": {
            "relief": _schema(type="str", choices=["flat", "raised", "sunken",
                                                   "groove", "ridge"],
                              default="flat"),
            "borderwidth": _schema(type="int", default=0),
        },
    },
    "labelframe": {
        "label": "Label Frame", "is_container": True,
        "default_w": 240, "default_h": 160, "default_resize": "stretch_both",
        "prop_schema": {
            "text": _schema(type="str", default=""),
            "labelanchor": _schema(type="str",
                                   choices=["nw", "n", "ne", "w", "e",
                                            "sw", "s", "se"], default="nw"),
        },
    },
    "notebook": {
        "label": "Notebook", "is_container": True,
        "default_w": 320, "default_h": 220, "default_resize": "stretch_both",
        # Tab titles are a list because the children map onto them in order.
        "prop_schema": {"tabs": _schema(type="list[str]", default=[])},
    },
    "panedwindow": {
        "label": "Paned Window", "is_container": True,
        "default_w": 320, "default_h": 220, "default_resize": "stretch_both",
        "prop_schema": {
            "orient": _schema(type="str", choices=["horizontal", "vertical"],
                              default="horizontal"),
        },
    },
    "freeform": {
        "label": "Freeform Area", "is_container": True,
        "default_w": 280, "default_h": 200, "default_resize": "stretch_both",
        # A freeform container's children use place() with relative coords, so
        # the region still scales (spec 6.7). It is the deliberate escape hatch
        # from grid inference, not a failure state.
        "prop_schema": {},
    },

    # ---- basic ------------------------------------------------------
    "label": {
        "label": "Label", "is_container": False,
        "default_w": 120, "default_h": 24, "default_resize": "fixed",
        "prop_schema": {
            "text": _schema(type="str", default=""),
            "anchor": _schema(type="str", choices=["w", "center", "e"],
                              default="w"),
            "wraplength": _schema(type="int", default=0),
        },
    },
    "button": {
        "label": "Button", "is_container": False,
        "default_w": 110, "default_h": 30, "default_resize": "fixed",
        "prop_schema": {
            "text": _schema(type="str", default=""),
            "command": _schema(type="handler", default=""),
            "state": _schema(type="str", choices=["normal", "disabled"],
                             default="normal"),
        },
    },
    "entry": {
        "label": "Entry", "is_container": False,
        "default_w": 180, "default_h": 26, "default_resize": "fixed",
        "prop_schema": {
            "placeholder": _schema(type="str", default=""),
            "show": _schema(type="str", default=""),      # "*" for a password
            "justify": _schema(type="str", choices=["left", "center", "right"],
                               default="left"),
        },
    },
    "text": {
        "label": "Text", "is_container": False,
        "default_w": 300, "default_h": 160, "default_resize": "stretch_both",
        "prop_schema": {
            "wrap": _schema(type="str", choices=["none", "char", "word"],
                            default="word"),
            "readonly": _schema(type="bool", default=False),
        },
    },
    "checkbutton": {
        "label": "Check Button", "is_container": False,
        "default_w": 140, "default_h": 24, "default_resize": "fixed",
        "prop_schema": {
            "text": _schema(type="str", default=""),
            "default": _schema(type="bool", default=False),
            "command": _schema(type="handler", default=""),
        },
    },
    "radiobutton": {
        "label": "Radio Button", "is_container": False,
        "default_w": 140, "default_h": 24, "default_resize": "fixed",
        "prop_schema": {
            "text": _schema(type="str", default=""),
            "group": _schema(type="str", default=""),
            "value": _schema(type="str", default=""),
            "command": _schema(type="handler", default=""),
        },
    },
    "combobox": {
        "label": "Combobox", "is_container": False,
        "default_w": 160, "default_h": 26, "default_resize": "fixed",
        "prop_schema": {
            "values": _schema(type="list[str]", default=[]),
            "readonly": _schema(type="bool", default=True),
            "command": _schema(type="handler", default=""),
        },
    },
    "listbox": {
        "label": "Listbox", "is_container": False,
        "default_w": 180, "default_h": 140, "default_resize": "stretch_both",
        "prop_schema": {
            "selectmode": _schema(type="str",
                                  choices=["browse", "single", "multiple",
                                           "extended"], default="browse"),
        },
    },
    "spinbox": {
        "label": "Spinbox", "is_container": False,
        "default_w": 110, "default_h": 26, "default_resize": "fixed",
        "prop_schema": {
            "from_": _schema(type="int", default=0),
            "to": _schema(type="int", default=100),
            "increment": _schema(type="int", default=1),
            "command": _schema(type="handler", default=""),
        },
    },
    "scale": {
        "label": "Scale", "is_container": False,
        "default_w": 200, "default_h": 30, "default_resize": "stretch_h",
        "prop_schema": {
            "orient": _schema(type="str", choices=["horizontal", "vertical"],
                              default="horizontal"),
            "from_": _schema(type="int", default=0),
            "to": _schema(type="int", default=100),
            "command": _schema(type="handler", default=""),
        },
    },
    "progressbar": {
        "label": "Progress Bar", "is_container": False,
        "default_w": 200, "default_h": 20, "default_resize": "stretch_h",
        "prop_schema": {
            "orient": _schema(type="str", choices=["horizontal", "vertical"],
                              default="horizontal"),
            "mode": _schema(type="str", choices=["determinate",
                                                 "indeterminate"],
                            default="determinate"),
        },
    },
    "separator": {
        "label": "Separator", "is_container": False,
        "default_w": 200, "default_h": 2, "default_resize": "stretch_h",
        "prop_schema": {
            "orient": _schema(type="str", choices=["horizontal", "vertical"],
                              default="horizontal"),
        },
    },

    # ---- data -------------------------------------------------------
    "treeview": {
        "label": "Table / Tree", "is_container": False,
        "default_w": 320, "default_h": 200, "default_resize": "stretch_both",
        "prop_schema": {
            "mode": _schema(type="str", choices=["table", "tree"],
                            default="table"),
            "columns": _schema(type="list[str]", default=[]),
            "show_headings": _schema(type="bool", default=True),
        },
    },

    # ---- composites (spec 5) ----------------------------------------
    # Each of these emits a real class in the generated ui/widgets.py, not an
    # inline blob. They are the difference between a toy and a usable tool.
    "image_canvas": {
        "label": "Image Canvas", "is_container": False,
        "default_w": 420, "default_h": 320, "default_resize": "stretch_both",
        "prop_schema": {
            "overlay": _schema(type="bool", default=False),
            "overlay_alpha": _schema(type="float", default=0.5),
            "colormap": _schema(type="str", default=""),
            "zoom_to_fit": _schema(type="bool", default=True),
        },
    },
    "chart_panel": {
        "label": "Chart Panel", "is_container": False,
        "default_w": 380, "default_h": 260, "default_resize": "stretch_both",
        "prop_schema": {
            "toolbar": _schema(type="bool", default=False),
            "tight_layout": _schema(type="bool", default=True),
        },
    },
    "scrubber": {
        "label": "Scrubber", "is_container": False,
        "default_w": 320, "default_h": 40, "default_resize": "stretch_h",
        "prop_schema": {
            "from_": _schema(type="int", default=0),
            "to": _schema(type="int", default=100),
            "show_total": _schema(type="bool", default=True),
            "command": _schema(type="handler", default=""),
        },
    },
    "log_pane": {
        "label": "Log Pane", "is_container": False,
        "default_w": 420, "default_h": 140, "default_resize": "stretch_both",
        "prop_schema": {
            "autoscroll": _schema(type="bool", default=True),
            "levels": _schema(type="list[str]",
                              default=["info", "warn", "error"]),
        },
    },
    "file_picker": {
        "label": "File Picker", "is_container": False,
        "default_w": 320, "default_h": 30, "default_resize": "stretch_h",
        "prop_schema": {
            "mode": _schema(type="str", choices=["file", "folder", "save"],
                            default="file"),
            "filetypes": _schema(type="list[str]", default=[]),
            "command": _schema(type="handler", default=""),
        },
    },
    "status_bar": {
        "label": "Status Bar", "is_container": False,
        "default_w": 480, "default_h": 24, "default_resize": "stretch_h",
        "prop_schema": {
            "progress": _schema(type="bool", default=False),
        },
    },
    "toolbar": {
        "label": "Toolbar", "is_container": False,
        "default_w": 480, "default_h": 36, "default_resize": "stretch_h",
        "prop_schema": {
            "buttons": _schema(type="list[str]", default=[]),
        },
    },

    # ---- menu -------------------------------------------------------
    "menubar": {
        "label": "Menu Bar", "is_container": False,
        "default_w": 480, "default_h": 24, "default_resize": "stretch_h",
        # A nested {menu: [items]} tree; validated structurally, not by key.
        "prop_schema": {"menus": _schema(type="tree", default=[])},
    },

    # ---- the untyped placeholder ------------------------------------
    GENERIC_KIND: {
        "label": "Generic (classify)", "is_container": False,
        "default_w": 160, "default_h": 60, "default_resize": "auto",
        "prop_schema": {},
    },
}


def is_container(kind: str) -> bool:
    """True when ``kind`` may own children.

    Reads CONTAINER_KINDS rather than PALETTE[kind]["is_container"] for an
    unknown kind, so a malformed .gspec cannot make a stray kind a parent."""
    return kind in CONTAINER_KINDS


def default_resize(kind: str) -> str:
    """The kind's natural resize behaviour, or "fixed" for an unknown kind.

    "fixed" is the safe default: a widget that does not stretch looks wrong but
    still renders, whereas wrongly stretching one can push every sibling out of
    the window."""
    entry = PALETTE.get(kind)
    if not entry:
        return "fixed"
    return str(entry.get("default_resize") or "fixed")


# ============================================================
# Shape
# ============================================================

@dataclass
class Shape:
    """One drawn rectangle. Spec 4.1.

    ``id`` is a uuid4 hex that survives edits, because the manifest's
    widget-name registry and the .gspec clarifications array both key off it —
    a shape that changed identity when it was moved would lose its answered
    questions and its stable widget name.
    """
    id: str
    kind: str
    x: int
    y: int
    w: int
    h: int
    label: str = ""
    note: str = ""
    resize: str = "auto"
    min_w: int = 0
    min_h: int = 0
    z: int = 0
    freeform: bool = False
    props: Dict[str, Any] = field(default_factory=dict)

    # -- geometry helpers used throughout gui_layout ------------------
    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def contains(self, other: "Shape", tol: int = 0) -> bool:
        """Is ``other`` fully inside this shape, allowing ``tol`` px of slop?

        Hand-drawn boxes miss by a pixel or two constantly, so containment that
        demanded exactness would report a child as a sibling and produce a flat
        layout from a nested drawing."""
        return (other.x >= self.x - tol and other.y >= self.y - tol
                and other.x2 <= self.x2 + tol and other.y2 <= self.y2 + tol)

    def overlaps(self, other: "Shape") -> bool:
        """Strict rectangle intersection — touching edges do not count."""
        return not (other.x >= self.x2 or other.x2 <= self.x
                    or other.y >= self.y2 or other.y2 <= self.y)


def new_shape(kind: str, x: int, y: int, **over: Any) -> Shape:
    """A shape of ``kind`` at ``(x, y)`` using the palette's defaults.

    Raises KeyError for a kind that is not in the catalogue — better here, at
    the point the caller named it, than as a missing template at emit time."""
    if kind not in PALETTE:
        raise KeyError(f"unknown widget kind: {kind!r}")
    entry = PALETTE[kind]
    s = Shape(
        id=uuid.uuid4().hex,
        kind=kind,
        x=int(x), y=int(y),
        w=int(entry["default_w"]), h=int(entry["default_h"]),
    )
    # Seed props with the schema's declared defaults so an emitted widget never
    # depends on a key the user never touched.
    for pname, pdef in (entry.get("prop_schema") or {}).items():
        if "default" in pdef:
            d = pdef["default"]
            s.props[pname] = list(d) if isinstance(d, list) else d
    for k, v in over.items():
        setattr(s, k, v)
    return s


# ============================================================
# Project + .gspec (spec 4.2)
# ============================================================

@dataclass
class Canvas:
    w: int = 1280
    h: int = 800
    grid_snap: int = 8


@dataclass
class Window:
    title: str = "Untitled"
    min_w: int = 900
    min_h: int = 600


@dataclass
class Clarification:
    """One answered question from the classification pass (spec 10.2).

    Persisted so the same question is never asked twice — an unanswered
    ``answer`` means it was skipped, which is different from never asked."""
    shape_id: str
    question: str
    answer: str = ""


@dataclass
class Project:
    project: str
    mode: str = "linked"                 # linked | standalone (spec 8)
    canvas: Canvas = field(default_factory=Canvas)
    window: Window = field(default_factory=Window)
    shapes: List[Shape] = field(default_factory=list)
    clarifications: List[Clarification] = field(default_factory=list)
    gspec_version: int = GSPEC_VERSION

    def shape_by_id(self, sid: str) -> Optional[Shape]:
        for s in self.shapes:
            if s.id == sid:
                return s
        return None


class GspecError(ValueError):
    """A .gspec that cannot be loaded, with a reason a user can act on."""


def _project_to_dict(p: Project) -> Dict[str, Any]:
    return {
        "gspec_version": p.gspec_version,
        "project": p.project,
        "mode": p.mode,
        "canvas": asdict(p.canvas),
        "window": asdict(p.window),
        "shapes": [asdict(s) for s in p.shapes],
        "clarifications": [asdict(c) for c in p.clarifications],
    }


def save_gspec(path: Any, project: Project) -> None:
    """Write ``project`` to ``path`` as .gspec JSON.

    sort_keys is deliberate: a byte-identical file for an unchanged project is
    what makes "save, reopen, save again" verifiable, and it keeps a .gspec
    diffable in version control."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_project_to_dict(project), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_gspec(path: Any) -> Project:
    """Read a .gspec. Raises GspecError with a reason, never a bare KeyError.

    An UNKNOWN FUTURE VERSION is refused rather than parsed optimistically: a
    newer build may carry kinds or fields this one would drop, and silently
    dropping part of a user's wireframe is worse than refusing to open it."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GspecError(f"no such .gspec: {p}")
    except json.JSONDecodeError as exc:
        raise GspecError(f"{p.name} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise GspecError(f"{p.name}: expected a JSON object at the top level")

    ver = raw.get("gspec_version")
    if not isinstance(ver, int):
        raise GspecError(f"{p.name}: missing gspec_version")
    if ver > GSPEC_VERSION:
        raise GspecError(
            f"{p.name} was written by a newer build (gspec_version {ver}; "
            f"this build understands {GSPEC_VERSION}). Update the app rather "
            f"than opening it here — loading it would silently drop anything "
            f"this version does not know about.")

    canvas_raw = raw.get("canvas") or {}
    window_raw = raw.get("window") or {}
    shapes: List[Shape] = []
    for i, sd in enumerate(raw.get("shapes") or []):
        if not isinstance(sd, dict):
            raise GspecError(f"{p.name}: shape #{i} is not an object")
        try:
            shapes.append(Shape(
                id=str(sd.get("id") or uuid.uuid4().hex),
                kind=str(sd.get("kind") or GENERIC_KIND),
                x=int(sd.get("x", 0)), y=int(sd.get("y", 0)),
                w=int(sd.get("w", 0)), h=int(sd.get("h", 0)),
                label=str(sd.get("label") or ""),
                note=str(sd.get("note") or ""),
                resize=str(sd.get("resize") or "auto"),
                min_w=int(sd.get("min_w", 0)), min_h=int(sd.get("min_h", 0)),
                z=int(sd.get("z", 0)),
                freeform=bool(sd.get("freeform", False)),
                props=dict(sd.get("props") or {}),
            ))
        except (TypeError, ValueError) as exc:
            raise GspecError(f"{p.name}: shape #{i} has a bad field: {exc}")

    clars = [
        Clarification(shape_id=str(c.get("shape_id") or ""),
                      question=str(c.get("question") or ""),
                      answer=str(c.get("answer") or ""))
        for c in (raw.get("clarifications") or []) if isinstance(c, dict)
    ]

    return Project(
        project=str(raw.get("project") or p.stem),
        mode=str(raw.get("mode") or "linked"),
        canvas=Canvas(w=int(canvas_raw.get("w", 1280)),
                      h=int(canvas_raw.get("h", 800)),
                      grid_snap=int(canvas_raw.get("grid_snap", 8))),
        window=Window(title=str(window_raw.get("title") or "Untitled"),
                      min_w=int(window_raw.get("min_w", 900)),
                      min_h=int(window_raw.get("min_h", 600))),
        shapes=shapes,
        clarifications=clars,
        gspec_version=ver,
    )
