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

SMALL MODELS: A PROFILE, A SCHEMA, A TREE, AND MORE THAN ONE TRY
----------------------------------------------------------------
Everything above was measured on Phi-4 14B, which does not fit an 8 GB card.
What a 3.8-8B model gets instead is chosen by a Profile (profile_for):

  * a JSON SCHEMA (wireframe_schema / gui_describe_tree.tree_schema) passed
    to the model call as json_schema=, so a constrained backend cannot emit a
    fence, a trailing comma, a made-up kind or prop, or a colour name. The
    reply is still checked here: the schema decides what CAN be written,
    check_reply what is ACCEPTED.
  * TREE mode below 10B parameters: the model writes rows, columns and
    containers, and gui_describe_tree places them. Pixel mode stays for large
    models and for requests that give exact positions.
  * BEST-OF-N on round 1 with distinct seeds and temperatures, the validator
    as judge; repairs never re-ask identically.
  * worked examples chosen PER REQUEST from wireframes that were built,
    generated and run (examples/gui/qt_tests), each one gate-checked at the
    project's canvas before a model sees it, budgeted to the real window.

describe() without a profile keeps the calls exactly as they were: pixel
mode, one call per round, the prompt alone with no keyword arguments, and
the 4096-token budget. Only the worked examples differ — chosen for the
request, and image_viewer (the old fixed one) when nothing matches.
"""
from __future__ import annotations

import difflib
import functools
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gui_colors
import gui_describe_tree as gtree
import gui_examples
import gui_layout
import gui_snap
import gui_spec
from gui_shapes import (CONTAINER_KINDS, GENERIC_KIND, PALETTE, Shape,
                        load_gspec, new_shape)

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
#: Lists and objects inside each other. A real reply needs about 20 (a tree
#: MAX_DEPTH deep, a menu prop at the bottom); past this it is a model stuck
#: repeating "[" until the token limit — and json.loads, the prop checks and
#: the repair prompt all RECURSE, so a 3000-deep reply raised RecursionError
#: out of describe and classify instead of costing one round.
MAX_JSON_DEPTH = 48

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

MODES = ("pixel", "tree")
#: Below this many billion parameters a model designs in TREE mode. Every
#: recorded pixel-mode failure was geometry, measured on a 14B model; the
#: models that fit an 8 GB card are 3.8-8B.
SMALL_MODEL_B = 10.0
#: At or below this, three round-1 candidates instead of two: a 3.8B model's
#: run-to-run variance is the thing best-of-N turns into pass rate.
TINY_MODEL_B = 4.5
#: What a tree reply needs. Minified (constrained) trees measure ~20 tokens
#: per widget against ~39 for a pixel row (phi3.5 tokenizer), so 40 widgets
#: fit in 1200 with room for the window and the nesting.
TREE_REPLY_TOKENS = 1200
#: A repair round's temperature, raised by REPEAT_STEP each time the model
#: sends back an answer it already sent — re-asking identically is how the
#: Phi-4 M2 fixture got "this same answer three rounds running".
REPAIR_TEMPERATURE = 0.1
REPEAT_STEP = 0.25
#: Seeds are fixed so a run can be reproduced: candidate k of round 1 gets
#: BASE_SEED + k, repair round r gets BASE_SEED + 100 * r (+ repeats).
BASE_SEED = 1
#: Worked examples per prompt, budget permitting.
MAX_EXAMPLES = 3


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
    #: Model CALLS made — one per round without a profile; best-of-N makes
    #: several in round 1. The Designer logs it as "N model call(s)".
    attempts: int = 0
    raw: str = ""
    #: "pixel" or "tree": how the ACCEPTED (or best) reply was written.
    mode: str = ""
    #: Rounds: the first request plus each repair.
    rounds: int = 0


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
    #: The reply was a layout TREE (payload is the tree, not shape rows).
    tree: bool = False
    #: The reply was cut off and closed after its last complete widget.
    salvaged: bool = False

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
# Profile — what a model of THIS size is asked for, and how
# ============================================================

@dataclass(frozen=True)
class Profile:
    """How describe() talks to one model.

    The default is today's behaviour exactly — pixel mode, one call per
    round, no keyword arguments reach the model call — so a caller that
    passes no profile (every test stub, the Tk shell) sees no change.
    profile_for() builds the one a real model gets."""
    mode: str = "pixel"
    #: Round-1 candidates; the validator picks among them.
    n_best: int = 1
    #: One temperature per round-1 candidate. () = the caller's own default.
    temperatures: Tuple[float, ...] = ()
    #: Base seed; None = no seed is passed (the engine's own random seed).
    seed: Optional[int] = None
    #: Pass json_schema= to the model call.
    constrained: bool = False
    #: The window the prompt and reply must share (tokens).
    n_ctx: int = N_CTX
    #: num_predict for each call. Pixel mode keeps REPLY_TOKENS.
    reply_tokens: int = REPLY_TOKENS
    #: Shapes the schema allows (maxItems); the reply budget is sized to it.
    max_shapes: int = MAX_SHAPES
    params_b: Optional[float] = None
    model: str = ""
    #: One line for the log: why this profile.
    reason: str = ""

    @property
    def budget_chars(self) -> int:
        """Characters the PROMPT may use: the window minus the reply and the
        chat template, at the measured CHARS_PER_TOKEN."""
        return max(1500, int((self.n_ctx - self.reply_tokens - SLACK_TOKENS)
                             * CHARS_PER_TOKEN))

    @property
    def custom(self) -> bool:
        """Anything beyond the plain one-call-per-round default."""
        return self != LEGACY


LEGACY = Profile()

#: Requests that give exact positions keep pixel mode at any model size: a
#: tree cannot say "at x = 40". "Coordinates" alone is not one: a stage's
#: X/Y, an ROI's or a GPS fix's coordinates are DATA the app edits, and
#: matching the bare word sent those requests to pixel mode on a 3.8B model
#: (review finding) — only coordinates ON the window or canvas count.
_EXACT = re.compile(
    r"\b\d{1,4}\s*(?:px|pixels?)\b"
    r"|\b[xy]\s*[=:]\s*\d"
    r"|\(\s*\d{1,4}\s*,\s*\d{1,4}\s*\)"
    r"|\bexact(?:ly)?\s+(?:positions?|coordinates?|placement)"
    r"|\b(?:pixel|screen|canvas|window)\s+(?:coordinates?|positions?)\b",
    re.IGNORECASE)


def wants_exact_positions(text: str) -> bool:
    return bool(_EXACT.search(text or ""))


_SIZE_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,2})?)\s*[bB](?![a-zA-Z])")
#: Ollama names that carry no size: the family's default tag. US-origin
#: models only — a non-US model is never chosen for the user, so it needs no
#: entry here (its size still parses from a tag like ":7b").
KNOWN_SIZES_B = {"phi3.5": 3.8, "phi3": 3.8, "phi4-mini": 3.8, "phi4": 14.7,
                 "llama3.2": 3.2, "llama3.1": 8.0, "llama3": 8.0,
                 "gemma3": 4.3, "gemma2": 9.2, "granite3.3": 8.2}
#: The same families as GGUF file names spell them (dashes, no tag),
#: most specific first.
_KNOWN_PREFIXES = (("phi-4-mini", 3.8), ("phi-3.5-mini", 3.8),
                   ("phi-3-mini", 3.8), ("phi-4", 14.7))


def parse_params_b(name: str) -> Optional[float]:
    """Billions of parameters from a model name — "llama3.1:8b" -> 8.0,
    "Llama-3.2-3B-Instruct-Q5_K_M" -> 3.0, "ollama:phi3.5" -> 3.8 and
    "Phi-3.5-mini-instruct-Q4_K_M" -> 3.8 by family. None when the name
    does not say."""
    s = str(name or "").strip()
    base = s.split("ollama:", 1)[-1]
    found = _SIZE_RE.findall(base.replace("_", " ").replace("-", " "))
    sizes = [float(f) for f in found if 0.1 <= float(f) <= 1000]
    if sizes:
        return sizes[-1]
    family = base.split(":", 1)[0].lower()
    if family in KNOWN_SIZES_B:
        return KNOWN_SIZES_B[family]
    stem = family.replace("_", "-")
    return next((b for p, b in _KNOWN_PREFIXES if stem.startswith(p)), None)


def profile_for(params_b: Optional[float] = None, *,
                n_ctx: Optional[int] = None, text: str = "",
                model: str = "", mode: Optional[str] = None,
                n_best: Optional[int] = None,
                constrained: bool = True) -> Profile:
    """The profile for a model of ``params_b`` billion parameters.

      * below SMALL_MODEL_B (or unknown): TREE mode, schema-constrained,
        best of 2 (3 at TINY_MODEL_B and under), 40 shapes at most;
      * SMALL_MODEL_B and up: pixel mode, constrained, one candidate;
      * a request with exact positions: pixel mode at any size.

    Unknown size counts as small: the models this was written for are the
    ones that fit an 8 GB card, and a tree is laid out correctly whatever
    the model's size — the cost of guessing small is a coarser layout, the
    cost of guessing large is a geometry failure."""
    exact = wants_exact_positions(text)
    small = params_b is None or params_b < SMALL_MODEL_B
    tiny = params_b is not None and params_b <= TINY_MODEL_B
    if mode not in MODES:
        mode = "pixel" if (exact or not small) else "tree"
    if n_best is None:
        n_best = 3 if tiny else (2 if small else 1)
    n_best = max(1, min(5, int(n_best)))
    temps = {1: (0.1,), 2: (0.2, 0.6), 3: (0.2, 0.5, 0.8)}.get(
        n_best, tuple(round(0.2 + 0.15 * i, 2) for i in range(n_best)))
    max_shapes = 40 if (small and mode == "tree") else MAX_SHAPES
    reply = TREE_REPLY_TOKENS if mode == "tree" else REPLY_TOKENS
    window = int(n_ctx) if n_ctx else N_CTX
    size = (f"{params_b:g}B" if params_b is not None else "size unknown")
    why = (f"{model or 'the model'} ({size}): {mode} mode"
           + (" (the request gives exact positions)" if exact and small
              and mode == "pixel" else "")
           + (f", best of {n_best}" if n_best > 1 else "")
           + (", schema-constrained" if constrained else "")
           + f", {window}-token window")
    return Profile(mode=mode, n_best=n_best, temperatures=temps,
                   seed=BASE_SEED, constrained=bool(constrained),
                   n_ctx=window, reply_tokens=reply, max_shapes=max_shapes,
                   params_b=params_b, model=model, reason=why)


def call_model(model_call: Callable[..., str], prompt: str,
               **opts: Any) -> str:
    """Call ``model_call(prompt, **opts)`` with only the options it accepts.

    The model call is injected, and the ones that exist take different
    things: a test stub takes the prompt alone, designer_project's takes
    json_schema / seed / temperature / num_predict, and an engine without
    the schema contract yet takes neither. None values are never passed —
    "no seed" means the callee's default, not seed=None. Read from the
    signature first so a TypeError raised INSIDE the model is not mistaken
    for an unsupported keyword; the retry covers callables whose signature
    cannot be read."""
    opts = {k: v for k, v in opts.items() if v is not None}
    if not opts:
        return model_call(prompt)
    try:
        params = inspect.signature(model_call).parameters.values()
        if not any(p.kind is p.VAR_KEYWORD for p in params):
            names = {p.name for p in params}
            opts = {k: v for k, v in opts.items() if k in names}
    except (TypeError, ValueError):
        pass
    if not opts:
        return model_call(prompt)
    try:
        return model_call(prompt, **opts)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
    return model_call(prompt)


# ============================================================
# The reply schema (constrained decoding)
# ============================================================

def wireframe_schema(canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H,
                     max_shapes: int = MAX_SHAPES) -> Dict[str, Any]:
    """The pixel-mode reply as JSON Schema, from PALETTE.

    One variant per kind, "kind" first, so everything after it is typed for
    that kind: its own props only (handler props left out), colour only
    where gui_colors says the kind takes it, a font only on text widgets.
    Geometry is integer with the canvas as its bounds — honoured by
    jsonschema and by llama.cpp's C++ converter (Ollama); the
    llama-cpp-python converter ignores minimum/maximum and caps an integer
    at 16 digits, and check_reply refuses anything past MAX_PIXELS. Bounded
    everywhere else (maxItems, maxLength) so a constrained reply cannot run
    on until num_predict cuts it off."""
    kinds = list(KINDS)
    defs = gtree.shared_defs(kinds)
    defs["x"] = {"type": "integer", "minimum": 0, "maximum": int(canvas_w)}
    defs["y"] = {"type": "integer", "minimum": 0, "maximum": int(canvas_h)}
    defs["w"] = {"type": "integer", "minimum": MIN_SIZE,
                 "maximum": int(canvas_w)}
    defs["h"] = {"type": "integer", "minimum": MIN_SIZE,
                 "maximum": int(canvas_h)}
    variants = []
    for k in kinds:
        props = {"kind": {"const": k}, "label": {"$ref": "#/$defs/label"},
                 "x": {"$ref": "#/$defs/x"}, "y": {"$ref": "#/$defs/y"},
                 "w": {"$ref": "#/$defs/w"}, "h": {"$ref": "#/$defs/h"},
                 "props": {"$ref": f"#/$defs/props-{k}"}}
        props.update(gtree.style_props(k))
        variants.append({"type": "object", "properties": props,
                         "required": ["kind", "label", "x", "y", "w", "h"],
                         "additionalProperties": False})
    defs["shape"] = {"anyOf": variants}
    return {"$defs": defs, "type": "object",
            "properties": {
                "window": {"$ref": "#/$defs/window"},
                "shapes": {"type": "array", "items": {"$ref": "#/$defs/shape"},
                           "minItems": 1,
                           "maxItems": max(1, min(int(max_shapes),
                                                  MAX_SHAPES))}},
            "required": ["window", "shapes"], "additionalProperties": False}


@functools.lru_cache(maxsize=16)
def _schema_json(mode: str, canvas_w: int, canvas_h: int,
                 max_shapes: int) -> str:
    if mode == "tree":
        return json.dumps(gtree.tree_schema())
    return json.dumps(wireframe_schema(canvas_w, canvas_h, max_shapes))


def reply_schema(profile: Profile, canvas_w: int = CANVAS_W,
                 canvas_h: int = CANVAS_H) -> Dict[str, Any]:
    """The schema for ``profile``'s mode — a fresh dict each call (built
    from a cached string, so a caller that edits it cannot poison the next
    describe), and identical JSON each time, so an engine that caches its
    grammar by the schema's hash compiles it once."""
    return json.loads(_schema_json(profile.mode, int(canvas_w),
                                   int(canvas_h), int(profile.max_shapes)))


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


def _catalogue(detail: bool, kinds: Sequence[str] = KINDS,
               head: str = "") -> str:
    """The closed vocabulary. Always EVERY kind — only the prop detail sheds.

    Handler-typed props ("command") are never listed: generation wires every
    callback itself as on_<name>, and nothing downstream reads the prop."""
    lines = []
    for k in kinds:
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
    head = head or ("You may ONLY use these widget kinds, spelled exactly as "
                    "written:")
    return "WIDGETS\n" + head + "\n" + "\n".join(lines)


def _q(v: float) -> int:
    return int(round(float(v) / GRID) * GRID)


# ============================================================
# Worked examples — chosen per request
# ============================================================
# MEASURED: the one fixed example (image_viewer) has no containers at all,
# and the requests that fail on real models are exactly the ones with tabs,
# labelled groups, menus and toolbars — taught to the model only by prose
# rules, which this project measured do not work (gui_examples' docstring).
# So the example is now picked FOR the request, from wireframes that were
# built, generated and run by tests/test_qt_wireframes.py — the A and B
# tiers; C is degraded on purpose.

QT_TESTS_DIR = gui_examples.EXAMPLES_DIR / "qt_tests"

#: name -> words in a request that make it relevant. Order is the tie-break.
EXAMPLE_POOL: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("b_notebook_tabs", ("tab", "tabs", "tabbed", "notebook", "pages",
                         "preferences", "sections")),
    ("b_options_group", ("group", "grouped", "options", "radio", "choose",
                         "choice", "labelled", "labeled", "labelframe",
                         "checkbox", "checkboxes", "resize")),
    ("b_nested_containers", ("panel", "panels", "nested", "source",
                             "target", "copy", "two sides", "left and right",
                             "labelframe")),
    ("b_toolbar_editor", ("toolbar", "tool bar", "editor", "status bar",
                          "status", "document", "notes", "notepad")),
    ("b_split_browser", ("split", "splitter", "browser", "library", "table",
                         "columns", "left", "right", "side by side",
                         "treeview", "files")),
    ("b_paned_vertical", ("console", "pane", "paned", "divider", "output",
                          "terminal", "above", "below")),
    ("b_dashboard", ("dashboard", "chart", "plot", "graph", "monitor",
                     "live", "log", "logs", "throughput")),
    ("b_settings_form", ("settings", "form", "fields", "config",
                         "configuration", "threads", "spinbox", "slider",
                         "dropdown", "combobox", "quality", "number")),
    ("a_login_form", ("login", "log in", "sign in", "signin", "password",
                      "username", "user name", "credentials")),
    ("a_image_viewer", ("image", "images", "viewer", "picture", "photo",
                        "frames", "frame by frame", "folder", "scrub",
                        "scrubber", "preview")),
    ("a_list_editor", ("list", "listbox", "add", "remove", "items",
                       "todo", "to-do", "entries")),
    (EXAMPLE_NAME, ("capture", "exposure", "camera", "bad timing",
                    "mistimed", "numeric rows")),
)
#: When nothing in the request matches: today's single example in pixel
#: mode (so a default prompt is unchanged), and in tree mode the two that
#: show the commonest shapes — rows of label + field, and a labelled group.
DEFAULT_EXAMPLES = {"pixel": (EXAMPLE_NAME,),
                    "tree": ("b_settings_form", "b_options_group")}


def _example_source(name: str) -> Optional[Dict[str, Any]]:
    path = QT_TESTS_DIR / f"{name}.gspec"
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return gui_examples.load(name)
    except Exception:
        return None


def _rescaled(raw: Dict[str, Any], canvas_w: int, canvas_h: int
              ) -> Dict[str, Any]:
    """An example in gui_examples._compact form, fitted to this canvas.

    Shrunk to fit, never stretched past the designer's own CANVAS_W x
    CANVAS_H: MEASURED, image_viewer stretched to Typhon's 1504 x 1016
    fails the gate ('label "Frames on a bad timing" and entry overlap'), and
    so does its own native 1280 x 800; its 1100 x 700 version passes at
    1100 x 700. Whether a fitting passes at THIS canvas is _fitted's
    question — gui_layout's tolerance grows with the canvas, so even the
    1100 x 700 drawing does not pass at 1504 x 1016.

    Rescaled by EDGES, not by origin-and-size. Scaling y and h separately and
    snapping each rounds them independently, and on image_viewer it turned
    "Exposure (ms)" at 136..160 and its spinbox at 152..184 into an overlap
    that the source did not have. Snapping both edges and taking the
    difference is monotone, so shapes that did not overlap still do not."""
    canvas = raw.get("canvas") or {}
    src_w = float(canvas.get("w") or 1280)
    src_h = float(canvas.get("h") or 800)
    sx = min(float(canvas_w), float(CANVAS_W), src_w) / src_w
    sy = min(float(canvas_h), float(CANVAS_H), src_h) / src_h
    compact = gui_examples._compact(raw)
    win_in = compact.get("window") or {}
    # Structure only — no colour and no font. image_viewer is a Barbie
    # capture tool, pink with a Magneto face, and a small model copies what
    # it is shown: left in, every described window would come back pink.
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


@functools.lru_cache(maxsize=256)
def _checked_example(name: str, mode: str, canvas_w: int, canvas_h: int
                     ) -> Optional[str]:
    """The example as PROMPT TEXT for this mode and canvas, or None when it
    is not on disk or does not pass this module's own gate there — an
    example the validator would refuse teaches refusal. Cached: the gate
    costs 1-7 ms an example, the same answer every time for one canvas."""
    raw = _example_source(name)
    if not raw:
        return None
    if mode == "pixel":
        fitted = _fitted(name, canvas_w, canvas_h)
        return render_wireframe(json.loads(fitted)) if fitted else None
    window = _rescaled(raw, canvas_w, canvas_h)["window"]
    try:
        tree = gtree.tree_from_shapes(_shapes_of(raw))
    except Exception:
        return None
    reply = {"window": window, "layout": tree}
    if not check_reply(json.dumps(reply), canvas_w=canvas_w,
                       canvas_h=canvas_h).ok:
        return None
    return gtree.render_reply(window, tree)


@functools.lru_cache(maxsize=256)
def _fitted(name: str, canvas_w: int, canvas_h: int) -> Optional[str]:
    """A pixel example that PASSES the gate on this canvas, as JSON, or None.

    The drawing itself when, shrunk to fit, it passes. Otherwise the same
    design laid out afresh by gui_describe_tree: its tree (nesting and
    order from the drawing) placed for THIS canvas. MEASURED: image_viewer
    passes at 1100 x 700 and fails at Typhon's 1504 x 1016 at ANY scale —
    gui_layout's clustering tolerance grows with the canvas, so its labels
    8 px above their spinboxes fall into the spinboxes' rows — and the
    layouter's 16 px gaps pass at every canvas tested."""
    raw = _example_source(name)
    if not raw:
        return None
    pixel = _rescaled(raw, canvas_w, canvas_h)
    if check_reply(json.dumps(pixel), canvas_w=canvas_w,
                   canvas_h=canvas_h).ok:
        return json.dumps(pixel)
    try:
        parsed = gtree.parse({"window": pixel["window"],
                              "layout": gtree.tree_from_shapes(
                                  _shapes_of(raw))})
        if parsed.root is None:
            return None
        lay = gtree.layout(parsed.root, canvas_w, canvas_h,
                           window=pixel["window"])
    except Exception:
        return None
    if lay.faults:
        return None
    rows = [{k: v for k, v in r.items() if v not in ("", None, {})}
            for r in lay.payload["shapes"]]
    for r in rows:
        r.setdefault("label", "")
        if isinstance(r.get("props"), dict):
            r["props"] = gui_examples._meaningful_props(r["kind"],
                                                        r["props"])
            if not r["props"]:
                r.pop("props")
    out = {"window": pixel["window"], "shapes": rows}
    if not check_reply(json.dumps(out), canvas_w=canvas_w,
                       canvas_h=canvas_h).ok:
        return None
    return json.dumps(out)


def _shapes_of(raw: Dict[str, Any]) -> List[Shape]:
    """The shapes of a .gspec dict — only the fields a tree is built from.
    The source geometry, not a rescaled one: a tree has no pixels, and its
    nesting and order are what the drawing at its own size says."""
    out = []
    for i, sd in enumerate(raw.get("shapes") or []):
        out.append(Shape(
            id=str(sd.get("id") or f"shape{i}"),
            kind=str(sd.get("kind") or GENERIC_KIND),
            x=int(sd.get("x", 0)), y=int(sd.get("y", 0)),
            w=int(sd.get("w", 0)), h=int(sd.get("h", 0)),
            label=str(sd.get("label") or ""),
            props=dict(sd.get("props") or {}),
            bg=str(sd.get("bg") or ""), fg=str(sd.get("fg") or ""),
            font=str(sd.get("font") or "")))
    return out


def select_examples(text: str, *, mode: str = "pixel",
                    canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H,
                    limit: int = MAX_EXAMPLES) -> List[str]:
    """The example names for ``text``, most relevant first, every one of
    which passes the gate in ``mode`` at this canvas.

    Relevance is a word match against EXAMPLE_POOL — deliberately dumb: it
    costs microseconds, it is predictable, and a test can say which request
    gets which example. Nothing matching falls back to DEFAULT_EXAMPLES."""
    low = " " + re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()) + " "
    scored = []
    for order, (name, words) in enumerate(EXAMPLE_POOL):
        hits = sum(1 for w in words if f" {w} " in low)
        if hits:
            scored.append((-hits, order, name))
    names = [n for _h, _o, n in sorted(scored)]
    if not names:
        names = list(DEFAULT_EXAMPLES.get(mode, ()))
    out = []
    for n in names:
        if len(out) >= max(0, int(limit)):
            break
        if _checked_example(n, mode, int(canvas_w), int(canvas_h)):
            out.append(n)
    return out


# ============================================================
# Requested widgets — what the description names outright
# ============================================================

#: (what the user asked for, the words that name it, the kinds that count).
#: ORDER MATTERS: each match is blanked out of the text before the later,
#: more general patterns run, so "radio buttons" and "Check Button" do not
#: also demand a button, nor "multi-line text box" a single-line entry.
#: Only wording that names ONE kind of widget is here, and a phrase that
#: two widgets can honestly answer lists both: a requirement read wrongly
#: would rank a right design below a wrong one and spend a repair round on
#: it. Vague words — "area", "field", "panel", "display" on their own —
#: are deliberately left out.
#:
#: "A log view" is a log_pane and nothing else. It used to accept a "text"
#: too, while the benchmark's grader wants a log_pane — so phi3.5's and
#: qwen2.5's C4 (a text box for "a large log view") passed Describe's check
#: and failed the grade (2026-10-05). The grader is the one that is right:
#: a text box is the user's to type in (readonly is off by default) and its
#: port replaces the whole text on every .set(), while a log_pane is the
#: read-only, auto-scrolling log whose port APPENDS one line — the widget
#: the code behind a log monitor is written against. A design with a text
#: box there now gets the repair round that names the log_pane.
#:
#: The same reason holds the other way: "a multi-line text box that shows
#: the selected file's details" (M1) is a text box, which the grader wants
#: and a log_pane (one appended line per .set()) is not. It accepted a
#: log_pane until the 2026-10-09 review found M1 passing Describe with one
#: and failing the grade ("no text"). Only log wording ("a multi-line log
#: view") wants a log_pane, and that is read as a log view first.
_REQUEST_WORDS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("radio buttons", r"radio\s?-?(?:buttons?|options?)", ("radiobutton",)),
    ("a checkbox", r"check\s?-?(?:box(?:es)?|buttons?)|tick\s?-?box(?:es)?",
     ("checkbutton",)),
    ("a dropdown", r"drop\s?-?\s?downs?|combo\s?-?box(?:es)?", ("combobox",)),
    ("a spin box", r"spin\s?-?box(?:es)?|spinners?", ("spinbox",)),
    ("a progress bar", r"progress\s?-?bars?", ("progressbar",)),
    ("a status bar", r"status\s?-?bars?", ("status_bar",)),
    ("a menu bar", r"menu\s?-?bars?", ("menubar",)),
    ("a toolbar", r"tool\s?-?bars?", ("toolbar", "button")),
    ("a slider", r"sliders?", ("scale", "scrubber")),
    ("tabs", r"tabs?|tabbed", ("notebook",)),
    ("a table", r"tables?|tree\s?-?views?", ("treeview",)),
    ("a list", r"list\s?-?box(?:es)?|list\s+of", ("listbox", "treeview",
                                                  "combobox")),
    ("a chart", r"charts?|graphs?|plot\s+(?:area|view|panel|window)s?",
     ("chart_panel",)),
    ("an image area",
     r"image\s+(?:area|view|viewer|preview|panel|display|canvas)s?"
     r"|live\s+view|camera\s+view|video\s+(?:view|feed|preview)",
     ("image_canvas",)),
    ("a log view", r"(?:multi\s?-?\s?line\s+)?log\s+(?:view|pane|panel"
                   r"|window|area|output)s?|multi\s?-?\s?line\s+logs?",
     ("log_pane",)),
    ("a file or folder picker",
     r"(?:file|folder|directory)\s+(?:picker|chooser|selector)s?",
     ("file_picker",)),
    ("a multi-line text box",
     r"multi\s?-?\s?line(?:\s+\w+)?(?:\s+(?:box|area|field|text\s?box))?",
     ("text",)),
    # "a box to type a number" (S2) is a text box too: phi3.5 drew no
    # entry there and Describe saw no gap.
    ("a text box",
     r"text\s?-?box(?:es)?|text\s+fields?|input\s+(?:box|field)s?"
     r"|search\s+box(?:es)?|box(?:es)?\s+(?:to|for)\s+(?:typ|enter)\w*",
     ("entry", "text", "combobox", "spinbox")),
    ("a button", r"buttons?", ("button", "toolbar")),
)

_REQUEST_RES = tuple((what, re.compile(r"\b(?:" + words + r")\b",
                                       re.IGNORECASE), kinds)
                     for what, words, kinds in _REQUEST_WORDS)

#: "no status bar", "without a menu bar": named, but NOT wanted.
_NEGATED = re.compile(r"\b(?:no|not|without)(?:\s+(?:a|an|any|the))?\s*$",
                      re.IGNORECASE)

#: Named once per window however often the text mentions them — and "three
#: tabs" is ONE notebook. Every other kind is counted.
_UNCOUNTED = frozenset({"a status bar", "a menu bar", "a toolbar", "tabs"})
_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
#: "two dropdowns", "three radio buttons", "2 large charts".
_NUMBER_BEFORE = re.compile(r"\b(two|three|four|five|six|[2-6])\s+"
                            r"(?:[a-z-]+\s+)?$", re.IGNORECASE)
#: "a dropdown for X and a dropdown for Y": each "a"/"an" is one more —
#: up to two words between, a quoted caption counting as one ("a 'Debug
#: logging' checkbox", "a font size spin box").
_ARTICLE_BEFORE = re.compile(r"\b(?:a|an|one|another)\s+(?:(?:'[^'\n]*'|"
                             r"\"[^\"\n]*\"|[a-z-]+)\s+){0,2}$",
                             re.IGNORECASE)

#: A button's caption as a description gives it: a Capitalised word and up
#: to two lower-case ones ("Scan for cameras", "Clear all", "OK"), or a
#: quoted caption. Articles, numbers and the words that point or count are
#: never its first word ("Two buttons" asks for two buttons, not one called
#: Two) — and never one of its lower-case words either: those say the
#: phrase is a sentence ("Add two", "At the bottom", "Also add a", "There
#: are two", "Use the arrow"), not a caption. A preposition can be in one
#: ("Scan for cameras", "Sign in").
_NOT_FIRST = (r"A|An|The|One|Two|Three|Four|Five|Six|Some|Several|Both|Each"
              r"|All|More|Other|Another|Any|Every|No|This|That|These|Those"
              r"|Its|Their|Your|Our|My")
_NOT_INNER = (r"a|an|the|one|two|three|four|five|six|seven|eight|nine|ten"
              r"|it|its|this|that|these|those|them|their|and|or|buttons?")
_CAPTION = (r"(?:'[^'\n]{1,40}'|\"[^\"\n]{1,40}\""
            r"|(?!(?:" + _NOT_FIRST + r")\b)[A-Z][\w/&+-]*"
            r"(?:\s+(?!(?:" + _NOT_INNER + r")\b)[a-z][\w-]*){0,2})")
#: "Scan for cameras and Connect buttons", "Start, Pause and Reset buttons",
#: "a Plot button". Case-sensitive on purpose: only a Capitalised word or a
#: quoted one is a caption. It runs after "radio buttons", "Check Button"
#: and "toolbar" are blanked, so those never read as captions. What it
#: matches is checked again by _named_buttons, which knows where the
#: sentence starts.
_NAMED_BUTTONS = re.compile(
    r"(?<![\w'\"])(" + _CAPTION + r"(?:\s*,\s*" + _CAPTION + r")*"
    r"(?:\s*,?\s+(?:and|or)\s+" + _CAPTION + r")?)\s+([Bb]uttons?)\b")
_CAPTION_SPLIT = re.compile(r"\s*,?\s+(?:and|or)\s+|\s*,\s*")
#: Where a sentence or a clause starts: a word there is Capitalised by the
#: grammar, so its capital says nothing about a caption.
_CLAUSE_END = ".!?:;(\n—–•*-"
#: The words that open a sentence before a comma — "At bottom, Start and
#: Stop buttons", "Finally, Start and Stop buttons" — which the caption
#: list would otherwise read as its first item.
_OPENERS = frozenset(
    "At On In Under Below Above Beside Beneath Behind Underneath Over Across"
    " Along Around Inside Outside Within Without Near Between Beyond To From"
    " For With By After Before Then Also Finally Lastly Additionally"
    " Meanwhile Otherwise Optionally Ideally Plus Here There".split())

_ORDINALS = ("", "first", "second", "third", "fourth", "fifth", "sixth")


@dataclass(frozen=True)
class Wanted:
    """One widget the description names outright.

    ``what`` is said to a person and a model alike ("a dropdown"),
    ``kinds`` are the kinds that answer it, ``count`` how many the text
    asks for ("two dropdowns"; "a dropdown for X and a dropdown for Y"),
    and ``name`` the caption a button must carry ("Scan for cameras" in
    "Scan for cameras and Connect buttons")."""
    what: str
    kinds: Tuple[str, ...]
    count: int = 1
    name: str = ""
    #: The words that named it — a shape whose caption says them but whose
    #: kind does not answer is pointed out (llama3.1:8b's C3: a labelframe
    #: titled "Frame slider" holding a spin box, four rounds running).
    words: Optional["re.Pattern[str]"] = field(default=None, compare=False,
                                               repr=False)


#: What a description is OF, before it says what that holds: "An image
#: viewer: ...", "A table editor with ...". It names the app, not one more
#: widget — "An image viewer: a large image area" asks for ONE image area,
#: and counting both would send a right design back for a second.
_SUBJECT = re.compile(r"^[^.:\n]{0,60}?(?=:|\s+with\s)", re.IGNORECASE)


def _subject_end(text: str) -> int:
    """Where the subject phrase heading ``text`` ends; 0 when it has none."""
    m = _SUBJECT.match(text)
    return m.end() if m else 0


def _mentions(prefix: str) -> int:
    """How many widgets one match stands for, from the text before it."""
    num = _NUMBER_BEFORE.search(prefix)
    if num:
        word = num.group(1).lower()
        return _NUMBERS.get(word) or int(word)
    return 1 if _ARTICLE_BEFORE.search(prefix) else 0


def _opens_clause(before: str) -> bool:
    """Whether the text after ``before`` starts a sentence or a clause."""
    before = before.rstrip(" \t")
    return not before or before[-1] in _CLAUSE_END


def _captions(phrase: str, plural: bool, opens: bool) -> List[str]:
    """The captions a matched "X, Y and Z button(s)" phrase really names;
    [] when it names none.

    A Capitalised word is a caption because it is Capitalised where a
    sentence would not be — so at the start of a sentence it says nothing:
    "Control buttons", "Navigation buttons" and "Add buttons to ..." name a
    sort of button there. A sentence may still open with a LIST of
    captions ("OK and Cancel buttons below the tabs"). One caption before
    "buttons" names a sort anywhere ("the Zoom buttons"), never a caption.
    REVIEW (2026-10-09): reading every opening word as a caption sent right
    designs back to add a button labelled 'Add two' or 'At the bottom'."""
    caps = [c.strip().strip("'\"").strip()
            for c in _CAPTION_SPLIT.split(phrase)]
    sep = _CAPTION_SPLIT.search(phrase)
    if phrase.lstrip()[:1] in ("'", '"'):
        opens = False             # a quoted caption is one wherever it is
    if opens and len(caps) > 1 and sep and "," in sep.group(0) and \
            not re.search(r"\b(?:and|or)\b", sep.group(0)) and \
            caps[0].split()[0] in _OPENERS:
        # "Finally, Start and Stop buttons": the opener is not one of them,
        # and what follows its comma is mid-sentence
        caps, opens = caps[1:], False
    caps = [c for c in caps if c]
    return caps if len(caps) >= (2 if plural or opens else 1) else []


def _named_buttons(s: str, out: List[Wanted]) -> str:
    """Append a Wanted per button caption ``s`` names; ``s`` with those
    phrases blanked, so the bare "button" in them is not read again. A
    phrase that names no caption is left for the plain "a button" read."""
    def blank(m: "re.Match[str]") -> str:
        if _NEGATED.search(s[max(0, m.start() - 16):m.start()]):
            return " " * len(m.group(0))
        caps = _captions(m.group(1), plural=m.group(2).lower() == "buttons",
                         opens=_opens_clause(s[:m.start()]))
        if not caps:
            return m.group(0)
        for cap in caps:
            if not any(w.name.lower() == cap.lower() for w in out if w.name):
                out.append(Wanted(f"a button labelled '{cap}'",
                                  ("button", "toolbar"), 1, cap))
        return " " * len(m.group(0))
    return _NAMED_BUTTONS.sub(blank, s)


def requested_widgets(text: Any) -> List[Wanted]:
    """The widgets ``text`` names outright, in the order of _REQUEST_WORDS,
    each kind at most once — with how many it asks for, and one Wanted per
    button it names by its caption.

    A word match, like select_examples: dumb, predictable, testable. It is
    used only to RANK valid designs and to say what a repair round should
    add — a design is never refused because of it (see _describe).

    Kinds alone were not enough: qwen2.5's C1 repair dropped the "Scan for
    cameras" and "Connect" buttons the description names, kept Start/Stop
    capture, and was accepted as complete because A button was there; best-
    of-N then preferred it to the candidate that had both (2026-10-05)."""
    s = str(text or "")
    out: List[Wanted] = []
    head = _subject_end(s)
    for what, rx, kinds in _REQUEST_RES:
        if what == "a button":
            s = _named_buttons(s, out)
        found = False
        count = 0

        def blank(m: "re.Match[str]") -> str:
            nonlocal found, count
            before = s[max(0, m.start() - 40):m.start()]
            if not _NEGATED.search(before[-16:]):
                found = True
                if m.end() > head:
                    count += _mentions(before)
            return " " * len(m.group(0))

        s = rx.sub(blank, s)
        if found:
            n = 1 if what in _UNCOUNTED else max(1, count)
            out.append(Wanted(what, kinds, n, words=rx))
    return out


def _caption(shape: Any) -> str:
    props = getattr(shape, "props", {}) or {}
    return " ".join(str(t) for t in (getattr(shape, "label", "") or "",
                                     props.get("text") or "") if t).strip()


def _answering(shapes: Sequence[Shape], kinds: Sequence[str]) -> int:
    """How many widgets of ``kinds`` the design has; a toolbar answering
    "a button" counts each of its buttons."""
    n = 0
    for s in shapes:
        if s.kind not in kinds:
            continue
        buttons = (getattr(s, "props", {}) or {}).get("buttons")
        if s.kind == "toolbar" and "button" in kinds and \
                isinstance(buttons, list):
            n += max(1, len(buttons))
        else:
            n += 1
    return n


def _button_captions(shapes: Sequence[Shape]) -> List[str]:
    """Every caption a person could press, lower-cased: each button, each
    toolbar button, and a file picker's built-in "Browse"."""
    out: List[str] = []
    for s in shapes:
        if s.kind == "button":
            out.append(_caption(s).lower())
        elif s.kind == "toolbar":
            for b in (getattr(s, "props", {}) or {}).get("buttons") or []:
                if isinstance(b, dict):
                    b = b.get("text") or b.get("label") or ""
                out.append(str(b).lower())
        elif s.kind == "file_picker":
            out.append("browse")
    return out


def _says(caption: str, name: str) -> bool:
    """Whether ``caption`` carries the first word of ``name``: "Clear" is a
    "Clear all" button, "Scan" a "Scan for cameras" one — and a word one
    shortens to the other ("Prev" and "Previous") is the same word.
    Matching too loosely only loses a repair round; too strictly sends a
    right design back for one."""
    first = re.findall(r"[a-z0-9]+", name.lower())
    if not first:
        return False
    want = first[0]
    for word in re.findall(r"[a-z0-9]+", caption.lower()):
        if word == want or (min(len(word), len(want)) >= 3 and (
                word.startswith(want) or want.startswith(word))):
            return True
    return False


def _unanswered(names: Sequence[str], captions: Sequence[str]) -> List[int]:
    """Indexes of ``names`` no caption answers, each caption answering at
    most one name — "Clear" and "Clear log" are two buttons. A matching by
    augmenting paths, so the order the names come in cannot lose one."""
    owner: Dict[int, int] = {}

    def take(i: int, seen: set) -> bool:
        for j, cap in enumerate(captions):
            if j in seen or not _says(cap, names[i]):
                continue
            seen.add(j)
            if j not in owner or take(owner[j], seen):
                owner[j] = i
                return True
        return False

    return [i for i in range(len(names)) if not take(i, set())]


def _plural(noun: str) -> str:
    return noun + ("es" if noun.endswith(("x", "s", "sh", "ch")) else "s")


def _gap(w: Wanted, have: int, shapes: Sequence[Shape]) -> str:
    """One requested widget the design lacks, phrased for a person and a
    model alike: "a progress bar (progressbar)", "a second dropdown
    (combobox)"."""
    kinds = " or ".join(w.kinds)
    article, _sp, noun = w.what.partition(" ")
    if article not in ("a", "an"):
        noun = ""
    missing = w.count - have
    if have == 0 and w.count == 1:
        text = f"{w.what} ({kinds})"
    elif have == 0:
        text = f"{w.count} {_plural(noun) if noun else w.what} ({kinds})"
    elif missing == 1 and noun and have + 1 < len(_ORDINALS):
        text = f"a {_ORDINALS[have + 1]} {noun} ({kinds})"
    else:
        text = (f"{missing} more {_plural(noun) if noun else w.what} "
                f"({kinds})")
    if have == 0 and w.words is not None:
        for s in shapes:
            cap = _caption(s)
            if s.kind not in w.kinds and cap and w.words.search(cap):
                inner = _held_kinds(s, shapes)
                text += (f" — '{cap}' is a {s.kind}"
                         + (f" holding {_a(inner)}" if inner else "")
                         + f", not {w.what}")
                break
    return text


def _a(kinds: Sequence[str]) -> str:
    """"a spinbox", "an entry and a label"."""
    return " and ".join(("an " if k[:1] in "aeiou" else "a ") + k
                        for k in kinds)


def _held_kinds(box: Shape, shapes: Sequence[Shape]) -> List[str]:
    """The widget kinds inside a container, top first, at most two: what a
    caption that says "slider" really holds. llama3.1:8b's C3 drew a
    labelframe "Frame slider" around a spin box four rounds running, told
    only that a slider was missing (2026-10-05)."""
    if box.kind not in CONTAINER_KINDS:
        return []
    out: List[str] = []
    for s in sorted(shapes, key=lambda s: (s.y, s.x)):
        if s is box or s.kind in CONTAINER_KINDS or s.kind in out:
            continue
        if (box.x <= s.x and s.x + s.w <= box.x + box.w
                and box.y <= s.y and s.y + s.h <= box.y + box.h):
            out.append(s.kind)
    return out[:2]


def missing_widgets(shapes: Sequence[Shape], wanted: Sequence[Wanted]
                    ) -> List[str]:
    """Each requested widget the design does not answer, in the order the
    description names them: a kind with no shape, fewer shapes than the
    text counts, a button caption no button carries."""
    named = [w for w in wanted if w.name]
    lost = {id(named[i]) for i in _unanswered(
        [w.name for w in named], _button_captions(shapes))} if named else set()
    out: List[str] = []
    for w in wanted:
        if w.name:
            if id(w) in lost:
                out.append(f"{w.what} ({' or '.join(w.kinds)})")
            continue
        have = _answering(shapes, w.kinds)
        if have < w.count:
            out.append(_gap(w, have, shapes))
    return out


def example_wireframe(canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H
                      ) -> Optional[Dict[str, Any]]:
    """The DEFAULT worked example (EXAMPLE_NAME), in gui_examples._compact
    form, fitted to this canvas so that it passes the gate there (see
    _fitted), with every refused key stripped. None if it is not on disk,
    or if no fitting passes."""
    fitted = _fitted(EXAMPLE_NAME, int(canvas_w), int(canvas_h))
    return json.loads(fitted) if fitted else None


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


def _examples_block(names: Sequence[str], mode: str, canvas_w: int,
                    canvas_h: int) -> str:
    texts = [t for t in (_checked_example(n, mode, canvas_w, canvas_h)
                         for n in names) if t]
    if not texts:
        return ""
    what = "wireframe" if mode == "pixel" else "layout"
    if len(texts) == 1:
        head = (f"EXAMPLE — a correct {what} on this canvas, for a different "
                f"request. Copy its structure and spacing, not its content.")
    else:
        head = (f"EXAMPLES — correct {what}s on this canvas, for OTHER "
                f"requests. Copy their structure and spacing, not their "
                f"content.")
    return head + "\n" + "\n\n".join(texts)


_SKELETON = """Reply with ONLY a JSON object in this exact shape. No prose, no code fence:
{"window": {"title": "<window title>", "bg": "#rrggbb"},
 "shapes": [
  {"kind": "<a kind from the list>", "label": "<caption>", "x": 0, "y": 0, "w": 0, "h": 0, "props": {}}
 ]}
"bg" and "props" are optional."""

# ---- tree mode --------------------------------------------------------

_TREE_ROLE = ("You design GUI layouts for a desktop app designer. You "
              "describe the window as a TREE: rows, columns and containers "
              "holding widgets from a fixed catalogue. The designer computes "
              "every position and size, so you never write x, y, w or h.")

_TREE_NODES = """NODES
- {"kind": "column", "children": [...]} stacks its children top to bottom.
- {"kind": "row", "children": [...]} puts its children side by side, left to right.
- {"kind": "frame" | "labelframe" | "freeform", "label": "...", "children": [...]} a container; its children stack top to bottom. labelframe = a bordered group with its label as the caption.
- {"kind": "notebook", "label": "...", "children": [{"kind": "page", "label": "<tab title>", "children": [...]}, ...]} one page per tab.
- {"kind": "panedwindow", "label": "...", "props": {"orient": "horizontal"}, "children": [<pane>, <pane>]} panes split by a draggable divider.
- A widget: {"kind": "<a widget kind>", "label": "<caption>", "props": {...}}.
- "grow": true on any node gives it the spare room; false keeps it at its natural size. Tables, text, images, charts, logs and lists grow by themselves."""

_TREE_RULES = """RULES
- The outermost node is usually a column. A menubar or toolbar goes first in it, a status_bar last.
- Caption an input with a label widget just before it, in a row: {"kind": "row", "children": [{"kind": "label", "label": "Name"}, {"kind": "entry", "label": "Name"}]}
- Put every action on a button. Give each widget its own label.
- Only containers, rows and columns have "children"."""

_TREE_SKELETON = """Reply with ONLY a JSON object in this exact shape. No prose, no code fence:
{"window": {"title": "<window title>", "bg": "#rrggbb"},
 "layout": {"kind": "column", "children": [
  {"kind": "<a widget kind>", "label": "<caption>", "props": {}}
 ]}}
"bg", "props" and "grow" are optional."""


def _compose(text: str, canvas_w: int, canvas_h: int, *, help_on: bool,
             example: str, detail: bool, mode: str = "pixel") -> str:
    if mode == "tree":
        canvas = (f"WINDOW\n- {canvas_w} x {canvas_h} px; the designer fits "
                  f"the layout to it.")
        parts = [_TREE_ROLE, canvas, _TREE_NODES,
                 _catalogue(detail, gtree.LEAF_KINDS,
                            "Widget kinds — use ONLY these, spelled exactly "
                            "as written:"),
                 _TREE_RULES]
    else:
        canvas = (f"CANVAS\n"
                  f"- {canvas_w} x {canvas_h} px. The origin (0, 0) is the "
                  f"top-left corner; x grows right, y grows down.\n"
                  f"- Every x, y, w and h is an integer multiple of {GRID}.\n"
                  f"- Keep every shape at least {EDGE_MARGIN} px from the "
                  f"canvas edges.")
        parts = [_ROLE, canvas, _catalogue(detail), _RULES]
    if help_on:
        parts.append(DECLARATION_HELP)
    if example:
        parts.append(example)
    # The request goes in VERBATIM — it is the user's own words, and it is
    # never what gets shed or cut.
    parts.append("REQUEST\n" + text)
    parts.append(_TREE_SKELETON if mode == "tree" else _SKELETON)
    return "\n\n".join(parts)


def _assemble(text: str, *, canvas_w: int, canvas_h: int,
              budget_chars: Optional[int], mode: str = "pixel",
              examples: Optional[Sequence[str]] = None
              ) -> Tuple[str, List[str]]:
    """(prompt, what was shed to fit).

    Shed WHOLE SECTIONS in a fixed order — the extra examples from the least
    relevant, then declaration help, then the last example, then per-kind
    prop detail — and never cut text at a character count. A hard cap
    slicing the prompt would stop the catalogue mid-list, and a model that
    sees half the kinds treats the missing half as forbidden. If even the
    leanest prompt is over budget it is still sent whole; the caller notes
    it. The order means a 4096-token window still gets one example AND the
    style help, as it always did; a bigger window buys more examples.

    ``examples`` None selects them for the request (select_examples)."""
    budget = DEFAULT_BUDGET_CHARS if budget_chars is None else int(budget_chars)
    if examples is None:
        examples = select_examples(text, mode=mode, canvas_w=canvas_w,
                                   canvas_h=canvas_h)
    names = list(examples)
    n = len(names)

    def fewer(keep: int) -> List[str]:
        return [f"{n - keep} of {n} worked examples"] if keep < n else []

    one = "worked example" if n == 1 else "worked examples"
    plan: List[Tuple[bool, List[str], bool, List[str]]] = [
        (True, names[:keep], True, fewer(keep))
        for keep in range(n, 0, -1)]
    plan.append((False, names[:1], True, fewer(min(1, n)) + ["style help"]))
    plan.append((False, [], True, ["style help"] + ([one] if n else [])))
    plan.append((False, [], False, ["style help"] + ([one] if n else [])
                 + ["prop detail"]))
    if not n:
        plan.insert(0, (True, [], True, []))
    prompt, shed = "", []
    for help_on, ex, detail, shed in plan:
        block = _examples_block(ex, mode, canvas_w, canvas_h) if ex else ""
        prompt = _compose(text, canvas_w, canvas_h, help_on=help_on,
                          example=block, detail=detail, mode=mode)
        if len(prompt) <= budget:
            break
    if not names and "worked example" not in shed:
        shed = shed + ["worked example (none on disk passes at this canvas)"]
    return prompt, shed


def build_prompt(text: str, *, canvas_w: int = CANVAS_W,
                 canvas_h: int = CANVAS_H, toolkit: str = "qt",
                 budget_chars: Optional[int] = None,
                 mode: str = "pixel") -> str:
    """The first request for ``text``.

    ``toolkit`` does not change a word of it, deliberately: a .gspec is
    toolkit-neutral (gui_emit_qt parses the same Tk-format font string, and
    gui_colors.COLOUR_CAPS gates both emitters), so a prompt that named either
    toolkit would steer the model toward widgets the other target lacks."""
    return _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                     budget_chars=budget_chars, mode=mode)[0]


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


def _render_bad(bad: Any) -> str:
    if gtree.looks_like_tree(bad):
        layout = next((bad[k] for k in ("layout", "root", "tree", "ui",
                                        "body", "main")
                       if isinstance(bad.get(k), (dict, list))), None)
        return gtree.render_reply(bad.get("window"), layout
                                  if layout is not None else bad)
    return render_wireframe(bad)


def _shown(bad: Any, raw: str, cap: int = 3000) -> str:
    """What the model returned, capped. Cut at a LINE so the model never sees
    a shape sliced mid-object and "repairs" the slice."""
    if not isinstance(bad, dict):
        return (raw or "")[:min(1000, cap)]
    text = _render_bad(bad)
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

#: Said when the model sent back an answer it had already sent: the same
#: prompt would get the same answer, so the prompt changes too.
REPEAT_NOTE = ("You sent this same answer before. Change it this time: fix "
               "each point below, even if that means a different layout.")


def repair_prompt(text: str, bad: Any, errors: Sequence[str], *,
                  raw: str = "", canvas_w: int = CANVAS_W,
                  canvas_h: int = CANVAS_H,
                  budget_chars: Optional[int] = None, mode: str = "pixel",
                  examples: Optional[Sequence[str]] = None,
                  repeated: bool = False) -> str:
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
    # Faults about a tree name nodes by their path; about a list of rows,
    # by position — and only the latter needs saying how positions count.
    where = ("" if gtree.looks_like_tree(bad) else
             " (shapes are numbered from 1, in the order you listed them)")
    prompt = ""
    for cap, bullets in _REPAIR_PLANS:
        head = ("Your previous answer was rejected.\n\n"
                + (REPEAT_NOTE + "\n\n" if repeated else "")
                + "WHAT YOU RETURNED\n" + _shown(bad, raw, cap) + "\n\n"
                "WHAT IS WRONG" + where + "\n" + _bullets(errors, bullets)
                + "\n\n")
        body, _ = _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                            budget_chars=budget - len(head) - len(tail),
                            mode=mode, examples=examples)
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


_PY_WORDS = {"True": "true", "False": "false", "None": "null"}


def _skip_blank(text: str, j: int) -> int:
    """The index of the next character that is not whitespace or inside a
    comment — so "x,  /* done */ ]" is a trailing comma too."""
    n = len(text)
    while j < n:
        if text[j] in " \t\r\n":
            j += 1
        elif text.startswith("//", j):
            k = text.find("\n", j)
            j = n if k < 0 else k
        elif text.startswith("/*", j):
            k = text.find("*/", j + 2)
            j = n if k < 0 else k + 2
        else:
            break
    return j


def _clean_json(text: str) -> str:
    """Near-JSON a small model writes -> JSON, outside strings only:
    // and /* */ comments dropped, a comma before } or ] dropped, Python's
    True / False / None spelled the JSON way, 'single' quotes made double.

    Each of these cost a whole round before (STAGE_NO_JSON, "use double
    quotes, no trailing commas and no comments") for an answer whose
    content was fine. Only used when strict parsing has already failed, so
    a reply that parses is never rewritten.

    Prose before the first "{" is kept as written, and an apostrophe inside
    a word is not a quote: "Here's the layout:" opened a single-quoted
    string that swallowed the whole JSON after it (review finding)."""
    start = text.find("{")
    head, text = (text[:start], text[start:]) if start > 0 else ("", text)
    out: List[str] = [head]
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                # ' is how a single-quoted string holds an apostrophe; in
                # JSON it is not an escape at all.
                out.append("'" if (quote == "'" and nxt == "'") else ch + nxt)
                i += 2
                continue
            if ch == quote:
                out.append('"')
                quote = ""
            elif ch == '"' and quote == "'":
                out.append('\\"')
            else:
                out.append(ch)
            i += 1
            continue
        if ch == "'" and i and text[i - 1].isalnum():
            out.append(ch)               # an apostrophe: "you'd", "users'"
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append('"')
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == ",":
            j = _skip_blank(text, i + 1)
            if j < n and text[j] in "}]":
                i += 1
                continue
        if ch.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            out.append(_PY_WORDS.get(word, word))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _close_truncated(text: str) -> Optional[str]:
    """A reply cut off by num_predict, closed after its LAST COMPLETE element
    of a list — a whole shape row, or a whole tree node — or None.

    A grammar cannot close an object the token limit cut short, and before
    this the whole reply was discarded: a 40-widget answer lost for its 41st.
    What survives is real model output, never invented; check_reply then
    judges it like any other reply, and describe() notes what was lost."""
    start = text.find("{")
    if start < 0:
        return None
    stack: List[str] = []
    in_str = esc = False
    cut: Optional[Tuple[int, List[str]]] = None
    for k in range(start, len(text)):
        ch = text[k]
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
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return None                 # it closed: not truncated
            if stack[-1] == "[":
                cut = (k + 1, list(stack))
    if cut is None:
        return None
    end, open_ = cut
    closers = "".join("]" if c == "[" else "}" for c in reversed(open_))
    return text[start:end] + closers


_REPLY_KEYS = ("shapes", "layout", "window", "root", "tree", "ui")


def _reply_like(payload: Any) -> bool:
    """A whole reply rather than a piece of one."""
    return isinstance(payload, dict) and (
        any(k in payload for k in _REPLY_KEYS)
        or gtree.looks_like_tree(payload))


def parse_reply(text: Any) -> Tuple[Any, List[str], bool]:
    """(payload, notes, salvaged). payload None when nothing parses.

    Strict first (nx_generate's scanner); then the near-JSON clean-up; then,
    for a reply that stops mid-object, the salvage of its complete part."""
    raw = text if isinstance(text, str) else ("" if text is None
                                              else str(text))
    if _json_depth(raw) > MAX_JSON_DEPTH:
        return None, [], False          # _no_json_fault says why
    payload = _extract_json(raw)
    if _reply_like(payload):
        return payload, [], False
    # The scanner, finding the whole object unparseable, carries on INTO it
    # and returns the last inner object that parses — one shape, or the
    # window. So near-JSON is tried before taking that.
    cleaned = _clean_json(raw)
    if cleaned != raw:
        fixed = _extract_json(cleaned)
        if _reply_like(fixed):
            return fixed, ["read the reply as JSON after removing comments, "
                           "trailing commas or Python spellings"], False
    if payload is not None:
        return payload, [], False
    if _looks_cut_off(cleaned):
        closed = _close_truncated(cleaned)
        if closed:
            try:
                payload = json.loads(closed)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return payload, [], True
    return None, [], False


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


def _json_depth(text: str) -> int:
    """How deep the reply's brackets nest, outside strings. A loop, not a
    recursion — it is what keeps a reply nested past the interpreter's
    recursion limit away from everything that recurses (MAX_JSON_DEPTH)."""
    depth = deepest = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
            if depth > deepest:
                deepest = depth
        elif ch in "}]":
            depth = max(0, depth - 1)
    return deepest


def _no_json_fault(raw: str) -> str:
    base = "the reply contained no JSON object"
    if _json_depth(raw) > MAX_JSON_DEPTH:
        return (base + f" it could read — lists and objects nest more than "
                f"{MAX_JSON_DEPTH} levels deep; reply with the flat structure "
                f"asked for")
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
                canvas_h: int = CANVAS_H,
                tidy: Optional[bool] = None) -> Checked:
    """Take one model reply as far through the layers as it gets.

    Either form is accepted whatever was asked for: a pixel reply ("shapes"
    rows with x/y/w/h) or a layout TREE ("layout"), which gui_describe_tree
    places and which then goes through the very same row checks and gate.
    A model asked for one and writing the other is still read.

    ``tidy`` (default: yes for pixel replies, no for trees) runs gui_snap
    and the measured repairs. A laid-out tree is already on the grid with
    every child inset and every sibling apart; snapping it only moved rows
    between pages that happened to share a y.

    The gate runs only on a schema-clean reply: laying out a wireframe with
    rows missing reports faults about the gaps (a notebook "missing" a page
    whose row was rejected) that vanish once the row is fixed, and a model
    told to fix a phantom tends to break something real."""
    raw = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    payload, parse_notes, salvaged = parse_reply(raw)
    if payload is None:
        return Checked(STAGE_NO_JSON, [_no_json_fault(raw)], None, raw)
    if gtree.looks_like_tree(payload):
        out = _check_tree(payload, raw, canvas_w, canvas_h)
    else:
        out = _check_rows(payload, raw, canvas_w, canvas_h,
                          tidy=True if tidy is None else bool(tidy))
    out.notes = parse_notes + out.notes
    if salvaged:
        out.salvaged = True
        what = ("widget" if out.tree else "shape")
        out.notes.insert(0, f"the reply was cut off before it finished; kept "
                            f"the {len(out.shapes) or out.accepted} complete "
                            f"{what}(s) before the cut — anything after it "
                            f"was lost, so add it by hand or describe less "
                            f"at once")
    return out


def _check_rows(payload: Any, raw: str, canvas_w: int, canvas_h: int, *,
                tidy: bool = True) -> Checked:
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
    if tidy:
        shapes, wheres = _unwrap_pages(shapes, wheres, notes)
        shapes, tidy_notes = _tidy(shapes, wheres, canvas_w, canvas_h)
        notes.extend(tidy_notes)
    _assign_z(shapes)
    faults = _gate(shapes, wheres, window, canvas_w, canvas_h)
    if faults:
        return Checked(STAGE_GATE, faults, payload, raw, [], window, notes)
    return Checked(STAGE_OK, [], payload, raw, shapes, window, notes)


def _check_tree(payload: Any, raw: str, canvas_w: int, canvas_h: int
                ) -> Checked:
    """A tree reply: parse -> lay out -> the row checks and the gate.

    Faults the row checks name by POSITION ("shape 4 (entry "Name")") are
    rewritten to the tree path the model wrote (column > row 2 > entry
    "Name"): it never saw a flat list, so a position means nothing to it.
    The checked payload kept for a repair round is the TREE, for the same
    reason."""
    parsed = gtree.parse(payload)
    if parsed.faults:
        return Checked(STAGE_SCHEMA, parsed.faults, payload, raw, [], {},
                       parsed.notes, accepted=parsed.good, tree=True)
    lay = gtree.layout(parsed.root, canvas_w, canvas_h, window=parsed.window)
    if lay.faults:
        return Checked(STAGE_GATE, lay.faults, payload, raw, [], {},
                       parsed.notes + lay.notes, accepted=1, tree=True)
    inner = _check_rows(lay.payload, raw, canvas_w, canvas_h, tidy=False)
    rows = lay.payload.get("shapes") or []
    names = {}
    for i, (row, path) in enumerate(zip(rows, lay.paths)):
        names[_where(i, row.get("kind"), row.get("label"))] = \
            gtree._short(path)

    def terms(text: str) -> str:
        for theirs in sorted(names, key=len, reverse=True):
            if theirs in text:
                text = text.replace(theirs, names[theirs])
        return text

    return Checked(inner.stage, [terms(f) for f in inner.faults], payload,
                   raw, inner.shapes, inner.window,
                   parsed.notes + lay.notes + [terms(n) for n in inner.notes],
                   accepted=inner.accepted or len(rows), tree=True)


# ============================================================
# Entry point
# ============================================================

def describe(text: str, *,
             model_call: Optional[Callable[..., str]] = None,
             canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H,
             toolkit: str = "qt", max_attempts: int = 3,
             budget_chars: Optional[int] = None,
             profile: Optional[Profile] = None,
             should_stop: Optional[Callable[[], bool]] = None
             ) -> DescribeResult:
    """A plain-English description -> a validated wireframe. NEVER RAISES.

    No model -> ok=False and ZERO calls, mirroring gui_classify.classify: the
    designer must keep working with no model loaded, and the caller shows the
    reason. A model that raises -> ok=False with the exception. Exhausting
    ``max_attempts`` ROUNDS -> ok=False, the best attempt's faults and raw
    reply, and NO shapes.

    ``profile`` (profile_for) sets the mode, the schema, the round-1
    candidates and the budget; None is the plain behaviour, one call per
    round with the prompt alone. ``should_stop`` is checked before every
    call, and passed to a model call that accepts it so a generation in
    progress can be cancelled too."""
    res = DescribeResult()
    try:
        _describe(res, text, model_call, int(canvas_w), int(canvas_h),
                  toolkit, max_attempts, budget_chars, profile or LEGACY,
                  should_stop)
    except Exception as exc:              # the promise is "never raises"
        res.ok = False
        res.shapes = []
        res.window = {}
        res.errors.append(f"describe failed unexpectedly: {exc!r}")
    return res


def _reply_key(cand: Checked) -> str:
    """What makes two replies "the same answer": the parsed payload when
    there is one (whitespace and key order do not count), else the text."""
    try:
        body = (json.dumps(cand.payload, sort_keys=True)
                if cand.payload is not None else str(cand.raw).strip())
    except (TypeError, ValueError):
        body = str(cand.raw)
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


def _call_options(prof: Profile, round_: int, k: int, repeats: int,
                  schema: Optional[Dict[str, Any]],
                  should_stop: Optional[Callable[[], bool]]
                  ) -> Dict[str, Any]:
    """The keyword arguments for one call. Round 1 candidate k gets
    temperatures[k] and seed + k; a repair runs cold (REPAIR_TEMPERATURE)
    unless the model has been repeating itself, which warms it by
    REPEAT_STEP per repeat and moves the seed — the same prompt at the same
    temperature and seed is the same answer."""
    opts: Dict[str, Any] = {"json_schema": schema, "should_stop": should_stop}
    if not prof.custom:
        return opts
    if round_ == 1 and prof.temperatures:
        opts["temperature"] = prof.temperatures[min(k, len(prof.temperatures)
                                                    - 1)]
    elif round_ > 1:
        opts["temperature"] = min(0.9, REPAIR_TEMPERATURE
                                  + REPEAT_STEP * repeats)
    if prof.seed is not None:
        opts["seed"] = (prof.seed + k if round_ == 1
                        else prof.seed + 100 * round_ + repeats)
    opts["num_predict"] = prof.reply_tokens
    return opts


def _describe(res: DescribeResult, text: Any,
              model_call: Optional[Callable[..., str]], canvas_w: int,
              canvas_h: int, toolkit: str, max_attempts: Any,
              budget_chars: Optional[int], prof: Profile,
              should_stop: Optional[Callable[[], bool]]) -> None:
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
    rounds = max(1, int(max_attempts))
    if budget_chars is not None:
        budget = int(budget_chars)
    else:
        budget = prof.budget_chars if prof.custom else DEFAULT_BUDGET_CHARS
    mode = prof.mode if prof.mode in MODES else "pixel"
    res.mode = mode

    examples = select_examples(text, mode=mode, canvas_w=canvas_w,
                               canvas_h=canvas_h)
    prompt, shed = _assemble(text, canvas_w=canvas_w, canvas_h=canvas_h,
                             budget_chars=budget, mode=mode,
                             examples=examples)
    notes: List[str] = []
    if prof.custom and prof.reason:
        notes.append("design: " + prof.reason)
    if shed:
        notes.append("to fit the model's context the prompt left out: "
                     + ", ".join(shed))
    if len(prompt) > budget:
        limit = int(math.ceil(max(0, budget) / CHARS_PER_TOKEN))
        notes.append(f"the prompt is ~{estimate_tokens(prompt)} tokens, over "
                     f"the ~{limit}-token budget even after shedding — a "
                     f"shorter description may work better")
    schema = reply_schema(prof, canvas_w, canvas_h) if prof.constrained \
        else None
    # The widgets the description names. Only a real model's profile checks
    # them: the plain one-call path (every stub, the Tk shell) is unchanged.
    wanted = requested_widgets(text) if prof.custom else []

    best: Optional[Checked] = None
    # A cut-off reply whose complete part passes: kept, and returned if no
    # WHOLE answer arrives — but while calls remain, the model is asked for
    # the whole thing, because what was cut off is part of what the user
    # asked for.
    held: Optional[Checked] = None
    # The valid design that leaves out the fewest requested widgets, and
    # which. Best-of-N used to take the FIRST valid candidate: phi3.5's
    # failures were all valid layouts missing a widget the request named,
    # while another candidate or one repair round would have had it. A
    # valid design is still never refused for this — it is returned, with
    # a note, when no complete one arrives.
    short: Optional[Checked] = None
    short_gaps: List[str] = []
    seen: Dict[str, int] = {}
    repeats = 0
    calls = 0

    def fallback() -> Optional[Tuple[Checked, List[str]]]:
        """The design to return when no complete one arrived: the valid
        one with the fewest gaps, a whole answer winning a tie with a
        salvaged (cut-off) one."""
        options = []
        if short is not None:
            options.append((len(short_gaps), 0, short, short_gaps))
        if held is not None:
            gaps = missing_widgets(held.shapes, wanted)
            options.append((len(gaps), 1, held, gaps))
        if not options:
            return None
        _n, _o, cand, gaps = min(options, key=lambda o: (o[0], o[1]))
        return cand, gaps

    def gap_note(gaps: List[str]) -> List[str]:
        if not gaps:
            return []
        return [f"the design leaves out {', '.join(gaps)}, which the "
                f"description asks for — draw {'it' if len(gaps) == 1 else 'them'}"
                f" in, or describe it again"]

    for round_ in range(1, rounds + 1):
        res.rounds = round_
        tries = max(1, prof.n_best) if round_ == 1 else 1
        for k in range(tries):
            if should_stop is not None and should_stop():
                res.attempts = calls
                fb = fallback()
                if fb is not None:
                    _accept(res, fb[0], notes + gap_note(fb[1]))
                    return
                res.errors = ["stopped before the model was asked again"]
                _fail(res, best, notes, keep_errors=True)
                return
            opts = _call_options(prof, round_, k, repeats, schema,
                                 should_stop)
            try:
                reply = call_model(model_call, prompt, **opts)
            except Exception as exc:
                res.attempts = calls + 1
                fb = fallback()
                if fb is not None:
                    _accept(res, fb[0], notes + gap_note(fb[1])
                            + [f"the next model call failed: {exc!r}"])
                    return
                res.errors = [f"the model call failed: {exc!r}"]
                _fail(res, best, notes, keep_errors=True)
                return
            calls += 1
            res.attempts = calls
            cand = check_reply(reply, canvas_w=canvas_w, canvas_h=canvas_h)
            more = round_ < rounds or k < tries - 1
            if cand.ok and cand.salvaged and more:
                held = held or cand
                cand = Checked(STAGE_NO_JSON, [_no_json_fault(cand.raw)],
                               None, cand.raw)
            if cand.ok:
                gaps = missing_widgets(cand.shapes, wanted)
                if not gaps:
                    if calls > 1 and prof.n_best > 1 and round_ == 1:
                        notes.append(f"candidate {k + 1} of {tries} passed")
                    _accept(res, cand, notes)
                    return
                # Ties go to the newer design: a repair round was shown
                # the older one and asked to add what it lacked.
                if short is None or len(gaps) <= len(short_gaps):
                    short, short_gaps = cand, gaps
                if not more:
                    break
                # Ranked as valid (its stage) with the gaps as its faults,
                # so it outranks every invalid reply and a repair round
                # starts from it, told exactly what to add.
                cand = replace(cand, faults=[
                    f"missing {g}: the description asks for it — add it, "
                    f"and keep everything else" for g in gaps])
            key = _reply_key(cand)
            if key in seen:
                repeats += 1
            seen[key] = seen.get(key, 0) + 1
            # Never let a worse candidate replace a better one: the repair
            # prompt is built from the BEST answer so far, so a reply that
            # regresses to prose does not make the next round start from
            # nothing. Ties go to the newer answer, so a model that is stuck
            # is at least shown the answer it just gave rather than an
            # identical prompt again.
            if best is None or cand.rank() >= best.rank():
                best = cand
        if round_ < rounds:
            repeated = seen.get(_reply_key(best), 0) > 1
            prompt = repair_prompt(text, best.payload, best.faults,
                                   raw=best.raw, canvas_w=canvas_w,
                                   canvas_h=canvas_h, budget_chars=budget,
                                   mode="tree" if best.tree else mode,
                                   examples=examples, repeated=repeated)
            if len(prompt) > budget and not any("repair prompt" in n
                                                for n in notes):
                notes.append(f"a repair prompt is ~{estimate_tokens(prompt)} "
                             f"tokens, over the budget even at its leanest — "
                             f"a shorter description may work better")
    fb = fallback()
    if fb is not None:
        _accept(res, fb[0], notes + gap_note(fb[1]))
        return
    res.errors = []
    _fail(res, best, notes, keep_errors=False)


def _accept(res: DescribeResult, cand: Checked, notes: List[str]) -> None:
    res.ok = True
    res.errors = []
    res.shapes = cand.shapes
    res.window = cand.window
    res.mode = "tree" if cand.tree else "pixel"
    res.notes = notes + cand.notes
    res.raw = cand.raw


def _fail(res: DescribeResult, best: Optional[Checked], notes: List[str],
          *, keep_errors: bool) -> None:
    """No shapes AND no window: a caller that applied the window title of a
    refused design would leave half of it on the user's canvas."""
    res.ok = False
    res.shapes = []
    res.window = {}
    if best is not None:
        res.errors = (list(res.errors) if keep_errors else []) + best.faults
        res.raw = best.raw
        res.mode = "tree" if best.tree else res.mode
        notes = notes + best.notes
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
