#!/usr/bin/env python
"""
Have the Council rebuild Typhon, and report where it fell short.

    python run_typhon_recreate.py --vault D:/scratch/vault
    python run_typhon_recreate.py --vault V --model C:/models/phi-4-Q4_K_M.gguf
    python run_typhon_recreate.py --vault V --stages layout,wiring
    python run_typhon_recreate.py --vault V --widgets pop_out,preset_picker
    python run_typhon_recreate.py --vault V --stub oracle     (no model)

THE THREE STAGES (typhon_recreate.py says why they are graded apart)
  layout   Typhon described part by part, through Describe it; graded shape
           by shape against typhon.gspec, then generated and run offscreen.
  wiring   Typhon's wireframe with its links taken off; the model links each
           control; graded against Typhon's links, then generated and run.
  widgets  Each hand-built widget written again, item by item, from a spec;
           graded by running Typhon's own tests against it, with failures
           sent back as repairs; then Typhon run with every rebuild in place.

WHAT COMES OUT
<out>/REPORT.md (problem areas first, then each stage's table and findings),
results.json (everything, for comparing runs), layout_typhon.png and
layout_rebuilt.png side by side, and widgets/<name>/<name>.py — each module
as the Council wrote it. Projects go in the vault, one fresh project per
generated app; nothing existing is touched and nothing in the repository is
changed.

--stub oracle answers every prompt with what Typhon actually has. It needs
no model and should score 100% everywhere: run it first on a new machine,
because anything less is the harness or the machine, not a model.

The widget stage runs pytest, so it needs pytest installed in this Python.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STAGES = ("layout", "wiring", "widgets")


def _csv(text: str) -> List[str]:
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", required=True,
                    help="scratch vault; the generated projects go here")
    ap.add_argument("--model", default="",
                    help="GGUF for the main slot (sets COUNCIL_GGUF_PATH); "
                         "default: the Council's configured coder role")
    ap.add_argument("--stages", default=",".join(STAGES),
                    help=f"comma-separated, from {', '.join(STAGES)}")
    ap.add_argument("--sections", default="",
                    help="layout: only these part ids (sections.json)")
    ap.add_argument("--controls", default="",
                    help="wiring: only these shape ids (s12,s21,…)")
    ap.add_argument("--widgets", default="",
                    help="widgets: only these modules (pop_out,…)")
    ap.add_argument("--repairs", type=int, default=2,
                    help="widgets: repair rounds after the first test run")
    ap.add_argument("--no-runtime", action="store_true",
                    help="skip building the generated apps offscreen")
    ap.add_argument("--stub", choices=("oracle",), default=None,
                    help="answer with Typhon itself instead of a model")
    ap.add_argument("--out", default="",
                    help="report folder (default <vault>/typhon_recreate/"
                         "<time>)")
    args = ap.parse_args(argv)

    stages = _csv(args.stages)
    bad = [s for s in stages if s not in STAGES]
    if bad:
        ap.error(f"unknown stage(s) {bad}; choose from {STAGES}")
    vault = Path(args.vault).resolve()
    vault.mkdir(parents=True, exist_ok=True)
    # Before anything imports the engine: the slot file and the main model
    # are read from these.
    os.environ["COUNCIL_VAULT_ROOT"] = str(vault)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    if args.model:
        os.environ["COUNCIL_GGUF_PATH"] = str(Path(args.model).resolve())

    import typhon_recreate as tr

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out).resolve() if args.out else \
        vault / "typhon_recreate" / stamp
    out.mkdir(parents=True, exist_ok=True)
    if args.stub:
        layout_call = wiring_call = widget_call = tr.oracle()
    else:
        layout_call = None                    # the Designer's own Describe
        wiring_call = tr.engine_call(tr.WIRING_NUM_PREDICT,
                                     tr.WIRING_TIMEOUT)
        widget_call = tr.engine_call(tr.WIDGET_NUM_PREDICT,
                                     tr.WIDGET_TIMEOUT)
    results = {"started": stamp, "stages": stages,
               "model": ("oracle stub" if args.stub else
                         args.model or "the Council's coder role")}
    runtime = not args.no_runtime

    def save() -> None:
        tr.write_report(results, out)

    t0 = time.time()
    if "layout" in stages:
        print("layout: describing Typhon part by part …", flush=True)
        results["layout"] = tr.run_layout(vault, model_call=layout_call,
                                          runtime=runtime,
                                          only=_csv(args.sections),
                                          out_dir=out)
        print(f"layout: {results['layout']['score']}%", flush=True)
        save()
    if "wiring" in stages:
        print("wiring: linking each control …", flush=True)
        results["wiring"] = tr.run_wiring(vault, model_call=wiring_call,
                                          runtime=runtime,
                                          only=_csv(args.controls))
        print(f"wiring: {results['wiring']['score']}%", flush=True)
        save()
    if "widgets" in stages:
        print("widgets: rebuilding the hand-built widgets (runs pytest) …",
              flush=True)
        results["widgets"] = tr.run_widgets(
            out / "widgets", model_call=widget_call, repairs=args.repairs,
            only=_csv(args.widgets), vault=vault if runtime else None)
        print(f"widgets: {results['widgets']['score']}%", flush=True)
    results["seconds"] = round(time.time() - t0, 1)
    report = tr.write_report(results, out)
    print("\nProblem areas:")
    for line in tr.problem_areas(results) or ["(none)"]:
        print(f"  - {line}")
    print(f"\nReport: {report}")
    print(f"Everything: {out / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
