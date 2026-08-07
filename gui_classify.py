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
persisted into the .gspec, so it is asked exactly once.
"""
from __future__ import annotations

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
                   siblings: Sequence[Shape]) -> str:
    """One generic shape, as the model should see it.

    Relative geometry matters more than absolute: "spans 95% of its container's
    width, at the very top" is what identifies a toolbar, and it survives the
    user resizing the canvas, which raw pixels do not."""
    lines = [f'- id: {s.id}',
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


def build_prompt(items: Sequence[str]) -> str:
    """The constrained request. The catalogue IS the vocabulary."""
    kinds = ", ".join(CLASSIFIABLE)
    return f"""You are labelling boxes in a hand-drawn GUI wireframe.

Each box below is an UNTYPED rectangle. Decide which Tkinter widget it was
meant to be, using its label, note, size and position.

You may ONLY use these widget kinds, spelled exactly as written:
{kinds}

BOXES
{chr(10).join(items)}

Reply with ONLY a JSON object in this exact shape. No prose, no code fence:

{{"shapes": [
  {{"id": "<the id above>", "kind": "<one kind from the list>",
    "confidence": 0.0, "props": {{}}}}
]}}

Rules:
- One entry per box, using the id exactly as given.
- confidence is 0.0-1.0: how sure you are. Be honest; a low number asks the
  user rather than guessing wrong.
- props is optional. Only use property names that belong to the kind you chose.
- A wide, short box at the top or bottom is usually a toolbar or status_bar.
- A large box with a note about images, layers or previews is an image_canvas.
- A box with column-like labels is a treeview."""


def repair_prompt(items: Sequence[str], bad: Any, errors: Sequence[str]) -> str:
    """One repair pass: hand the model its own output and the exact faults.

    Same shape as nx_generate.repair_prompt — showing the model what it
    returned alongside what was wrong repairs far more reliably than restating
    the original request, which it has already demonstrably misread."""
    return f"""Your previous answer was rejected.

WHAT YOU RETURNED
{json.dumps(bad, indent=2)[:3000]}

WHAT IS WRONG
{chr(10).join('- ' + e for e in errors)}

{build_prompt(items)}
Fix every point above. Reply with ONLY the corrected JSON object."""


# ============================================================
# Validation
# ============================================================

def validate_answer(payload: Any, wanted: Sequence[str]
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
    one wrongly should keep the four."""
    errs: List[str] = []
    out: Dict[str, Dict[str, Any]] = {}
    clean: set = set()
    if not isinstance(payload, dict):
        return {}, clean, ["reply was not a JSON object"]
    rows = payload.get("shapes")
    if not isinstance(rows, list):
        return {}, clean, ["reply has no 'shapes' list"]

    want = set(wanted)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errs.append(f"entry {i} is not an object")
            continue
        sid = str(row.get("id") or "")
        if sid not in want:
            errs.append(f"entry {i}: unknown id {sid!r}")
            continue
        row_ok = True
        kind = str(row.get("kind") or "")
        if kind not in CLASSIFIABLE:
            errs.append(f"{sid}: {kind!r} is not a widget kind "
                        f"(pick one of the listed kinds)")
            continue
        schema = PALETTE[kind].get("prop_schema") or {}
        props = row.get("props")
        props = props if isinstance(props, dict) else {}
        kept: Dict[str, Any] = {}
        for pk, pv in props.items():
            if pk not in schema:
                errs.append(f"{sid}: {kind} has no property {pk!r}")
                row_ok = False
                continue
            choices = schema[pk].get("choices")
            if choices and pv not in choices:
                errs.append(f"{sid}: {pk}={pv!r} must be one of "
                            f"{', '.join(map(str, choices))}")
                row_ok = False
                continue
            kept[pk] = pv
        try:
            conf = float(row.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out[sid] = {"kind": kind, "confidence": max(0.0, min(1.0, conf)),
                    "props": kept}
        if row_ok:
            clean.add(sid)

    for sid in want - set(out):
        if not any(sid in e for e in errs):
            errs.append(f"{sid}: no classification returned")
    return out, clean, errs


# ============================================================
# Entry point
# ============================================================

def classify(shapes: Sequence[Shape], layout_tree: Any = None,
             model_call: Optional[Callable[[str], str]] = None, *,
             max_attempts: int = 3
             ) -> Tuple[List[Classification], List[Question]]:
    """Type every generic shape. Returns (classifications, questions).

    NO MODEL CALL happens when nothing is generic — the common path for a
    carefully drawn wireframe, and the reason a typed wireframe generates with
    zero inference."""
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
    items: List[str] = []
    for s in generic:
        pid = getattr(nodes.get(s.id), "parent_id", None)
        container = by_id.get(pid) if pid else None
        sibs = [by_id[c] for c in getattr(nodes.get(pid), "children", [])
                if c in by_id] if pid and pid in nodes else [
            x for x in shapes if x.id != s.id]
        items.append(describe_shape(s, container, sibs))

    wanted = [s.id for s in generic]
    accepted: Dict[str, Dict[str, Any]] = {}
    clean_ids: set = set()
    errors: List[str] = []
    raw_payload: Any = None

    prompt = build_prompt(items)
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            reply = model_call(prompt) or ""
        except Exception as exc:
            errors = [f"the model call failed: {exc!r}"]
            break
        raw_payload = _extract_json(reply)
        if raw_payload is None:
            errors = ["the reply contained no JSON object"]
        else:
            got, clean, errors = validate_answer(raw_payload, wanted)
            accepted.update(got)
            clean_ids |= clean
            # Stop only when every shape came back with NO fault. A shape whose
            # kind was right but whose props were wrong is kept as a fallback
            # yet still re-asked, so the prop is repaired rather than dropped.
            if len(clean_ids) == len(wanted):
                break
        if attempt < max_attempts:
            missing = [i for i in wanted if i not in clean_ids]
            remaining = [d for d, sid in zip(items, wanted) if sid in missing]
            prompt = repair_prompt(remaining or items,
                                   raw_payload if raw_payload is not None
                                   else reply[:1000], errors)

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
    """Reuse nx_generate's balanced-brace scanner.

    It handles fences, prose either side, and braces inside strings — all of
    which a regex between the first '{' and the last '}' gets wrong, as the
    Grapher's analyst proved by silently dropping valid specs. Falls back to a
    plain json.loads only if that module is unavailable."""
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
