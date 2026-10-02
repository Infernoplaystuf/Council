"""
council_core.bench_codebehind — the code suite through the REAL code-behind
writer ("Write it with the model…"), then the case's hidden test.

WHAT RUNS
For each code case (tests/data/llm_bench/code_cases.json) a real Designer
project is made in the scratch vault — one widget per port, the case's port
names and types, Generated so handlers.py exists — and the button is handed to
council_core.designer_codebehind exactly as the Wiring group hands it:
plan() (the signature from the ports, the grounding shortlist, the file and
what may be replaced) then run() with NO model call of ours, so the writer
uses its own: local_chat role "coder" with seed and stop, best-of-N sized
from the coder model, the prompt budgeted to its real window, every gate,
the sandboxed smoke run under the project's interpreter, and the repairs.

ADAPTING A CASE (each case's "codebehind" block says which)
  function mode  K1 K3 K4 K5 K7 K8. The model writes ONE pure function into
                 logic.py; the button gets the script link (inputs -> the
                 function's parameters, the result's keys -> output ports)
                 and its handler is the deterministic linked stub Generate
                 writes (gui_emit.handler_stub): port reads, the error
                 envelope, report_error and clear_ports. Where a case's task
                 names handler-only API ("call self.report_error(...)") its
                 instruction says the function-mode equivalent instead
                 ("raise ValueError(...) — the app reports it and clears the
                 outputs"), which is what that stub does with a raise.
  handler mode   K2 K6. Both need STATE between presses (the list before
                 the first filter; when the stopwatch started), which a pure
                 function cannot keep. Handler mode writes the method body —
                 but its reference gate refuses any self.<attribute> that is
                 not a port or a handler, so the writer as built cannot pass
                 either unless the model finds another place for the state.
                 They are run anyway: that limit is a finding, not a skip.

WHAT IS GRADED
The accepted code, assembled as the app would have it (function mode: the
linked stub in handlers.py plus the new logic.py; handler mode: the wrapped
method), against the case's hidden test in a fresh interpreter
(llm_bench.run_hidden_test). A writer that offers nothing fails at the
gate its best candidate stopped at.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Port kinds that are shown taller than one line.
_TALL = {"listbox": 160, "treeview": 160, "text": 120, "log_pane": 120,
         "image_canvas": 160}

#: gui_codebehind stage -> the benchmark's category for a writer that
#: offered nothing.
_STAGE_CATEGORY = {"extract": "no_code", "shape": "shape",
                   "policy": "policy", "references": "references",
                   "names": "undefined_name", "smoke run": "smoke_failure"}


def case_shapes(case: Dict[str, Any]) -> List[Any]:
    """One widget per port, stacked down the canvas, carrying the case's
    port name and type — so the generated app's ports are the case's."""
    from gui_shapes import new_shape
    shapes = []
    y = 24
    for p in case["ports"]:
        s = new_shape(p["kind"], 24, y)
        s.label = p.get("label") or p["name"]
        s.w = 360 if p["kind"] != "button" else 140
        s.h = _TALL.get(p["kind"], 32)
        port = {"name": p["name"]}
        if p["kind"] != "button" and p.get("type"):
            port["type"] = p["type"]
        s.port = port
        if p["kind"] == "combobox" and p.get("values"):
            s.props["values"] = list(p["values"])
        if p["kind"] == "treeview" and p.get("columns"):
            s.props["columns"] = list(p["columns"])
        if p["kind"] == "file_picker" and p.get("mode"):
            s.props["mode"] = p["mode"]
        if p["kind"] in ("label", "checkbutton") and "text" in s.props:
            s.props["text"] = ""
        shapes.append(s)
        y += s.h + 16
    return shapes


def make_project(case: Dict[str, Any], vault: Path) -> Tuple[str, Path,
                                                             List[Any], Any]:
    """(name, project dir, shapes, the button shape): a fresh Qt project,
    saved and Generated. Raises RuntimeError with the reason."""
    import gui_projects
    import run_describe_prompts as rdp
    from . import designer_project as dp
    name = rdp.fresh_name(f"Code {case['id']}", vault)
    mode = "linked" if case.get("linked") else "standalone"
    made = dp.create(name, mode, vault, "qt")
    if not made.ok:
        raise RuntimeError(f"create: {made.message}")
    pdir = gui_projects.project_path(name, vault)
    shapes = case_shapes(case)
    saved = dp.save(name, shapes, vault)
    if not saved.ok:
        raise RuntimeError(f"save: {saved.message}")
    gen = dp.generate(name, shapes, pdir, vault)
    if not gen.ok:
        raise RuntimeError("generate: " + "; ".join(gen.lines[-3:]))
    button = next(s for s in shapes if s.kind == "button")
    return name, pdir, shapes, button


HANDLERS_HEADER = ('"""handlers.py — the code behind, as the Designer would '
                   'have it (council_core.bench_codebehind)."""\n'
                   'from __future__ import annotations\n')


def assemble(case: Dict[str, Any], plan: Any, review: Any
             ) -> Tuple[str, Dict[str, str]]:
    """(handlers.py source, extra files) for the hidden test."""
    import gui_emit
    handler = case["handler"]
    if plan.mode == "function":
        stub = gui_emit.handler_stub(handler, dict(plan.link),
                                     title=plan.label)
        src = HANDLERS_HEADER + "\n\nclass HandlerMixin:\n" + stub
        return src, {"logic.py": review.after}
    method = review.written
    if plan.handler and plan.handler != handler:
        method = re.sub(rf"def\s+{re.escape(plan.handler)}\s*\(",
                        f"def {handler}(", method, count=1)
    return HANDLERS_HEADER + "\n\nclass HandlerMixin:\n" + method, {}


def category_of(plan: Any, review: Any) -> Tuple[str, str]:
    """(category, detail) for a writer that offered nothing."""
    if not plan.ok:
        return "plan_error", "; ".join(plan.problems)[:400]
    r = review.result
    if r is None:
        return "pipeline_error", "no result"
    errors = list(r.errors or [])
    first = errors[0] if errors else ""
    if r.ok and not review.ok:
        return "policy", first or "the spliced file was refused"
    if r.stopped:
        return "stopped", first
    if first.startswith("the model call failed"):
        return "model_error", first
    if first.startswith(("the code writer failed", "the code writer failed "
                         "unexpectedly")):
        return "pipeline_error", first
    best = r.best
    if best is None:
        return "no_code", first or "nothing came back"
    import gui_codebehind as gcb
    stage = gcb.STAGE_NAMES_TEXT[best.stage]
    faults = list(best.faults or errors)
    text = " ".join(faults).lower()
    cat = _STAGE_CATEGORY.get(stage, stage)
    if stage == "extract":
        if "cut off" in text:
            cat = "truncated"
        elif "syntax" in text or "does not parse" in text:
            cat = "syntax"
    if stage == "references" and ("port" in text or "self." in text):
        cat = "wrong_port"
    if stage == "policy" and "import" in text:
        cat = "forbidden_import"
    return cat, (faults[0] if faults else first)[:400]


def run_case(case: Dict[str, Any], vault: Path, *,
             test_python: Optional[str] = None, timeout: int = 20,
             should_stop=None, n_best: Optional[int] = None
             ) -> Dict[str, Any]:
    """Make the project, run the writer, grade. Fills the row's grading
    fields; the caller adds the model-call cost. Never raises.

    ``n_best`` None is the writer's own choice — what dc.run() picks with
    no model call of ours: gui_codebehind.default_n_best of the coder
    model's size. It is computed here so the report can say it, and passed
    explicitly so --replay can repeat it."""
    import gui_codebehind as gcb
    from . import designer_codebehind as dc
    from . import llm_bench as lb
    spec = dict(case.get("codebehind") or {})
    mode = spec.get("mode") or "function"
    row: Dict[str, Any] = {"passed": False, "mode": mode}
    try:
        _name, pdir, shapes, button = make_project(case, vault)
    except Exception as exc:                              # noqa: BLE001
        row.update(category="pipeline_error", detail=repr(exc)[:300])
        return row
    req = dc.Request(project_dir=pdir, shapes=shapes, shape_id=button.id,
                     instruction=spec.get("instruction") or case["task"],
                     mode=mode, inputs=list(spec.get("inputs") or []),
                     outputs=dict(spec.get("outputs") or {}),
                     function=str(spec.get("function") or ""),
                     should_stop=should_stop)
    plan = dc.plan(req)
    if n_best is None:
        n_best = gcb.default_n_best(dc.coder_params_b())
    row["n_best"] = int(n_best)
    if plan.ok:
        review = dc.run(plan, should_stop=should_stop, n_best=int(n_best))
    else:
        review = dc.Review(plan, None)
    res = review.result
    if res is not None:
        row.update(attempts=res.attempts,
                   repair_rounds=sum(1 for c in res.calls
                                     if c.get("kind") == "repair"),
                   writer_seconds=round(res.seconds, 2),
                   gates=list(res.gates)[:8], notes=list(res.notes)[:6],
                   writer_calls=list(res.calls))
    row["function"] = plan.link.get("function", "") if plan.link else ""
    row["link"] = dict(plan.link or {})
    row["writer_model"] = review.model
    if not review.ok:
        cat, why = category_of(plan, review)
        row.update(category=cat, detail=why, stage="writer",
                   code=(res.best.code if res is not None and res.best
                         else "")[:4000])
        return row
    row["code"] = (review.written or "")[:4000]
    handlers_src, extra = assemble(case, plan, review)
    verdict, detail = lb.run_hidden_test(handlers_src, case,
                                         python=test_python, timeout=timeout,
                                         extra_files=extra)
    row["stage"] = "test"
    row["passed"] = verdict == "ok"
    row["category"] = {"infra": "harness_error"}.get(verdict, verdict)
    row["detail"] = detail[:400]
    return row
