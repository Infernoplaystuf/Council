"""
gui_classify.py — *** THE ONLY MODULE IN THE FEATURE THAT CALLS A MODEL. ***

Untyped rectangles -> widget kinds + properties, validated against the
catalogue before anything is emitted.

WHY THIS IS THE ONLY ONE
------------------------
A drawn wireframe already encodes position, nesting, z-order, alignment,
grouping, resize behaviour and labels — computably, with no inference. The one
thing it cannot encode is what an UNLABELLED, UNTYPED box was meant to be. So
the model is asked exactly that and nothing else, and its answer is checked
against a fixed catalogue before it can reach the emitter. A wireframe built
entirely from typed palette shapes never loads a model at all, which is not an
optimisation but the reliability story: the common path has no inference in it.

model_call is INJECTED (house style: tool_forge.generate_tool,
nx_generate.generate), so every test here runs with a scripted stub and no GGUF.
Swapping to the coder role is the CALLER's job — this module must not reach for
role_models, or it stops being testable.

WHY A LOW-CONFIDENCE ANSWER BECOMES A QUESTION, NOT A GUESS
-----------------------------------------------------------
"Layer 47" is genuinely ambiguous: a label, an entry, or a scrubber's readout.
Silently picking one produces a GUI that looks finished and is wrong in a way
the user only discovers by using it. Asking costs one click and the answer is
persisted into the .gspec (council_core.designer_project.generate writes every
accepted kind back), so it is asked exactly once.

WHY BOXES ARE NUMBERED, NOT NAMED BY ID
--------------------------------------
MEASURED with the phi3.5 tokenizer: a shape id is 32 hex characters and 32
TOKENS, and a pretty classification row 71, so the 700-token reply held about
nine boxes — the tenth was cut off, the JSON failed to parse, and after three
attempts every box became a flagged label. And a small model has to echo 32
hex digits exactly. "box": 3 is one token and cannot be mistyped into another
box's number. The number maps back to the id here.

WHY THE PROMPT LISTS EVERY KIND'S PROPS
---------------------------------------
validate_answer refuses a prop the kind does not have, and the prompt used to
list only the kind NAMES while saying "only use property names that belong to
the kind" — so any prop the model guessed cost a round. The catalogue now
carries each kind's props and their types (gui_describe's own catalogue), the
reply schema allows exactly those (json_schema=, for a constrained backend),
and each prop is type-checked with gui_describe's checker: "values": "Low,High"
used to pass and fill a combobox with the characters L, o, w, ",", H.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gui_shapes import GENERIC_KIND, PALETTE, Shape

# Below this, the model is guessing and the user is asked instead (spec 10.2).
CONFIDENCE_FLOOR = 0.7

# Kinds the classifier may choose. The catalogue minus the placeholder itself —
# "generic" is the question, it cannot be the answer.
CLASSIFIABLE = tuple(sorted(k for k in PALETTE if k != GENERIC_KIND))

# When every attempt fails, a shape becomes this. A label renders, is visible,
# and is obviously wrong — a silent drop would leave a hole the user has to
# notice, and a guessed treeview would look deliberate.
FALLBACK_KIND = "label"

#: The confidences a constrained reply may give. A closed list rather than a
#: number: the llama.cpp converter allows 16 decimal digits for a "number",
#: and nothing here needs more than one.
CONFIDENCES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

#: Reply tokens: one minified row is ~25-40 tokens on the phi3.5 tokenizer
#: (box number, kind, confidence, a few props); ROW_TOKENS leaves room for a
#: list prop, BASE_TOKENS for the wrapper. Scaled so ten boxes no longer cut
#: the reply off at the ninth.
BASE_TOKENS, ROW_TOKENS, MIN_TOKENS, MAX_TOKENS = 96, 64, 256, 4096
#: Repairs run cold, and warmer each time the model repeats an answer.
REPAIR_TEMPERATURE, REPEAT_STEP, BASE_SEED = 0.1, 0.25, 1


def num_predict_for(n_boxes: int) -> int:
    """Reply tokens for ``n_boxes`` rows — sized to the answer, not fixed."""
    return max(MIN_TOKENS, min(MAX_TOKENS,
                               BASE_TOKENS + ROW_TOKENS * max(1, n_boxes)))


@dataclass
class Classification:
    shape_id: str
    kind: str
    confidence: float = 0.0
    props: Dict[str, Any] = field(default_factory=dict)
    flagged: bool = False        # the model failed; this is the fallback
    reason: str = ""


@dataclass
class Question:
    """A clarification for the bottom pane (spec 10.2)."""
    shape_id: str
    question: str
    options: List[str] = field(default_factory=list)
    default: str = ""


def needs_model(shapes: Sequence[Shape]) -> bool:
    """True only when at least one shape is still untyped."""
    return any(s.kind == GENERIC_KIND for s in shapes)


# ============================================================
# Prompt (spec 10.1)
# ============================================================

def describe_shape(s: Shape, container: Optional[Shape],
                   siblings: Sequence[Shape],
                   number: Optional[int] = None) -> str:
    """One generic shape, as the model should see it.

    Relative geometry matters more than absolute: "spans 95% of its container's
    width, at the very top" is what identifies a toolbar, and it survives the
    user resizing the canvas, which raw pixels do not."""
    head = f"- box {number}:" if number is not None else f"- id: {s.id}"
    lines = [head,
             f'  label: {s.label or "(none)"}',
             f'  note: {s.note or "(none)"}',
             f'  size_px: {s.w}x{s.h} at ({s.x}, {s.y})']
    if container is not None and container.w > 0 and container.h > 0:
        lines.append(
            f'  within {container.kind}: '
            f'{s.w / container.w:.0%} of its width, '
            f'{s.h / container.h:.0%} of its height, '
            f'starting {(s.x - container.x) / container.w:.0%} across and '
            f'{(s.y - container.y) / container.h:.0%} down')
    else:
        lines.append('  within: the main window')
    sib = [x.label for x in siblings if x.id != s.id and x.label]
    if sib:
        lines.append(f'  siblings: {", ".join(sib[:8])}')
    return "\n".join(lines)


def _catalogue(detail: bool = True) -> str:
    """Every classifiable kind with its props and their types — the same
    lines gui_describe shows a designing model, so the two agree. Without
    ``detail``, the kinds alone (what a long box list sheds first)."""
    try:
        import gui_describe
        return gui_describe._catalogue(
            detail, CLASSIFIABLE,
            "You may ONLY use these widget kinds, spelled exactly as written, "
            "and only the props listed for each:" if detail else
            "You may ONLY use these widget kinds, spelled exactly as "
            "written:")
    except Exception:                    # pragma: no cover - always present
        return ("You may ONLY use these widget kinds, spelled exactly as "
                "written:\n" + ", ".join(CLASSIFIABLE))


#: The window a prompt and its reply share when the caller does not say
#: (gui_describe.N_CTX, the engine's default load). Measured: with every
#: kind's props listed, 20 boxes estimate 2,311 prompt + 1,376 reply tokens;
#: past that the prop detail is shed, as gui_describe sheds it.
N_CTX, CHARS_PER_TOKEN, SLACK_TOKENS = 4096, 2.9, 256


def fits(prompt: str, n_boxes: int, n_ctx: int = N_CTX) -> bool:
    """Prompt (estimated) + the reply budget + template slack <= window."""
    return (len(prompt) / CHARS_PER_TOKEN + num_predict_for(n_boxes)
            + SLACK_TOKENS) <= n_ctx


def build_prompt(items: Sequence[str], *, detail: bool = True) -> str:
    """The constrained request. The catalogue IS the vocabulary."""
    return f"""You are labelling boxes in a hand-drawn wireframe of a desktop app.

Each box below is an UNTYPED rectangle, numbered. Decide which widget it was
meant to be, using its label, note, size and position.

{_catalogue(detail)}

BOXES
{chr(10).join(items)}

Reply with ONLY a JSON object in this exact shape. No prose, no code fence:

{{"shapes": [
  {{"box": 1, "kind": "<one kind from the list>", "confidence": 0.9, "props": {{}}}}
]}}

Rules:
- One entry per box, using the box's number.
- confidence is 0.0-1.0: how sure you are. Be honest; a low number asks the
  user rather than guessing wrong.
- props is optional. Only use the props listed for the kind you chose.
- A wide, short box at the top or bottom is usually a toolbar or status_bar.
- A large box with a note about images, layers or previews is an image_canvas.
- A box with column-like labels is a treeview."""


REPEAT_NOTE = ("You sent this same answer before. Change it this time: fix "
               "each point below.")


def repair_prompt(items: Sequence[str], bad: Any, errors: Sequence[str],
                  *, repeated: bool = False, detail: bool = True) -> str:
    """One repair pass: hand the model its own output and the exact faults.

    Same shape as nx_generate.repair_prompt — showing the model what it
    returned alongside what was wrong repairs far more reliably than restating
    the original request, which it has already demonstrably misread."""
    return f"""Your previous answer was rejected.
{(chr(10) + REPEAT_NOTE + chr(10)) if repeated else ""}
WHAT YOU RETURNED
{json.dumps(bad, indent=2)[:3000]}

WHAT IS WRONG
{chr(10).join('- ' + e for e in errors)}

{build_prompt(items, detail=detail)}
Fix every point above. Reply with ONLY the corrected JSON object."""


def _prompt(make: Callable[..., str], n_boxes: int, n_ctx: int) -> str:
    """``make(detail=True)``, or the kinds-only form when that does not fit
    the window — never cut mid-catalogue."""
    full = make(detail=True)
    return full if fits(full, n_boxes, n_ctx) else make(detail=False)


# ============================================================
# The reply schema (constrained decoding)
# ============================================================

def answer_schema(numbers: Sequence[int]) -> Dict[str, Any]:
    """The reply as JSON Schema: one row per box, the box an enum of the
    numbers asked about, one variant per kind with that kind's props typed
    (gui_describe_tree.props_def — the same definitions Describe uses)."""
    import gui_describe_tree as gtree
    nums = sorted({int(n) for n in numbers}) or [1]
    defs = gtree.shared_defs(CLASSIFIABLE)
    defs["box"] = {"enum": nums}
    defs["confidence"] = {"enum": list(CONFIDENCES)}
    variants = [{"type": "object",
                 "properties": {"box": {"$ref": "#/$defs/box"},
                                "kind": {"const": k},
                                "confidence": {"$ref": "#/$defs/confidence"},
                                "props": {"$ref": f"#/$defs/props-{k}"}},
                 "required": ["box", "kind", "confidence"],
                 "additionalProperties": False} for k in CLASSIFIABLE]
    defs["row"] = {"anyOf": variants}
    return {"$defs": defs, "type": "object",
            "properties": {"shapes": {"type": "array",
                                      "items": {"$ref": "#/$defs/row"},
                                      "minItems": 1,
                                      "maxItems": len(nums)}},
            "required": ["shapes"], "additionalProperties": False}


# ============================================================
# Validation
# ============================================================

def _row_id(row: Dict[str, Any], numbers: Dict[int, str],
            want: set) -> Tuple[str, str]:
    """(shape id, how to name it in a fault). "box": n first; an exact
    "id" is still read, for a reply written against an older prompt."""
    box = row.get("box")
    if isinstance(box, str) and box.strip().lstrip("#").isdigit():
        box = int(box.strip().lstrip("#"))
    if isinstance(box, float) and box.is_integer():
        box = int(box)
    if isinstance(box, int) and not isinstance(box, bool):
        sid = numbers.get(box, "")
        return (sid if sid in want else ""), f"box {box}"
    sid = str(row.get("id") or "")
    return (sid if sid in want else ""), (f"id {sid!r}" if sid else "")


def _check_prop(kind: str, pk: str, pv: Any) -> Tuple[bool, Any, str]:
    """(ok, value, why) — gui_describe's type check, and the kind's choices."""
    schema = PALETTE[kind].get("prop_schema") or {}
    pdef = schema[pk]
    try:
        import gui_describe
        ok, value, why = gui_describe._coerce(pdef, pv)
    except Exception:                    # pragma: no cover - always present
        ok, value, why = True, pv, ""
    if not ok:
        return False, None, why
    choices = pdef.get("choices")
    if choices and value not in choices:
        return False, None, f"must be one of {', '.join(map(str, choices))}"
    return True, value, ""


def validate_answer(payload: Any, wanted: Sequence[str],
                    numbers: Optional[Dict[int, str]] = None
                    ) -> Tuple[Dict[str, Dict[str, Any]], set, List[str]]:
    """(accepted, clean_ids, errors).

    ``accepted`` is every row with a VALID KIND, props cleaned of anything the
    schema does not allow. ``clean_ids`` is the subset that had no fault at all.

    The two are separate because a row can be half-right: a correct kind with a
    hallucinated prop key. Keeping the kind means three attempts later the shape
    still becomes a treeview rather than collapsing to the label fallback; but
    it is NOT clean, so it is still re-asked while attempts remain. Accepting it
    outright would end the loop early and silently drop the prop, which is what
    an earlier version of this function did.

    Partial credit is deliberate: a reply that types four shapes correctly and
    one wrongly should keep the four.

    ``numbers`` maps the box numbers the prompt showed to shape ids."""
    errs: List[str] = []
    out: Dict[str, Dict[str, Any]] = {}
    clean: set = set()
    if not isinstance(payload, dict):
        return {}, clean, ["reply was not a JSON object"]
    rows = payload.get("shapes")
    if not isinstance(rows, list):
        return {}, clean, ["reply has no 'shapes' list"]

    numbers = dict(numbers or {})
    name_of = {sid: f"box {n}" for n, sid in numbers.items()}
    want = set(wanted)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errs.append(f"entry {i} is not an object")
            continue
        sid, named = _row_id(row, numbers, want)
        if not sid:
            errs.append(f"entry {i}: {named or 'no box number'} is not one "
                        f"of the boxes asked about")
            continue
        where = name_of.get(sid, sid)
        row_ok = True
        kind = str(row.get("kind") or "")
        if kind not in CLASSIFIABLE:
            errs.append(f"{where}: {kind!r} is not a widget kind "
                        f"(pick one of the listed kinds)")
            continue
        schema = PALETTE[kind].get("prop_schema") or {}
        props = row.get("props")
        props = props if isinstance(props, dict) else {}
        kept: Dict[str, Any] = {}
        for pk, pv in props.items():
            if pk not in schema:
                allowed = sorted(p for p, d in schema.items()
                                 if d.get("type") != "handler")
                errs.append(f"{where}: {kind} has no property {pk!r} "
                            f"(allowed: {', '.join(allowed) or 'none'})")
                row_ok = False
                continue
            if schema[pk].get("type") == "handler" or pv is None:
                continue                 # generation wires callbacks itself
            ok, value, why = _check_prop(kind, pk, pv)
            if not ok:
                errs.append(f"{where}: props.{pk} {why}")
                row_ok = False
                continue
            kept[pk] = value
        try:
            conf = float(row.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf != conf:                 # NaN
            conf = 0.0
        out[sid] = {"kind": kind, "confidence": max(0.0, min(1.0, conf)),
                    "props": kept}
        if row_ok:
            clean.add(sid)

    for sid in want - set(out):
        where = name_of.get(sid, sid)
        if not any(where in e for e in errs):
            errs.append(f"{where}: no classification returned")
    return out, clean, errs


# ============================================================
# Entry point
# ============================================================

def _call(model_call: Callable[..., str], prompt: str, **opts: Any) -> str:
    """gui_describe.call_model: only the options the callable accepts."""
    try:
        import gui_describe
        return gui_describe.call_model(model_call, prompt, **opts)
    except ImportError:                  # pragma: no cover - always present
        return model_call(prompt)


def classify(shapes: Sequence[Shape], layout_tree: Any = None,
             model_call: Optional[Callable[..., str]] = None, *,
             max_attempts: int = 3, n_ctx: Optional[int] = None
             ) -> Tuple[List[Classification], List[Question]]:
    """Type every generic shape. Returns (classifications, questions).

    NO MODEL CALL happens when nothing is generic — the common path for a
    carefully drawn wireframe, and the reason a typed wireframe generates with
    zero inference.

    The call gets json_schema= (answer_schema over the boxes still asked
    about), num_predict= scaled to their number, and on a repair a seed and
    temperature that move whenever the model repeats itself — each passed
    only if model_call accepts it, so a stub taking the prompt alone still
    works."""
    generic = [s for s in shapes if s.kind == GENERIC_KIND]
    if not generic:
        return [], []
    if model_call is None:
        return ([Classification(s.id, FALLBACK_KIND, 0.0, flagged=True,
                                reason="no model available to classify")
                 for s in generic],
                [_question(s) for s in generic])

    by_id = {s.id: s for s in shapes}
    nodes = getattr(layout_tree, "nodes", {}) or {}
    numbers = {n: s.id for n, s in enumerate(generic, 1)}
    number_of = {sid: n for n, sid in numbers.items()}
    items: List[str] = []
    for n, s in enumerate(generic, 1):
        pid = getattr(nodes.get(s.id), "parent_id", None)
        container = by_id.get(pid) if pid else None
        sibs = [by_id[c] for c in getattr(nodes.get(pid), "children", [])
                if c in by_id] if pid and pid in nodes else [
            x for x in shapes if x.id != s.id]
        items.append(describe_shape(s, container, sibs, n))

    wanted = [s.id for s in generic]
    accepted: Dict[str, Dict[str, Any]] = {}
    clean_ids: set = set()
    errors: List[str] = []
    raw_payload: Any = None
    asked = list(wanted)
    seen: set = set()
    repeats = 0
    window = int(n_ctx or N_CTX)

    prompt = _prompt(lambda detail: build_prompt(items, detail=detail),
                     len(asked), window)
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        opts: Dict[str, Any] = {
            "json_schema": answer_schema([number_of[i] for i in asked]),
            "num_predict": num_predict_for(len(asked))}
        if attempt > 1:
            opts["temperature"] = min(0.9, REPAIR_TEMPERATURE
                                      + REPEAT_STEP * repeats)
            opts["seed"] = BASE_SEED + 100 * attempt + repeats
        try:
            reply = _call(model_call, prompt, **opts) or ""
        except Exception as exc:
            errors = [f"the model call failed: {exc!r}"]
            break
        key = hashlib.sha1(str(reply).strip().encode(
            "utf-8", "replace")).hexdigest()
        repeated = key in seen
        repeats += repeated
        seen.add(key)
        raw_payload = _extract_json(reply)
        if raw_payload is None:
            errors = ["the reply contained no JSON object"]
        else:
            got, clean, errors = validate_answer(raw_payload, wanted, numbers)
            accepted.update(got)
            clean_ids |= clean
            # Stop only when every shape came back with NO fault. A shape whose
            # kind was right but whose props were wrong is kept as a fallback
            # yet still re-asked, so the prop is repaired rather than dropped.
            if len(clean_ids) == len(wanted):
                break
        if attempt < attempts:
            asked = [i for i in wanted if i not in clean_ids] or list(wanted)
            remaining = [d for d, sid in zip(items, wanted) if sid in asked]
            bad = raw_payload if raw_payload is not None else reply[:1000]
            prompt = _prompt(
                lambda detail: repair_prompt(remaining or items, bad, errors,
                                             repeated=repeated,
                                             detail=detail),
                len(asked), window)

    results: List[Classification] = []
    questions: List[Question] = []
    for s in generic:
        got = accepted.get(s.id)
        if got is None:
            # Every attempt failed for this shape. A label is visible and
            # obviously wrong, which is the honest failure.
            results.append(Classification(
                s.id, FALLBACK_KIND, 0.0, flagged=True,
                reason="; ".join(errors[:3]) or "the model did not classify it"))
            questions.append(_question(s))
            continue
        c = Classification(s.id, got["kind"], got["confidence"],
                           dict(got["props"]))
        results.append(c)
        if c.confidence < CONFIDENCE_FLOOR:
            questions.append(_question(s, suggested=c.kind))
    return results, questions


def _extract_json(text: str) -> Any:
    """Reuse nx_generate's balanced-brace scanner, then gui_describe's
    near-JSON clean-up (trailing commas, comments, Python spellings).

    The scanner handles fences, prose either side, and braces inside strings
    — all of which a regex between the first '{' and the last '}' gets
    wrong, as the Grapher's analyst proved by silently dropping valid specs.
    Falls back to a plain json.loads only if those modules are unavailable."""
    try:
        import gui_describe
        payload, _notes, _salvaged = gui_describe.parse_reply(text)
        return payload
    except ImportError:                  # pragma: no cover - always present
        pass
    try:
        from nx_generate import extract_json
        return extract_json(text)
    except Exception:
        try:
            return json.loads(str(text).strip())
        except Exception:
            return None


def _question(s: Shape, suggested: str = "") -> Question:
    """The clarification for one ambiguous shape (spec 10.2)."""
    opts = ["label", "entry", "button"]
    if suggested and suggested not in opts:
        opts.insert(0, suggested)
    hint = f'"{s.label}"' if s.label else "an unlabelled box"
    return Question(
        shape_id=s.id,
        question=f"{hint} — which widget is this?",
        options=opts,
        default=suggested or FALLBACK_KIND,
    )


def apply_classifications(cls: Sequence[Classification]) -> Dict[str, Any]:
    """Classifications -> the mapping gui_spec.build expects."""
    return {c.shape_id: {"kind": c.kind, "props": c.props} for c in cls}


def persistable(cls: Sequence[Classification]) -> Dict[str, Dict[str, Any]]:
    """The answers worth WRITING BACK to the wireframe: every one the model
    actually gave — not the flagged fallbacks, which are a failure to answer
    and must be asked again next time, not frozen as labels."""
    return {c.shape_id: {"kind": c.kind, "props": dict(c.props)}
            for c in cls if not c.flagged}
