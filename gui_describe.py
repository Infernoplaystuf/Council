"""
gui_describe.py — "Describe it": a plain-English GUI description -> a VALIDATED
wireframe (a list of gui_shapes.Shape), via an injected model call.

PURE. Stdlib plus the designer's own pure modules (gui_shapes, gui_snap,
gui_layout, gui_spec, gui_colors, gui_examples) and nx_generate's JSON scanner.
No PySide6, no tkinter, no council_engine, no role_models — a test AST-checks
that, because the moment this module reaches for a model loader or a toolkit it
stops being testable with a scripted stub.

WHY THE MODEL IS INJECTED
-------------------------
Same house style as gui_classify.classify and nx_generate.generate: the caller
owns model selection (which role, which GGUF, which lock), this module owns the
contract. Every path below — a valid reply, prose around a fence, a truncated
reply, a hallucinated kind, a model that raises — is exercised by a scripted
stub, and none of them loads anything.

WHY A VALIDATE/REPAIR LOOP AND NOT ONE-SHOT
-------------------------------------------
Asked to design a GUI, a local model produces JSON that is syntactically fine
and structurally wrong (measured and recorded in gui_examples and gui_snap: two
full-canvas containers stacked into a blank app, one of three identical rows
captured by a frame, an image panel sized to exactly fill its own parent, the
requested background colour dropped). So the reply is checked in layers, and
EVERY fault a layer finds is collected before anything goes back to the model —
a repair round is a full generation, and spending one per fault is how three
attempts run out on a wireframe with four problems:

  1. parse       nx_generate.extract_json — fences, prose, braces in strings
  2. schema      closed catalogue, closed prop keys, prop TYPES, #rrggbb colours
  3. tidy        gui_snap.snap — DETERMINISTIC repair, no model round spent
  4. gate        gui_layout.infer -> gui_spec.build -> gui_spec.validate, the
                 exact pipeline council_core.designer_project.generate runs, so
                 nothing is accepted here that Generate would then refuse

WHY A TYPE CHECK THAT NEITHER EXISTING VALIDATOR HAS
----------------------------------------------------
gui_classify.validate_answer and gui_spec.validate both check prop KEYS and
CHOICES but not types. A model writing "values": "Low,High" on a combobox
passes both, and gui_emit_qt then runs list(_prop(w, "values", [])) and fills
the dropdown with the characters L, o, w, ",", H, ... — a GUI that looks built
and is nonsense. Every prop is checked against its prop_schema "type" here.

WHY COLOUR NAMES ARE REJECTED RATHER THAN IGNORED
-------------------------------------------------
gui_colors has a closed #rgb/#rrggbb grammar, and both of its consumers swallow
anything else: resolve_scene's own() and gui_spec.build's root_bg both catch the
ValueError and fall back to "". So "bg": "pink" produces an uncoloured app with
no message anywhere. It is a fault here, with the hex to use instead.

WHY id / script / drives / port / requires ARE REFUSED
------------------------------------------------------
Shape ids are the app's (the manifest's widget-name registry keys off them); a
model-invented id collides with that registry. script/drives/port/requires are
kept OUT of props in gui_shapes precisely so that a model-authored import
target, sequence link, binding or package allowlist is structurally impossible.
They are ignored with a note, never applied — and the worked example has them
stripped, so the model is not taught to write them.
"""
from __future__ import annotations

import difflib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gui_colors
import gui_examples
import gui_layout
import gui_snap
import gui_spec
from gui_shapes import CONTAINER_KINDS, GENERIC_KIND, PALETTE, Shape, new_shape

try:                                    # the balanced-brace scanner, reused
    from nx_generate import extract_json as _nx_extract_json
except Exception:                       # pragma: no cover - always present
    _nx_extract_json = None


# ============================================================
# Constants
# ============================================================

# The designer canvas this module designs for. Smaller than the .gspec default
# (1280 x 800) on purpose: it is the size of the Qt designer's drawing area, so
# a described wireframe opens without scrolling.
CANVAS_W = 1100
CANVAS_H = 700

GRID = gui_snap.GRID            # 8 — the lattice the DesignerCanvas snaps to
EDGE_MARGIN = gui_snap.MARGIN   # 8 — kept clear at the canvas edge
MIN_SIZE = gui_snap.MIN_SIZE    # 8 — below this a widget cannot be seen
CHILD_INSET = 16                # what the prompt ASKS for inside a container

# The whole catalogue a model may use: every palette kind except the untyped
# placeholder. PALETTE order is kept (containers first) because that is also
# the order that reads best in the prompt.
KINDS: Tuple[str, ...] = tuple(k for k in PALETTE if k != GENERIC_KIND)

MAX_SHAPES = 60                 # a sanity cap, not a design limit
MAX_FAULTS = 15                 # bullets per repair prompt; the rest summarised
FULL_CANVAS = 0.95              # "covers the whole window"
TOOLKITS = ("qt", "tk")

# Prompt budget. A 4096-token context has to hold the prompt AND the reply.
# REPLY_TOKENS is what designer_project actually asks for (DESCRIBE_NUM_PREDICT
# — a test pins the two together): reserving less here than the caller
# requests let council_engine's clamp pass a prompt whose reply llama_cpp then
# quietly cut short. 256 of slack is for the chat template.
# CHARS_PER_TOKEN is MEASURED, not guessed: this module's own prompt is 3.05
# chars/token under both Phi-4's and Llama 3.2's tokenizers (5094 chars ->
# 1671 tokens), and a real Phi-4 reply 2.8. 2.9 sits under the prompt's figure
# so the estimate errs toward shedding rather than overflowing.
N_CTX = 4096
REPLY_TOKENS = 1800
SLACK_TOKENS = 256
CHARS_PER_TOKEN = 2.9
DEFAULT_BUDGET_CHARS = int((N_CTX - REPLY_TOKENS - SLACK_TOKENS)
                           * CHARS_PER_TOKEN)                     # 5916

# Bounds on numbers a reply may carry. Geometry past this is not a window, and
# an int prop past int32 dies in QSpinBox.setRange; an integer literal of 309
# digits raised OverflowError out of float() and ended describe() early.
MAX_PIXELS = 100_000
INT32 = (-2 ** 31, 2 ** 31 - 1)
MAX_FONT_SIZE = 200
MAX_FAULT_CHARS = 240           # one fault echoes model values; keep it short

# The worked example. image_viewer and not PROMPT_EXAMPLES' barbie_capture_v2:
# it is half the size, all root-level (no nesting to rescale), and it still
# shows labels ABOVE their inputs, a folder picker, an image panel with a
# scrubber under it. Its pink and its Magneto font are NOT shown: see
# EXAMPLE_UNSTYLED.
EXAMPLE_NAME = "image_viewer"
#: Keys the worked example never shows. See example_wireframe.
EXAMPLE_UNSTYLED = frozenset({"bg", "fg", "font"})

# Shape fields a reply may set.
SHAPE_KEYS = ("kind", "label", "x", "y", "w", "h", "props", "bg", "fg", "font")
WINDOW_KEYS = ("title", "bg", "fg", "font")

# Never applied, whatever the model says. See the module docstring.
REFUSED_KEYS: Dict[str, str] = {
    "id": "the app assigns ids",
    "script": "script links are added by the user in the designer",
    "drives": "sequence links are added by the user in the designer",
    "port": "ports are derived from the label, or set by the user",
    "requires": "packages are declared by the user in the designer",
}

# .gspec bookkeeping a model may copy from an example it has seen. Harmless and
# meaningless here, so ignored with a note rather than spent a repair round on.
IGNORED_KEYS = frozenset({"note", "resize", "min_w", "min_h", "z", "freeform"})

# Only for the fix HINT on a rejected colour name — never applied. Applying it
# would make the closed grammar open again one name at a time.
_NAMED_HINT = {
    "pink": "#ffc0cb", "hot pink": "#ff69b4", "red": "#ff0000",
    "green": "#008000", "blue": "#0000ff", "white": "#ffffff",
    "black": "#000000", "grey": "#808080", "gray": "#808080",
    "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080",
}

# Every prop_schema "type" the type check below understands. A test pins that
# the catalogue uses no other — a new type would otherwise pass unchecked.
KNOWN_PROP_TYPES = frozenset({"str", "int", "float", "bool", "list[str]",
                              "tree", "handler"})

# Stages a reply reaches. Higher is better; the best-so-far candidate is the
# one with the highest (stage, -faults).
STAGE_NO_JSON = 0
STAGE_SCHEMA = 1
STAGE_GATE = 2
STAGE_OK = 3


# ============================================================
# Result types
# ============================================================

@dataclass
class DescribeResult:
    """What describe() hands the UI.

    ``shapes`` and ``window`` are EMPTY unless ``ok`` — an invalid wireframe
    is never handed back, because the designer would draw it and the user
    would reasonably assume it had been checked. ``window`` is then
    {"title", "bg", "fg", "font"}, "" meaning unset. ``raw`` is the model
    reply the ``errors`` describe (the best attempt's), for the log."""
    ok: bool = False
    shapes: List[Shape] = field(default_factory=list)
    window: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    attempts: int = 0
    raw: str = ""


@dataclass
class Checked:
    """One reply, taken through every validation layer it could reach."""
    stage: int
    faults: List[str] = field(default_factory=list)
    payload: Any = None
    raw: str = ""
    shapes: List[Shape] = field(default_factory=list)
    window: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    #: Rows that passed the schema, even when others did not.
    accepted: int = 0

    @property
    def ok(self) -> bool:
        return self.stage == STAGE_OK and not self.faults

    def rank(self) -> Tuple[int, int, int]:
        # Stage first: an unparseable reply has ONE fault and is still worse
        # than a parsed one with five, so counting faults alone would let
        # "Sorry, I can't" replace a nearly-right wireframe.
        # Then whether ANY row survived. The structural refusals — no
        # "shapes", an empty list, too many — are each exactly one fault, so
        # by fault count alone "{}" outranked a five-shape answer with two
        # bad rows and became the next repair prompt's starting point.
        has_rows = 1 if (self.stage > STAGE_SCHEMA or self.accepted) else 0
        return (self.stage, has_rows, -len(self.faults))


# ============================================================
# Prompt
# ============================================================

def estimate_tokens(text: str) -> int:
    """A conservative token estimate (see CHARS_PER_TOKEN)."""
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


_ROLE = ("You design GUI wireframes for a desktop app designer. You turn a "
         "plain-English request into rectangles on a canvas, each one a real "
         "widget from a fixed catalogue.")

# Short on purpose. gui_examples records that a list of prohibitions did NOT
# fix the model's layouts — the model followed each rule's letter and produced
# the same shapes — so the rules only name the geometry the gate enforces and
# the worked example carries the rest.
_RULES = f"""RULES
- A child lies fully inside its container, at least {CHILD_INSET} px from each of its edges.
- Siblings never overlap. Leave at least {GRID} px between neighbours.
- At most one container may cover the whole window.
- Put every action on a button.
- A container holds at least 2 children, or none.
- notebook: props.tabs has one title per page. Draw each page (usually a frame) side by side inside the notebook, left to right in tab order.
- A widget's caption goes in "label"."""


def _uncolourable() -> List[str]:
    return [k for k in KINDS if not gui_colors.caps(k)]


# describe's OWN declaration help: the subset of gui_examples.DECLARATION_HELP
# this module accepts. That text also teaches port/script/drives/requires, all
# of which are refused here — sending it would teach the model to spend its
# reply on keys that are then thrown away. Shed first when over budget.
DECLARATION_HELP = (
    "OPTIONAL STYLE\n"
    '- "bg" / "fg" on the window or on a shape: "#rrggbb" only; colour names '
    'such as "pink" are rejected. A shape with no bg takes its container\'s, '
    "which is how a label reads as transparent. "
    + ", ".join(_uncolourable()) + " cannot take colour.\n"
    '- "font" on the window or on a text widget: "<family> <size>", '
    'optionally followed by bold or italic, e.g. "Arial 12 bold".')


def _type_text(pdef: Dict[str, Any]) -> str:
    """One prop's type as the model should read it: the allowed values when
    the schema has choices, else the JSON form the type check accepts."""
    choices = pdef.get("choices")
    if choices:
        return "|".join(str(c) for c in choices)
    t = pdef.get("type")
    return {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "true|false",
        "list[str]": '["a", "b"]',
        "tree": '[{"title": "File", "items": ["Open", "-", "Quit"]}]',
    }.get(str(t), str(t))


#: What a kind IS, for the names that do not say. MEASURED on Phi-4 Q4: asked
#: for "a labelled group called 'Rating'" and "two labelled panels", it drew a
#: plain frame plus a label every time (two runs, both prompts) — the
#: catalogue said only "labelframe [container] — props: text, labelanchor".
#: Kept to the ambiguous names; "button" needs no gloss, and every word here
#: is paid for in the same context the reply needs.
KIND_HINTS = {
    "labelframe": "a bordered group with a caption (its label) on the border",
    "panedwindow": "two or more panes split by a draggable divider",
    "freeform": "an area whose children keep their exact drawn positions",
    "scrubber": "a slider with a number box, for stepping through frames",
    "chart_panel": "a plot area",
    "log_pane": "a scrolling read-only log",
    "file_picker": "a path box with a Browse button",
    "treeview": "a table (props.columns) or a tree",
}


def _catalogue(detail: bool) -> str:
    """The closed vocabulary. Always EVERY kind — only the prop detail sheds.

    Handler-typed props ("command") are never listed: generation wires every
    callback itself as on_<name>, and nothing downstream reads the prop."""
    lines = []
    for k in KINDS:
        tag = " [container]" if k in CONTAINER_KINDS else ""
        if k in KIND_HINTS:
            tag += f" = {KIND_HINTS[k]}"
        if not detail:
            lines.append(f"- {k}{tag}")
            continue
        schema = PALETTE[k].get("prop_schema") or {}
        props = [f"{p} ({_type_text(d)})" for p, d in schema.items()
                 if d.get("type") != "handler"]
        lines.append(f"- {k}{tag}" + (" — props: " + ", ".join(props)
                                      if props else ""))
    head = "You may ONLY use these widget kinds, spelled exactly as written:"
    return "WIDGETS\n" + head + "\n" + "\n".join(lines)


def _q(v: float) -> int:
    return int(round(float(v) / GRID) * GRID)


def example_wireframe(canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H
                      ) -> Optional[Dict[str, Any]]:
    """The worked example, in gui_examples._compact form, RESCALED to this
    canvas, with every refused key stripped. None if it is not on disk.

    Rescaled by EDGES, not by origin-and-size. Scaling y and h separately and
    snapping each rounds them independently, and on this example it turned
    "Exposure (ms)" at 136..160 and its spinbox at 152..184 into an overlap
    that the source did not have. Snapping both edges and taking the
    difference is monotone, so shapes that did not overlap still do not."""
    try:
        raw = gui_examples.load(EXAMPLE_NAME)
    except Exception:
        return None
    canvas = raw.get("canvas") or {}
    sx = float(canvas_w) / float(canvas.get("w") or 1280)
    sy = float(canvas_h) / float(canvas.get("h") or 800)
    compact = gui_examples._compact(raw)
    win_in = compact.get("window") or {}
    # Structure only — no colour and no font. The example is a Barbie capture
    # tool, pink with a Magneto face, and a small model copies what it is
    # shown: left in, every described window would come back pink.
    window = {k: win_in[k] for k in WINDOW_KEYS
              if win_in.get(k) and k not in EXAMPLE_UNSTYLED}
    shapes = []
    for s in compact.get("shapes") or []:
        row = {k: v for k, v in s.items()
               if k not in REFUSED_KEYS and k not in EXAMPLE_UNSTYLED}
        x1, x2 = _q(s["x"] * sx), _q((s["x"] + s["w"]) * sx)
        y1, y2 = _q(s["y"] * sy), _q((s["y"] + s["h"]) * sy)
        row["x"], row["w"] = x1, max(GRID, x2 - x1)
        row["y"], row["h"] = y1, max(GRID, y2 - y1)
        shapes.append(row)
    return {"window": window, "shapes": shapes}


def render_wireframe(payload: Any) -> str:
    """One shape per line. Half the characters of indent=2 and still easy for
    a model to read — which matters twice: in the example, and when a model is
    shown its own rejected answer under the same budget."""
    if not isinstance(payload, dict) or not isinstance(payload.get("shapes"),
                                                       list):
        return json.dumps(payload, ensure_ascii=False)
    head = [f' {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},'
            for k, v in payload.items() if k != "shapes"]
    rows = payload["shapes"]
    body = [f"  {json.dumps(r, ensure_ascii=False)}"
            + ("," if i < len(rows) - 1 else "") for i, r in enumerate(rows)]
    return "\n".join(["{"] + head + [' "shapes": ['] + body + [" ]", "}"])


def _example_block(canvas_w: int, canvas_h: int) -> str:
    ex = example_wireframe(canvas_w, canvas_h)
    if not ex:
        return ""
    return ("EXAMPLE — a correct wireframe on this canvas, for a different "
            "request. Copy its structure and spacing, not its content.\n"
            + render_wireframe(ex))


_SKELETON = """Reply with ONLY a JSON object in this exact shape. No prose, no code fence:
{"window": {"title": "<window title>", "bg": "#rrggbb"},
 "shapes": [
  {"kind": "<a kind from the list>", "label": "<caption>", "x": 0, "y": 0, "w": 0, "h": 0, "props": {}}
 ]}
"bg" and "props" are optional."""


def _compose(text: str, canvas_w: int, canvas_h: int, *, help_on: bool,
             example: str, detail: bool) -> str:
    canvas = (f"CANVAS\n"
              f"- {canvas_w} x {canvas_h} px. The origin (0, 0) is the "
              f"top-left corner; x grows right, y grows down.\n"
              f"- Every x, y, w and h is an integer multiple of {GRID}.\n"
              f"- Keep every shape at least {EDGE_MARGIN} px from the canvas "
              f"edges.")
    parts = [_ROLE, canvas, _catalogue(detail), _RULES]
    if help_on:
        parts.append(DECLARATION_HELP)
    if example:
        parts.append(example)
    # The request goes in VERBATIM — it is the user's own words, and it is
    # never what gets shed or cut.
    parts.append("REQUEST\n" + text)
    parts.append(_SKELETON)
    return "\n\n".join(parts)


def _assemble(text: str, *, canvas_w: int, canvas_h: int,
              budget_chars: Optional[int]) -> Tuple[str, List[str]]:
    """(prompt, what was shed to fit).

    Shed WHOLE SECTIONS in a fixed order — declaration help, then the example,
    then per-kind prop detail — and never cut text at a character count. A
    hard cap slicing the prompt would stop the catalogue mid-list, and a model
    that sees half the kinds treats the missing half as forbidden. If even the
    leanest prompt is over budget it is still sent whole; the caller notes it.
    """
    budget = DEFAULT_BUDGET_CHARS if budget_chars is None else int(budget_chars)
    example = _example_block(canvas_w, canvas_h)
    plan = [
        (True, example, True, []),
        (False, example, True, ["style help"]),
        (False, "", True, ["style help", "worked example"]),
        (False, "", False, ["style help", "worked example", "prop detail"]),
    ]
    prompt, shed = "", []
    for help_on, ex, detail, shed in plan:
        prompt = _compose(text, canvas_w, canvas_h, help_on=help_on,
                          example=ex, detail=detail)
        if len(prompt) <= budget:
            break
    if not example and "worked example" not in shed:
        shed = shed + ["worked example (not on disk)"]
    return prompt, shed


def build_prompt(text: str, *, canvas_w: int = CANVAS_W,
                 canvas_h: int = CANVAS_H, toolkit: str = "qt",
                 budget_chars: Optional[int] = None) -> str:
    """The first request for ``text``.

    ``toolkit`` does not change a word of it, deliberately: a .gspec is
    toolkit-neutral (gui_emit_qt parses the same Tk-format font string, and
    gui_colors.COLOUR_CAPS gates both emitters), so a prompt that named either
    toolkit would steer the model toward widgets the other target lacks."""
    return _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                     budget_chars=budget_chars)[0]


def _bullets(errors: Sequence[str], limit: int = MAX_FAULTS) -> str:
    def short(e: str) -> str:
        # A fault echoes the model's own values (a label, a prop), and one
        # 5000-character label used to make a single bullet bigger than the
        # whole budget.
        e = str(e)
        return e if len(e) <= MAX_FAULT_CHARS else e[:MAX_FAULT_CHARS - 1] + "…"
    shown = [f"- {short(e)}" for e in list(errors)[:limit]]
    more = len(errors) - limit
    if more > 0:
        shown.append(f"- ...and {more} more problem(s) of the same kinds")
    return "\n".join(shown)


def _shown(bad: Any, raw: str, cap: int = 3000) -> str:
    """What the model returned, capped. Cut at a LINE so the model never sees
    a shape sliced mid-object and "repairs" the slice."""
    if not isinstance(bad, dict):
        return (raw or "")[:min(1000, cap)]
    text = render_wireframe(bad)
    if len(text) <= cap:
        return text
    lines, used = [], 0
    for ln in text.splitlines():
        if used + len(ln) + 1 > cap - 100:
            break
        lines.append(ln)
        used += len(ln) + 1
    hidden = len(text.splitlines()) - len(lines)
    return "\n".join(lines) + f"\n  ... ({hidden} more line(s) not shown)"


#: (what-you-returned cap, bullets) tried in order until a repair prompt fits.
_REPAIR_PLANS = ((3000, MAX_FAULTS), (1800, MAX_FAULTS), (1000, 8), (600, 4))


def repair_prompt(text: str, bad: Any, errors: Sequence[str], *,
                  raw: str = "", canvas_w: int = CANVAS_W,
                  canvas_h: int = CANVAS_H,
                  budget_chars: Optional[int] = None) -> str:
    """One repair round: the model's own answer, every fault, then the request.

    Same shape as gui_classify.repair_prompt and nx_generate.repair_prompt —
    showing a model what it returned beside what is wrong repairs far more
    reliably than restating the request it already misread. The original
    prompt is re-budgeted to what is LEFT after the header, so a repair round
    fits the same context the first round did.

    The HEADER is bounded too: what the model returned, then the fault list,
    are shrunk step by step until the whole prompt fits. A header nobody
    bounded used to push repair rounds over budget silently — the first
    prompt was checked, the repairs never were."""
    tail = "\n\nFix every point above. Reply with ONLY the corrected JSON object."
    budget = DEFAULT_BUDGET_CHARS if budget_chars is None else int(budget_chars)
    prompt = ""
    for cap, bullets in _REPAIR_PLANS:
        head = ("Your previous answer was rejected.\n\n"
                "WHAT YOU RETURNED\n" + _shown(bad, raw, cap) + "\n\n"
                "WHAT IS WRONG (shapes are numbered from 1, in the order you "
                "listed them)\n" + _bullets(errors, bullets) + "\n\n")
        body, _ = _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                            budget_chars=budget - len(head) - len(tail))
        prompt = head + body + tail
        if len(prompt) <= budget:
            break
    return prompt


# ============================================================
# Parse
# ============================================================

def _extract_json(text: str) -> Any:
    """nx_generate's balanced-brace scanner (fences, prose, braces inside
    strings); a plain json.loads only if that module is unavailable."""
    if _nx_extract_json is not None:
        try:
            return _nx_extract_json(text)
        except Exception:
            return None
    try:
        return json.loads(str(text).strip())
    except Exception:
        return None


def _looks_cut_off(text: str) -> bool:
    """True when an object opens and its braces never close — the reply hit
    the token limit, which needs a different fix (fewer shapes) than bad
    JSON does."""
    start = text.find("{")
    if start < 0:
        return False
    depth, in_str, esc = 0, False, False
    for ch in text[start:]:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth > 0 or in_str


def _no_json_fault(raw: str) -> str:
    base = "the reply contained no JSON object"
    if _looks_cut_off(raw):
        return (base + " — it stops before the object is closed, as if cut "
                "off; reply with the whole object, using fewer shapes if needed")
    if "{" in raw:
        return (base + " that parses — use double quotes, no trailing commas "
                "and no comments")
    return base


# ============================================================
# Schema
# ============================================================

def _where(i: int, kind: Any = "", label: Any = "") -> str:
    """'shape 4 (button "Run")' — names a fault by POSITION, because the model
    wrote no ids and position is the one handle it can find its shape by."""
    k = kind if isinstance(kind, str) and kind else "?"
    lab = label.strip() if isinstance(label, str) else ""
    if len(lab) > 30:
        lab = lab[:29] + "…"
    return f'shape {i + 1} ({k} "{lab}")' if lab else f"shape {i + 1} ({k})"


def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "a list"
    if isinstance(v, dict):
        return "an object"
    return type(v).__name__


def _as_int(v: Any) -> Optional[int]:
    """x/y/w/h as an int, or None. bool is refused although Python calls it an
    int: "x": true is a model error, not the pixel 1."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        # Checked BEFORE anything calls float() on it: a 309-digit integer
        # raised OverflowError there and ended describe() mid-loop.
        return v if abs(v) <= MAX_PIXELS else None
    if isinstance(v, float):
        return (int(round(v)) if math.isfinite(v) and abs(v) <= MAX_PIXELS
                else None)
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except (ValueError, OverflowError):
            return None
        return (int(round(f)) if math.isfinite(f) and abs(f) <= MAX_PIXELS
                else None)
    return None


def _encodable(s: str) -> bool:
    """False for text UTF-8 cannot hold. json.loads turns "\\udc00" into a
    lone surrogate; accepted, it made Generate's file write raise and would
    make the tokenizer of the next repair round raise too."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _deep_encodable(v: Any) -> bool:
    if isinstance(v, str):
        return _encodable(v)
    if isinstance(v, (list, tuple)):
        return all(_deep_encodable(x) for x in v)
    if isinstance(v, dict):
        return all(_deep_encodable(k) and _deep_encodable(x)
                   for k, x in v.items())
    return True


def _font_size_problem(font: str) -> str:
    """Why a font string's size is unusable, or "". Tk font strings put the
    size anywhere after the family ("Arial 12 bold"); a size of 0 or an
    eleven-digit one generated and then killed the app in QFont.setPointSize."""
    for token in font.replace("{", " ").replace("}", " ").split():
        if token.lstrip("-").isdigit():
            size = abs(int(token))
            if size == 0 or size > MAX_FONT_SIZE:
                return f"font size {token} is out of range (1-{MAX_FONT_SIZE})"
    return ""


def _coerce(pdef: Dict[str, Any], v: Any) -> Tuple[bool, Any, str]:
    """(ok, value, why) for one prop value against its declared type.

    Only LOSSLESS coercions: an integral float for an int, an int for a float,
    a number inside a list of strings. Anything that would change what the
    user sees is a fault with the fix spelled out."""
    t = str(pdef.get("type") or "")
    if not _deep_encodable(v):
        return False, None, "contains characters that are not text"
    if t == "str":
        if isinstance(v, str):
            return True, v, ""
        return False, None, f"must be text, got {_jtype(v)}"
    if t == "int":
        lo, hi = INT32
        if isinstance(v, float) and math.isfinite(v) and v.is_integer() \
                and lo <= v <= hi:
            v = int(v)
        if isinstance(v, int) and not isinstance(v, bool):
            if not lo <= v <= hi:
                # QSpinBox/QSlider hold 32-bit ints: a byte count as a
                # spinbox "to" generated, then died in setRange at startup.
                return False, None, (f"{str(v)[:24]} is out of range — a "
                                     f"whole number from {lo} to {hi}")
            return True, v, ""
        return False, None, (f"must be a whole number, got {_jtype(v)} "
                             f"{str(v)[:40]!r}")
    if t == "float":
        if isinstance(v, int) and not isinstance(v, bool) \
                and abs(v) > INT32[1]:
            return False, None, f"{str(v)[:24]} is out of range"
        if isinstance(v, (int, float)) and not isinstance(v, bool) \
                and math.isfinite(float(v)):
            return True, float(v), ""
        return False, None, f"must be a number, got {_jtype(v)} {str(v)[:40]!r}"
    if t == "bool":
        if isinstance(v, bool):
            return True, v, ""
        return False, None, f"must be true or false, got {_jtype(v)} {v!r}"
    if t == "list[str]":
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            eg = json.dumps(parts or ["a", "b"], ensure_ascii=False)
            return False, None, (f"must be a JSON list of strings such as {eg}"
                                 f" — the single string {v!r} would be split "
                                 f"into its characters")
        if not isinstance(v, list):
            return False, None, (f"must be a JSON list of strings, got "
                                 f"{_jtype(v)}")
        out = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(str(item))
            else:
                return False, None, (f"must be a list of strings; it contains "
                                     f"{_jtype(item)}")
        return True, out, ""
    if t == "tree":
        shape_hint = ('[{"title": "File", "items": ["Open", "-", "Quit"]}]')
        if not isinstance(v, list):
            return False, None, f"must be a list like {shape_hint}"
        out = []
        for menu in v:
            items = menu.get("items") if isinstance(menu, dict) else None
            title = menu.get("title") if isinstance(menu, dict) else None
            if not isinstance(title, str) or not isinstance(items, list) \
                    or not all(isinstance(x, str) for x in items):
                return False, None, (f"must be a list of menus like "
                                     f"{shape_hint}")
            out.append({"title": title, "items": list(items)})
        return True, out, ""
    # A type this check does not know. KNOWN_PROP_TYPES is pinned by a test,
    # so reaching here means the catalogue grew — accept rather than block.
    return True, v, ""


def _check_colour(value: Any, where: str, channel: str, kind: Optional[str],
                  faults: List[str], notes: List[str]) -> str:
    if value is None or value == "":
        return ""
    s = value.strip() if isinstance(value, str) else None
    if s is None or not s or not gui_colors.is_colour(s):
        hint = _NAMED_HINT.get(str(value).strip().lower())
        faults.append(f'{where}: {channel} {value!r} is not a colour — use '
                      f'"#rrggbb"' + (f', e.g. "{hint}" for {value}'
                                      if hint else ""))
        return ""
    colour = gui_colors.normalise(s)
    if kind is not None and not gui_colors.can_colour(kind, channel):
        reason = gui_colors.note(kind)
        notes.append(f"{where}: {channel} {colour} dropped — a {kind} cannot "
                     f"take a {channel} colour" + (f" ({reason})" if reason
                                                   else ""))
        return ""
    return colour


def _check_font(value: Any, where: str, kind: Optional[str],
                faults: List[str], notes: List[str]) -> str:
    if value is None or value == "":
        return ""
    if (not isinstance(value, str) or len(value.strip()) > 64
            or not _encodable(value)):
        faults.append(f'{where}: font must be text like "Arial 12 bold", got '
                      f"{_jtype(value)}")
        return ""
    size = _font_size_problem(value)
    if size:
        faults.append(f"{where}: {size}")
        return ""
    if kind is not None and not gui_colors.can_font(kind):
        notes.append(f"{where}: font dropped — a {kind} shows no text of its "
                     f"own")
        return ""
    return value.strip()


def _check_props(kind: str, props: Any, where: str, faults: List[str],
                 notes: List[str]) -> Dict[str, Any]:
    """The kind's defaults, overlaid with every VALID prop from the reply."""
    schema = PALETTE[kind].get("prop_schema") or {}
    merged: Dict[str, Any] = {}
    for p, d in schema.items():
        if "default" in d:
            dv = d["default"]
            merged[p] = list(dv) if isinstance(dv, list) else dv
    if props is None:
        return merged
    if not isinstance(props, dict):
        faults.append(f'{where}: "props" must be an object, got {_jtype(props)}')
        return merged
    allowed = sorted(p for p, d in schema.items() if d.get("type") != "handler")
    for pk, pv in props.items():
        if pk not in schema:
            faults.append(f'{where}: {kind} has no property "{pk}" (allowed: '
                          f'{", ".join(allowed) or "none"})')
            continue
        pdef = schema[pk]
        if pdef.get("type") == "handler":
            notes.append(f"{where}: ignored props.{pk} — generation wires "
                         f"every callback itself, as on_<name>")
            continue
        if pv is None:
            continue                     # "unset": keep the default
        ok, value, why = _coerce(pdef, pv)
        if not ok:
            faults.append(f"{where}: props.{pk} {why}")
            continue
        choices = pdef.get("choices")
        if choices and value not in choices:
            faults.append(f"{where}: props.{pk}={value!r} must be one of "
                          f"{', '.join(map(str, choices))}")
            continue
        merged[pk] = value
    return merged


def _check_row(i: int, row: Any, faults: List[str], notes: List[str]
               ) -> Tuple[Optional[Shape], str]:
    """One reply row -> (Shape or None, its position name)."""
    if not isinstance(row, dict):
        where = f"shape {i + 1}"
        faults.append(f"{where} is not an object, got {_jtype(row)}")
        return None, where
    before = len(faults)
    kind, label = row.get("kind"), row.get("label")
    where = _where(i, kind, label)

    if label is None:
        label = ""
    elif isinstance(label, (int, float)) and not isinstance(label, bool):
        label = str(label)
    elif not isinstance(label, str):
        faults.append(f'{where}: "label" must be text, got {_jtype(label)}')
        label = ""
    elif not _encodable(label):
        faults.append(f'{where}: "label" contains characters that are not '
                      f"text")
        label = ""

    if not isinstance(kind, str) or not kind:
        faults.append(f'{where}: no "kind" — every shape needs one of the '
                      f"listed kinds")
    elif kind == GENERIC_KIND:
        faults.append(f'{where}: "generic" is a placeholder, not a widget — '
                      f"pick one of the listed kinds")
    elif kind not in PALETTE:
        near = difflib.get_close_matches(kind, KINDS, n=1, cutoff=0.6)
        faults.append(f"{where}: {kind!r} is not a widget kind — use one of "
                      f"the listed kinds"
                      + (f' (did you mean "{near[0]}"?)' if near else ""))

    for k in row:
        if k in SHAPE_KEYS:
            continue
        if k in REFUSED_KEYS:
            notes.append(f'{where}: ignored "{k}" — {REFUSED_KEYS[k]}')
        elif k in IGNORED_KEYS:
            notes.append(f'{where}: ignored "{k}"')
        else:
            faults.append(f'{where}: unknown field "{k}" — a shape has only '
                          + ", ".join(SHAPE_KEYS))

    geo: Dict[str, int] = {}
    for k in ("x", "y", "w", "h"):
        v = _as_int(row.get(k))
        if v is None:
            faults.append(f'{where}: "{k}" must be a whole number of pixels, '
                          f"got {row.get(k)!r}")
        else:
            geo[k] = v
    for k in ("w", "h"):
        if k in geo and geo[k] < MIN_SIZE:
            faults.append(f"{where}: {k}={geo[k]} is too small — at least "
                          f"{MIN_SIZE}")

    kind_ok = isinstance(kind, str) and kind in PALETTE and kind != GENERIC_KIND
    props = (_check_props(kind, row.get("props"), where, faults, notes)
             if kind_ok else {})
    bg = _check_colour(row.get("bg"), where, "bg",
                       kind if kind_ok else None, faults, notes)
    fg = _check_colour(row.get("fg"), where, "fg",
                       kind if kind_ok else None, faults, notes)
    font = _check_font(row.get("font"), where, kind if kind_ok else None,
                       faults, notes)

    if len(faults) != before or not kind_ok or len(geo) != 4:
        return None, where
    # new_shape: a FRESH uuid4 id, whatever the reply said.
    return new_shape(kind, geo["x"], geo["y"], w=geo["w"], h=geo["h"],
                     label=label, props=props, bg=bg, fg=fg,
                     font=font), where


def _check_window(win: Any, faults: List[str], notes: List[str]
                  ) -> Dict[str, Any]:
    out = {"title": "Untitled", "bg": "", "fg": "", "font": ""}
    if win is None:
        notes.append('no "window" in the reply; titled "Untitled"')
        return out
    if not isinstance(win, dict):
        faults.append(f'"window" must be an object like {{"title": "My app"}},'
                      f" got {_jtype(win)}")
        return out
    title = win.get("title")
    if title is None or (isinstance(title, str) and not title.strip()):
        notes.append('the window has no title; titled "Untitled"')
    elif isinstance(title, str) and not _encodable(title):
        faults.append('window "title" contains characters that are not text')
    elif isinstance(title, (str, int, float)) and not isinstance(title, bool):
        out["title"] = str(title).strip()[:120]
    else:
        faults.append(f'window "title" must be text, got {_jtype(title)}')
    out["bg"] = _check_colour(win.get("bg"), "window", "bg", None, faults,
                              notes)
    out["fg"] = _check_colour(win.get("fg"), "window", "fg", None, faults,
                              notes)
    out["font"] = _check_font(win.get("font"), "window", None, faults, notes)
    for k in win:
        if k not in WINDOW_KEYS:
            notes.append(f'ignored window "{k}"')
    return out


def _check_schema(payload: Any, faults: List[str], notes: List[str]
                  ) -> Tuple[List[Shape], List[str], Dict[str, Any]]:
    """(accepted shapes, position names, window). Faults for EVERY row."""
    if not isinstance(payload, dict):
        faults.append('the reply was not a JSON object with "window" and '
                      '"shapes"')
        return [], [], {}
    for k in payload:
        if k in ("window", "shapes"):
            continue
        if k in REFUSED_KEYS:
            notes.append(f'ignored "{k}" — {REFUSED_KEYS[k]}')
        else:
            notes.append(f'ignored top-level "{k}"')
    window = _check_window(payload.get("window"), faults, notes)
    rows = payload.get("shapes")
    if not isinstance(rows, list):
        hint = (' — that looks like ONE shape; put it inside {"window": ..., '
                '"shapes": [...]}' if "kind" in payload else "")
        faults.append('the reply has no "shapes" list' + hint)
        return [], [], window
    if not rows:
        faults.append('"shapes" is empty — the request needs at least one '
                      "widget")
        return [], [], window
    if len(rows) > MAX_SHAPES:
        faults.append(f"{len(rows)} shapes is too many (at most {MAX_SHAPES})"
                      f" — merge or drop some")
        return [], [], window
    shapes, wheres = [], []
    for i, row in enumerate(rows):
        s, where = _check_row(i, row, faults, notes)
        wheres.append(where)
        if s is not None:
            shapes.append(s)
    return shapes, wheres, window


# ============================================================
# Deterministic repair
# ============================================================

#: Where a model puts a container's children when it nests them instead of
#: listing them. MEASURED on Phi-4 Q4: "a list of folders on the left, a table
#: on the right" came back as a frame with its widgets inside the frame's own
#: "shapes", three rounds running — the repair prompt's 'unknown field
#: "shapes"' did not move it. Nesting is how most JSON describes a tree, so
#: this is flattened here rather than argued with.
NESTED_KEYS = ("shapes", "children", "widgets")
#: Deeper than any real window. Past it, rows are left nested and their key
#: is reported as an unknown field like any other.
MAX_NESTING = 8
#: Containers whose children are PAGES (a tab each, a pane each), not widgets.
PAGE_HOLDERS = frozenset({"notebook", "panedwindow"})


def _nested_key(row: Any) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    return next((k for k in NESTED_KEYS if isinstance(row.get(k), list)),
                None)


def _as_absolute(parent: Dict[str, Any], child: Dict[str, Any]
                 ) -> Tuple[Dict[str, Any], bool]:
    """(child with canvas coordinates, whether it was read as RELATIVE).

    A nested child's x/y may mean the canvas or the parent's corner, and
    models do both. Absolute when that box already lies inside the parent;
    relative when it only fits inside the parent as an OFFSET; otherwise left
    as given, for the gate to report like any misplaced shape."""
    p = [_as_int(parent.get(k)) for k in ("x", "y", "w", "h")]
    c = [_as_int(child.get(k)) for k in ("x", "y", "w", "h")]
    if None in p or None in c:
        return child, False
    px, py, pw, ph = p
    cx, cy, cw, ch = c
    if px <= cx and py <= cy and cx + cw <= px + pw and cy + ch <= py + ph:
        return child, False
    if 0 <= cx and 0 <= cy and cx + cw <= pw and cy + ch <= ph:
        return dict(child, x=px + cx, y=py + cy), True
    return child, False


def _flatten_rows(rows: List[Any], notes: List[str], depth: int) -> List[Any]:
    out: List[Any] = []
    for row in rows:
        key = _nested_key(row)
        if key is None or depth >= MAX_NESTING:
            out.append(row)
            continue
        parent = {k: v for k, v in row.items() if k != key}
        out.append(parent)                # the container BEFORE its children
        kids, relative = [], 0
        for child in row[key]:
            if isinstance(child, dict):
                child, was_relative = _as_absolute(parent, child)
                relative += was_relative
            kids.append(child)
        if kids:
            notes.append(
                f'moved {len(kids)} shape(s) out of the "{key}" of '
                f'{parent.get("kind")} {parent.get("label")!r} into the flat '
                f"list" + (f" ({relative} had x/y relative to it)"
                           if relative else ""))
        out.extend(_flatten_rows(kids, notes, depth + 1))
    return out


def _flatten_payload(payload: Any, notes: List[str]) -> Any:
    """The payload with every nested child list pulled up into "shapes".

    A NEW dict; the parsed reply is not modified. Anything that is not the
    expected {"shapes": [...]} shape is returned untouched for the schema
    check to report."""
    if not isinstance(payload, dict) or not isinstance(payload.get("shapes"),
                                                       list):
        return payload
    rows = payload["shapes"]
    if not any(_nested_key(r) for r in rows):
        return payload
    return dict(payload, shapes=_flatten_rows(rows, notes, 0))

def _pull_inside(shapes: Sequence[Shape], wheres: Sequence[str],
                 canvas_w: int, canvas_h: int) -> List[str]:
    """Move a shape that STARTS past the right or bottom edge back inside,
    keeping its size. In place.

    gui_snap.clamp_to_canvas cannot: it raises x to the margin but only ever
    TRIMS the far edge, so a label at x=1180 on a 1100 canvas comes out 8px
    wide and still off-canvas. Moving it costs nothing and spends no round."""
    notes: List[str] = []
    lim_x = canvas_w - EDGE_MARGIN - MIN_SIZE
    lim_y = canvas_h - EDGE_MARGIN - MIN_SIZE
    for s, where in zip(shapes, wheres):
        moved = False
        if s.x > lim_x:
            s.x = max(EDGE_MARGIN, canvas_w - EDGE_MARGIN - s.w)
            moved = True
        if s.y > lim_y:
            s.y = max(EDGE_MARGIN, canvas_h - EDGE_MARGIN - s.h)
            moved = True
        if moved:
            notes.append(f"{where}: started past the canvas edge; moved back "
                         f"inside at its own size")
    return notes


#: Plain wrappers — the kinds a model puts around a notebook's pages.
WRAPPER_KINDS = frozenset({"frame", "freeform"})


def _unwrap_pages(shapes: List[Shape], wheres: List[str], notes: List[str]
                  ) -> Tuple[List[Shape], List[str]]:
    """Drop a plain frame wrapped around a notebook's pages.

    MEASURED on Phi-4 Q4, the tabbed-preferences prompt: the three page
    frames were right, but inside one more frame the size of the notebook, so
    the notebook had ONE page and three tab titles. Unambiguous when the
    notebook's only child is a plain frame whose own children are exactly as
    many containers as there are titles: those are the pages, and the wrapper
    is the only thing in the way. Anything less exact is left for the gate to
    report.

    Returns NEW lists; shapes and position names stay aligned."""
    kids = gui_layout.build_containment_tree(shapes, warnings=[])
    by_id = {s.id: s for s in shapes}
    drop = set()
    for book in shapes:
        tabs = book.props.get("tabs") if book.kind == "notebook" else None
        inner = kids.get(book.id, [])
        if not isinstance(tabs, list) or len(tabs) < 2 or len(inner) != 1:
            continue
        wrapper = by_id[inner[0]]
        pages = [by_id[i] for i in kids.get(wrapper.id, [])]
        if (wrapper.kind in WRAPPER_KINDS and len(pages) == len(tabs)
                and all(p.kind in CONTAINER_KINDS for p in pages)):
            drop.add(wrapper.id)
            notes.append(f"removed the frame wrapped around {book.kind}'s "
                         f"{len(pages)} pages — they are its tabs")
    if not drop:
        return shapes, wheres
    kept = [(s, w) for s, w in zip(shapes, wheres) if s.id not in drop]
    return [s for s, _ in kept], [w for _, w in kept]


def _trim_overflow(shapes: Sequence[Shape], wheres: Sequence[str]
                   ) -> List[str]:
    """Trim a shape that STARTS well inside a container but runs past its far
    edge, so it ends inside it. In place.

    MEASURED on Phi-4 Q4, the tabbed-preferences prompt: three good page
    frames, and a log pane 1040 px wide starting inside a 520 px page. It ran
    out of the page AND the notebook, so containment decided it belonged to
    neither, the notebook "had 8 pages", and three rounds went on a fault
    about the notebook that never named the log pane.

    gui_snap.inset_children deliberately does NOT do this — it decides
    parenthood with a 4 px slop so a widget hanging just below a frame is not
    squashed into it. The rule here is narrower for the same reason: the
    shape's top-left corner must be CHILD_INSET inside the container on both
    axes, which a widget hanging off an edge never is. Only the far edges
    move; where the shape starts is where the model put it.

    Containers first, outermost first, so a page is trimmed into its notebook
    before the page's own children are measured against it.

    A plain widget is never trimmed INTO a notebook or a splitter: their
    children are pages, and the same prompt's "Apply and Close below the
    tabs" were drawn just inside the notebook's bottom edge — trimmed in,
    they would have become two more tabs.

    A SECOND, shallower tier: a corner only EDGE_MARGIN inside also counts,
    but only when trimming keeps at least half of the shape on each axis.
    MEASURED on the file-browser prompt: a treeview 8 px inside its frame's
    left edge — gui_snap's own inset — ran 24 px past the frame's bottom, fell
    out of it as an overlapping sibling, and the model sent the same answer
    three rounds running. Half-kept is what separates that from a widget
    merely hanging off an edge, which would keep almost nothing.
    """
    notes: List[str] = []
    where_of = {id(s): w for s, w in zip(shapes, wheres)}
    containers = [s for s in shapes if s.kind in CONTAINER_KINDS]
    order = (sorted(containers, key=lambda s: -(s.w * s.h))
             + [s for s in shapes if s.kind not in CONTAINER_KINDS])

    def starts_inside(c: Shape, s: Shape, inset: int) -> bool:
        return (c.x + inset <= s.x <= c.x + c.w - inset
                and c.y + inset <= s.y <= c.y + c.h - inset)

    for s in order:
        eligible = [c for c in containers if c is not s
                    and (s.kind in CONTAINER_KINDS
                         or c.kind not in PAGE_HOLDERS)]
        deep = [c for c in eligible if starts_inside(c, s, CHILD_INSET)]
        shallow = [c for c in eligible if starts_inside(c, s, EDGE_MARGIN)]
        c = min(deep or shallow or [None],
                key=lambda h: h.w * h.h if h is not None else 0)
        if c is None:
            continue
        right, bottom = c.x + c.w - EDGE_MARGIN, c.y + c.h - EDGE_MARGIN
        over_x, over_y = s.x + s.w - right, s.y + s.h - bottom
        if over_x <= 0 and over_y <= 0:
            continue
        new_w = max(MIN_SIZE, right - s.x) if over_x > 0 else s.w
        new_h = max(MIN_SIZE, bottom - s.y) if over_y > 0 else s.h
        if not deep and (new_w * 2 < s.w or new_h * 2 < s.h):
            continue                    # hanging off an edge: not a child
        s.w, s.h = new_w, new_h
        notes.append(
            f"{where_of.get(id(s), s.kind)}: trimmed to end inside {c.kind} "
            f"{c.label!r} — it starts inside it and ran "
            f"{max(over_x, over_y)}px past its edge")
    return notes


#: Sibling overlaps up to this deep are separated here; deeper ones are sent
#: back to the model. It is the layout engine's own clustering tolerance at
#: the root (2% of 1100 = 22 px, rounded up to the grid): overlaps under it
#: vanish into a clean grid, so nothing else ever reported them, and a
#: toolbar running 16 px into the text area under it was accepted as drawn.
SMALL_OVERLAP = 24


def _overlap(a: Shape, b: Shape) -> Tuple[int, int]:
    return (min(a.x + a.w, b.x + b.w) - max(a.x, b.x),
            min(a.y + a.h, b.y + b.h) - max(a.y, b.y))


def _separate_siblings(shapes: Sequence[Shape], wheres: Sequence[str]
                       ) -> List[str]:
    """Pull apart siblings that overlap by a few pixels. In place.

    The upper (or left) shape gives up the overlap, on the axis where it is
    shallower, ending where the other begins. A container is only shrunk when
    none of its own children would end up outside it; otherwise, and for
    anything deeper than SMALL_OVERLAP, the gate reports the overlap."""
    notes: List[str] = []
    where_of = {id(s): w for s, w in zip(shapes, wheres)}
    kids = gui_layout.build_containment_tree(shapes, warnings=[])
    by_id = {s.id: s for s in shapes}
    for parent, ids in kids.items():
        sibs = [by_id[i] for i in ids]
        for i, a in enumerate(sibs):
            for b in sibs[i + 1:]:
                ox, oy = _overlap(a, b)
                if ox <= 0 or oy <= 0 or min(ox, oy) > SMALL_OVERLAP:
                    continue
                if oy <= ox:
                    first, second = sorted((a, b), key=lambda s: (s.y, s.x))
                    new = second.y - first.y
                    inner = [by_id[c] for c in kids.get(first.id, [])]
                    if new < MIN_SIZE or any(c.y + c.h > first.y + new
                                             for c in inner):
                        continue
                    first.h = new
                else:
                    first, second = sorted((a, b), key=lambda s: (s.x, s.y))
                    new = second.x - first.x
                    inner = [by_id[c] for c in kids.get(first.id, [])]
                    if new < MIN_SIZE or any(c.x + c.w > first.x + new
                                             for c in inner):
                        continue
                    first.w = new
                notes.append(f"{where_of.get(id(first), first.kind)}: "
                             f"shortened by {min(ox, oy)}px so it no longer "
                             f"overlaps {where_of.get(id(second), second.kind)}")
    return notes


def _tidy(shapes: List[Shape], wheres: Sequence[str], canvas_w: int,
          canvas_h: int) -> Tuple[List[Shape], List[str]]:
    """gui_snap.snap plus the one repair it lacks. Returns NEW shapes, in the
    same order (snap deep-copies and never reorders), so position names still
    line up for the gate."""
    notes = _pull_inside(shapes, wheres, canvas_w, canvas_h)
    notes += _trim_overflow(shapes, wheres)
    out, snap_notes = gui_snap.snap(shapes, canvas_w=canvas_w,
                                    canvas_h=canvas_h)
    grid = [n for n in snap_notes if n.endswith("px grid")]
    if grid:
        # One line, not one per shape: a model that ignored the grid would
        # otherwise bury the notes that matter under twenty identical ones.
        notes.append(f"{len(grid)} shape(s) moved onto the {GRID}px grid")
    notes.extend(n for n in snap_notes if not n.endswith("px grid"))
    # After snap, so every edge it moves to is already on the grid.
    notes.extend(_separate_siblings(out, wheres))
    return out, notes


def _assign_z(shapes: Sequence[Shape]) -> None:
    """Containers before their children, children in reading order. In place.

    The designer canvas paints in (z, id) order, so a container with a higher
    z than its children is drawn OVER them; and gui_layout orders a notebook's
    pages by (z, id), which with reading order makes the pages drawn left to
    right the tabs titled left to right."""
    kids = gui_layout.build_containment_tree(shapes, warnings=[])
    by_id = {s.id: s for s in shapes}
    order: List[str] = []

    def visit(pid: Optional[str]) -> None:
        for cid in sorted(kids.get(pid, []),
                          key=lambda i: (by_id[i].y, by_id[i].x, i)):
            order.append(cid)
            visit(cid)

    visit(None)
    for z, sid in enumerate(order, 1):
        by_id[sid].z = z


# ============================================================
# The gate — the same pipeline Generate runs
# ============================================================

def _gate(shapes: Sequence[Shape], wheres: Sequence[str],
          window: Dict[str, Any], canvas_w: int, canvas_h: int) -> List[str]:
    faults: List[str] = []
    index = {s.id: i for i, s in enumerate(shapes)}

    for i, s in enumerate(shapes):
        if s.x < 0 or s.y < 0 or s.x2 > canvas_w or s.y2 > canvas_h:
            faults.append(f"{wheres[i]} is outside the {canvas_w} x "
                          f"{canvas_h} canvas")

    full = [i for i, s in enumerate(shapes)
            if s.w >= FULL_CANVAS * canvas_w and s.h >= FULL_CANVAS * canvas_h]
    if len(full) >= 2:
        # The exact failure gui_examples was written for: a full-canvas frame
        # plus a full-canvas notebook came up as a completely blank app. Snap
        # may since have nested one inside the other, which renders — but as
        # a wrapper around a wrapper, and only by luck of an 8px inset.
        faults.append(" and ".join(wheres[i] for i in full)
                      + " each cover the whole window — keep at most one "
                        "full-window container and put everything else "
                        "inside it (two stacked full-window containers is "
                        "how a generated app comes up blank)")

    try:
        tree = gui_layout.infer(shapes, canvas_w, canvas_h)
        spec = gui_spec.build(
            shapes, tree, project="described",
            title=str(window.get("title") or "Untitled"),
            root_bg=str(window.get("bg") or ""),
            root_fg=str(window.get("fg") or ""),
            root_font=str(window.get("font") or ""))
        _valid, errs = gui_spec.validate(spec)
    except Exception as exc:              # never raise out of describe()
        faults.append(f"the wireframe could not be laid out: {exc!r}")
        return faults

    # Layout WARNINGS that Generate lets through but that mean the drawing is
    # not what the model described. Both are promoted to faults: an overlap
    # turns the whole container into free placement, and a widget drawn
    # inside a button is silently re-parented somewhere else.
    for w in tree.warnings:
        if " overlap, so " in w:
            faults.append(f"layout: {_by_position(w, shapes, wheres)} Move "
                          f"them apart so they do not overlap.")
        elif "cannot hold children" in w:
            faults.append(f"layout: {_by_position(w, shapes, wheres)} Only a "
                          "container (" + ", ".join(sorted(CONTAINER_KINDS))
                          + ") can hold other widgets.")

    # Every pair of siblings, by pixel. The layout warning above only sees an
    # overlap deep enough to break its grid; this sees all of them, so an
    # accepted wireframe never has two siblings drawn over each other.
    kids = gui_layout.build_containment_tree(shapes, warnings=[])
    for ids in kids.values():
        sibs = [index[i] for i in ids if i in index]
        for n, i in enumerate(sibs):
            for j in sibs[n + 1:]:
                ox, oy = _overlap(shapes[i], shapes[j])
                if ox > 0 and oy > 0:
                    faults.append(f"{wheres[i]} and {wheres[j]} overlap by "
                                  f"{ox} x {oy} px — move them apart, or "
                                  f"shrink one, so they do not touch")

    for i, s in enumerate(shapes):
        if s.kind != "notebook":
            continue
        node = tree.nodes.get(s.id)
        pages = len(node.children) if node is not None else 0
        tabs = list((s.props or {}).get("tabs") or [])
        if len(tabs) != pages:
            faults.append(f"{wheres[i]}: props.tabs has {len(tabs)} title(s) "
                          f"but {pages} page(s) are drawn inside it — give "
                          f"one title per page, and draw each page (a frame) "
                          f"side by side inside the notebook")

    # validate() names a widget "<label or kind> (<widget name>)" at the start
    # of an error and '<widget name>' inside one; the model never saw widget
    # names, so rewrite both to the position it can find its shape by.
    heads, names = {}, {}
    for w in spec.widgets:
        i = index.get(w.shape_id)
        if i is not None:
            heads[f"{w.label or w.kind} ({w.name})"] = wheres[i]
            names[repr(w.name)] = wheres[i]
    for e in errs:
        for theirs, mine in heads.items():
            if e.startswith(theirs + ":"):
                e = mine + e[len(theirs):]
                break
        for theirs, mine in names.items():
            e = e.replace(theirs, mine)
        faults.append(e)
    return faults


def _by_position(warning: str, shapes: Sequence[Shape],
                 wheres: Sequence[str]) -> str:
    """Rewrite a gui_layout warning's shape names to positions.

    gui_layout names a shape by repr(label or kind) — "'Run'", "'frame'" —
    which the model can only map back when it is unique; two unlabelled
    frames are both "'frame'". So only UNIQUE names are rewritten, and an
    ambiguous one is left as the layout wrote it rather than guessed at."""
    seen: Dict[str, List[int]] = {}
    for i, s in enumerate(shapes):
        seen.setdefault(repr(s.label or s.kind), []).append(i)
    # Longest first, so a name that happens to contain another name is
    # rewritten whole rather than having its middle replaced.
    for token in sorted(seen, key=len, reverse=True):
        if len(seen[token]) == 1 and token in warning:
            warning = warning.replace(token, wheres[seen[token][0]])
    return warning


# ============================================================
# One reply, every layer
# ============================================================

def check_reply(raw: str, *, canvas_w: int = CANVAS_W,
                canvas_h: int = CANVAS_H) -> Checked:
    """Take one model reply as far through the layers as it gets.

    The gate runs only on a schema-clean reply: laying out a wireframe with
    rows missing reports faults about the gaps (a notebook "missing" a page
    whose row was rejected) that vanish once the row is fixed, and a model
    told to fix a phantom tends to break something real."""
    raw = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    payload = _extract_json(raw)
    if payload is None:
        return Checked(STAGE_NO_JSON, [_no_json_fault(raw)], None, raw)
    faults: List[str] = []
    notes: List[str] = []
    # Flattened BEFORE the schema, and the flat payload is what a repair
    # prompt shows back, so "shape 4" in a fault is shape 4 in what the model
    # reads.
    payload = _flatten_payload(payload, notes)
    shapes, wheres, window = _check_schema(payload, faults, notes)
    if faults:
        return Checked(STAGE_SCHEMA, faults, payload, raw, [], window, notes,
                       accepted=len(shapes))
    shapes, wheres = _unwrap_pages(shapes, wheres, notes)
    tidy, tidy_notes = _tidy(shapes, wheres, canvas_w, canvas_h)
    notes.extend(tidy_notes)
    _assign_z(tidy)
    faults = _gate(tidy, wheres, window, canvas_w, canvas_h)
    if faults:
        return Checked(STAGE_GATE, faults, payload, raw, [], window, notes)
    return Checked(STAGE_OK, [], payload, raw, tidy, window, notes)


# ============================================================
# Entry point
# ============================================================

def describe(text: str, *,
             model_call: Optional[Callable[[str], str]] = None,
             canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H,
             toolkit: str = "qt", max_attempts: int = 3,
             budget_chars: Optional[int] = None) -> DescribeResult:
    """A plain-English description -> a validated wireframe. NEVER RAISES.

    No model -> ok=False and ZERO calls, mirroring gui_classify.classify: the
    designer must keep working with no model loaded, and the caller shows the
    reason. A model that raises -> ok=False with the exception. Exhausting
    ``max_attempts`` -> ok=False, the best attempt's faults and raw reply, and
    NO shapes."""
    res = DescribeResult()
    try:
        _describe(res, text, model_call, int(canvas_w), int(canvas_h),
                  toolkit, max_attempts, budget_chars)
    except Exception as exc:              # the promise is "never raises"
        res.ok = False
        res.shapes = []
        res.errors.append(f"describe failed unexpectedly: {exc!r}")
    return res


def _describe(res: DescribeResult, text: Any,
              model_call: Optional[Callable[[str], str]], canvas_w: int,
              canvas_h: int, toolkit: str, max_attempts: Any,
              budget_chars: Optional[int]) -> None:
    if not isinstance(text, str) or not text.strip():
        res.errors = ["nothing to design — describe the GUI first"]
        return
    if toolkit not in TOOLKITS:
        res.errors = [f"unknown toolkit {toolkit!r} (expected one of "
                      f"{', '.join(TOOLKITS)})"]
        return
    if model_call is None:
        res.errors = ["no model available to design from a description — "
                      "load a model, or draw the wireframe by hand"]
        return
    if canvas_w < 8 * GRID or canvas_h < 8 * GRID:
        res.errors = [f"the canvas {canvas_w} x {canvas_h} is too small to "
                      f"design on"]
        return
    attempts = max(1, int(max_attempts))
    budget = DEFAULT_BUDGET_CHARS if budget_chars is None else int(budget_chars)

    prompt, shed = _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                             budget_chars=budget)
    notes: List[str] = []
    if shed:
        notes.append("to fit the model's context the prompt left out: "
                     + ", ".join(shed))
    if len(prompt) > budget:
        limit = int(math.ceil(max(0, budget) / CHARS_PER_TOKEN))
        notes.append(f"the prompt is ~{estimate_tokens(prompt)} tokens, over "
                     f"the ~{limit}-token budget even after shedding — a "
                     f"shorter description may work better")

    best: Optional[Checked] = None
    for attempt in range(1, attempts + 1):
        try:
            reply = model_call(prompt)
        except Exception as exc:
            res.attempts = attempt
            res.errors = [f"the model call failed: {exc!r}"]
            if best is not None:
                res.errors += best.faults
                res.raw = best.raw
                notes.extend(best.notes)
            res.notes = notes
            return
        res.attempts = attempt
        cand = check_reply(reply, canvas_w=canvas_w, canvas_h=canvas_h)
        if cand.ok:
            res.ok = True
            res.shapes = cand.shapes
            res.window = cand.window
            res.notes = notes + cand.notes
            res.raw = cand.raw
            return
        # Never let a worse round replace a better one: the repair prompt is
        # built from the BEST answer so far, so a reply that regresses to
        # prose does not make the next round start from nothing. Ties go to
        # the newer answer, so a model that is stuck is at least shown the
        # answer it just gave rather than an identical prompt again.
        if best is None or cand.rank() >= best.rank():
            best = cand
        if attempt < attempts:
            prompt = repair_prompt(text, best.payload, best.faults,
                                   raw=best.raw, canvas_w=canvas_w,
                                   canvas_h=canvas_h, budget_chars=budget)
            if len(prompt) > budget and not any("repair prompt" in n
                                                for n in notes):
                notes.append(f"a repair prompt is ~{estimate_tokens(prompt)} "
                             f"tokens, over the budget even at its leanest — "
                             f"a shorter description may work better")

    # No shapes AND no window: a caller that applied the window title of a
    # refused design would leave half of it on the user's canvas.
    res.ok = False
    res.shapes = []
    res.window = {}
    if best is not None:
        res.errors = list(best.faults)
        res.raw = best.raw
        notes.extend(best.notes)
    res.notes = notes


def fault_summary(result: Any) -> str:
    """A few lines for the UI log. Duck-typed on DescribeResult's fields."""
    ok = bool(getattr(result, "ok", False))
    n = int(getattr(result, "attempts", 0) or 0)
    tries = f"{n} attempt{'s' if n != 1 else ''}"
    notes = list(getattr(result, "notes", None) or [])
    if ok:
        k = len(getattr(result, "shapes", None) or [])
        line = f"designed {k} widget{'s' if k != 1 else ''} in {tries}"
        if notes:
            line += f" ({len(notes)} note{'s' if len(notes) != 1 else ''})"
        return line
    errors = list(getattr(result, "errors", None) or [])
    head = (f"could not design it after {tries}" if n
            else "could not design it")
    if not errors:
        return head
    return head + ":\n" + _bullets(errors)
