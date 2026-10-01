#!/usr/bin/env python
"""
Grade the Designer's "Describe it" against a real model, end to end.

    python run_describe_prompts.py --vault D:/scratch/vault
    python run_describe_prompts.py --vault V --only E1,M3,H1
    python run_describe_prompts.py --vault V --model C:/models/phi-4-Q4_K_M.gguf

WHAT A PROMPT HAS TO SURVIVE
----------------------------
Each prompt in examples/gui/describe_prompts/prompts.json goes through the same
path a user's description does, and then two steps further:

  1. DESCRIBE   gui_describe turns the text into a wireframe (the model call,
                the checks, up to two repair rounds).
  2. GRADE      the wireframe is checked HERE, independently of gui_describe's
                own verdict: palette kinds only, inside the 1100x700 canvas,
                gui_spec.validate passes, no code fields, and the prompt's own
                expectations (kinds, counts, labels, typed props, tab count).
  3. GENERATE   saved into a fresh Qt project and generated through
                designer_project.generate — the Generate button's own code —
                which must not block and must say "policy: OK".
  4. RUN        the generated App is constructed offscreen in a SUBPROCESS,
                its value widgets are poked so every handler fires, and any
                traceback or "handler raised" line fails it.

A subprocess for step 4 because a generated app is foreign code: a crash in it
must cost one prompt's grade, not the whole run, and a dialog it opens by
mistake must hit the timeout rather than block this process forever.

It writes only inside --vault, one fresh project per prompt, and never deletes
a project it did not just create. Nothing here needs a display.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROMPTS = HERE / "examples" / "gui" / "describe_prompts" / "prompts.json"
CANVAS_W, CANVAS_H = 1100, 700
RUNTIME_TIMEOUT = 90

#: FAIL outranks PARTIAL outranks PASS. A refusal is SAFE only where the
#: prompt allows one (expect.ok is null).
PASS, PARTIAL, FAIL, SAFE = "PASS", "PARTIAL", "FAIL", "SAFE-REFUSED"


def load_prompts(path: Path = PROMPTS) -> List[Dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["prompts"]


# ============================================================
# Grading a wireframe
# ============================================================

def _text_of(shape) -> str:
    """Everything a user would read on a shape, lower-cased."""
    props = getattr(shape, "props", {}) or {}
    parts = [str(getattr(shape, "label", "") or "")]
    for key in ("tabs", "buttons", "values", "columns", "menus"):
        value = props.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(json.dumps(v) if isinstance(v, dict) else str(v)
                         for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _prop_matches(shape, rule: Dict[str, Any]) -> bool:
    value = (getattr(shape, "props", {}) or {}).get(rule["prop"])
    if "equals" in rule:
        try:
            return float(value) == float(rule["equals"])
        except (TypeError, ValueError):
            return False
    wanted = str(rule["contains"]).lower()
    if isinstance(value, (list, tuple)):
        return any(wanted == str(v).strip().lower() for v in value)
    return False          # a string "a,b,c" is the bug the check exists for


def structural_problems(shapes, canvas_w=CANVAS_W, canvas_h=CANVAS_H,
                        title="Described") -> List[str]:
    """What makes a wireframe unusable, whatever the prompt asked for.

    Checked for EVERY prompt, which is why the prompt file's "only_palette"
    and "no_code_fields" flags are documentation rather than switches: no
    prompt is allowed a non-palette kind or a model-authored code field.

    Re-derived here rather than trusted from gui_describe: a grader that asks
    the thing under test whether it passed grades nothing.
    """
    import gui_layout
    import gui_spec
    from gui_shapes import PALETTE

    problems = []
    for i, s in enumerate(shapes, 1):
        tag = f"shape {i} ({s.kind} {s.label!r})"
        if s.kind not in PALETTE or s.kind == "generic":
            problems.append(f"{tag}: not a palette kind")
        if s.x < 0 or s.y < 0 or s.x + s.w > canvas_w or s.y + s.h > canvas_h:
            problems.append(f"{tag}: outside the {canvas_w}x{canvas_h} canvas")
        if (getattr(s, "script", None) or getattr(s, "drives", None)
                or getattr(s, "port", None)):
            problems.append(f"{tag}: carries a script, sequence link or port "
                            f"the model was not allowed to author")
    # Siblings drawn over each other, by pixel. gui_spec.validate does not
    # see an overlap the layout's grid clustering absorbs.
    kids = gui_layout.build_containment_tree(shapes, warnings=[])
    by_id = {s.id: s for s in shapes}
    for ids in kids.values():
        sibs = [by_id[i] for i in ids]
        for n, a in enumerate(sibs):
            for b in sibs[n + 1:]:
                ox = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
                oy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
                if ox > 0 and oy > 0:
                    problems.append(f"{a.kind} {a.label!r} and {b.kind} "
                                    f"{b.label!r} overlap by {ox} x {oy} px")
    try:
        tree = gui_layout.infer(shapes, canvas_w, canvas_h)
        spec = gui_spec.build(shapes, tree, {}, project="graded",
                              mode="standalone", title=title,
                              min_w=400, min_h=300)
        valid, errors = gui_spec.validate(spec)
        problems.extend(f"validate: {e}" for e in errors if not valid)
    except Exception as exc:                             # noqa: BLE001
        problems.append(f"layout/spec raised: {exc!r}")
    return problems


def expectation_misses(expect: Dict[str, Any], shapes) -> List[str]:
    """What the prompt asked for and did not get. Soft: a PARTIAL, not a FAIL."""
    kinds = Counter(s.kind for s in shapes)
    misses = []
    for kind in expect.get("kinds_all", []):
        if not kinds[kind]:
            misses.append(f"no {kind}")
    any_of = expect.get("kinds_any", [])
    if any_of and not any(kinds[k] for k in any_of):
        misses.append(f"none of {any_of}")
    for kind, n in (expect.get("count_at_least") or {}).items():
        if kinds[kind] < n:
            misses.append(f"{kinds[kind]} {kind}, wanted at least {n}")
    words = expect.get("labels_any", [])
    if words:
        text = " ".join(_text_of(s) for s in shapes)
        if not any(w.lower() in text for w in words):
            misses.append(f"no label mentioning any of {words}")
    tabs = expect.get("notebook_tabs")
    if tabs:
        books = [s for s in shapes if s.kind == "notebook"]
        if not any(len((b.props or {}).get("tabs") or []) == tabs
                   for b in books):
            misses.append(f"no notebook with {tabs} tabs")
    for rule in expect.get("props_any", []):
        if not any(s.kind == rule["kind"] and _prop_matches(s, rule)
                   for s in shapes):
            misses.append(f"no {rule['kind']} with {rule['prop']} "
                          f"{'=' if 'equals' in rule else 'containing'} "
                          f"{rule.get('equals', rule.get('contains'))!r}")
    return misses


def grade(expect: Dict[str, Any], result) -> Dict[str, Any]:
    """The wireframe's grade: PASS / PARTIAL / FAIL / SAFE-REFUSED."""
    if not getattr(result, "ok", False):
        reason = "; ".join((getattr(result, "errors", None) or [])[:3])
        if expect.get("ok") is True:
            return {"grade": FAIL, "problems": [f"refused: {reason}"]}
        return {"grade": SAFE, "problems": [f"refused: {reason}"]}

    shapes = list(result.shapes)
    hard = structural_problems(shapes)
    n = len(shapes)
    if n > expect.get("max_shapes", 60):
        hard.append(f"{n} shapes, more than {expect.get('max_shapes', 60)}")
    if expect.get("ok") is True and n < expect.get("min_shapes", 1):
        hard.append(f"only {n} shape(s), wanted at least "
                    f"{expect.get('min_shapes')}")
    if hard:
        return {"grade": FAIL, "problems": hard}
    soft = expectation_misses(expect, shapes) if expect.get("ok") else []
    return {"grade": PARTIAL if soft else PASS, "problems": soft}


# ============================================================
# Generating it, and running what was generated
# ============================================================

#: Run inside the generated project, in a fresh interpreter. Pokes every value
#: widget so each handler fires — the part of the Qt emitter that used to wire
#: buttons only — and never touches a FilePicker, whose Browse opens a modal.
RUNTIME_PROBE = r'''
import faulthandler, os, sys, traceback
# A hang reports WHERE, on stderr, before the parent's timeout kills it.
faulthandler.dump_traceback_later(int(sys.argv[2]), exit=True)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["COUNCIL_NO_DIALOGS"] = "1"
project = sys.argv[1]
sys.path.insert(0, project)
os.chdir(project)
errors = []
sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
from PySide6.QtWidgets import (QApplication, QAbstractButton, QCheckBox,
                               QComboBox, QMenuBar, QRadioButton, QSlider,
                               QSpinBox, QTabBar)
qapp = QApplication([])
from app import App
w = App()
w.show()
qapp.processEvents()
poked = 0
def not_ours(widget):
    """Qt's own buttons, and a FilePicker's Browse. The menubar's overflow
    button pops a menu and waits for a click nobody makes — measured: it hung
    the mail-client prompt for the whole timeout. Browse opens a modal."""
    if widget.objectName().startswith("qt_"):
        return True
    p = widget.parent()
    while p is not None:
        if isinstance(p, (QMenuBar, QTabBar)) or type(p).__name__ == "FilePicker":
            return True
        p = p.parent()
    return False
for widget in w.findChildren(QAbstractButton):
    if not_ours(widget) or not widget.isEnabled():
        continue
    if isinstance(widget, (QCheckBox, QRadioButton)):
        widget.toggle()
    else:
        widget.click()
    poked += 1
for box in w.findChildren(QComboBox):
    if box.count() > 1:
        box.setCurrentIndex(box.count() - 1); poked += 1
for spin in w.findChildren(QSpinBox):
    spin.setValue(spin.maximum()); poked += 1
for slider in w.findChildren(QSlider):
    slider.setValue(slider.maximum()); poked += 1
# Every leaf menu item, triggered directly — not by opening the menu, which
# waits for a click. Wrappers are held for the whole walk: a QAction whose
# parent menu's wrapper has been collected can read as "already deleted".
bars = w.findChildren(QMenuBar)
for bar in bars:
    tops = list(bar.actions())
    menus = [a.menu() for a in tops]
    for menu in menus:
        if menu is None:
            continue
        items = list(menu.actions())
        for act in items:
            if not act.isSeparator() and act.menu() is None:
                act.trigger(); poked += 1
for _ in range(5):
    qapp.processEvents()
print(f"BUILT poked={poked} slot_errors={len(errors)}")
for e in errors:
    print(e, file=sys.stderr)
getattr(w, "request_close", w.close)()
qapp.processEvents()
'''


#: Committed memory the probed app may use (MB): a generated Qt window is
#: ~150 MB; anything near this is a runaway, not an app.
RUNTIME_MEMORY_MB = 4096


def run_generated(pdir: Path, python: str = sys.executable) -> Dict[str, Any]:
    """Construct the generated App offscreen, in its own process.

    Through council_core.child_proc: the app and anything it starts live in
    a Job Object (killed together on the timeout), the machine's commit
    headroom is checked first, and a process the MACHINE could not start —
    0xC0000142 (STATUS_DLL_INIT_FAILED), which every probe late in the
    phi4 benchmark exited with when the PC ran out of virtual memory — is
    returned as {"ok": False, "infra": why}, never as the app's failure."""
    from council_core import child_proc
    env = child_proc.child_env({"QT_QPA_PLATFORM": "offscreen",
                                "COUNCIL_NO_DIALOGS": "1"})
    ran = child_proc.run(
        [python, "-c", RUNTIME_PROBE, str(pdir), str(RUNTIME_TIMEOUT - 10)],
        cwd=str(pdir), env=env, timeout=RUNTIME_TIMEOUT,
        memory_limit_mb=RUNTIME_MEMORY_MB)
    if ran.infra:
        return {"ok": False, "infra": ran.infra,
                "detail": f"not graded — {ran.infra}"[:400]}
    if ran.timed_out:
        return {"ok": False, "detail": f"timed out after {RUNTIME_TIMEOUT}s "
                                       f"(a modal dialog?)"}
    if ran.error and ran.returncode is None:
        return {"ok": False, "detail": ran.error[:400]}
    proc = ran
    out, err = ran.stdout, ran.stderr
    built = next((line for line in out.splitlines()
                  if line.startswith("BUILT")), "")
    # Tracebacks, and the ports runtime's own "handler raised" / "callback
    # raised" lines — which it PRINTS, to stdout, after catching the error, so
    # sys.excepthook never sees them and a stderr-only scan graded an app
    # whose every handler threw as a pass. Not any line saying "Error": Qt's
    # offscreen plugin prints harmless warnings.
    bad = [line for line in (out.splitlines() + err.splitlines())
           if "Traceback" in line or "handler raised" in line
           or "callback raised" in line or line.startswith("Timeout (")]
    ok = proc.returncode == 0 and bool(built) and "slot_errors=0" in built \
        and not bad
    detail = built or f"exit {proc.returncode}"
    if not ok:
        why = bad[0] if bad else (err.strip().splitlines() or
                                  ["(no stderr)"])[-1]
        detail += " :: " + why.strip()
    out = {"ok": ok, "detail": detail[:400], "seconds": ran.seconds}
    if ran.leaked:
        out["leaked"] = ran.leaked          # killed; said, not hidden
    return out


def generate_project(name: str, pdir: Path, shapes,
                     vault: Path) -> Dict[str, Any]:
    """Save and Generate through designer_project — the Generate button's
    own path."""
    from council_core import designer_project as dp

    saved = dp.save(name, shapes, vault)
    if not saved.ok:
        return {"ok": False, "detail": saved.message}
    out = dp.generate(name, shapes, pdir, vault)
    lines = list(out.lines)
    bad = [line for line in lines
           if line.startswith(("cannot generate", "BLOCKED", "generate failed",
                               "policy REFUSED"))]
    ok = out.ok and not out.blocked and "policy: OK" in lines and not bad
    return {"ok": ok, "detail": "; ".join(bad or lines[-2:])[:400]}


def fresh_name(base: str, vault: Path) -> str:
    """A project name nobody has used. Never clobbers: a directory of the
    same name may hold an app.py someone edited."""
    import gui_projects
    n = 1
    while True:
        name = f"{base} {n}"
        if not gui_projects.project_path(name, vault).exists():
            return name
        n += 1


# ============================================================
# One prompt, and the whole set
# ============================================================

def run_one(prompt: Dict[str, Any], vault: Path, *,
            model_call: Optional[Callable[[str], str]] = None,
            runtime: bool = True, python: str = sys.executable
            ) -> Dict[str, Any]:
    import gui_projects
    from council_core import designer_project as dp

    record: Dict[str, Any] = {"id": prompt["id"], "tier": prompt["tier"],
                              "name": prompt["name"]}
    # The project first, as a user has one open before they describe: the
    # description is designed for the PROJECT's toolkit, and every prompt
    # here is about Qt.
    name = fresh_name(f"Describe {prompt['id']}", vault)
    created = dp.create(name, "standalone", vault, "qt")
    if not created.ok:
        record.update(grade=FAIL, problems=[f"create: {created.message}"])
        return record
    pdir = gui_projects.project_path(name, vault)
    t0 = time.time()
    try:
        result = dp.describe(prompt["text"], pdir, model_call=model_call)
    except Exception as exc:                             # noqa: BLE001
        record.update(grade=FAIL, problems=[f"describe raised: {exc!r}"],
                      seconds=round(time.time() - t0, 1))
        return record
    record["seconds"] = round(time.time() - t0, 1)
    record["attempts"] = getattr(result, "attempts", None)
    record["shapes"] = len(getattr(result, "shapes", []) or [])
    record["kinds"] = dict(Counter(s.kind for s in result.shapes or []))
    record.update(grade(prompt["expect"], result))
    record["notes"] = list(getattr(result, "notes", []) or [])[:8]

    record["project"] = str(pdir)
    if not result.ok or record["grade"] == FAIL:
        return record
    gen = generate_project(name, pdir, result.shapes, vault)
    record["generate"] = gen
    if not gen["ok"]:
        record["grade"] = FAIL
        record["problems"].append(f"generate: {gen['detail']}")
        return record
    if runtime:
        ran = run_generated(pdir, python)
        record["runtime"] = ran
        if not ran["ok"]:
            record["grade"] = FAIL
            record["problems"].append(f"runtime: {ran['detail']}")
    return record


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", required=True,
                    help="scratch vault; one new project per prompt goes here")
    ap.add_argument("--only", default="", help="comma-separated prompt ids")
    ap.add_argument("--model", default="",
                    help="GGUF for the main slot (sets COUNCIL_GGUF_PATH)")
    ap.add_argument("--out", default="", help="append one JSON line per prompt")
    ap.add_argument("--no-runtime", action="store_true",
                    help="skip constructing the generated app")
    args = ap.parse_args(argv)

    vault = Path(args.vault).resolve()
    vault.mkdir(parents=True, exist_ok=True)
    # Before anything imports the engine: the slot file and the main model are
    # read from these.
    os.environ["COUNCIL_VAULT_ROOT"] = str(vault)
    if args.model:
        os.environ["COUNCIL_GGUF_PATH"] = str(Path(args.model).resolve())

    wanted = {p.strip() for p in args.only.split(",") if p.strip()}
    prompts = [p for p in load_prompts() if not wanted or p["id"] in wanted]
    rows = []
    for prompt in prompts:
        record = run_one(prompt, vault, runtime=not args.no_runtime)
        rows.append(record)
        if args.out:
            with open(args.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        print(f"{record['id']:<3} {record['grade']:<12} "
              f"{record.get('seconds', '?'):>6}s "
              f"x{record.get('attempts') or '?'} "
              f"{record.get('shapes', 0):>2} shapes  {record['name']}"
              + (f"  :: {'; '.join(record['problems'])[:200]}"
                 if record.get("problems") else ""), flush=True)

    tally = Counter(r["grade"] for r in rows)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    return 0 if not tally.get(FAIL) else 1


if __name__ == "__main__":
    raise SystemExit(main())
