"""
tools/council_ab.py — before/after test of the Council on this PC, with real
models, scored, in one command.

    conda activate wizardCouncil
    python tools\\council_ab.py --model ollama:gemma3:12b

asks the same fixed questions (tools/council_ab_questions.json: synthetic and
public only) of two builds of the app, side by side, and writes a scored
report:

  chat     the Council tab's own turn (CouncilActions.send, the code the tab
           runs: data fast path, vault context, front desk, council) at the
           build before PR #51 and at this checkout. Per question: right or
           wrong (the question's checks), which path answered (data / one
           model / council), whether it was handed on and why, model calls,
           model loads, seconds to the first text and to the whole answer.
           Each question gets a fresh conversation in a fresh synthetic vault,
           so nothing one question leaves behind reaches the next.
  kg       Connections: council_core.kg_bench (30 labelled sentences) before
           PR #50 and now. Needs an Ollama model (--kg-model, or --model
           ollama:NAME).
  ledger   Ledger speed at four builds: before PR #48, before PR #56 (the
           long-log stall), now with one process, and now as the app runs
           (several helper processes since PR #57). Two measures, each with
           a time limit so a stall shows as "over N s" instead of hanging
           the run: one long eddy-current log (2,000 / 4,000 / 12,000 rows),
           and a batch of 44 workbooks through the Ledger tab's own Extract
           (ledger_ingest.run_extract), with what it found, so a faster
           build that finds less shows.

The "before" builds are git worktrees of this repository made in the output
folder (no network: the commits only have to be in this clone). Nothing is
written to your vault or your data; the output folder holds everything:

    <out>/report.html, report.md     the scored comparison
    <out>/chat-<side>.jsonl          one line per question asked
    <out>/kg-<side>.json, ledger-<build>.json
    <out>/vault-<side>/              each side's synthetic vault
    <out>/code-<ref>/                the worktrees (removed at the end unless
                                     --keep-worktrees)

Run again with the same --out to carry on after a stop: questions already in
a side's .jsonl are not asked again.

WHICH SIDES
    --sides before:app,after:app            (the default)
  app      what the tab does as it opens at that build: before #51 the home
           build answered every question with ONE Writer (DEMO_MODE); after,
           one model answers first and hands on to the council
  council  Deliberation ticked: always the whole council (same at both)
  single   COUNCIL_CHAT_MODE=single: one Writer, never handed on
  Add after:council to see what the full council gets right that the front
  desk does not hand it (slow: ~10-30 model calls a question).

WHICH MODELS
  --model ollama:NAME     every role on that Ollama model (COUNCIL_BACKEND=
                          ollama, COUNCIL_OLLAMA_MODEL=NAME)
  --model C:\\x\\m.gguf     every role on that GGUF (COUNCIL_BACKEND=gguf)
  (none)                  your own setup: backend_settings.json and
                          personality_backends.json are copied from your
                          vault (--from-vault, default this checkout's vault)
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
QUESTIONS = HERE / "council_ab_questions.json"

#: The builds just before each change, on qt-migration's first-parent line.
BEFORE = {
    "chat": "2b213b57fb0cb589b218a0f6ec03c539770055a2",    # before PR #51
    "kg": "31daf5fb21bfa43f5c709ceeff9d71a85ca9a6cb",      # before PR #50
    "ledger": "35a2ba7e51712018f462112b98df97356b9deb81",  # before PR #48
}

#: The Ledger's builds, oldest first: (file key, column title, ref, extra
#: environment). "" is this checkout. COUNCIL_LEDGER_WORKERS=1 turns PR
#: #57's helper processes off (a build without them ignores it).
LEDGER_BUILDS = (
    ("pre48", "before #48", BEFORE["ledger"], {}),
    ("pre56", "before #56 (stall)",
     "8561178279d7ee8da45971db7e36c8ed6be0084d", {}),
    ("now1", "now, one process", "", {"COUNCIL_LEDGER_WORKERS": "1"}),
    ("now", "now", "", {}),
)

#: Words only the bundled sample data (assets/sample_data) has: one in an
#: answer means the demo purchase orders / inventory reached it.
SAMPLE_MARKERS = ("TradeWorks", "Hilltop Ind", "Acme Supply", "BoltCo",
                  "Northbridge", "Power Strip", "acmesupply.example")

LEDGER_ROWS = (2000, 4000, 12000)
#: The batch: ledger_bench's eleven workbooks, this many copies of each.
BATCH_COPIES = 4


# ============================================================
# Checks (the shapes council_core.council_bench.check takes; copied so this
# file scores the same way whichever build it is pointed at)
# ============================================================

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_FENCE = re.compile(r"```(?:python|py)?[^\n]*\n(.*?)```", re.DOTALL)


def check(answer: str, checks: Sequence[Dict[str, Any]]) -> List[str]:
    """The checks ``answer`` fails ([] = right)."""
    text = answer or ""
    low = text.lower()
    failed = []
    for c in checks:
        if "contains" in c:
            miss = [w for w in c["contains"] if w.lower() not in low]
            if miss:
                failed.append(f"missing {miss}")
        if "any" in c and not any(w.lower() in low for w in c["any"]):
            failed.append(f"none of {c['any']}")
        if "not_contains" in c:
            bad = [w for w in c["not_contains"] if w.lower() in low]
            if bad:
                failed.append(f"contains {bad}")
        if "regex" in c and not re.search(c["regex"], text,
                                          re.IGNORECASE | re.MULTILINE):
            failed.append(f"no match for {c['regex']!r}")
        if "number" in c:
            want = float(c["number"])
            tol = float(c.get("tol", 1e-9))
            nums = []
            for raw in _NUM.findall(text):
                try:
                    nums.append(float(raw.replace(",", "")))
                except ValueError:
                    pass
            if not any(abs(n - want) <= tol for n in nums):
                failed.append(f"no {want:g}")
        if c.get("python_parses"):
            blocks = _FENCE.findall(text)
            if not blocks:
                failed.append("no Python block")
            for b in blocks:
                try:
                    ast.parse(b)
                except SyntaxError as exc:
                    failed.append(f"Python does not parse: {exc.msg}")
                    break
    return failed


def load_questions(path: Path = QUESTIONS) -> List[Dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    qs = raw["questions"] if isinstance(raw, dict) else raw
    seen = set()
    for q in qs:
        if q["id"] in seen:
            raise ValueError(f"question id {q['id']!r} twice in {path}")
        seen.add(q["id"])
    return qs


# ============================================================
# The synthetic vault (every planted answer is in council_ab_questions.json)
# ============================================================

def make_vault(vault: Path, data: Optional[Path] = None,
               from_vault: Optional[Path] = None) -> Path:
    """A fresh vault: synthetic files in data_in/ (plus ``data``'s files), and
    the model settings of ``from_vault`` when given."""
    if vault.exists():
        shutil.rmtree(vault)
    inp = vault / "data_in"
    inp.mkdir(parents=True)
    (inp / "pump_notes.txt").write_text(
        "Maintenance note, 2026-08-14.\n"
        "The P-200 transfer pump must use an EPDM seal from now on; the Viton "
        "seal swelled in the caustic line within two weeks.\n"
        "Contact for the seal change: Rowan Ellis (maintenance planning).\n",
        encoding="utf-8")
    (inp / "alloy_review.md").write_text(
        "# Alloy review\n\nHastelloy C-276 is kept for the chloride service "
        "lines. 316L stays everywhere else.\n", encoding="utf-8")
    (inp / "supplier_email_0912.txt").write_text(
        "From: purchasing\nSubject: tube quote\n\nThe Hastelloy tube quote "
        "came back at 14 weeks lead time.\n", encoding="utf-8")
    (inp / "heat_exchanger_spec.txt").write_text(
        "HX-3301 specification. Shell: carbon steel. Tubes: Hastelloy C-22, "
        "19 mm OD.\n", encoding="utf-8")
    (inp / "shift_handover.txt").write_text(
        "Shift handover 2026-09-02. Furnace 2 back in service. No alloy "
        "issues. Titanium fixtures cleaned.\n", encoding="utf-8")
    lines = ["run_id,date,operator,peak_temp_c,supply_voltage_v,notes"]
    for i in range(30):
        run = 101 + i
        volts = 188 if run == 117 else 228 + (i * 7) % 9
        note = "power dip during soak" if run == 117 else ""
        lines.append(f"R-{run},2026-09-{1 + i % 28:02d},"
                     f"{['JB', 'KM', 'RE'][i % 3]},{1040 + (i * 13) % 37},"
                     f"{volts},{note}")
    (inp / "furnace_runs.csv").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8")
    lines = ["inspection_id,part,inspector,result"]
    fails = {3, 9, 14, 21, 27, 33, 38}
    for i in range(1, 41):
        lines.append(f"INS-{i:03d},PN-{2000 + i},"
                     f"{['JB', 'KM'][i % 2]},{'FAIL' if i in fails else 'PASS'}")
    (inp / "inspections.csv").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    if data is not None and Path(data).is_dir():
        shutil.copytree(data, inp / Path(data).name, dirs_exist_ok=True)
    if from_vault is not None:
        for name in ("backend_settings.json", "personality_backends.json"):
            src = Path(from_vault) / name
            if src.is_file():
                shutil.copy2(src, vault / name)
    return vault


# ============================================================
# One side of the chat test (runs in a child process, in the build's folder)
# ============================================================

def _write(path: Path, rec: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def done_in(path: Path) -> set:
    out = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("id"):
            out.add((r["id"], int(r.get("rep", 0))))
    return out


def chat_side(code: Path, vault: Path, mode: str, out: Path, side: str,
              questions: List[Dict[str, Any]], repeat: int = 1,
              warmup: bool = True) -> int:
    sys.path.insert(0, str(code))
    os.chdir(code)
    os.environ["COUNCIL_VAULT_ROOT"] = str(vault)
    os.environ.pop("COUNCIL_SAMPLE_DATA", None)
    if mode == "single":
        os.environ["COUNCIL_CHAT_MODE"] = "single"
    if not os.environ.get("COUNCIL_BACKEND"):
        # No --model: the main model and engine knobs saved in the vault
        # (copied from yours), put in force as the app's launch does.
        try:
            from council_core import backend_settings, engine_settings
            backend_settings.apply_saved_gguf_path(vault)
            engine_settings.apply_saved(vault)
        except Exception as exc:                          # noqa: BLE001
            print(f"  saved model settings not applied: {exc!r}", flush=True)
    import branding
    from council_core import conversations, council_options
    from council_core.turn_meter import TurnMeter
    from council_qt.tabs.council import CouncilActions

    if mode == "single":
        demo = True
    elif mode == "council":
        demo = False
    else:
        # What build_council passes at this build: SINGLE_MODEL_COUNCIL since
        # PR #51, DEMO_MODE before it.
        demo = bool(getattr(branding, "SINGLE_MODEL_COUNCIL",
                            getattr(branding, "DEMO_MODE", False)))
    first = CouncilActions(vault_dir=vault, demo_mode=demo)
    models, problem = first.models()
    if models is None:
        _write(out, {"fatal": f"the council could not be loaded: {problem}"})
        print("Could not load the council:", problem, flush=True)
        return 2

    def options():
        opts = council_options.CouncilOptions.defaults(demo_mode=demo)
        if mode == "council":
            opts.deliberate = True
        return opts.effective(demo)

    def ask(text: str, sid: str):
        act = CouncilActions(vault_dir=vault, demo_mode=demo)
        act._models = models
        act._own_session_id = sid
        for m in conversations.personalities_of(models):
            if hasattr(m, "session_id"):
                m.session_id = sid
        t0 = time.monotonic()
        seen = {"first": None, "handed": "", "who": []}

        def on_token(who, token):
            if seen["first"] is None and str(token).strip():
                seen["first"] = time.monotonic() - t0

        def on_event(ev):
            kind = getattr(ev, "kind", "")
            text = str(getattr(ev, "text", "") or "")
            who = str(getattr(ev, "who", "") or getattr(ev, "agent", "") or "")
            if text.startswith("Handing this to the council"):
                seen["handed"] = text.split(":", 1)[-1].strip().rstrip(".")
            if kind == "final":
                if seen["first"] is None:
                    seen["first"] = time.monotonic() - t0
                if who and who not in seen["who"]:
                    seen["who"].append(who)

        with TurnMeter() as meter:
            try:
                res = act.send(text, options(), on_event=on_event,
                               on_token=on_token)
                err = "" if res.ok else (res.message or "no answer")
            except Exception as exc:                      # noqa: BLE001
                res, err = None, repr(exc)
        return res, err, seen, meter.result, time.monotonic() - t0

    if warmup and not done_in(out):
        # Loads the models before the clock matters for any question.
        ask("Reply with the single word OK.", f"ab-{side}-warmup")
    done = done_in(out)
    total = len(questions) * repeat
    n = 0
    for rep in range(repeat):
        for q in questions:
            n += 1
            if (q["id"], rep) in done:
                continue
            res, err, seen, meter, wall = ask(q["text"],
                                              f"ab-{side}-{q['id']}-{rep}")
            answer = (getattr(res, "answer", "") or "") if res else ""
            route = (getattr(res, "route", "") or "") if res else ""
            path = ("data" if route == "data" else
                    "one" if route == "direct" else
                    "council" if res is not None and res.ok else "")
            failed = check(answer, q.get("checks", [])) if not err \
                else ["no answer"]
            rec = {
                "id": q["id"], "rep": rep, "kind": q.get("kind", ""),
                "expect": q.get("expect"), "side": side, "mode": mode,
                "correct": not failed, "failed": failed, "error": err,
                "path": path, "route": route, "handed_on": seen["handed"],
                "confidence": int(getattr(res, "confidence", 0) or 0)
                if res else 0,
                "depth": (getattr(res, "depth", "") or "") if res else "",
                "verdict": (getattr(res, "verdict", "") or "") if res else "",
                "calls": meter.calls, "model_s": round(meter.seconds, 1),
                "loads": meter.loads, "load_s": round(meter.load_s, 1),
                "by_role": dict(meter.by_role),
                "first_text_s": round(seen["first"], 1)
                if seen["first"] is not None else None,
                "wall_s": round(wall, 1),
                "sample_leak": [m for m in SAMPLE_MARKERS
                                if m.lower() in answer.lower()],
                "answer": answer,
            }
            _write(out, rec)
            mark = "right" if rec["correct"] else "WRONG"
            print(f"  [{n}/{total}] {side} {q['id']}: {mark}, {path or '?'}"
                  f"{' (handed on: ' + seen['handed'] + ')' if seen['handed'] else ''}"
                  f", {meter.calls} calls, {wall:.0f} s", flush=True)
    return 0


# ============================================================
# Connections and Ledger sides (also child processes)
# ============================================================

def kg_side(code: Path, model: str, out: Path) -> int:
    sys.path.insert(0, str(code))
    os.chdir(code)
    from dataclasses import asdict
    from council_core import kg_bench
    t0 = time.monotonic()
    rep = kg_bench.run_model(model)
    d = asdict(rep)
    d["wall_s"] = round(time.monotonic() - t0, 1)
    out.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    print(f"  P={rep.precision:.2f} R={rep.recall:.2f} F1={rep.f1:.2f}",
          flush=True)
    return 0


def make_ledger_files(folder: Path, rows: int):
    """A small LMM-shaped template and a long eddy-current log of ``rows``
    readings (one per row: date, inspector, part, flaw, stage, response) —
    the shape that stalled on the offline PC (part-number VALUE cells)."""
    import openpyxl
    folder.mkdir(parents=True, exist_ok=True)
    tpl = folder / "template.xlsx"
    if not tpl.exists():
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "Part Attributes"
        for col, label in enumerate(["Part Number", "Frequency (kHz)",
                                     "Power (W)", "Date"], start=1):
            ws.cell(2, col, label)
        ws["A5"] = "NDE Inspection"
        r = 6
        for b in "ABC":
            ws.cell(r, 2, "Flaw #")
            for i in range(3):
                ws.cell(r, 3 + i, f"{i + 1}{b}")
            ws.cell(r, 6, "Inspector Initials")
            ws.cell(r, 7, "Date")
            for k, label in enumerate(["Eddy Current Response Pre-Conceal",
                                       "Eddy Current Response Post-Conceal"]):
                ws.cell(r + 1 + k, 2, label)
            r += 4
        wb.save(tpl)
    log = folder / f"EC_log_{rows}.xlsx"
    if not log.exists():
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Log"
        ws.append(["Date", "Inspector", "Part Number", "Flaw", "Stage",
                   "Response"])
        flaws = [f"{i}{b}" for b in "ABC" for i in range(1, 4)]
        for n in range(rows):
            ws.append(["2026-09-25", ["JB", "KM", "RE"][n % 3],
                       f"PN-{1000 + n // 18}", flaws[n % 9],
                       ["Pre-Conceal", "Post-Conceal"][(n // 9) % 2],
                       round(10 + (n * 37) % 600 / 10, 1)])
        wb.save(log)
    return tpl, log


def ledger_one(code: Path, folder: Path, rows: int) -> int:
    """Read one long log at this build; prints a JSON line."""
    tpl, log = make_ledger_files(folder, rows)
    sys.path.insert(0, str(code))
    os.chdir(code)
    from sample_ledger import read_template
    from source_extract import extract
    schema = read_template(tpl)
    t0 = time.monotonic()
    run = extract(schema, [log], use_images=False)
    secs = time.monotonic() - t0
    found = len(getattr(run, "obs", None) or getattr(run, "values", None)
                or [])
    print(json.dumps({"rows": rows, "seconds": round(secs, 1),
                      "values": found}), flush=True)
    return 0


def make_lmm_template(path: Path) -> Path:
    """The stand-in LMM template (the real one stays on your PC): the
    sections and field names ledger_bench writes for."""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "LMM"
    big = Font(size=16, bold=True)

    def head(cell, text):
        ws[cell] = text
        ws[cell].font = big

    def grid(top, rows, extras=()):
        r = top
        for b in "ABC":
            ws.cell(r, 2, "Flaw #")
            for i in range(10):
                ws.cell(r, 3 + i, f"{i + 1}{b}")
            for j, e in enumerate(extras):
                ws.cell(r, 13 + j, e)
            for k, label in enumerate(rows):
                ws.cell(r + 1 + k, 2, label.format(b=b))
            r += len(rows) + 2
        return r

    head("A1", "Part Attributes")
    for col, label in enumerate(
            ["Part Number", "Frequency (kHz)", "%pec", "Power (W)",
             "Scan Speed (mm/s)", "Jump Speed (mm/s)", "Beam Expander",
             "Spot Size (mm)", "Date", "Roughness"], start=1):
        ws.cell(2, col, label)
    head("A5", "Flaw Attributes")
    head("R5", "Tables for Presentation")
    end = grid(6, ["Laser Team FOD {b} (um)", "Flaw Orientation (deg)",
                   "Flaw Length (in)"])
    for rr in range(6, end):
        ws.cell(rr, 19, f"=C{rr}")
        ws.cell(rr, 20, f"=AVERAGE(C{rr}:L{rr})")
    head(f"A{end + 1}", "NDE Inspection")
    end = grid(end + 2, ["Eddy Current Response Pre-Conceal",
                         "Eddy Current Response Mid-Conceal",
                         "Eddy Current Response Post-Conceal", "FPI"],
               extras=["Inspector Initials", "Date"])
    head(f"A{end + 1}", "Concealment")
    for col, label in enumerate(["Media", "Media Run Time", "Media Size",
                                 "Tub Status", "Amplitude", "Amount", "Notes"],
                                start=2):
        ws.cell(end + 2, col, label)
    vis = end + 10
    head(f"A{vis}", "Visual Inspection")
    grid(vis + 1, ["Inspector 1", "Inspector 2", "Inspector 3"],
         extras=["Inspector Initials"])
    wb.save(path)
    return path


def make_batch(folder: Path, copies: int = BATCH_COPIES) -> Path:
    """ledger_bench's eleven workbooks (this checkout's generator, so every
    build reads the same files), ``copies`` of each under their own names."""
    src = folder / "batch"
    if (src / "done").is_file():
        return src
    if src.exists():
        shutil.rmtree(src)
    gen = folder / "batch-gen"
    if gen.exists():
        shutil.rmtree(gen)
    tpl = make_lmm_template(folder / "LMM_standin_template.xlsx")
    subprocess.run([sys.executable, str(REPO / "ledger_bench.py"), "make",
                    str(tpl), str(gen)], cwd=REPO, check=True,
                   capture_output=True, text=True)
    src.mkdir(parents=True)
    for k in range(copies):
        for f in sorted(gen.glob("*.xlsx")):
            shutil.copy2(f, src / f"c{k + 1}_{f.name}")
    (src / "done").write_text("", encoding="utf-8")
    return src


def ledger_batch(code: Path, folder: Path) -> int:
    """The Ledger tab's Extract over the batch at this build; prints a JSON
    line: seconds and what it found."""
    src = folder / "batch"
    tpl = folder / "LMM_standin_template.xlsx"
    work = folder / f"run-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(code))
    os.chdir(code)
    from council_core import ledger_ingest as li
    csv_path = work / "LMM_parent.csv"
    li.create_parent(str(tpl), csv_path)
    t0 = time.monotonic()
    es = li.run_extract(csv_path, [src], review_path=work / "review.xlsx",
                        use_images=False)
    secs = time.monotonic() - t0
    rec = {"batch": len([p for p in src.iterdir() if p.suffix == ".xlsx"]),
           "seconds": round(secs, 1)}
    for k in ("files", "values", "pre_accepted", "to_review"):
        v = getattr(es, k, None)
        rec[k] = len(v) if isinstance(v, (list, tuple, set, dict)) else v
    print(json.dumps(rec, default=str), flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


def _timed_child(args: List[str], limit_s: int, env: Dict[str, str],
                 key: str, value: Any) -> Dict[str, Any]:
    cmd = [sys.executable, str(Path(__file__).resolve())] + args
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=limit_s, env=env)
        line = [x for x in p.stdout.splitlines() if x.startswith("{")]
        rec = json.loads(line[-1]) if line else {
            key: value, "error": (p.stderr or "no output")[-600:]}
    except subprocess.TimeoutExpired:
        rec = {key: value, "timed_out": True, "seconds": limit_s}
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    return rec


def _said(rec: Dict[str, Any], limit_s: int) -> str:
    if rec.get("timed_out"):
        return f"over {limit_s} s (stopped)"
    return f"{rec['seconds']} s" if "seconds" in rec else "failed"


def ledger_side(code: Path, folder: Path, out: Path, rows: Sequence[int],
                limit_s: int, env: Optional[Dict[str, str]] = None,
                batch: bool = True, batch_limit_s: int = 1800) -> int:
    env = dict(os.environ if env is None else env)
    results: Dict[str, Any] = {"long_log": [], "batch": None}
    for n in rows:
        make_ledger_files(folder, n)
        rec = _timed_child(["_ledger-one", "--code", str(code), "--work",
                            str(folder), "--rows", str(n)], limit_s, env,
                           "rows", n)
        results["long_log"].append(rec)
        print(f"  long log, {n} rows: {_said(rec, limit_s)}", flush=True)
    if batch:
        make_batch(folder)
        rec = _timed_child(["_ledger-batch", "--code", str(code), "--work",
                            str(folder)], batch_limit_s, env, "batch", 0)
        results["batch"] = rec
        print(f"  batch of {rec.get('batch', '?')} workbooks: "
              f"{_said(rec, batch_limit_s)}, {rec.get('values', '?')} values"
              f", {rec.get('pre_accepted', '?')} pre-accepted", flush=True)
    out.write_text(json.dumps(results, indent=1, default=str),
                   encoding="utf-8")
    return 0


# ============================================================
# Builds, sides and the report
# ============================================================

def worktree(ref: str, out: Path) -> Path:
    """This checkout for "after"/"HEAD"/"", else a worktree of ``ref``."""
    if ref in ("", "after", "HEAD", "head"):
        return REPO
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", ref],
                         capture_output=True, text=True, check=True
                         ).stdout.strip()
    path = out / f"code-{sha[:7]}"
    if not (path / "council_qt").is_dir():
        subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                        "--force", str(path), sha], check=True,
                       capture_output=True, text=True)
    return path


def build_name(code: Path) -> str:
    sha = subprocess.run(["git", "-C", str(code), "rev-parse", "--short",
                          "HEAD"], capture_output=True, text=True
                         ).stdout.strip()
    return f"this checkout @ {sha}" if code == REPO else sha or code.name


def remove_worktrees(out: Path) -> None:
    for p in out.glob("code-*"):
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove",
                        "--force", str(p)], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "prune"],
                   capture_output=True, text=True)


def model_env(model: str) -> Dict[str, str]:
    env = dict(os.environ)
    if not model:
        return env
    if model.startswith("ollama:"):
        env["COUNCIL_BACKEND"] = "ollama"
        env["COUNCIL_OLLAMA_MODEL"] = model[len("ollama:"):]
    else:
        env["COUNCIL_BACKEND"] = "gguf"
        env["COUNCIL_GGUF_PATH"] = str(Path(model).expanduser().resolve())
        env.setdefault("COUNCIL_GGUF_SEED", "1234")
    return env


def child(args: List[str], env: Dict[str, str]) -> int:
    cmd = [sys.executable, "-u", str(Path(__file__).resolve())] + args
    return subprocess.call(cmd, env=env)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("id"):
            out.append(r)
    return out


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def summarise(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(recs) or 1
    asked = [r for r in recs if not r.get("error")]
    by_kind: Dict[str, List[int]] = {}
    for r in recs:
        k = by_kind.setdefault(r.get("kind") or "?", [0, 0])
        k[0] += bool(r["correct"])
        k[1] += 1
    paths: Dict[str, int] = {}
    for r in recs:
        paths[r.get("path") or "failed"] = paths.get(r.get("path") or
                                                     "failed", 0) + 1
    expected = [r for r in recs if r.get("expect") in ("one", "council")]
    routed = sum(r.get("path") == r["expect"] for r in expected)
    model_answered = [r for r in recs if r.get("path") in ("one", "council")]
    roles: Dict[str, int] = {}
    for r in recs:
        for role, k in (r.get("by_role") or {}).items():
            roles[role] = roles.get(role, 0) + int(k)
    return {
        "questions": len(recs),
        "right": sum(bool(r["correct"]) for r in recs),
        "accuracy": round(sum(bool(r["correct"]) for r in recs) / n, 3),
        "errors": sum(bool(r.get("error")) for r in recs),
        "by_kind": by_kind, "paths": paths,
        "handed_on": sum(bool(r.get("handed_on")) for r in recs),
        "routing_ok": routed, "routing_n": len(expected),
        "mean_calls": round(sum(r["calls"] for r in recs) / n, 1),
        "total_calls": sum(r["calls"] for r in recs),
        "loads": sum(r.get("loads", 0) for r in recs),
        "median_first_text_s": _median([r.get("first_text_s")
                                        for r in model_answered]),
        "median_wall_s": _median([r["wall_s"] for r in asked]),
        "total_wall_s": round(sum(r["wall_s"] for r in recs), 1),
        "sample_leaks": sum(bool(r.get("sample_leak")) for r in recs),
        "by_role": roles,
    }


ROWS = (("Right answers", lambda s: f"{s['right']}/{s['questions']} "
         f"({s['accuracy']:.0%})", "right", "high"),
        ("Answered from the data / one model / council",
         lambda s: " / ".join(str(s["paths"].get(k, 0))
                              for k in ("data", "one", "council")),
         None, None),
        ("Handed on to the council", lambda s: str(s["handed_on"]),
         None, None),
        ("Took the expected path", lambda s: f"{s['routing_ok']}/"
         f"{s['routing_n']}", "routing_ok", "high"),
        ("Model calls (mean a question)", lambda s: str(s["mean_calls"]),
         "mean_calls", "low"),
        ("Model calls (total)", lambda s: str(s["total_calls"]),
         "total_calls", "low"),
        ("Model calls by role", lambda s: ", ".join(
            f"{k} {v}" for k, v in sorted(s["by_role"].items(),
                                          key=lambda kv: -kv[1])) or "-",
         None, None),
        ("Model loads", lambda s: str(s["loads"]), "loads", "low"),
        ("First text seen (median s)",
         lambda s: str(s["median_first_text_s"]), "median_first_text_s",
         "low"),
        ("Whole answer (median s)", lambda s: str(s["median_wall_s"]),
         "median_wall_s", "low"),
        ("All questions (s)", lambda s: str(s["total_wall_s"]),
         "total_wall_s", "low"),
        ("Answers with demo sample data", lambda s: str(s["sample_leaks"]),
         "sample_leaks", "low"),
        ("Errors", lambda s: str(s["errors"]), "errors", "low"))


def _better(a, b, better):
    if a is None or b is None or a == b or better is None:
        return ""
    return "better" if (b > a) == (better == "high") else "worse"


def report(out: Path, sides: List[str], meta: Dict[str, Any]) -> str:
    """report.md and report.html in ``out``; returns the Markdown."""
    recs = {s: read_jsonl(out / f"chat-{s.replace(':', '-')}.jsonl")
            for s in sides}
    sums = {s: summarise(r) for s, r in recs.items() if r}
    md = [f"# Council before/after — {meta.get('when', '')}", "",
          f"Model: {meta.get('model') or 'your saved setup'}. "
          f"Builds: " + ", ".join(f"{k} = {v}" for k, v in
                                  meta.get("builds", {}).items()), ""]
    h = ["<!doctype html><meta charset='utf-8'><title>Council before/after"
         "</title><style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;"
         "max-width:1200px;color:#1d2330;background:#fff}table{border-collapse:"
         "collapse;margin:8px 0 20px}td,th{border:1px solid #d5d9e0;padding:"
         "4px 8px;text-align:left;vertical-align:top}th{background:#f1f3f6}"
         ".better{color:#0b7a3e}.worse{color:#b3261e}.no{color:#b3261e}"
         ".ok{color:#0b7a3e}details pre{white-space:pre-wrap;max-width:520px;"
         "font-size:12px}</style>",
         f"<h1>Council before/after</h1><p>{html.escape(md[2])}</p>"]
    names = [s for s in sides if s in sums]
    if names:
        md += ["## Chat", "", "| | " + " | ".join(names) + " |",
               "|---|" + "---|" * len(names)]
        h.append("<h2>Chat</h2><table><tr><th></th>" + "".join(
            f"<th>{html.escape(s)}</th>" for s in names) + "</tr>")
        for label, fmt, key, better in ROWS:
            cells = [fmt(sums[s]) for s in names]
            marks = [""] + [_better(sums[names[0]].get(key),
                                    sums[s].get(key), better)
                            for s in names[1:]] if key else [""] * len(names)
            md.append(f"| {label} | " + " | ".join(
                c + (f" ({m})" if m else "") for c, m in zip(cells, marks))
                + " |")
            h.append(f"<tr><td>{html.escape(label)}</td>" + "".join(
                f"<td class='{m}'>{html.escape(c)}</td>"
                for c, m in zip(cells, marks)) + "</tr>")
        h.append("</table>")
        kinds = sorted({k for s in names for k in sums[s]["by_kind"]})
        md += ["", "Right answers by kind:", "",
               "| kind | " + " | ".join(names) + " |",
               "|---|" + "---|" * len(names)]
        h.append("<h3>Right answers by kind</h3><table><tr><th>kind</th>" +
                 "".join(f"<th>{html.escape(s)}</th>" for s in names) +
                 "</tr>")
        for k in kinds:
            cells = [("{}/{}".format(*sums[s]["by_kind"][k])
                      if k in sums[s]["by_kind"] else "-") for s in names]
            md.append(f"| {k} | " + " | ".join(cells) + " |")
            h.append(f"<tr><td>{k}</td>" + "".join(
                f"<td>{c}</td>" for c in cells) + "</tr>")
        h.append("</table>")
        # Per question
        ids = []
        for s in names:
            for r in recs[s]:
                if r["id"] not in ids:
                    ids.append(r["id"])
        md += ["", "## Each question", "",
               "| question | expect | " + " | ".join(names) + " |",
               "|---|---|" + "---|" * len(names)]
        h.append("<h2>Each question</h2><p>Open a cell to read the answer."
                 "</p><table><tr><th>question</th><th>expect</th>" + "".join(
                     f"<th>{html.escape(s)}</th>" for s in names) + "</tr>")
        for qid in ids:
            mcells, hcells, expect = [], [], ""
            for s in names:
                rs = [r for r in recs[s] if r["id"] == qid]
                if not rs:
                    mcells.append("-")
                    hcells.append("<td>-</td>")
                    continue
                expect = rs[0].get("expect") or ""
                right = sum(bool(r["correct"]) for r in rs)
                r = rs[0]
                cell = (f"{'right' if right == len(rs) else 'WRONG' if not right else f'{right}/{len(rs)} right'}"
                        f", {r.get('path') or 'failed'}"
                        f"{' (handed on: ' + r['handed_on'] + ')' if r.get('handed_on') else ''}"
                        f", {r['calls']} calls, {r['wall_s']} s")
                mcells.append(cell)
                why = "; ".join(r.get("failed") or []) or r.get("error") or ""
                hcells.append(
                    f"<td><span class='{'ok' if right == len(rs) else 'no'}'>"
                    f"{html.escape(cell)}</span>"
                    + (f"<br><small>{html.escape(why)}</small>" if why else "")
                    + f"<details><summary>answer</summary><pre>"
                    f"{html.escape((r.get('answer') or r.get('error') or '')[:4000])}"
                    "</pre></details></td>")
            md.append(f"| {qid} | {expect} | " + " | ".join(mcells) + " |")
            h.append(f"<tr><td>{html.escape(qid)}</td><td>{expect}</td>"
                     + "".join(hcells) + "</tr>")
        h.append("</table>")
    # Connections
    kg = {s: out / f"kg-{s}.json" for s in ("before", "after")}
    kg = {s: json.loads(p.read_text(encoding="utf-8"))
          for s, p in kg.items() if p.is_file()}
    if kg:
        md += ["", "## Connections (links found in sentences, kg_bench)", "",
               "| | " + " | ".join(kg) + " |", "|---|" + "---|" * len(kg)]
        h.append("<h2>Connections: links found in sentences</h2><table><tr>"
                 "<th></th>" + "".join(f"<th>{s}</th>" for s in kg) + "</tr>")
        for key, label in (("precision", "Precision"), ("recall", "Recall"),
                           ("f1", "F1"), ("errors", "JSON errors"),
                           ("seconds_per_snippet", "Seconds a sentence")):
            cells = [str(round(v.get(key), 2)) if isinstance(
                v.get(key), (int, float)) else str(v.get(key, "-"))
                for v in kg.values()]
            md.append(f"| {label} | " + " | ".join(cells) + " |")
            h.append(f"<tr><td>{label}</td>" + "".join(
                f"<td>{c}</td>" for c in cells) + "</tr>")
        h.append("</table>")
    # Ledger
    led = {}
    for key, title, _ref, _env in LEDGER_BUILDS:
        p = out / f"ledger-{key}.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):           # a run before the batch
                data = {"long_log": data, "batch": None}
            led[title] = data
    if led:
        md += ["", "## Ledger speed", "",
               "| | " + " | ".join(led) + " |", "|---|" + "---|" * len(led)]
        h.append("<h2>Ledger speed</h2><table><tr><th></th>" + "".join(
            f"<th>{html.escape(s)}</th>" for s in led) + "</tr>")

        def cell(r: Optional[Dict[str, Any]], batch: bool) -> str:
            if r is None:
                return "-"
            if r.get("timed_out"):
                return f"over {r['seconds']} s (stopped)"
            if "seconds" not in r:
                return "failed"
            if batch:
                return (f"{r['seconds']} s: {r.get('values', '?')} values, "
                        f"{r.get('pre_accepted', '?')} pre-accepted")
            return f"{r['seconds']} s, {r.get('values', '?')} values"

        rows = sorted({r["rows"] for v in led.values()
                       for r in v.get("long_log") or [] if "rows" in r})
        lines = [(f"Long log, {n} rows",
                  [cell(next((x for x in v.get("long_log") or []
                              if x.get("rows") == n), None), False)
                   for v in led.values()]) for n in rows]
        if any(v.get("batch") for v in led.values()):
            n = next(v["batch"].get("batch") for v in led.values()
                     if v.get("batch"))
            lines.append((f"Extract, {n} workbooks",
                          [cell(v.get("batch"), True) for v in led.values()]))
        for label, cells in lines:
            md.append(f"| {label} | " + " | ".join(cells) + " |")
            h.append(f"<tr><td>{html.escape(label)}</td>" + "".join(
                f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
        h.append("</table>")
    text = "\n".join(md) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    (out / "report.html").write_text("\n".join(h), encoding="utf-8")
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="council_ab", description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    # internal steps, each run in its own process
    c = sub.add_parser("_chat-side")
    c.add_argument("--code", required=True)
    c.add_argument("--vault", required=True)
    c.add_argument("--mode", required=True)
    c.add_argument("--side", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--questions", default=str(QUESTIONS))
    c.add_argument("--only", default="")
    c.add_argument("--repeat", type=int, default=1)
    k = sub.add_parser("_kg-side")
    k.add_argument("--code", required=True)
    k.add_argument("--model", required=True)
    k.add_argument("--out", required=True)
    lb = sub.add_parser("_ledger-batch")
    lb.add_argument("--code", required=True)
    lb.add_argument("--work", required=True)
    lo = sub.add_parser("_ledger-one")
    lo.add_argument("--code", required=True)
    lo.add_argument("--work", required=True)
    lo.add_argument("--rows", type=int, required=True)
    r = sub.add_parser("report")
    r.add_argument("out")
    r.add_argument("--sides", default="before:app,after:app")

    for p in (ap,):
        p.add_argument("--model", default="",
                       help="ollama:NAME or a .gguf path (default: your "
                            "saved setup)")
        p.add_argument("--out", default="",
                       help="output folder (default ~/council_ab/<time>)")
        p.add_argument("--sides", default="before:app,after:app")
        p.add_argument("--before", default=BEFORE["chat"],
                       help="the chat test's 'before' build")
        p.add_argument("--tests", default="chat,kg,ledger")
        p.add_argument("--only", default="",
                       help="question ids, comma separated")
        p.add_argument("--questions", default=str(QUESTIONS))
        p.add_argument("--repeat", type=int, default=1)
        p.add_argument("--data", default="",
                       help="a folder of your own files to add to data_in")
        p.add_argument("--from-vault", default="",
                       help="copy model settings from this vault")
        p.add_argument("--kg-model", default="")
        p.add_argument("--ledger-rows", default=",".join(
            map(str, LEDGER_ROWS)))
        p.add_argument("--ledger-limit", type=int, default=300)
        p.add_argument("--ledger-builds", default=",".join(
            k for k, *_ in LEDGER_BUILDS),
            help="which Ledger builds: " + ", ".join(
                f"{k} ({t})" for k, t, *_ in LEDGER_BUILDS))
        p.add_argument("--no-batch", action="store_true",
                       help="ledger: only the long logs")
        p.add_argument("--keep-worktrees", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "_chat-side":
        qs = load_questions(Path(a.questions))
        if a.only:
            want = set(a.only.split(","))
            qs = [q for q in qs if q["id"] in want]
        return chat_side(Path(a.code), Path(a.vault), a.mode, Path(a.out),
                         a.side, qs, a.repeat)
    if a.cmd == "_kg-side":
        return kg_side(Path(a.code), a.model, Path(a.out))
    if a.cmd == "_ledger-batch":
        return ledger_batch(Path(a.code), Path(a.work))
    if a.cmd == "_ledger-one":
        return ledger_one(Path(a.code), Path(a.work), a.rows)
    if a.cmd == "report":
        print(report(Path(a.out), a.sides.split(","), {}))
        return 0

    out = Path(a.out) if a.out else (
        Path.home() / "council_ab" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    tests = set(a.tests.split(","))
    env = model_env(a.model)
    from_vault = Path(a.from_vault) if a.from_vault else (
        Path(os.environ["COUNCIL_VAULT_ROOT"])
        if os.environ.get("COUNCIL_VAULT_ROOT") else REPO / "vault")
    sides = [s.strip() for s in a.sides.split(",") if s.strip()]
    meta = {"when": time.strftime("%Y-%m-%d %H:%M"), "model": a.model,
            "builds": {}}
    print(f"Output: {out}", flush=True)
    try:
        if "chat" in tests:
            for side in sides:
                build, _, mode = side.partition(":")
                code = worktree(a.before if build == "before" else build, out)
                meta["builds"][f"chat {build}"] = build_name(code)
                tag = side.replace(":", "-")
                vault = out / f"vault-{tag}"
                jsonl = out / f"chat-{tag}.jsonl"
                if not vault.exists() or not jsonl.exists():
                    make_vault(vault, Path(a.data) if a.data else None,
                               None if a.model else from_vault)
                print(f"== chat {side} ({code})", flush=True)
                child(["_chat-side", "--code", str(code), "--vault",
                       str(vault), "--mode", mode or "app", "--side", tag,
                       "--out", str(jsonl), "--questions", a.questions,
                       "--repeat", str(a.repeat)]
                      + (["--only", a.only] if a.only else []), env)
            report(out, [s.replace(":", "-") for s in sides], meta)
        kg_model = a.kg_model or (a.model[len("ollama:"):]
                                  if a.model.startswith("ollama:") else "")
        if "kg" in tests:
            if not kg_model:
                print("== kg skipped: it needs an Ollama model "
                      "(--kg-model NAME)", flush=True)
            else:
                for side, ref in (("before", BEFORE["kg"]), ("after", "")):
                    code = worktree(ref, out)
                    meta["builds"][f"kg {side}"] = build_name(code)
                    target = out / f"kg-{side}.json"
                    if target.exists():
                        continue
                    print(f"== kg {side} ({code})", flush=True)
                    child(["_kg-side", "--code", str(code), "--model",
                           kg_model, "--out", str(target)], env)
        if "ledger" in tests:
            rows = [int(x) for x in a.ledger_rows.split(",") if x]
            want = set(a.ledger_builds.split(","))
            for key, title, ref, extra in LEDGER_BUILDS:
                if key not in want:
                    continue
                code = worktree(ref, out)
                meta["builds"][f"ledger {title}"] = build_name(code)
                target = out / f"ledger-{key}.json"
                if target.exists():
                    continue
                print(f"== ledger {title} ({code})", flush=True)
                # One process changes nothing for a single file: the long
                # logs are timed once for this checkout.
                ledger_side(code, out / "ledger-files", target,
                            [] if key == "now1" else rows, a.ledger_limit,
                            env=dict(env, **extra), batch=not a.no_batch)
    finally:
        if not a.keep_worktrees:
            remove_worktrees(out)
    text = report(out, [s.replace(":", "-") for s in sides], meta)
    print(text)
    print(f"Report: {out / 'report.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
