"""
typhon_recreate.py — the Council rebuilds Typhon, and says where it fell short.

WHAT THIS IS
Typhon is the camera capture app that ships as a Designer example
(examples/gui/typhon.gspec) plus the Qt widgets written by hand for it
(council_qt/widgets/: the capture slider, the camera settings tabs and
window, the preset and classifier pickers, Pop out). This module hands the
Council's own models the job of building it again, in three stages that are
graded APART, so a weak stage cannot hide behind a strong one and each
failure names the part of the Council to improve:

  1. LAYOUT    Typhon described in plain English, one part of the window at a
               time (examples/gui/typhon_recreate/sections.json), through the
               Designer's own Describe it on a canvas that part's size. The
               parts are placed where Typhon has them, graded shape by shape
               against typhon.gspec, then generated and run offscreen.
  2. WIRING    Typhon's own wireframe with every script link taken off. For
               each control the model picks the library function it runs and
               which ports feed it and show its result — what the Designer's
               Wiring group asks a person. Graded against Typhon's links, then
               Typhon is generated with the model's links and run offscreen.
  3. WIDGETS   Each hand-built widget module written again from a spec read
               off the original: its purpose, and for every name the app and
               the tests use, the signature and what it does — never a body.
               The model writes one top-level item at a time; the module is
               assembled and graded by RUNNING TYPHON'S OWN TESTS against it.
               Failing tests go back to the model as repairs.

WHY STAGES ARE GRADED APART
Stage 2 runs on Typhon's wireframe, not stage 1's, and stage 3 on Typhon's
generated app, not one built from stages 1 and 2. A rebuilt layout has
different shapes and port names, so wiring graded on it would mix a layout
miss with a wiring miss. Apart, "the classifier rows were laid out wrong"
and "Rename was linked to save_as" are two findings, not one blur.

WHICH TESTS COUNT FOR A WIDGET
Every test file that imports the widget is run three times: against the
original module, against an EMPTY one (every item a stub that raises), and
against the rebuild. Only tests that pass on the original and fail on the
empty module measure the widget; a test that passes with the widget gone
(one reading the .gspec, say) says nothing about a rebuild and is left out
of its score. The original run also proves the machine can run the tests at
all — a test that fails there is the environment's, not the model's.

THE MODEL IS INJECTED
As in gui_describe and gui_codebehind: every stage takes a model call, so a
scripted stub exercises every path and no test loads a model. ``oracle``
builds the perfect stub — it answers each prompt with what Typhon actually
has — which proves the harness can award full marks before a real model is
blamed for not earning them.

SAFETY
Model-written widget code goes through gui_policy.validate (the gate every
generated app passes) before anything runs it, and runs only inside a test
process under council_core.child_proc's time and memory limits. Everything
is written inside the vault and the output folder the caller names; nothing
in the repository is changed.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / "examples" / "gui"
TYPHON = EXAMPLES / "typhon.gspec"
SECTIONS = EXAMPLES / "typhon_recreate" / "sections.json"
TESTS = HERE / "tests"

#: The hand-built widgets, smallest first: a run that is stopped part way
#: still has the cheap ones graded.
WIDGETS: Tuple[str, ...] = ("pop_out", "preset_picker", "classifier_picker",
                            "settings_tabs", "capture_review",
                            "camera_settings_window")
WIDGET_PACKAGE = "council_qt.widgets"

PASS, PARTIAL, FAIL = "PASS", "PARTIAL", "FAIL"

ModelCall = Callable[..., str]


# ============================================================
# Typhon, and the plain-English parts
# ============================================================

def load_typhon() -> Any:
    """typhon.gspec as a gui_shapes.Project."""
    import gui_shapes
    return gui_shapes.load_gspec(TYPHON)


def load_sections(path: Path = SECTIONS) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _inside(shape: Any, region: Sequence[int]) -> bool:
    """The shape's top-left corner is in the region. The top-left, not the
    centre: Typhon's notebook spans the part below its own tab row."""
    x, y, w, h = region
    return x <= shape.x < x + w and y <= shape.y < y + h


def section_of(shape: Any, sections: Sequence[Dict[str, Any]]) -> str:
    """The id of the part ``shape`` is graded in, or ""."""
    for sec in sections:
        if _inside(shape, sec["region"]):
            return sec["id"]
    return ""


# ============================================================
# Stage 1 — layout
# ============================================================

@dataclass
class SectionResult:
    id: str
    name: str
    ok: bool = False
    shapes: List[Any] = field(default_factory=list)   # canvas coordinates
    window: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    attempts: int = 0
    seconds: float = 0.0


def describe_section(sec: Dict[str, Any], model_call: Optional[ModelCall],
                     *, profile: Any = None) -> SectionResult:
    """One part through Describe it, on a canvas that part's size, its
    shapes moved to where the part sits in Typhon's window.

    With no model_call the Designer's own call is used, with the profile
    its model's size picks — designer_project.describe's path — so a run
    on the PC is graded on what a user would get."""
    import gui_describe
    from council_core import designer_project as dp

    x0, y0, w, h = sec["region"]
    out = SectionResult(sec["id"], sec.get("name", sec["id"]))
    call = model_call
    if call is None:
        profile = profile or dp.describe_profile(sec["text"])
        call = dp.describe_model_call
    t0 = time.time()
    res = gui_describe.describe(sec["text"], model_call=call, canvas_w=w,
                                canvas_h=h, toolkit="qt", profile=profile)
    out.seconds = round(time.time() - t0, 1)
    out.attempts = int(getattr(res, "attempts", 0) or 0)
    out.notes = list(getattr(res, "notes", []) or [])
    out.errors = list(getattr(res, "errors", []) or [])
    out.ok = bool(getattr(res, "ok", False))
    out.window = dict(getattr(res, "window", {}) or {})
    for n, s in enumerate(res.shapes or []):
        s.x += x0
        s.y += y0
        s.id = f"{sec['id']}_{n:02d}"
        out.shapes.append(s)
    return out


def stitch(results: Sequence[SectionResult]) -> List[Any]:
    """Every part's shapes on one canvas, z in part order."""
    shapes = []
    for r in results:
        shapes.extend(r.shapes)
    for z, s in enumerate(shapes, 1):
        s.z = z
    return shapes


#: Kinds that answer for each other when a match is "near": a number in a
#: text box, a dropdown for a list, a label for a title.
FAMILIES = ({"entry", "spinbox", "text"}, {"combobox", "listbox"},
            {"label", "log_pane"}, {"frame", "labelframe"})


def _family(a: str, b: str) -> bool:
    return a == b or any(a in f and b in f for f in FAMILIES)


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9µ]+", str(text or "").lower()))


def shape_text(shape: Any) -> str:
    """What a person reads on a shape: its label, and the visible text in
    its props (tabs, values, a label's text)."""
    props = getattr(shape, "props", {}) or {}
    parts = [str(getattr(shape, "label", "") or "")]
    for key in ("text", "tabs", "values"):
        v = props.get(key)
        if isinstance(v, (list, tuple)):
            parts.extend(str(i) for i in v)
        elif v:
            parts.append(str(v))
    return " ".join(p for p in parts if p)


def label_similarity(a: str, b: str) -> float:
    """Word overlap (Jaccard) of two texts; 1.0 when both are empty."""
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def iou(a: Any, b: Any) -> float:
    ox = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    oy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if ox <= 0 or oy <= 0:
        return 0.0
    inter = ox * oy
    return inter / float(a.w * a.h + b.w * b.h - inter)


def match_shapes(original: Sequence[Any], rebuilt: Sequence[Any]
                 ) -> List[Dict[str, Any]]:
    """Each original shape paired with the rebuilt shape that best answers
    for it, or None. Greedy on a score that wants the same kind first, then
    the same words, then the same place: label-heavy, because "is there a
    Train button" matters more than where it sits within a part."""
    pairs = []
    for i, o in enumerate(original):
        for j, r in enumerate(rebuilt):
            if not _family(o.kind, r.kind):
                continue
            words = label_similarity(shape_text(o), shape_text(r))
            score = (2.0 if o.kind == r.kind else 1.0) + 2.0 * words \
                + 1.0 * iou(o, r)
            pairs.append((score, i, j))
    pairs.sort(reverse=True)
    used_o, used_r, best = set(), set(), {}
    for score, i, j in pairs:
        if i in used_o or j in used_r:
            continue
        used_o.add(i)
        used_r.add(j)
        best[i] = j
    rows = []
    for i, o in enumerate(original):
        j = best.get(i)
        r = rebuilt[j] if j is not None else None
        rows.append({
            "original": o, "rebuilt": r,
            "same_kind": bool(r is not None and r.kind == o.kind),
            "words": (label_similarity(shape_text(o), shape_text(r))
                      if r is not None else 0.0),
            "iou": iou(o, r) if r is not None else 0.0})
    return rows


def _describe_shape(s: Any) -> str:
    text = shape_text(s)
    return f"{s.kind} {text!r}" if text else s.kind


def grade_layout(original: Sequence[Any],
                 sections: Sequence[Dict[str, Any]],
                 results: Sequence[SectionResult],
                 window: Dict[str, Any], want_window: Dict[str, Any]
                 ) -> Dict[str, Any]:
    """Per part and overall: how many of Typhon's shapes the rebuild has
    (same kind / near kind), how many read the same, how well they line
    up, and what is missing or extra — the findings, in a person's words."""
    by_sec: Dict[str, Dict[str, Any]] = {}
    totals = Counter()
    findings: List[str] = []
    res_by_id = {r.id: r for r in results}
    for sec in sections:
        sid = sec["id"]
        o_sec = [s for s in original if section_of(s, sections) == sid]
        res = res_by_id.get(sid)
        r_sec = list(res.shapes) if res is not None else []
        rows = match_shapes(o_sec, r_sec)
        found = sum(1 for r in rows if r["same_kind"])
        near = sum(1 for r in rows if r["rebuilt"] is not None
                   and not r["same_kind"])
        texted = [r for r in rows if _words(shape_text(r["original"]))]
        read_same = sum(1 for r in texted if r["words"] >= 0.6)
        ious = [r["iou"] for r in rows if r["rebuilt"] is not None]
        matched_ids = {id(r["rebuilt"]) for r in rows if r["rebuilt"]}
        extra = [s for s in r_sec if id(s) not in matched_ids]
        missing = [r["original"] for r in rows if r["rebuilt"] is None]
        wrong_kind = [(r["original"], r["rebuilt"]) for r in rows
                      if r["rebuilt"] is not None and not r["same_kind"]]
        entry = {
            "name": sec.get("name", sid), "ok": bool(res and res.ok),
            "attempts": res.attempts if res else 0,
            "seconds": res.seconds if res else 0.0,
            "original": len(o_sec), "rebuilt": len(r_sec),
            "same_kind": found, "near_kind": near,
            "labelled": len(texted), "read_same": read_same,
            "mean_iou": round(sum(ious) / len(ious), 2) if ious else 0.0,
            "missing": [_describe_shape(s) for s in missing],
            "wrong_kind": [f"{_describe_shape(o)} drawn as a {r.kind}"
                           for o, r in wrong_kind],
            "extra": [_describe_shape(s) for s in extra],
            "errors": (res.errors[:5] if res else []),
        }
        by_sec[sid] = entry
        totals.update(original=len(o_sec), rebuilt=len(r_sec),
                      same_kind=found, near_kind=near, labelled=len(texted),
                      read_same=read_same)
        if res is not None and not res.ok:
            why = "; ".join(res.errors[:2]) or "no reason given"
            findings.append(f"Describe it refused the {entry['name']} part "
                            f"after {res.attempts} call(s): {why}")
        elif missing or wrong_kind:
            bits = []
            if missing:
                bits.append(f"missing {', '.join(entry['missing'][:6])}"
                            + (" …" if len(missing) > 6 else ""))
            if wrong_kind:
                bits.append("; ".join(entry["wrong_kind"][:3]))
            findings.append(f"{entry['name']}: " + "; ".join(bits))
    want_bg = str(want_window.get("bg", "")).lower()
    got_bg = str(window.get("bg", "")).lower()
    if want_bg and got_bg != want_bg:
        findings.append(f"The window colour came back {got_bg or 'unset'}, "
                        f"not Typhon's {want_bg}.")
    n = totals["original"] or 1
    score = round(100.0 * (totals["same_kind"] + 0.5 * totals["near_kind"])
                  / n, 1)
    return {"score": score, "totals": dict(totals), "sections": by_sec,
            "window": {"wanted": want_window, "got": window,
                       "bg_ok": not want_bg or got_bg == want_bg},
            "findings": findings}


def run_layout(vault: Path, *, model_call: Optional[ModelCall] = None,
               runtime: bool = True, only: Sequence[str] = (),
               out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Stage 1, start to finish. Never raises for a model's failure."""
    import run_describe_prompts as rdp

    spec = load_sections()
    sections = [s for s in spec["sections"] if not only or s["id"] in only]
    typhon = load_typhon()
    results = [describe_section(sec, model_call) for sec in sections]
    rebuilt = stitch(results)
    window = {}
    for r in results:                      # the first part that set a colour
        if r.window.get("bg"):
            window = dict(r.window)
            break
    original = [s for s in typhon.shapes
                if section_of(s, sections)]
    graded = grade_layout(original, sections, results, window,
                          spec.get("window", {}))
    cw, ch = spec["canvas"]["w"], spec["canvas"]["h"]
    graded["structural"] = rdp.structural_problems(rebuilt, cw, ch,
                                                   title="Typhon")[:20] \
        if rebuilt else ["no shapes at all"]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        draw_wireframe(typhon.shapes, (cw, ch), out_dir / "layout_typhon.png",
                       bg=typhon.window.bg, title="Typhon (typhon.gspec)")
        draw_wireframe(rebuilt, (cw, ch), out_dir / "layout_rebuilt.png",
                       bg=window.get("bg") or "", title="Rebuilt by the Council")
        graded["pictures"] = ["layout_typhon.png", "layout_rebuilt.png"]
    if rebuilt:
        graded["generate"], graded["runtime"] = _generate_and_run(
            vault, "Typhon rebuilt layout", rebuilt, mode="standalone",
            canvas=(cw, ch), window={"title": "Typhon", **window},
            runtime=runtime)
        if not graded["generate"].get("ok"):
            graded["findings"].append(
                "The rebuilt layout did not generate: "
                + graded["generate"].get("detail", ""))
        elif runtime and not graded["runtime"].get("ok"):
            graded["findings"].append(
                "The rebuilt layout generated but did not run cleanly: "
                + graded["runtime"].get("detail", ""))
    return graded


# ============================================================
# Stage 2 — wiring
# ============================================================

#: The modules Typhon's links name. All of them are gui_policy.LINKED_MODULES.
WIRING_MODULES = ("frame_camera", "frame_classes", "frame_timing",
                  "frame_roi", "gui_settings")
#: Functions offered per control. The whole catalogue is ~150 functions —
#: more than a 4096-token window holds beside the ports.
SHORTLIST = 14


@dataclass(frozen=True)
class Offer:
    module: str
    name: str
    signature: str
    summary: str
    result_keys: Tuple[str, ...]


def catalogue(modules: Sequence[str] = WIRING_MODULES) -> List[Offer]:
    """Every public function a link may name, read by PARSING the modules
    (designer_wiring.module_info) — the Module/Function dropdowns' list."""
    from council_core import designer_wiring as dw
    out = []
    for m in modules:
        info = dw.module_info(m)
        for f in info.functions:
            out.append(Offer(m, f.name, f.signature(), f.summary,
                             tuple(f.result_keys)))
    return out


def context_label(shape: Any, shapes: Sequence[Any]) -> str:
    """The label a person reads as this control's caption: the nearest
    label shape just above it or just to its left."""
    best, best_d = "", 1e9
    for s in shapes:
        if s.kind != "label" or s is shape or not (s.label or "").strip():
            continue
        above = (s.y + s.h <= shape.y + 4 and shape.y - (s.y + s.h) <= 40
                 and s.x < shape.x + shape.w and shape.x < s.x + s.w)
        left = (s.x + s.w <= shape.x + 4 and shape.x - (s.x + s.w) <= 40
                and s.y < shape.y + shape.h and shape.y < s.y + s.h)
        if above or left:
            d = abs(shape.y - s.y) + abs(shape.x - s.x)
            if d < best_d:
                best, best_d = s.label.strip(), d
    return best


_STOP = {"the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "this",
         "is", "it", "by", "with", "one", "each", "every", "be", "as", "at"}


def _stem(word: str) -> str:
    """"cropped" -> "crop", "timings" -> "timing", "frames" -> "frame":
    enough for a caption and a docstring to meet."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)]
            break
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def _key_words(text: str) -> set:
    raw = re.findall(r"[a-z0-9]+", re.sub(r"_", " ", str(text).lower()))
    return {_stem(w) for w in raw if w not in _STOP}


def shortlist(shape: Any, caption: str, part: str,
              offers: Sequence[Offer], k: int = SHORTLIST) -> List[Offer]:
    """The k functions whose names and summaries share the most words with
    the control — its own label and port first, then its caption and the
    part of the window it is in. Deliberately dumb, like
    gui_describe.select_examples: a miss here is reported as a RETRIEVAL
    miss, apart from the model's."""
    own = _key_words(" ".join([shape.label or "",
                               str((shape.port or {}).get("name", ""))]))
    near = _key_words(" ".join([caption, part])) - own
    scored = []
    for n, o in enumerate(offers):
        name, summary = _key_words(o.name), _key_words(o.summary)
        score = (3 * len(own & name) + 3 * len(own & summary)
                 + len(near & name) + 0.5 * len(near & summary))
        scored.append((-score, n, o))
    scored.sort()
    return [o for _s, _n, o in scored[:k]]


WIRING_PROMPT = """\
You are wiring a camera capture app built in a GUI designer. One control at a
time, you choose the ONE library function it runs when it is pressed or
changed, which of the app's ports are passed to it (in order), and which
ports show which key of the dict it returns.

THE CONTROL
{control}

THE APP'S PORTS (name: widget — its caption)
{ports}

FUNCTIONS YOU MAY LINK (module.function(parameters) -> keys of the result)
{offers}

Rules:
- "inputs" are port names, one per parameter, in the parameter order.
  Leave out parameters that have a default (shown as =…) unless a port
  clearly supplies them.
- "outputs" maps a port name to a key of the result. A button, image or
  chart cannot show a result.
- Use only names listed above.

Reply with JSON only, no prose:
{{"module": "...", "function": "...", "inputs": ["..."], "outputs": {{"port": "key"}}}}
"""

WIRING_REPAIR = """\
Your link was refused:
{problems}

Your reply was:
{reply}

Reply with the corrected JSON only.
"""


def _port_lines(shapes: Sequence[Any], ports: Sequence[Any]) -> str:
    by_id = {s.id: s for s in shapes}
    lines = []
    for p in ports:
        s = by_id.get(p.shape_ids[0]) if p.shape_ids else None
        cap = ""
        if s is not None:
            cap = (s.label or "").strip() or context_label(s, shapes)
        lines.append(f"{p.name}: {p.kind}" + (f" — {cap}" if cap else ""))
    return "\n".join(lines)


def _offer_lines(offers: Sequence[Offer]) -> str:
    out = []
    for o in offers:
        keys = f" -> {', '.join(o.result_keys)}" if o.result_keys else ""
        line = f"{o.module}.{o.signature}{keys}"
        if o.summary:
            line += f"  # {o.summary[:90]}"
        out.append(line)
    return "\n".join(out)


def control_text(shape: Any, shapes: Sequence[Any], part: str) -> str:
    """The control as a person would name it."""
    kind = {"combobox": "dropdown", "spinbox": "number box",
            "entry": "text box"}.get(shape.kind, shape.kind)
    label = (shape.label or "").strip()
    caption = context_label(shape, shapes)
    bits = [f"A {kind}"]
    if label:
        bits.append(f"labelled {label!r}")
    if caption and caption != label:
        bits.append(f"under/beside the caption {caption!r}")
    if shape.port and shape.port.get("name"):
        bits.append(f"(its own port is {shape.port['name']})")
    bits.append(f"in the part of the window called {part!r}.")
    if shape.kind != "button":
        bits.append("It runs its function when its value changes.")
    return " ".join(bits)


def wiring_prompt(shape: Any, shapes: Sequence[Any], ports: Sequence[Any],
                  offers: Sequence[Offer], part: str) -> str:
    return WIRING_PROMPT.format(control=control_text(shape, shapes, part),
                                ports=_port_lines(shapes, ports),
                                offers=_offer_lines(offers))


def parse_link(reply: str) -> Tuple[Dict[str, Any], str]:
    """(link, "") or ({}, why) from a model reply."""
    from council_core import designer_wiring as dw
    import nx_generate
    try:
        payload = nx_generate.extract_json(reply or "")
    except Exception as exc:                             # noqa: BLE001
        return {}, f"no JSON in the reply ({exc})"
    if not isinstance(payload, dict):
        return {}, "the reply is not a JSON object"
    link = dw.normalise(payload)
    if not link:
        return {}, "the reply names no module or function"
    return link, ""


def link_problems(link: Dict[str, Any], shape: Any, ports: Sequence[Any],
                  offers: Sequence[Offer]) -> List[str]:
    from council_core import designer_wiring as dw
    out = list(dw.problems(link, ports, kind=shape.kind, mode="linked"))
    names = {(o.module, o.name) for o in offers}
    if link and (link["module"], link["function"]) not in names:
        out.append(f"{link['module']}.{link['function']} is not one of the "
                   f"functions listed")
    return out


def wire_one(shape: Any, shapes: Sequence[Any], ports: Sequence[Any],
             offers: Sequence[Offer], part: str, model_call: ModelCall, *,
             repairs: int = 1) -> Dict[str, Any]:
    """One control: prompt, parse, check, at most ``repairs`` repairs."""
    prompt = wiring_prompt(shape, shapes, ports, offers, part)
    calls, link, problems, reply = 0, {}, [], ""
    for _ in range(1 + max(0, repairs)):
        calls += 1
        try:
            reply = str(model_call(prompt) or "")
        except Exception as exc:                         # noqa: BLE001
            problems = [f"the model call raised {exc!r}"]
            break
        link, why = parse_link(reply)
        problems = [why] if why else link_problems(link, shape, ports, offers)
        if not problems:
            break
        prompt = (wiring_prompt(shape, shapes, ports, offers, part) + "\n"
                  + WIRING_REPAIR.format(problems="\n".join(
                      f"- {p}" for p in problems), reply=reply[:1500]))
    return {"link": link, "problems": problems, "calls": calls,
            "reply": reply[:600], "prompt_chars": len(prompt)}


def grade_link(got: Dict[str, Any], want: Dict[str, Any]) -> Dict[str, Any]:
    """PASS: same function, same inputs in order, same outputs. PARTIAL:
    same function. FAIL: anything else."""
    from council_core import designer_wiring as dw
    got, want = dw.normalise(got), dw.normalise(want)
    same_fn = bool(got) and (got["module"], got["function"]) == \
        (want["module"], want["function"])
    same_in = same_fn and got["inputs"] == want["inputs"]
    same_out = same_fn and got["outputs"] == want["outputs"]
    grade = PASS if same_in and same_out else PARTIAL if same_fn else FAIL
    return {"grade": grade, "function": same_fn, "inputs": same_in,
            "outputs": same_out}


def unwired(project: Any) -> Tuple[List[Any], Dict[str, Dict[str, Any]]]:
    """Typhon's shapes with every script link taken off, and the links."""
    import copy
    shapes = copy.deepcopy(list(project.shapes))
    want = {}
    for s in shapes:
        if s.script:
            want[s.id] = dict(s.script)
            s.script = {}
    return shapes, want


def run_wiring(vault: Path, *, model_call: ModelCall, runtime: bool = True,
               only: Sequence[str] = ()) -> Dict[str, Any]:
    """Stage 2, start to finish."""
    from council_core import designer_wiring as dw

    spec = load_sections()
    sections = spec["sections"]
    names = {s["id"]: s.get("name", s["id"]) for s in sections}
    typhon = load_typhon()
    shapes, want = unwired(typhon)
    ports = dw.project_ports(shapes)
    offers = catalogue()
    rows, tally = [], Counter()
    accepted: Dict[str, Dict[str, Any]] = {}
    findings: List[str] = []
    retrieval = 0
    for s in shapes:
        if s.id not in want or (only and s.id not in only):
            continue
        part = names.get(section_of(s, sections), "the window")
        caption = context_label(s, shapes)
        offered = shortlist(s, caption, part, offers)
        target = (want[s.id]["module"], want[s.id]["function"])
        in_list = any((o.module, o.name) == target for o in offered)
        t0 = time.time()
        done = wire_one(s, shapes, ports, offered, part, model_call)
        if done["link"] and not done["problems"]:
            accepted[s.id] = done["link"]
        g = grade_link(done["link"], want[s.id])
        tally[g["grade"]] += 1
        label = (s.label or "").strip() or caption or s.id
        row = {"id": s.id, "control": f"{s.kind} {label!r}",
               "wanted": dw.describe(want[s.id]),
               "got": dw.describe(done["link"]) if done["link"] else "",
               "in_shortlist": in_list, "seconds": round(time.time() - t0, 1),
               **g, **{k: done[k] for k in ("problems", "calls",
                                            "prompt_chars")}}
        rows.append(row)
        if not in_list:
            retrieval += 1
        if g["grade"] == FAIL:
            why = ("the right function was not among those offered"
                   if not in_list else
                   f"linked {row['got'] or 'nothing'}"
                   + (f" ({'; '.join(done['problems'][:1])})"
                      if done["problems"] else ""))
            findings.append(f"{row['control']}: wanted {row['wanted']}; "
                            f"{why}")
        elif g["grade"] == PARTIAL:
            what = [k for k in ("inputs", "outputs") if not g[k]]
            findings.append(f"{row['control']}: right function, wrong "
                            f"{' and '.join(what)} (got {row['got']}, "
                            f"wanted {row['wanted']})")
    n = len(rows) or 1
    score = round(100.0 * (tally[PASS] + 0.5 * tally[PARTIAL]) / n, 1)
    out = {"score": score, "tally": dict(tally), "controls": rows,
           "retrieval_misses": retrieval, "findings": findings}
    if retrieval:
        out["findings"].insert(0, f"For {retrieval} of {len(rows)} controls "
                               f"the right function was not in the "
                               f"{SHORTLIST} offered — the shortlist, not "
                               f"the model, lost those.")
    # Typhon generated with exactly the links that were graded; a control
    # whose link was refused stays unwired, as the Wiring group leaves it.
    import copy
    rewired = copy.deepcopy(shapes)
    for s in rewired:
        if accepted.get(s.id):
            s.script = accepted[s.id]
    out["generate"], out["runtime"] = _generate_and_run(
        vault, "Typhon rewired", rewired, mode=typhon.mode,
        canvas=(typhon.canvas.w, typhon.canvas.h),
        window=_window_dict(typhon.window), requires=typhon.requires,
        runtime=runtime)
    if not out["generate"].get("ok"):
        out["findings"].append("Typhon with the model's links did not "
                               "generate: " + out["generate"].get("detail", ""))
    elif runtime and not out["runtime"].get("ok"):
        out["findings"].append("Typhon with the model's links generated but "
                               "did not run cleanly: "
                               + out["runtime"].get("detail", ""))
    return out


# ============================================================
# Stage 3 — the hand-built widgets
# ============================================================

def widget_module(name: str) -> str:
    return f"{WIDGET_PACKAGE}.{name}"


def widget_path(name: str) -> Path:
    return HERE / "council_qt" / "widgets" / f"{name}.py"


def _imports_widget(tree: ast.AST, name: str) -> Tuple[set, set]:
    """(aliases the module is bound to, names imported from it) in a file."""
    mod = widget_module(name)
    aliases, direct = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == mod:
                    aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == WIDGET_PACKAGE:
                for a in node.names:
                    if a.name == name:
                        aliases.add(a.asname or a.name)
            elif node.module == mod:
                for a in node.names:
                    direct.add(a.name)
    return aliases, direct


def widget_tests(name: str) -> List[Path]:
    """Every test file that imports the widget, by parsing."""
    out = []
    for p in sorted(TESTS.glob("test_*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        aliases, direct = _imports_widget(tree, name)
        if aliases or direct:
            out.append(p)
    return out


def _users(name: str) -> List[Path]:
    """The Council modules (not tests) that import the widget."""
    out = []
    skip = {widget_path(name).resolve()}
    for p in list(HERE.glob("*.py")) + list((HERE / "council_qt").rglob(
            "*.py")) + list((HERE / "council_core").rglob("*.py")):
        if p.resolve() in skip:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if name not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        if any(_imports_widget(tree, name)):
            out.append(p)
    return out


def surface(name: str, files: Optional[Sequence[Path]] = None) -> set:
    """Every name of the widget module the app or its tests use: attribute
    reads off the module, names imported from it, monkeypatch/getattr
    targets spelled as strings. What a rebuild must provide."""
    files = list(files) if files is not None else (widget_tests(name)
                                                   + _users(name))
    mod = widget_module(name)
    names: set = set()
    for p in files:
        text = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        aliases, direct = _imports_widget(tree, name)
        names |= direct
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(
                    node.value, ast.Name) and node.value.id in aliases:
                names.add(node.attr)
            elif isinstance(node, ast.Call) and len(node.args) >= 2 \
                    and isinstance(node.args[0], ast.Name) \
                    and node.args[0].id in aliases \
                    and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                names.add(node.args[1].value)        # setattr(cr, "x", ...)
        for m in re.finditer(re.escape(mod) + r"\.([A-Za-z_]\w*)", text):
            names.add(m.group(1))
    return names


@dataclass
class Piece:
    """One top-level item of a widget module: what the model is asked to
    write, the original's spec for it, and where it came from."""
    name: str
    kind: str                       # "class" | "function" | "constants"
    spec: str
    original: str                   # the original's source (oracle only)
    calls: Tuple[str, ...] = ()     # Council functions the original calls
    names: Tuple[str, ...] = ()     # what a constants piece binds


def _doc_head(doc: Optional[str], limit: int) -> str:
    """A docstring's first paragraphs, up to ``limit`` characters."""
    if not doc:
        return ""
    text = textwrap.dedent(doc).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    para = cut.rfind("\n\n")
    return (cut[:para] if para > limit // 3 else cut).rstrip() + " …"


def _signature(node: Any) -> str:
    """"def f(a, b=1) -> str" as written, without the body."""
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args}){ret}"


def _public_method(name: str) -> bool:
    """Public, the constructor, or a Qt override (camelCase event name)."""
    return (not name.startswith("_") or name == "__init__"
            or (name.endswith("Event") or name in ("eventFilter",
                                                   "sizeHint",
                                                   "minimumSizeHint")))


def _module_calls(node: ast.AST, aliases: Dict[str, str]) -> List[str]:
    """module.function calls into Council modules the original makes."""
    seen = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id in aliases:
            full = f"{aliases[n.value.id]}.{n.attr}"
            if full not in seen:
                seen.append(full)
    return seen


def _council_aliases(tree: ast.Module) -> Dict[str, str]:
    """alias -> module, for the Council modules the original imports."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.split(".")[0] in ("council_core", "council_qt"):
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in ("council_core", "council_qt"):
                    out[a.asname or a.name] = a.name
    return out


def _call_lines(calls: Sequence[str]) -> List[str]:
    """Each Council call as `module.f(signature)  # summary`, read off its
    source with designer_wiring.module_info (never imported)."""
    from council_core import designer_wiring as dw
    lines = []
    infos: Dict[str, Any] = {}
    for full in calls:
        mod, _, fn = full.rpartition(".")
        if mod not in infos:
            infos[mod] = dw.module_info(mod)
        info = infos[mod]
        f = info.function(fn) if info.found else None
        if f is not None:
            lines.append(f"{mod}.{f.typed_signature()}"
                         + (f"  # {f.summary[:100]}" if f.summary else ""))
        else:
            members = info.members(fn) if info.found else ()
            if members:
                lines.append(f"{mod}.{fn}: {', '.join(members[:14])}")
            else:
                lines.append(full)
    return lines


#: Characters of spec per piece. A 4096-token window at ~3 characters a
#: token, less the reply, leaves about this much; a larger one is trimmed and
#: the trim is REPORTED, because a spec cut short is a likely cause of a miss.
SPEC_BUDGET = 7000


def pieces(name: str, used: Optional[set] = None) -> Tuple[str, List[Piece]]:
    """(the import header, the pieces) of a widget module, in source order.

    The header is the original's imports, given, not written: which Qt
    classes a widget uses is not what is being measured, and an import the
    model gets wrong fails every test at once and hides everything else.
    A piece is a top-level class or function the app or tests use; the
    module-level constants make one more piece. Private helpers
    are the model's to write inside the piece that needs them."""
    source = widget_path(name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = surface(name) if used is None else used
    lines = source.splitlines()
    header = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            header.append(ast.get_source_segment(source, node) or "")
    aliases = _council_aliases(tree)
    out: List[Piece] = []
    consts = []
    for node in tree.body:
        seg = "\n".join(lines[(node.lineno - 1):node.end_lineno])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in used:
                continue
            spec = _signature(node) + "\n" + textwrap.indent(
                _doc_head(ast.get_docstring(node), 1500), "    ")
            calls = _module_calls(node, aliases)
            out.append(Piece(node.name, "function", spec, seg, tuple(calls)))
        elif isinstance(node, ast.ClassDef):
            if node.name not in used:
                continue
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            parts = [f"class {node.name}({bases}):" if bases
                     else f"class {node.name}:",
                     textwrap.indent(_doc_head(ast.get_docstring(node), 1500),
                                     "    ")]
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and _public_method(item.name):
                    doc = _doc_head(ast.get_docstring(item), 400)
                    parts.append(textwrap.indent(_signature(item), "    "))
                    if doc:
                        parts.append(textwrap.indent(doc, "        "))
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    text = ast.get_source_segment(source, item) or ""
                    if len(text) < 120:
                        parts.append(textwrap.indent(
                            "class attribute: " + text.split("=")[0].strip(),
                            "    "))
            calls = _module_calls(node, aliases)
            out.append(Piece(node.name, "class", "\n".join(parts), seg,
                             tuple(calls)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Every module-level constant, used outside or not: they are
            # how the module's own items agree with each other (the setting
            # kinds, the row heights), so every piece is shown their names.
            names = assigned_names(node)
            if names:
                comment = _comment_above(lines, node.lineno)
                consts.append((names, comment, seg))
    if consts:
        spec = "\n".join(
            (f"# {c}\n" if c else "") + ", ".join(n) + " = …"
            for n, c, _s in consts)
        orig = "\n".join(s for _n, _c, s in consts)
        out.insert(0, Piece("constants", "constants", spec, orig,
                            names=tuple(n for ns, _c, _s in consts
                                        for n in ns)))
    return "\n".join(header), out


def assigned_names(node: ast.AST) -> List[str]:
    """The names a top-level assignment binds: `A = 1`, `A, B = 1, 2`,
    `A: int = 1`."""
    targets = node.targets if isinstance(node, ast.Assign) else [
        getattr(node, "target", None)]
    out: List[str] = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out.extend(e.id for e in t.elts if isinstance(e, ast.Name))
    return out


def _comment_above(lines: Sequence[str], lineno: int) -> str:
    """The #: / # comment block right above a line, joined."""
    got = []
    i = lineno - 2
    while i >= 0 and lines[i].lstrip().startswith("#"):
        got.append(lines[i].lstrip().lstrip("#:").strip())
        i -= 1
    return " ".join(reversed(got))[:400]


WIDGET_PROMPT = """\
You are rebuilding one Python module of a PySide6 (Qt) camera capture app,
{module}, one top-level item at a time. The module's purpose:

{purpose}

The module starts with exactly these imports (already written — do not repeat
them; add an import inside your code only if you need something else from the
standard library or PySide6.QtCore/QtGui/QtWidgets):

{header}

Other items of this module, written separately (you may use them):
{others}
{calls}
WRITE THIS ITEM — {what}:

{spec}

Rules:
- Output ONLY one fenced ```python block with the complete {kind_word}.
- Keep every name, parameter and default shown above exactly.
- Any private helper you need goes inside your block, named starting with
  _{prefix}_ so it cannot clash with another item's helpers.
- No placeholders; write working code.
"""

WIDGET_REPAIR = """\
Your {kind_word} `{name}` was run against the app's tests and these failed:

{failures}

Your code was:
```python
{code}
```

Output the corrected {kind_word} as ONE fenced ```python block, nothing else.
"""


def _purpose(name: str) -> str:
    tree = ast.parse(widget_path(name).read_text(encoding="utf-8"))
    return _doc_head(ast.get_docstring(tree), 1800)


def piece_prompt(name: str, header: str, piece: Piece,
                 all_pieces: Sequence[Piece]) -> Tuple[str, bool]:
    """(prompt, trimmed) — the spec cut to SPEC_BUDGET when it is longer."""
    others = []
    for p in all_pieces:
        if p is piece:
            continue
        first = p.spec.splitlines()[0] if p.spec else p.name
        others.append(f"  {first}")
    calls = ""
    if piece.calls:
        calls = ("\nCouncil functions the original calls (signatures):\n"
                 + "\n".join(f"  {c}" for c in _call_lines(piece.calls))
                 + "\n")
    spec, trimmed = piece.spec, False
    if len(spec) > SPEC_BUDGET:
        spec, trimmed = spec[:SPEC_BUDGET] + "\n    …(spec trimmed)", True
    what = {"class": f"the class {piece.name}",
            "function": f"the function {piece.name}",
            "constants": "the module-level constants"}[piece.kind]
    kind_word = {"class": "class", "function": "function",
                 "constants": "constant definitions"}[piece.kind]
    prompt = WIDGET_PROMPT.format(
        module=widget_module(name), purpose=_purpose(name), header=header,
        others="\n".join(others) or "  (none)", calls=calls, what=what,
        spec=spec, kind_word=kind_word,
        prefix=piece.name.lower() if piece.kind != "constants" else "const")
    return prompt, trimmed


_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)(?:```|\Z)", re.S)


def extract_code(reply: str) -> str:
    """The first fenced block, or the whole reply when there is none."""
    m = _FENCE.search(reply or "")
    return (m.group(1) if m else (reply or "")).strip("\n")


COUNCIL_IMPORT = re.compile(r"'(council_core|council_qt)' is part of the "
                            r"Council")


def check_piece(code: str, piece: Piece) -> List[str]:
    """Why ``code`` cannot stand in for ``piece``: it does not parse, it
    does not define the item, or the policy gate refuses it."""
    import gui_policy
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"does not parse: line {exc.lineno}: {exc.msg}"]
    if piece.kind != "constants":
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef))}
        if piece.name not in defined:
            return [f"does not define {piece.name}"]
    ok, errs = gui_policy.validate(code, mode="linked", toolkit="qt")
    # The gate is a generated APP's, and this is the Council's own widget
    # code: reaching council_core / council_qt is what the originals do (a
    # lazy `from council_core import live_display`). Everything else the
    # gate refuses stays refused.
    errs = [e for e in errs if not COUNCIL_IMPORT.search(e)]
    return [f"policy: {e}" for e in errs[:5]]


def placeholder(piece: Piece) -> str:
    """What stands in for a piece the model did not deliver, so the module
    still imports and every OTHER item is still graded."""
    if piece.kind == "class":
        return (f"class {piece.name}:\n"
                f"    def __init__(self, *args, **kwargs):\n"
                f"        raise NotImplementedError('{piece.name} was not "
                f"rebuilt')\n")
    if piece.kind == "function":
        return (f"def {piece.name}(*args, **kwargs):\n"
                f"    raise NotImplementedError('{piece.name} was not "
                f"rebuilt')\n")
    return "".join(f"{n} = None  # not rebuilt\n" for n in piece.names) \
        or "# (constants not rebuilt)\n"


def assemble(header: str, codes: Sequence[Tuple[Piece, str]]
             ) -> Tuple[str, Dict[str, Tuple[int, int]]]:
    """(module source, piece name -> (first line, last line))."""
    parts = ['"""Rebuilt by the Council (typhon_recreate)."""',
             "from __future__ import annotations", "", header, ""]
    text = "\n".join(parts)
    spans = {}
    for piece, code in codes:
        start = text.count("\n") + 2
        text += "\n\n" + code.rstrip() + "\n"
        spans[piece.name] = (start, text.count("\n"))
    return text, spans


SWAP_PLUGIN = '''\
"""pytest plugin: load each COUNCIL_SWAP module file under its real name
before any test imports it. Written by typhon_recreate."""
import importlib
import importlib.util
import os
import sys

for item in filter(None, os.environ.get("COUNCIL_SWAP", "").split(os.pathsep)):
    name, _, path = item.partition("=")
    parent, _, leaf = name.rpartition(".")
    pkg = importlib.import_module(parent)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    setattr(pkg, leaf, mod)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:          # the tests will say what is missing
        print(f"COUNCIL_SWAP: {name} failed to import: {exc!r}",
              file=sys.stderr)
'''

#: One widget's test files may take this long, collection included.
TEST_TIMEOUT = 900


def run_tests(tests: Sequence[Path], work: Path, *,
              swap: Optional[Dict[str, Path]] = None,
              tag: str = "run") -> Dict[str, Any]:
    """Run ``tests`` in a child pytest, optionally with modules swapped;
    {"outcomes": {test id: passed|failed|error|skipped}, "failures":
    {test id: message}, "infra": why the run itself failed or ""}."""
    from council_core import child_proc
    work.mkdir(parents=True, exist_ok=True)
    (work / "council_swap_plugin.py").write_text(SWAP_PLUGIN,
                                                 encoding="utf-8")
    junit = work / f"{tag}.xml"
    if junit.exists():
        junit.unlink()
    env_extra = {
        "QT_QPA_PLATFORM": "offscreen", "COUNCIL_NO_DIALOGS": "1",
        "PYTHONPATH": os.pathsep.join([str(work), str(HERE),
                                       os.environ.get("PYTHONPATH", "")]),
        "COUNCIL_SWAP": os.pathsep.join(
            f"{k}={v}" for k, v in (swap or {}).items()),
    }
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "council_swap_plugin", f"--junitxml={junit}",
            # xunit1 adds each case's file, for when classname comes out empty
            "-o", "junit_family=xunit1",
            "--tb=short", *[str(t) for t in tests]]
    ran = child_proc.run(argv, cwd=str(HERE),
                         env=child_proc.child_env(env_extra),
                         timeout=TEST_TIMEOUT, memory_limit_mb=4096)
    out: Dict[str, Any] = {"outcomes": {}, "failures": {}, "infra": "",
                           "seconds": getattr(ran, "seconds", None)}
    if ran.infra:
        out["infra"] = ran.infra
        return out
    if ran.timed_out:
        out["infra"] = f"timed out after {TEST_TIMEOUT}s"
    if not junit.exists():
        out["infra"] = out["infra"] or ("pytest wrote no report: "
                                        + (ran.stderr or ran.stdout or "")
                                        [-400:])
        return out
    for case in ET.parse(junit).getroot().iter("testcase"):
        # A suite on another drive than the app (a temp folder on C:, the
        # app on D:) has no relative path, and pytest leaves classname empty.
        mod = case.get("classname", "") or re.split(
            r"[\\/]", case.get("file", ""))[-1].rsplit(".py", 1)[0]
        tid = f"{mod}::{case.get('name', '')}"
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed" if child.tag == "failure" else "error"
                msg = (child.get("message") or "") + "\n" + (child.text or "")
                out["failures"][tid] = msg.strip()[-1500:]
            elif child.tag == "skipped":
                state = "skipped"
        out["outcomes"][tid] = state
    return out


def counted_tests(original: Dict[str, str], empty: Dict[str, str]
                  ) -> List[str]:
    """Tests that measure the widget: pass on the original, not on the
    empty module."""
    return sorted(t for t, s in original.items()
                  if s == "passed" and empty.get(t) != "passed")


def blame(failures: Dict[str, str], spans: Dict[str, Tuple[int, int]],
          module_file: Path, all_pieces: Sequence[Piece]
          ) -> Dict[str, List[str]]:
    """piece name -> the failing tests that point at it: a traceback line
    inside that piece of the rebuilt file, else the piece named in the
    failure text."""
    out: Dict[str, List[str]] = {}
    fname = module_file.name
    names = [p.name for p in all_pieces if p.kind != "constants"]
    for tid, msg in failures.items():
        hit = None
        for m in re.finditer(re.escape(fname) + r"\D{1,3}(\d+)", msg):
            line = int(m.group(1))
            for pname, (a, b) in spans.items():
                if a <= line <= b:
                    hit = pname
        if hit is None:
            for pname in names:
                if re.search(r"\b" + re.escape(pname) + r"\b", msg):
                    hit = pname
                    break
        if hit is None and "constants" in spans:
            hit = "constants"
        if hit:
            out.setdefault(hit, []).append(tid)
    return out


def write_piece(name: str, header: str, piece: Piece,
                all_pieces: Sequence[Piece], model_call: ModelCall,
                failures: str = "", code: str = "") -> Dict[str, Any]:
    """One model call for one piece (a repair when ``failures`` is given)."""
    prompt, trimmed = piece_prompt(name, header, piece, all_pieces)
    kind_word = {"class": "class", "function": "function",
                 "constants": "constant definitions"}[piece.kind]
    if failures:
        prompt += "\n" + WIDGET_REPAIR.format(
            kind_word=kind_word, name=piece.name, failures=failures,
            code=code[:6000])
    t0 = time.time()
    try:
        reply = str(model_call(prompt) or "")
    except Exception as exc:                             # noqa: BLE001
        return {"code": "", "problems": [f"the model call raised {exc!r}"],
                "trimmed": trimmed, "seconds": round(time.time() - t0, 1),
                "prompt_chars": len(prompt), "truncated": False}
    new = extract_code(reply)
    problems = check_piece(new, piece)
    truncated = "```" in reply and reply.count("```") == 1
    if truncated and problems:
        problems.insert(0, "the reply stopped before the code block closed "
                           "(out of tokens?)")
    return {"code": new, "problems": problems, "trimmed": trimmed,
            "seconds": round(time.time() - t0, 1),
            "prompt_chars": len(prompt), "truncated": truncated}


def rebuild_widget(name: str, work: Path, *, model_call: ModelCall,
                   repairs: int = 2,
                   baseline: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """Stage 3 for one widget: write every piece, assemble, run the
    widget's tests, send failures back as repairs, grade."""
    tests = widget_tests(name)
    header, plist = pieces(name)
    wdir = work / name
    wdir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {"widget": name,
                              "tests": [t.name for t in tests],
                              "pieces": {}, "rounds": []}
    if baseline is None:
        baseline = widget_baseline(name, wdir, header, tests)
    report["baseline"] = {k: baseline[k] for k in ("counted", "original_fail",
                                                   "infra")}
    counted = baseline["counted"]
    codes: Dict[str, str] = {}
    for p in plist:
        done = write_piece(name, header, p, plist, model_call)
        report["pieces"][p.name] = {k: done[k] for k in (
            "problems", "trimmed", "seconds", "prompt_chars", "truncated")}
        report["pieces"][p.name]["calls"] = 1
        codes[p.name] = done["code"] if not done["problems"] else ""
    module_file = wdir / f"{name}.py"
    result: Dict[str, Any] = {}
    for rnd in range(repairs + 1):
        source, spans = assemble(header, [
            (p, codes[p.name] or placeholder(p)) for p in plist])
        module_file.write_text(source, encoding="utf-8")
        result = run_tests(tests, wdir, swap={widget_module(name):
                                              module_file},
                           tag=f"round{rnd}")
        passed = [t for t in counted
                  if result["outcomes"].get(t) == "passed"]
        failing = {t: result["failures"].get(t, "not run")
                   for t in counted if t not in passed}
        report["rounds"].append({"passed": len(passed),
                                 "counted": len(counted),
                                 "infra": result["infra"]})
        if not failing or rnd == repairs or result["infra"]:
            break
        blamed = blame(failing, spans, module_file, plist)
        # The pieces with the most failures first; a few per round keeps a
        # round's cost bounded on a module with dozens of pieces.
        order = sorted(blamed.items(), key=lambda kv: -len(kv[1]))[:4]
        if not order:
            break
        by_name = {p.name: p for p in plist}
        for pname, tids in order:
            p = by_name[pname]
            text = "\n\n".join(f"{t}:\n{failing[t][-700:]}" for t in tids[:3])
            done = write_piece(name, header, p, plist, model_call,
                               failures=text,
                               code=codes[pname] or placeholder(p))
            rec = report["pieces"][pname]
            rec["calls"] += 1
            if not done["problems"]:
                codes[pname] = done["code"]
                rec["problems"] = []
            else:
                rec["repair_problems"] = done["problems"]
    report["module"] = str(module_file)
    final = report["rounds"][-1] if report["rounds"] else {}
    n = len(counted) or 1
    report["score"] = round(100.0 * final.get("passed", 0) / n, 1)
    report["passed"] = final.get("passed", 0)
    report["counted"] = len(counted)
    report["failing"] = sorted(t for t in counted
                               if result.get("outcomes", {}).get(t)
                               != "passed")
    return report


def widget_baseline(name: str, wdir: Path, header: str,
                    tests: Sequence[Path]) -> Dict[str, Any]:
    """Which of the widget's tests count (see the module docstring)."""
    wdir.mkdir(parents=True, exist_ok=True)
    original = run_tests(tests, wdir, tag="original")
    # Every item a placeholder rather than absent: a test file that does
    # `from ... import CaptureReviewer` still collects, and its tests that
    # never touch the widget pass — and so are not counted.
    _h, plist = pieces(name)
    empty_file = wdir / f"{name}_empty.py"
    empty_file.write_text(assemble(header, [(p, placeholder(p))
                                            for p in plist])[0],
                          encoding="utf-8")
    empty = run_tests(tests, wdir, swap={widget_module(name): empty_file},
                      tag="empty")
    counted = counted_tests(original["outcomes"], empty["outcomes"])
    return {"counted": counted,
            "original_fail": sorted(t for t, s in original["outcomes"].items()
                                    if s in ("failed", "error")),
            "infra": original["infra"] or empty["infra"]}


def widget_findings(rep: Dict[str, Any]) -> List[str]:
    """What went wrong with one widget, in a person's words."""
    out = []
    name = rep["widget"]
    if rep["baseline"]["infra"]:
        out.append(f"{name}: its tests could not run on this machine "
                   f"({rep['baseline']['infra']}), so it was not graded.")
        return out
    if rep["baseline"]["original_fail"]:
        out.append(f"{name}: {len(rep['baseline']['original_fail'])} of its "
                   f"tests fail on the ORIGINAL here, so they were not "
                   f"counted (the machine, not the model).")
    for pname, p in rep["pieces"].items():
        if p.get("trimmed"):
            out.append(f"{name}.{pname}: its spec was longer than "
                       f"{SPEC_BUDGET} characters and was cut short.")
        if p.get("problems"):
            out.append(f"{name}.{pname}: never delivered — "
                       f"{p['problems'][0]}")
    if rep["counted"]:
        out.append(f"{name}: {rep['passed']} of {rep['counted']} of Typhon's "
                   f"tests pass on the rebuild"
                   + (f"; still failing: {', '.join(t.split('::')[-1] for t in rep['failing'][:5])}"
                      + (" …" if len(rep["failing"]) > 5 else "")
                      if rep["failing"] else "."))
    return out


def run_widgets(work: Path, *, model_call: ModelCall, repairs: int = 2,
                only: Sequence[str] = (),
                vault: Optional[Path] = None) -> Dict[str, Any]:
    """Stage 3 for every widget (or ``only`` those); with a ``vault``, then
    Typhon itself run with all of them in place (run_assembled)."""
    reports = []
    for name in WIDGETS:
        if only and name not in only:
            continue
        reports.append(rebuild_widget(name, work, model_call=model_call,
                                      repairs=repairs))
    counted = sum(r["counted"] for r in reports)
    passed = sum(r["passed"] for r in reports)
    findings = []
    for r in reports:
        findings.extend(widget_findings(r))
    out = {"score": round(100.0 * passed / (counted or 1), 1),
           "passed": passed, "counted": counted, "widgets": reports,
           "findings": findings}
    if vault is not None and reports:
        swap = {widget_module(r["widget"]): Path(r["module"])
                for r in reports if r.get("module")}
        out["assembled"] = run_assembled(vault, swap)
        if not out["assembled"].get("ok"):
            findings.append("Typhon built with every rebuilt widget in place "
                            "did not run cleanly: "
                            + out["assembled"].get("detail", ""))
    return out


def run_assembled(vault: Path, swap: Dict[str, Path]) -> Dict[str, Any]:
    """Typhon built from its own example, run offscreen with the rebuilt
    widget modules loaded in place of the originals and every control
    poked: whether the rebuild holds up inside the real app, not only
    under its tests."""
    import gui_projects
    import run_describe_prompts as rdp
    from council_core import designer_examples as dx

    name = rdp.fresh_name("Typhon with rebuilt widgets", vault)
    try:
        dx.build_project("typhon", name, vault, toolkit="qt")
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": f"building Typhon failed: {exc}"}
    pdir = gui_projects.project_path(name, vault)
    ran = rdp.run_generated(pdir, prelude=app_prelude(swap))
    ran["project"] = str(pdir)
    return ran


# ============================================================
# The Council's own model calls
# ============================================================

#: Per stage: (tokens a reply may take, seconds a call may take). A widget
#: class is the longest thing asked for; a CPU-placed model writing 3000
#: tokens is minutes.
WIRING_NUM_PREDICT, WIRING_TIMEOUT = 400, 180
WIDGET_NUM_PREDICT, WIDGET_TIMEOUT = 3000, 900
ROLE = "coder"


def engine_call(num_predict: int, timeout: int, *,
                temperature: float = 0.2) -> ModelCall:
    """A model call through the Council's engine, the coder role — the
    role Describe it and the code writer use. council_engine is imported
    on the first call, never before, so a stub run loads nothing."""
    def call(prompt: str, **_kw: Any) -> str:
        import council_engine
        from council_core import designer_project as dp
        return dp.local_chat(
            council_engine, messages=[{"role": "user", "content": prompt}],
            temperature=temperature, num_predict=int(num_predict),
            timeout=int(timeout), role=ROLE)
    return call


# ============================================================
# Generating a wireframe and running what came out
# ============================================================

def _window_dict(window: Any) -> Dict[str, Any]:
    return {k: getattr(window, k) for k in ("title", "bg", "fg", "font",
                                            "min_w", "min_h")
            if getattr(window, k, None) not in (None, "")}


def _generate_and_run(vault: Path, base: str, shapes: Sequence[Any], *,
                      mode: str, canvas: Tuple[int, int],
                      window: Dict[str, Any], requires: Sequence[str] = (),
                      runtime: bool = True,
                      swap: Optional[Dict[str, Path]] = None
                      ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A fresh Qt project in the vault, the window and canvas applied, the
    shapes saved and generated through the Generate button's own code, and
    the app built offscreen with every control poked
    (run_describe_prompts.run_generated)."""
    import gui_projects
    import run_describe_prompts as rdp
    from council_core import designer_project as dp

    name = rdp.fresh_name(base, vault)
    made = dp.create(name, mode, vault, "qt")
    if not made.ok:
        return {"ok": False, "detail": f"create: {made.message}"}, {}
    values = {"canvas_w": canvas[0], "canvas_h": canvas[1], **window}
    if requires:
        values["requires"] = ", ".join(requires)
    applied = dp.apply_window(name, values, list(shapes), vault)
    if not applied.ok:
        return {"ok": False, "detail": f"window: {applied.message}"}, {}
    pdir = gui_projects.project_path(name, vault)
    gen = rdp.generate_project(name, pdir, list(shapes), vault)
    gen["project"] = str(pdir)
    if not gen["ok"] or not runtime:
        return gen, {}
    return gen, rdp.run_generated(pdir, prelude=app_prelude(swap))


def app_prelude(swap: Optional[Dict[str, Path]] = None) -> str:
    """Python run before the probe: the Council on sys.path, as a linked
    project's main.py puts it, then each swapped module loaded under its
    real name (the same loader the test runs use)."""
    lines = ["import sys", f"sys.path.insert(0, {str(HERE)!r})"]
    if swap:
        lines.append("import os")
        lines.append("os.environ['COUNCIL_SWAP'] = " + repr(os.pathsep.join(
            f"{k}={v}" for k, v in swap.items())))
        lines.append(SWAP_PLUGIN)
    return "\n".join(lines) + "\n"


# ============================================================
# Pictures
# ============================================================

#: Fill per kind, so a missing list or a dropdown drawn as a text box can be
#: seen at a glance in the side-by-side picture.
KIND_FILL = {"button": "#d9e7f5", "label": "#00000000", "entry": "#ffffff",
             "spinbox": "#ffffff", "combobox": "#fff3d6", "listbox": "#e8f5e1",
             "image_canvas": "#333333", "scrubber": "#cccccc",
             "notebook": "#00000000", "frame": "#00000000",
             "file_picker": "#f3e5f5"}


_QAPP: Any = None


def draw_wireframe(shapes: Sequence[Any], canvas: Tuple[int, int],
                   path: Path, *, bg: str = "", title: str = "") -> bool:
    """A PNG of a wireframe: each shape a box filled by kind, with its text.
    Offscreen; False (and no file) when Qt cannot paint here."""
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
        from PySide6.QtWidgets import QApplication
    except Exception:                                    # noqa: BLE001
        return False
    global _QAPP
    _QAPP = QApplication.instance() or QApplication([])   # fonts need it
    w, h = canvas
    img = QImage(w, h + 32, QImage.Format_ARGB32)
    img.fill(QColor("#ffffff"))
    p = QPainter(img)
    try:
        p.fillRect(0, 32, w, h, QColor(bg or "#f4f4f4"))
        p.setPen(QColor("#000000"))
        p.setFont(QFont("Arial", 14, QFont.Bold))
        p.drawText(QRect(8, 0, w - 16, 32), Qt.AlignVCenter, title)
        p.setFont(QFont("Arial", 9))
        for s in sorted(shapes, key=lambda s: getattr(s, "z", 0)):
            r = QRect(s.x, s.y + 32, s.w, s.h)
            p.fillRect(r, QColor(KIND_FILL.get(s.kind, "#eeeeee")))
            p.setPen(QPen(QColor("#ffffff" if bg else "#555555"), 1))
            p.drawRect(r)
            text = shape_text(s) or s.kind
            p.setPen(QColor("#000000") if KIND_FILL.get(s.kind, "#eee")
                     not in ("#00000000", "#333333") else QColor("#ffffff"))
            p.drawText(r.adjusted(3, 1, -3, -1),
                       Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap,
                       text[:80])
    finally:
        p.end()
    return img.save(str(path))


# ============================================================
# The oracle: a stub that answers with what Typhon has
# ============================================================

def oracle() -> ModelCall:
    """A model call that answers every prompt this module sends with what
    Typhon actually has: each part's shapes, each control's link, each
    widget item's original source. Full marks from it prove the harness can
    award them; anything less is a harness bug, not a model's miss."""
    spec = load_sections()
    typhon = load_typhon()
    by_text = {}
    for sec in spec["sections"]:
        x0, y0, _w, _h = sec["region"]
        rows = []
        for s in typhon.shapes:
            if section_of(s, spec["sections"]) != sec["id"]:
                continue
            row = {"kind": s.kind, "label": s.label, "x": s.x - x0,
                   "y": s.y - y0, "w": s.w, "h": s.h,
                   "props": _oracle_props(s)}
            for k in ("bg", "fg", "font"):
                if getattr(s, k):
                    row[k] = getattr(s, k)
            rows.append(row)
        window = {"title": "Typhon", "bg": typhon.window.bg,
                  "fg": typhon.window.fg, "font": ""} \
            if sec["id"] == "header" else {"title": "", "bg": "", "fg": "",
                                           "font": ""}
        by_text[sec["text"]] = json.dumps({"window": window, "shapes": rows})
    links = {}
    shapes, want = unwired(typhon)
    for s in shapes:
        if s.id in want:
            links[control_text(s, shapes, "")[:60]] = json.dumps(want[s.id])
    sources = {}
    for name in WIDGETS:
        _h, plist = pieces(name)
        for p in plist:
            sources[(widget_module(name), p.kind, p.name)] = \
                _with_helpers(name, p, plist)

    def call(prompt: str, **_kw: Any) -> str:
        for text, reply in by_text.items():
            if text in prompt:
                return reply
        if prompt.startswith("You are wiring"):
            for key, reply in links.items():
                if key in prompt:
                    return reply
            return "{}"
        m = re.search(r"rebuilding one Python module of a PySide6 \(Qt\) "
                      r"camera capture app,\n(\S+), one", prompt)
        w = re.search(r"WRITE THIS ITEM — the (class|function|module-level "
                      r"constants) ?(\w*)", prompt)
        if m and w:
            kind = {"class": "class", "function": "function"}.get(
                w.group(1), "constants")
            name = w.group(2) if kind != "constants" else "constants"
            code = sources.get((m.group(1), kind, name), "")
            return f"```python\n{code}\n```"
        return ""
    return call


def _with_helpers(name: str, piece: Piece, plist: Sequence[Piece]) -> str:
    """The original source of ``piece`` preceded by every top-level item of
    the original that it reaches and no piece covers — the private helpers
    a model is told to write inside its block. Source order, so a helper
    class is defined before a class that subclasses it."""
    source = widget_path(name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    covered = {p.name for p in plist if p.kind != "constants"}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            covered |= set(assigned_names(node))     # the constants piece
    defs: Dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for n in assigned_names(node):
                defs[n] = node

    def names_in(node: ast.AST) -> set:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    if piece.kind == "constants":
        roots = [n for n in tree.body if isinstance(n, (ast.Assign,
                                                        ast.AnnAssign))
                 and (ast.get_source_segment(source, n) or "")
                 in piece.original]
    else:
        roots = [defs[piece.name]]
    need, todo = set(), set()
    for r in roots:
        todo |= names_in(r)
    while todo:
        n = todo.pop()
        if n in need or n in covered or n not in defs:
            continue
        need.add(n)
        todo |= names_in(defs[n])
    helpers = [node for node in tree.body
               if any(defs.get(n) is node for n in need)
               and node not in roots]
    chunks = ["\n".join(lines[node.lineno - 1:node.end_lineno])
              for node in helpers]
    return "\n\n".join(chunks + [piece.original])


def _oracle_props(s: Any) -> Dict[str, Any]:
    """The props Describe accepts for this kind: the catalogue's own keys,
    without the handler props a model is never asked to write."""
    from gui_shapes import PALETTE
    schema = PALETTE.get(s.kind, {}).get("prop_schema") or {}
    return {k: v for k, v in (s.props or {}).items()
            if k in schema and schema[k].get("type") != "handler"}


# ============================================================
# The report
# ============================================================

def problem_areas(results: Dict[str, Any]) -> List[str]:
    """The stage-level conclusions a person acts on, before the details."""
    out = []
    lay, wir, wid = (results.get(k) for k in ("layout", "wiring", "widgets"))
    if lay:
        refused = [s["name"] for s in lay["sections"].values() if not s["ok"]]
        if refused:
            out.append(f"Layout: Describe it could not produce "
                       f"{len(refused)} of {len(lay['sections'])} parts "
                       f"({', '.join(refused)}).")
        t = lay["totals"]
        if t.get("original"):
            out.append(f"Layout: {t.get('same_kind', 0)} of "
                       f"{t['original']} of Typhon's shapes came back as the "
                       f"same kind of widget, {t.get('read_same', 0)} of "
                       f"{t.get('labelled', 0)} labelled ones read the same.")
    if wir:
        n = sum(wir["tally"].values())
        out.append(f"Wiring: {wir['tally'].get(PASS, 0)} of {n} controls "
                   f"linked exactly as Typhon, {wir['tally'].get(PARTIAL, 0)} "
                   f"to the right function with the wrong ports.")
        if wir.get("retrieval_misses"):
            out.append(f"Wiring: the function shortlist missed the right "
                       f"function for {wir['retrieval_misses']} controls.")
    if wid:
        for r in wid["widgets"]:
            undelivered = [k for k, p in r["pieces"].items()
                           if p.get("problems")]
            line = (f"Widgets: {r['widget']} passes {r['passed']} of "
                    f"{r['counted']} of Typhon's tests")
            if undelivered:
                line += (f"; {len(undelivered)} of {len(r['pieces'])} items "
                         f"never delivered")
            out.append(line + ".")
    return out


def write_report(results: Dict[str, Any], out_dir: Path) -> Path:
    """results.json and REPORT.md in ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(results, indent=1, default=str), encoding="utf-8")
    lines = ["# The Council rebuilds Typhon", "",
             f"Run {results.get('started', '')}, model: "
             f"{results.get('model') or 'the Council’s configured models'}.",
             ""]
    lines += ["## Problem areas", ""]
    lines += [f"- {p}" for p in problem_areas(results)] or ["- (none)"]
    for key, title in (("layout", "Layout"), ("wiring", "Wiring"),
                       ("widgets", "Widgets")):
        r = results.get(key)
        if not r:
            continue
        lines += ["", f"## {title}: {r['score']}%", ""]
        if key == "layout":
            if r.get("pictures"):
                lines += [f"![Typhon]({r['pictures'][0]})",
                          f"![Rebuilt]({r['pictures'][1]})", ""]
            lines += ["| Part | Shapes (Typhon / rebuilt) | Same kind | "
                      "Read the same | Calls |", "|---|---|---|---|---|"]
            for s in r["sections"].values():
                lines.append(f"| {s['name']}{'' if s['ok'] else ' (refused)'}"
                             f" | {s['original']} / {s['rebuilt']} | "
                             f"{s['same_kind']} | {s['read_same']} of "
                             f"{s['labelled']} | {s['attempts']} |")
            lines.append("")
            gen, run = r.get("generate", {}), r.get("runtime", {})
            if gen:
                lines.append(f"Generate: {'OK' if gen.get('ok') else 'failed'}"
                             f" — {gen.get('detail', '')}")
            if run:
                lines.append(f"Run: {'OK' if run.get('ok') else 'failed'} — "
                             f"{run.get('detail', '')}")
        if key == "wiring":
            lines += ["| Control | Wanted | Got | Grade |", "|---|---|---|---|"]
            for c in r["controls"]:
                lines.append(f"| {c['control']} | {c['wanted']} | "
                             f"{c['got'] or '—'} | {c['grade']} |")
            lines.append("")
            gen, run = r.get("generate", {}), r.get("runtime", {})
            if gen:
                lines.append(f"Generate: {'OK' if gen.get('ok') else 'failed'}"
                             f" — {gen.get('detail', '')}")
            if run:
                lines.append(f"Run: {'OK' if run.get('ok') else 'failed'} — "
                             f"{run.get('detail', '')}")
        if key == "widgets":
            lines += ["| Widget | Tests passed | Items | Model calls |",
                      "|---|---|---|---|"]
            for w in r["widgets"]:
                calls = sum(p.get("calls", 0) for p in w["pieces"].values())
                lines.append(f"| {w['widget']} | {w['passed']} of "
                             f"{w['counted']} | {len(w['pieces'])} | "
                             f"{calls} |")
            asm = r.get("assembled")
            if asm:
                lines += ["", f"Typhon with every rebuilt widget in place: "
                              f"{'OK' if asm.get('ok') else 'failed'} — "
                              f"{asm.get('detail', '')}"]
        if r.get("findings"):
            lines += ["", "### Findings", ""]
            lines += [f"- {f}" for f in r["findings"]]
    path = out_dir / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
