"""
council_core.llm_bench — can a LOCAL model build the GUI, the code behind
it, and answer from a package's documentation? And how fast, on THIS PC?

    # one model, every suite, the pipelines the user runs (unattended-safe)
    python -m council_core.llm_bench --model ollama:llama3.1:8b \
        --suites gui,code,docs --pipeline new --out bench.json

    # the same harness, the old pipeline (before/after on one model)
    python -m council_core.llm_bench --model ollama:llama3.1:8b \
        --suites gui,code --pipeline baseline --out before.json

    # the Models tab's "Check this PC" for one model: warm speed, placement
    # and a quick reliability probe (1 GUI, 1 function, 2 docs questions)
    python -m council_core.llm_bench --model ollama:phi3.5 --check

    # 200 back-to-back child processes: the 0xC0000142 regression check
    python -m council_core.llm_bench --soak 200

THE MODEL IS CHOSEN THE WAY THE APP CHOOSES IT
--model ollama:<name> writes a model_slots.json into the scratch vault that
puts every role on that model (bench_engine.use_model) and points
COUNCIL_VAULT_ROOT at the scratch vault before anything reads it. Every
call goes through council_engine.local_chat — the app's own route (on this
PC: the localhost Ollama; the council env has no llama_cpp). A GGUF path
works the same way where llama_cpp is installed. The old --backend gguf /
--backend ollama --ollama-model flags still work for the baseline.

TWO PIPELINES, ONE HARNESS
  new       what the user gets today. GUI: designer_project.describe with NO
            model call of ours, so its real profile is used (tree mode and
            best-of-N for a small model, pixel mode for a large one, the
            json_schema, seeds, the per-request examples, the window). Code:
            the real code-behind writer (bench_codebehind: plan -> run with
            its gates, smoke run, repairs and best-of-N) on a real project,
            then the hidden test. Docs: docs_bench through docs_qa and the
            bundled MCP documentation server (tools/pydocs_mcp_server.py),
            role "docs". Each call is OBSERVED (bench_engine.EngineTap):
            tokens, seconds, reply tok/s and whether a schema constrained it
            come from council_engine.last_call_stats.
  baseline  the pipeline the first measurements used. GUI: Describe with a
            hand-made model call (pixel mode, no schema, one call per round,
            temperature 0.1, 1800 tokens). Code: one call with
            BASELINE_CODE_PROMPT. Kept so a change is measured on the same
            cases with the same graders.

GUI SUITE (tests/data/llm_bench/gui_cases.json)
    Twelve plain-English app descriptions (4 simple, 4 medium, 4 complex).
    The wireframe Describe returns is graded HERE, independently:
    run_describe_prompts.structural_problems (palette kinds, inside the
    canvas, no sibling overlap, gui_spec.validate) and the case's own
    acceptance criteria (required kinds, counts, labels, props, ports). Then
    it is saved and Generated in the scratch vault (the Generate button's
    own code, policy gate included) and the generated Qt app is constructed
    offscreen in a child process that pokes every control.

CODE SUITE (tests/data/llm_bench/code_cases.json)
    Eight small apps — ports with kinds and types, one button, what the button
    must do — each with a HIDDEN test that runs the produced handler against
    stand-in ports (council_core.bench_ports) in a fresh interpreter. The
    baseline is graded by these gates, in order, first failure wins:

        extract   a code block, or a bare def                -> no_code
        parse     ast.parse                                  -> syntax
        handler   the method the button calls is defined     -> missing_handler
        policy    gui_policy.validate on the assembled
                  handlers.py (the gate Generate and Run use) -> forbidden_import
                                                                / policy
        ports     every self.ports.<name> exists, every
                  method exists on that kind, no invented
                  self.<widget>                              -> wrong_port
        test      the hidden test, subprocess, timeout       -> test_failure
                                                                / timeout
    The new pipeline's writer has its own gates; a case it offers nothing
    for fails at the stage its best candidate reached (bench_codebehind).

DOCS SUITE (tests/data/docsbench — an invented package, so only reading the
docs can answer): accuracy, citation correctness, "not covered" on the two
questions the docs do not answer, code tasks against hidden tests, seconds.

WHAT IS RECORDED PER CASE
    pass/fail and why (category + detail), model calls, repair rounds, prompt
    and output tokens, reply tok/s, wall seconds, and every model reply. The
    JSON report is rewritten after every case, so an interrupted run keeps
    what it measured. A table is printed at the end.

A CHILD THE MACHINE COULD NOT START IS NOT A MODEL FAILURE
    Hidden tests, runtime probes, smoke runs and docs code tests all run
    through council_core.child_proc: a Job Object per child (the tree dies
    with a timeout; leaked descendants are counted and killed), a commit cap,
    a wait for commit headroom, no console. The old phi4 run's 0xC0000142
    exits were the PC running out of virtual memory (see child_proc); such a
    result is now category "harness_error": not graded, retried first, and
    counted apart ("INVALID") instead of as the model's failure.

REPLAY
    --replay report.json re-grades a recorded run with the CURRENT code and
    no model, feeding each case its recorded replies. A change to parsing,
    deterministic repair or the gates is measured on real model output in
    seconds; a change to the prompt is not (it would draw other replies).

MODELS ARE OPT-IN
    Nothing here loads a model on import, and the unit tests drive every path
    with scripted replies or tests/fake_ollama. A real model runs only from
    this CLI or the Models tab's "Check this PC" (check_this_pc).

The scratch vault is a fresh SHORT temp folder unless --vault is given
(Windows MAX_PATH: a generated project nests ~120 characters deep), and is
removed at the end unless --keep-vault. It never touches the user's vault.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Stdlib-only at import time: nothing here may load a model or the engine.
from .bench_engine import (MAX_REPLY_CHARS, Backend, CallStat,  # noqa: F401
                           EngineTap, ScriptedBackend)
from .bench_engine import estimate as _estimate  # noqa: F401

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "tests" / "data" / "llm_bench"
GUI_CASES = DATA_DIR / "gui_cases.json"
CODE_CASES = DATA_DIR / "code_cases.json"
PORTS_MODULE = Path(__file__).resolve().parent / "bench_ports.py"

TIERS = ("simple", "medium", "complex")
PIPELINES = ("new", "baseline")
SUITES = ("gui", "code", "docs")

#: The code-behind call. Same temperature and role as Describe it; a handler
#: is short, so 1200 tokens is generous and still leaves a 4k context room.
CODE_TEMPERATURE, CODE_NUM_PREDICT, CODE_ROLE = 0.1, 1200, "coder"
TEST_TIMEOUT_S = 20
#: A scratch vault deeper than this cannot hold a generated project on
#: Windows without hitting MAX_PATH (see main()).
MAX_VAULT_CHARS = 110
#: Committed memory one hidden test may use (MB) — child_proc's cap.
TEST_MEMORY_MB = 2048
#: Categories that mean "not graded": the PC, not the model, failed.
INVALID_CATEGORIES = ("harness_error",)

#: The quick reliability probe ("Check this PC", --check): one simple GUI
#: (the length converter — the GUI side of code case K8), one function-mode
#: code case, two docs questions.
QUICK_GUI, QUICK_CODE, QUICK_DOCS = ("S2",), ("K1",), ("q01", "q04")

# Port kinds the code suite (and its stand-in ports) understands.
PORT_KINDS = ("entry", "label", "spinbox", "scale", "checkbutton", "combobox",
              "progressbar", "file_picker", "text", "listbox", "treeview",
              "log_pane", "status_bar", "image_canvas", "button")


# ============================================================
# Cases
# ============================================================

def _test_source(case: Dict[str, Any]) -> str:
    t = case.get("test", "")
    return "\n".join(t) if isinstance(t, list) else str(t)


def load_gui_cases(path: Any = None) -> List[Dict[str, Any]]:
    return json.loads(Path(path or GUI_CASES).read_text(
        encoding="utf-8"))["cases"]


def load_code_cases(path: Any = None) -> List[Dict[str, Any]]:
    cases = json.loads(Path(path or CODE_CASES).read_text(
        encoding="utf-8"))["cases"]
    for c in cases:
        c["test_source"] = _test_source(c)
    return cases


def select(cases: List[Dict[str, Any]], only: str = "") -> List[Dict[str, Any]]:
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()}
    return [c for c in cases if not wanted or c["id"] in wanted]


# ============================================================
# Model backends (CallStat, Backend, ScriptedBackend, EngineTap: bench_engine)
# ============================================================

class EngineBackend(Backend):
    """council_engine.local_chat — the call every Council feature makes —
    with the BASELINE's own prompt and options (no schema, no seed).

    Token counts and speeds come from council_engine.last_call_stats when
    the engine recorded this call (exact on both backends: Ollama's
    prompt_eval_count / eval_count, llama.cpp's own counts); else from
    council_engine.estimate_tokens, which counts the message text, not the
    chat template around it (a few tokens)."""

    name = "gguf"

    def __init__(self, model: str = "") -> None:
        super().__init__()
        import council_engine                       # loads nothing yet
        self.engine = council_engine
        self.model = model
        if model.lower().startswith("ollama:"):
            self.name = "ollama-engine"

    def _chat(self, messages, *, temperature, num_predict, role):
        before = (self.engine.last_call_stats(role) or {}).get("seq")
        text = self.engine.local_chat(messages, temperature=temperature,
                                      num_predict=num_predict, role=role)
        stats = self.engine.last_call_stats(role) or {}
        if stats and stats.get("seq") != before and \
                stats.get("gen_tokens") is not None:
            self._last_extra = stats
            p = int(stats.get("prompt_tokens") or 0)
            o = int(stats.get("gen_tokens") or 0)
            return text, p, o, bool(stats.get("truncated")) or \
                o >= num_predict - 2
        p = sum(self.engine.estimate_tokens(m.get("content") or "")
                for m in messages)
        o = self.engine.estimate_tokens(text)
        return text, p, o, o >= num_predict - 2

    def describe(self) -> Dict[str, Any]:
        if self.name != "gguf":
            return {"backend": self.name, "model": self.model}
        out: Dict[str, Any] = {"backend": self.name,
                               "model": os.environ.get("COUNCIL_GGUF_PATH", ""),
                               "gpu_layers": os.environ.get(
                                   "COUNCIL_GGUF_GPU_LAYERS", "99 (default)")}
        try:
            st = self.engine.n_ctx_status()
            out["n_ctx"], out["n_ctx_source"] = st.get("n_ctx"), st.get("source")
        except Exception:
            pass
        try:
            import llama_cpp
            out["llama_cpp"] = llama_cpp.__version__
        except Exception:
            pass
        return out


class OllamaBackend(Backend):
    """A localhost Ollama /api/chat call, with council_engine._ollama_chat's
    own options (num_ctx 8192, num_gpu 99, num_keep 128). Comparison only —
    see the module docstring."""

    name = "ollama"

    def __init__(self, model: str, host: str = "http://127.0.0.1:11434",
                 timeout: int = 900):
        super().__init__()
        import council_engine
        council_engine._ensure_localhost(host)       # refuses a remote host
        self.model, self.host, self.timeout = model, host.rstrip("/"), timeout

    def _chat(self, messages, *, temperature, num_predict, role):
        import urllib.request

        from . import local_models
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "options": {"temperature": float(temperature),
                               "num_predict": int(num_predict),
                               "num_ctx": 8192, "num_gpu": 99,
                               "num_keep": 128}}
        req = urllib.request.Request(
            self.host + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        # Never through a proxy: the host passed _ensure_localhost, but with
        # HTTP_PROXY set urlopen handed the whole chat to the proxy.
        with local_models.open_direct(req, self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = (data.get("message") or {}).get("content", "") or ""
        return (text, data.get("prompt_eval_count", 0),
                data.get("eval_count", 0), data.get("done_reason") == "length")

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name, "model": self.model, "host": self.host,
                "num_ctx": 8192}


def _since(backend: Backend, start: int) -> Dict[str, Any]:
    calls = backend.calls[start:]
    out = {"model_calls": len(calls),
           "prompt_tokens": sum(c.prompt_tokens for c in calls),
           "output_tokens": sum(c.output_tokens for c in calls),
           "model_seconds": round(sum(c.seconds for c in calls), 2),
           "hit_token_limit": sum(1 for c in calls if c.hit_limit),
           "replies": [c.reply for c in calls]}
    rates = [c.gen_tok_s for c in calls if c.gen_tok_s]
    if rates:
        out["gen_tok_s"] = round(statistics.median(rates), 1)
    pp = [c.prompt_tok_s for c in calls if c.prompt_tok_s]
    if pp:
        out["prompt_tok_s"] = round(statistics.median(pp), 1)
    known = [c for c in calls if c.constrained is not None]
    if known:
        out["constrained_calls"] = sum(1 for c in known if c.constrained)
    bad = [c for c in calls if c.schema_valid is False]
    if bad:
        out["schema_invalid_calls"] = len(bad)
    models = sorted({c.model for c in calls if c.model})
    if models:
        out["served_by"] = models
    return out


# ============================================================
# GUI suite
# ============================================================

def describe_call(backend: Backend) -> Callable[[str], str]:
    """designer_project.describe_model_call, on ``backend``: the same role,
    temperature and token budget the Describe-it button uses."""
    from council_core import designer_project as dp

    def call(prompt: str) -> str:
        return backend.chat([{"role": "user", "content": prompt}],
                            temperature=dp.DESCRIBE_TEMPERATURE,
                            num_predict=dp.DESCRIBE_NUM_PREDICT,
                            role=dp.DESCRIBE_ROLE)
    return call


def _shape_text(shapes) -> str:
    """Everything a user reads on the wireframe, lower-cased: the grader's
    own _text_of (label and list props) plus a caption written into the
    "text" or "placeholder" prop, where a checkbox's words often go."""
    import run_describe_prompts as rdp
    parts = []
    for s in shapes:
        parts.append(rdp._text_of(s))
        props = getattr(s, "props", {}) or {}
        for key in ("text", "placeholder"):
            if isinstance(props.get(key), str):
                parts.append(props[key].lower())
    return " ".join(parts)


def acceptance_misses(expect: Dict[str, Any], shapes) -> List[str]:
    """Every acceptance criterion the wireframe does not meet.

    run_describe_prompts.expectation_misses covers kinds_all, kinds_any (one
    group), count_at_least, labels_any, notebook_tabs and props_any; this adds
    kinds_any as SEVERAL groups, count_at_most, labels_all (each group of
    alternatives must appear), min/max_shapes and ports_min (port TYPES,
    derived by gui_ports.build_ports exactly as Generate derives them)."""
    import run_describe_prompts as rdp
    base = {k: v for k, v in expect.items() if k != "kinds_any"}
    misses = rdp.expectation_misses(base, shapes)
    kinds = Counter(s.kind for s in shapes)
    groups = expect.get("kinds_any") or []
    if groups and not isinstance(groups[0], list):
        groups = [groups]
    for g in groups:
        if not any(kinds[k] for k in g):
            misses.append(f"none of {g}")
    for kind, n in (expect.get("count_at_most") or {}).items():
        if kinds[kind] > n:
            misses.append(f"{kinds[kind]} {kind}, wanted at most {n}")
    text = _shape_text(shapes)
    for alts in expect.get("labels_all") or []:
        alts = alts if isinstance(alts, list) else [alts]
        if not any(a.lower() in text for a in alts):
            misses.append(f"no label mentioning {' / '.join(alts)}")
    n = len(shapes)
    if n < int(expect.get("min_shapes", 1)):
        misses.append(f"only {n} shape(s), wanted at least "
                      f"{expect.get('min_shapes')}")
    if n > int(expect.get("max_shapes", 60)):
        misses.append(f"{n} shapes, wanted at most {expect.get('max_shapes')}")
    want_ports = expect.get("ports_min") or {}
    if want_ports:
        try:
            import gui_ports
            have = Counter(p.type for p in gui_ports.build_ports(shapes))
        except Exception as exc:                          # noqa: BLE001
            have = Counter()
            misses.append(f"ports could not be derived: {exc!r}")
        for ptype, k in want_ports.items():
            if have[ptype] < k:
                misses.append(f"{have[ptype]} {ptype} port(s), wanted at "
                              f"least {k}")
    return misses


def _count_miss(m: str) -> bool:
    return "wanted at most" in m or m.startswith("only ") or "shapes, wanted" in m


def gui_failure_category(result: Any) -> Tuple[str, str]:
    """(category, first fault) for a describe() that did not return ok.

    The best attempt's raw reply is taken back through gui_describe's own
    layers to learn how far it got, rather than guessing from wording."""
    import gui_describe as gd
    errors = list(getattr(result, "errors", None) or [])
    first = errors[0] if errors else ""
    if first.startswith("the model call failed"):
        return "model_error", first
    if first.startswith("describe failed unexpectedly"):
        return "pipeline_error", first
    raw = getattr(result, "raw", "") or ""
    if not raw:
        return "model_error", first or "no reply"
    chk = gd.check_reply(raw)
    if chk.stage == gd.STAGE_NO_JSON:
        return ("truncated" if gd._looks_cut_off(raw) else "invalid_json",
                (chk.faults or [first])[0])
    if chk.stage == gd.STAGE_SCHEMA:
        return "schema_kind", (chk.faults or [first])[0]
    faults = chk.faults or errors
    if any("overlap" in f for f in faults):
        return "overlap", next(f for f in faults if "overlap" in f)
    return "layout", (faults or [first])[0]


def profile_info(profile: Any) -> Dict[str, Any]:
    """What Describe was told about the model, for the report."""
    if profile is None:
        return {}
    return {k: getattr(profile, k, None) for k in (
        "mode", "n_best", "constrained", "n_ctx", "reply_tokens",
        "max_shapes", "params_b", "model", "reason")}


def run_gui_case(case: Dict[str, Any], backend: Backend, *,
                 vault: Optional[Path] = None, generate: bool = True,
                 runtime_python: Optional[str] = None,
                 pipeline: str = "baseline",
                 should_stop: Optional[Callable[[], bool]] = None
                 ) -> Dict[str, Any]:
    """One description through Describe it, then graded (and optionally
    Generated and run). Never raises.

    pipeline "baseline": Describe with describe_call(backend) — a hand-made
    model call, so the plain profile (pixel mode, no schema, one call per
    round). pipeline "new": Describe with NO model call of ours, so the
    Designer's own: the real profile for the model the coder role uses and
    council_engine.local_chat with json_schema and seeds. ``backend`` is
    then the EngineTap observing those calls."""
    from council_core import designer_project as dp
    import run_describe_prompts as rdp

    row: Dict[str, Any] = {"id": case["id"], "tier": case["tier"],
                           "name": case["name"], "passed": False,
                           "pipeline": pipeline}
    start = len(backend.calls)
    t0 = time.perf_counter()
    pdir, name = None, ""
    try:
        if vault is not None:
            import gui_projects
            name = rdp.fresh_name(f"Bench {case['id']}", vault)
            made = dp.create(name, "standalone", vault, "qt")
            if not made.ok:
                raise RuntimeError(f"create: {made.message}")
            pdir = gui_projects.project_path(name, vault)
        if pipeline == "new":
            # Exactly what describe() computes when it is given no model
            # call — computed here only so the report can say what it was.
            profile = dp.describe_profile(case["text"])
            row["profile"] = profile_info(profile)
            result = dp.describe(case["text"], pdir, profile=profile,
                                 should_stop=should_stop)
        else:
            result = dp.describe(case["text"], pdir,
                                 model_call=describe_call(backend))
    except Exception as exc:                              # noqa: BLE001
        row.update(category="pipeline_error", detail=repr(exc)[:300])
        row.update(_since(backend, start))
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    row["describe_seconds"] = round(time.perf_counter() - t0, 2)
    row.update(_since(backend, start))
    row["attempts"] = int(getattr(result, "attempts", 0) or 0)
    rounds = int(getattr(result, "rounds", 0) or 0)
    row["repair_rounds"] = max(0, (rounds or row["attempts"]) - 1)
    if getattr(result, "mode", ""):
        row["mode"] = result.mode
    row["describe_ok"] = bool(result.ok)
    shapes = list(result.shapes or [])
    row["shapes"] = len(shapes)
    row["kinds"] = dict(Counter(s.kind for s in shapes))
    row["notes"] = list(getattr(result, "notes", []) or [])[:6]
    if not result.ok:
        cat, why = gui_failure_category(result)
        row.update(category=cat, detail=why[:300],
                   faults=list(result.errors or [])[:6])
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    hard = rdp.structural_problems(shapes)
    if hard:
        row.update(category="overlap" if any("overlap" in h for h in hard)
                   else "layout", detail=hard[0][:300], faults=hard[:6])
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    misses = acceptance_misses(case.get("expect") or {}, shapes)
    if misses:
        counts = [m for m in misses if _count_miss(m)]
        category = ("count_bounds" if counts and len(counts) == len(misses)
                    else "missing_required")
        # Describe now SALVAGES a reply cut off mid-object (the complete
        # shapes before the cut). The widgets that are then missing were
        # never written: the cause is the token limit, not the model's
        # understanding — count it where it belongs.
        import gui_describe as gd
        if any(gd._looks_cut_off(c.reply or "")
               for c in backend.calls[start:]):
            category = "truncated"
        row.update(category=category, detail="; ".join(misses)[:300],
                   faults=misses)
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    if generate and pdir is not None:
        # Save and Generate through designer_project — the Generate button's
        # own path, policy gate included.
        gen = rdp.generate_project(name, pdir, shapes, vault)
        row["generate"] = gen
        if not gen["ok"]:
            row.update(category="generate_failed", detail=gen["detail"])
            row["seconds"] = round(time.perf_counter() - t0, 2)
            return row
        if runtime_python:
            ran = rdp.run_generated(pdir, runtime_python)
            row["runtime"] = ran
            if not ran["ok"]:
                # A probe the PC could not START (child_proc: out of
                # virtual memory) says nothing about the app: not graded.
                row.update(category="harness_error" if ran.get("infra")
                           else "runtime_failed", detail=ran["detail"])
                row["seconds"] = round(time.perf_counter() - t0, 2)
                return row
    row.update(passed=True, category="ok", detail="")
    row["seconds"] = round(time.perf_counter() - t0, 2)
    return row


# ============================================================
# Code-behind suite: the baseline prompt
# ============================================================

def _port_doc(p: Dict[str, Any], handler_of: Dict[str, str]) -> str:
    """One port as the model should read it."""
    k, t, n = p["kind"], p.get("type", "str"), p["name"]
    lab = f' "{p["label"]}"' if p.get("label") else ""
    if k == "entry":
        if t in ("int", "float"):
            what = (f".get() -> {t}, or None when the box is blank or not a "
                    f"{'whole number' if t == 'int' else 'number'}")
        else:
            what = ".get() -> str (a path)" if t == "path" else ".get() -> str"
        return f"- {n}: text box{lab}. {what}; .set(value)"
    docs = {
        "label": ".set(text) shows text; .get() -> str",
        "spinbox": ".get() -> int; .set(int)",
        "scale": ".get() -> number; .set(number)",
        "checkbutton": ".get() -> True/False; .set(True/False)",
        "combobox": (".get() -> the chosen text; .set(text). Choices: "
                     + ", ".join(p.get("values") or [])),
        "progressbar": ".set(0..100)",
        "file_picker": ".get() -> the path as str; .set(path)",
        "text": ".get() -> str; .set(str)",
        "listbox": (".items() -> every item (list of str); .get() -> the "
                    "selected items; .set(list) replaces every item"),
        "treeview": (".rows() -> every row (list of tuples of str); "
                     ".set(rows) replaces every row, each row a list of "
                     "values" + (f" (columns: {', '.join(p['columns'])})"
                                 if p.get("columns") else "")),
        "log_pane": ".set(line) appends one line (write-only)",
        "status_bar": ".set(text) (write-only)",
        "image_canvas": ".set(path or PIL image, or None to blank it) "
                        "(write-only)",
        "button": f"pressing it calls self.{handler_of.get(n, 'on_' + n)}()",
    }
    noun = {"label": "label", "listbox": "list", "treeview": "table",
            "file_picker": "file picker", "combobox": "dropdown",
            "checkbutton": "checkbox", "spinbox": "spin box",
            "log_pane": "log", "status_bar": "status bar",
            "image_canvas": "image", "button": "button", "text": "text area",
            "progressbar": "progress bar", "scale": "slider"}.get(k, k)
    return f"- {n}: {noun}{lab}. {docs.get(k, '')}"


#: THE BASELINE, verbatim (29 lines once the ports are filled in for an
#: average case). Deliberately simple: no worked example, no repair round, one
#: call. Whatever replaces it is measured against this.
BASELINE_CODE_PROMPT = """\
You are writing the Python code behind one button of a desktop app made with a GUI designer.
The app's window class inherits HandlerMixin, and you write ONE method of HandlerMixin.
The window's widgets are reached ONLY through typed ports: self.ports.<name>.

PORTS IN THIS APP
{ports}
Every port also has .clear() (empties it) and .enable(True/False).

HELPERS ON self
- self.report_error(title, exc): shows an error message to the user (exc may be an exception or text).

RULES
- Use only the port names and methods listed above. Do not create widgets or windows.
- Imports allowed: the Python standard library{linked}, plus numpy, pandas and PIL.
  Never import subprocess, socket, urllib, pickle, ctypes or importlib; no eval or exec;
  no os.remove, os.unlink or shutil.rmtree.
- Put imports inside the method. Do not ask for input and do not block.

TASK
{task}

Reply with ONLY one ```python code block that contains the complete method:
def {handler}(self, *args) -> None:
    ..."""


def baseline_code_prompt(case: Dict[str, Any]) -> str:
    handler_of = {p["name"]: p.get("handler") or f"on_{p['name']}"
                  for p in case["ports"] if p["kind"] == "button"}
    ports = "\n".join(_port_doc(p, handler_of) for p in case["ports"])
    linked = case.get("linked") or []
    linked_txt = (", the app module" + ("s " if len(linked) > 1 else " ")
                  + ", ".join(linked)) if linked else ""
    return BASELINE_CODE_PROMPT.format(ports=ports, linked=linked_txt,
                                       task=case["task"],
                                       handler=case["handler"])


# ============================================================
# Code-behind suite: the gates
# ============================================================

@dataclass
class CodeCheck:
    """How far one produced handler got. ``category`` is "ok" or the first
    gate that refused it."""
    ok: bool = False
    stage: str = ""
    category: str = ""
    detail: str = ""
    faults: List[str] = field(default_factory=list)
    code: str = ""
    handlers_src: str = ""


_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)(?:```|\Z)",
                    re.DOTALL)


def extract_code(reply: str, handler: str = "") -> str:
    """The code in a reply: the fenced block that defines ``handler`` (else
    the longest fenced block), or the whole reply when it has a def and no
    fence. An unterminated fence — a reply cut off by the token limit — is
    taken to the end, so the parse gate reports it as what it is."""
    reply = reply or ""
    blocks = [m.group(2) for m in _FENCE.finditer(reply)]
    blocks = [b for b in blocks if b.strip()]
    if blocks:
        for b in blocks:
            if handler and re.search(rf"def\s+{re.escape(handler)}\s*\(", b):
                return b.strip("\n")
        return max(blocks, key=len).strip("\n")
    if re.search(r"^\s*def\s+\w+\s*\(", reply, re.MULTILINE):
        start = re.search(r"^\s*(?:import |from |def |class |@)", reply,
                          re.MULTILINE)
        return reply[start.start() if start else 0:].strip("\n")
    return ""


def _segment(src_lines: List[str], node: ast.AST) -> str:
    first = min([node.lineno] + [d.lineno for d in
                                 getattr(node, "decorator_list", [])])
    return textwrap.dedent("\n".join(src_lines[first - 1:node.end_lineno]))


def _is_main_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__")


HANDLERS_HEADER = '''"""handlers.py — written by a model, assembled by council_core.llm_bench."""
from __future__ import annotations
'''


def assemble_handlers(code: str, handler: str
                      ) -> Tuple[Optional[str], str, List[str]]:
    """(handlers.py source, category, faults) from whatever the model wrote.

    Accepts a bare method, a method with imports or helpers around it, or a
    whole ``class HandlerMixin`` (any class name): methods and class
    attributes go into HandlerMixin, everything else at module level. A
    ``if __name__ == "__main__"`` block and demo code are dropped."""
    src = textwrap.dedent(code or "").strip("\n")
    if not src.strip():
        return None, "no_code", ["the reply contained no code"]
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return None, "syntax", [f"line {exc.lineno}: {exc.msg}"]
    lines = src.splitlines()
    module, methods, found = [], [], False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module.append(_segment(lines, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args
            if node.name == handler or (args and args[0].arg == "self"):
                methods.append(_segment(lines, node))
                found |= node.name == handler
            else:
                module.append(_segment(lines, node))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_segment(lines, sub))
                    found |= sub.name == handler
                elif isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    methods.append(_segment(lines, sub))
        elif _is_main_guard(node):
            continue
        elif isinstance(node, ast.Expr):
            continue                       # a docstring or a demo call
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            module.append(_segment(lines, node))
        else:
            continue                       # demo code: App(), loops, prints
    if not found:
        return None, "missing_handler", [
            f"no method {handler}(self, ...) in the reply"]
    body = "\n\n".join(textwrap.indent(m, "    ") for m in methods)
    out = (HANDLERS_HEADER + ("\n" + "\n".join(module) + "\n" if module else "")
           + "\n\nclass HandlerMixin:\n" + body + "\n")
    try:
        ast.parse(out)
    except SyntaxError as exc:
        return None, "syntax", [f"assembled handlers.py line {exc.lineno}: "
                                f"{exc.msg}"]
    return out, "ok", []


def _port_ref(node: ast.AST) -> Optional[str]:
    """'name' for self.ports.name or self.ports["name"], else None."""
    def is_self_ports(n: ast.AST) -> bool:
        return (isinstance(n, ast.Attribute) and n.attr == "ports"
                and isinstance(n.value, ast.Name) and n.value.id == "self")
    if isinstance(node, ast.Attribute) and is_self_ports(node.value):
        return node.attr
    if isinstance(node, ast.Subscript) and is_self_ports(node.value):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value
    return None


def port_faults(handlers_src: str, case: Dict[str, Any]) -> List[str]:
    """Every port the code names that the app does not have, every method it
    calls that the port's kind does not have, and every self.<attribute> it
    reads that nothing defines (a widget reached around the ports)."""
    from council_core.bench_ports import API, UI_HELPERS
    kinds = {p["name"]: p["kind"] for p in case["ports"]}
    tree = ast.parse(handlers_src)
    faults: List[str] = []
    defined = set(UI_HELPERS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"):
                    defined.add(sub.attr)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            defined.add(str(node.args[1].value))
    seen = set()
    for node in ast.walk(tree):
        name = _port_ref(node)
        if name is not None and name not in ("read", "apply"):
            if name not in kinds and name not in seen:
                seen.add(name)
                faults.append(f"self.ports.{name}: this app has no port "
                              f"{name!r} (ports: {', '.join(kinds)})")
            continue
        if isinstance(node, ast.Attribute):
            owner = _port_ref(node.value)
            if owner in kinds and node.attr not in API.get(kinds[owner], ()):
                key = (owner, node.attr)
                if key not in seen:
                    seen.add(key)
                    allowed = [a for a in API.get(kinds[owner], ())
                               if a not in ("name", "type", "direction",
                                            "widget")]
                    faults.append(
                        f"self.ports.{owner}.{node.attr}: a {kinds[owner]} "
                        f"port has no .{node.attr} (it has "
                        f"{', '.join('.' + a + '()' for a in allowed)})")
            elif (isinstance(node.value, ast.Name) and node.value.id == "self"
                  and node.attr not in defined and node.attr not in seen):
                seen.add(node.attr)
                faults.append(f"self.{node.attr}: nothing defines it — reach "
                              f"widgets through self.ports.<name>")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear_ports"):
            for a in node.args:
                if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                        and a.value not in kinds):
                    faults.append(f"clear_ports({a.value!r}): no such port")
    return faults


def static_check(code: str, case: Dict[str, Any]) -> CodeCheck:
    """Every gate except the hidden test. Cheap; no subprocess."""
    import gui_policy
    chk = CodeCheck(code=code or "")
    src, cat, faults = assemble_handlers(code, case["handler"])
    if src is None:
        stage = {"no_code": "extract", "syntax": "parse"}.get(cat, "handler")
        chk.stage, chk.category, chk.faults = stage, cat, faults
        chk.detail = faults[0] if faults else cat
        return chk
    chk.handlers_src = src
    mode = "linked" if case.get("linked") else "standalone"
    ok, errs = gui_policy.validate(src, mode, (), "qt")
    if not ok:
        # An import fault is one gui_policy reports on an import statement's
        # line; its wording varies ("not on the allowlist", "an app module,
        # so this project is not standalone", "part of the Council").
        import_lines = {n.lineno for n in ast.walk(ast.parse(src))
                        if isinstance(n, (ast.Import, ast.ImportFrom))}

        def on_import(e: str) -> bool:
            m = re.match(r"line (\d+):", e)
            return bool(m) and int(m.group(1)) in import_lines
        imp = [e for e in errs if on_import(e)]
        chk.stage = "policy"
        chk.category = "forbidden_import" if imp else "policy"
        chk.faults = errs
        chk.detail = (imp or errs)[0]
        return chk
    pf = port_faults(src, case)
    if pf:
        chk.stage, chk.category, chk.faults = "ports", "wrong_port", pf
        chk.detail = pf[0]
        return chk
    chk.stage, chk.category, chk.ok = "static", "ok", True
    return chk


def run_hidden_test(handlers_src: str, case: Dict[str, Any], *,
                    python: Optional[str] = None,
                    timeout: int = TEST_TIMEOUT_S,
                    workdir: Optional[Path] = None,
                    extra_files: Optional[Dict[str, str]] = None
                    ) -> Tuple[str, str]:
    """('ok' | 'test_failure' | 'timeout' | 'infra', detail). The handler
    runs in a FRESH interpreter, in a scratch folder, with dialogs and
    displays off, through council_core.child_proc: in a Job Object with a
    commit cap, after a wait for commit headroom. 'infra' means the PC could
    not start it (0xC0000142 and friends) even after retrying — the handler
    was NOT tested, and must not be graded. ``extra_files`` are written
    beside handlers.py (the code-behind writer's logic.py)."""
    from council_core import child_proc
    own = workdir is None
    work = Path(workdir or tempfile.mkdtemp(prefix="llm_bench_case_"))
    try:
        work.mkdir(parents=True, exist_ok=True)
        (work / "handlers.py").write_text(handlers_src, encoding="utf-8")
        for fname, text in (extra_files or {}).items():
            (work / fname).write_text(text, encoding="utf-8")
        shutil.copyfile(PORTS_MODULE, work / "bench_ports.py")
        spec = {"ports": case["ports"], "test": case["test_source"]
                if "test_source" in case else _test_source(case),
                "fake_clock": bool(case.get("fake_clock")),
                "sys_path": [str(APP_ROOT)] if case.get("linked") else []}
        (work / "case.json").write_text(json.dumps(spec), encoding="utf-8")
        env = child_proc.child_env({
            "QT_QPA_PLATFORM": "offscreen", "COUNCIL_NO_DIALOGS": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
        proc = child_proc.run(
            [python or sys.executable, "bench_ports.py", "case.json"],
            cwd=str(work), env=env, timeout=timeout,
            memory_limit_mb=TEST_MEMORY_MB)
        if proc.infra:
            return "infra", proc.infra
        if proc.timed_out:
            return "timeout", f"the hidden test did not finish in {timeout}s"
        lines = proc.stdout.splitlines()
        if "BENCH_OK" in lines:
            return "ok", ""
        verdict = next((ln for ln in lines if ln.startswith("BENCH_FAIL")), "")
        if not verdict:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [
                proc.error or f"exit {proc.returncode}"]
            verdict = "BENCH_FAIL crashed: " + tail[0]
        return "test_failure", verdict[len("BENCH_FAIL "):][:400]
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def check_code(reply: str, case: Dict[str, Any], *,
               python: Optional[str] = None,
               timeout: int = TEST_TIMEOUT_S) -> CodeCheck:
    """All gates, in order, on one model reply."""
    code = extract_code(reply, case["handler"])
    if not code.strip():
        return CodeCheck(stage="extract", category="no_code",
                         detail="the reply contained no code block or def",
                         faults=["no code"])
    chk = static_check(code, case)
    if not chk.ok:
        return chk
    verdict, detail = run_hidden_test(chk.handlers_src, case, python=python,
                                      timeout=timeout)
    chk.stage = "test"
    chk.ok = verdict == "ok"
    # The PC could not start the test: NOT the handler's failure.
    chk.category = "harness_error" if verdict == "infra" else verdict
    chk.detail = detail
    chk.faults = [detail] if detail else []
    return chk


# ============================================================
# Code-behind suite: strategies and the run
# ============================================================

def baseline_strategy(case: Dict[str, Any], backend: Backend
                      ) -> Tuple[str, int]:
    """(reply, repair rounds). One call, BASELINE_CODE_PROMPT."""
    reply = backend.chat([{"role": "user",
                           "content": baseline_code_prompt(case)}],
                         temperature=CODE_TEMPERATURE,
                         num_predict=CODE_NUM_PREDICT, role=CODE_ROLE)
    return reply, 0


STRATEGIES: Dict[str, Callable[[Dict[str, Any], Backend], Tuple[str, int]]] = {
    "baseline": baseline_strategy,
}
#: Strategies that run a whole pipeline instead of returning one reply.
#: "codebehind": the Designer's code-behind writer (bench_codebehind).
PIPELINE_STRATEGIES = ("codebehind",)


def run_code_case(case: Dict[str, Any], backend: Backend, *,
                  strategy: str = "baseline", python: Optional[str] = None,
                  timeout: int = TEST_TIMEOUT_S,
                  vault: Optional[Path] = None,
                  should_stop: Optional[Callable[[], bool]] = None,
                  n_best: Optional[int] = None) -> Dict[str, Any]:
    """One code case. "baseline": one reply, every gate here, the hidden
    test. "codebehind": the real code-behind writer on a real project in
    ``vault`` (a temp folder when None), then the hidden test — the model
    calls are the writer's own, observed by ``backend`` (an EngineTap).
    ``n_best`` overrides the writer's first-round samples (replay uses the
    recorded one); None = the writer's own choice from the model's size."""
    row: Dict[str, Any] = {"id": case["id"], "tier": case["tier"],
                           "name": case["name"], "passed": False,
                           "strategy": strategy}
    start = len(backend.calls)
    t0 = time.perf_counter()
    if strategy in PIPELINE_STRATEGIES:
        from council_core import bench_codebehind
        own = vault is None
        where = Path(vault or tempfile.mkdtemp(prefix="lbc_"))
        try:
            row.update(bench_codebehind.run_case(
                case, where, test_python=python, timeout=timeout,
                should_stop=should_stop, n_best=n_best))
        except Exception as exc:                          # noqa: BLE001
            row.update(category="pipeline_error", detail=repr(exc)[:300])
        finally:
            if own:
                shutil.rmtree(where, ignore_errors=True)
        row.update(_since(backend, start))
        row.setdefault("repair_rounds", 0)
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    try:
        reply, repairs = STRATEGIES[strategy](case, backend)
    except Exception as exc:                              # noqa: BLE001
        row.update(category="model_error", detail=repr(exc)[:300],
                   repair_rounds=0)
        row.update(_since(backend, start))
        row["seconds"] = round(time.perf_counter() - t0, 2)
        return row
    row.update(_since(backend, start))
    row["repair_rounds"] = repairs
    chk = check_code(reply, case, python=python, timeout=timeout)
    if not chk.ok and chk.category in ("no_code", "syntax") and \
            backend.calls and backend.calls[-1].hit_limit:
        chk.category, chk.detail = "truncated", ("the reply hit the token "
                                                 "limit: " + chk.detail)
    row.update(passed=chk.ok, category=chk.category, detail=chk.detail[:400],
               stage=chk.stage, faults=chk.faults[:6])
    row["code"] = chk.code[:4000]
    row["seconds"] = round(time.perf_counter() - t0, 2)
    return row


# ============================================================
# Docs suite (docs_bench through the docs role)
# ============================================================

DOCS_ROLE = "docs"


def _docs_category(item: Dict[str, Any]) -> str:
    if item.get("infra"):
        return "harness_error"
    if item.get("passed"):
        return "ok"
    detail = str(item.get("detail") or "")
    kind = item.get("kind")
    if kind == "negative":
        return "invented"
    if kind == "code":
        return "no_code" if not item.get("code") else "code_failure"
    if detail.startswith("not covered"):
        return "said_not_covered"
    if detail.startswith("missing"):
        return "missing_facts"
    if detail.startswith("cited the wrong"):
        return "wrong_citation"
    return "model_error" if detail else "failed"


def run_docs(backend: Backend, *, items: Any = "all",
             should_stop: Optional[Callable[[], bool]] = None,
             progress: Optional[Callable[[str], None]] = None
             ) -> Dict[str, Any]:
    """The docs benchmark (council_core.docs_bench: the invented package,
    served by the bundled MCP server tools/pydocs_mcp_server.py) answered
    through docs_qa by the model of the "docs" role. Returns {rows,
    summary}: one row per item (graded like the other suites, with the
    model calls the EngineTap ``backend`` saw for it) and docs_bench's own
    summary (accuracy, citations right, "not covered" right, code)."""
    from council_core import docs_bench, docs_qa
    say = progress or (lambda _s: None)
    bench = docs_bench.load_bench()
    marks: List[int] = []

    def mark(line: str) -> None:
        marks.append(len(backend.calls))
        say(f"docs {line}")

    model_call = docs_qa.engine_model_call(role=DOCS_ROLE)
    rep = docs_bench.run(model_call, items=items, bench=bench,
                         should_stop=should_stop, progress=mark)
    d = rep.to_dict()
    texts = {q["id"]: (q.get("question") or q.get("task") or "")
             for _kind, q in docs_bench.bench_items(bench)}
    rows = []
    for i, item in enumerate(d["items"]):
        start = marks[i] if i < len(marks) else len(backend.calls)
        end = marks[i + 1] if i + 1 < len(marks) else len(backend.calls)
        cost = _since(backend, start)
        if end < len(backend.calls):
            sub = Backend()
            sub.calls = backend.calls[start:end]
            cost = _since(sub, 0)
        row = {"id": item["id"], "tier": item["kind"],
               "name": texts.get(item["id"], "")[:80],
               "passed": bool(item.get("passed")),
               "category": _docs_category(item),
               "detail": str(item.get("detail") or "")[:400],
               "citation_ok": item.get("citation_ok"),
               "covered": item.get("covered"),
               "seconds": round(float(item.get("seconds") or 0.0), 2),
               "answer": item.get("answer", ""), "code": item.get("code", "")}
        row.update(cost)
        row["model_calls"] = max(row.get("model_calls", 0),
                                 int(item.get("model_calls") or 0))
        rows.append(row)
    summary = dict(d["summary"])
    if rep.error:
        summary["error"] = rep.error
    return {"rows": rows, "summary": summary}


# ============================================================
# Reports
# ============================================================

def _invalid(r: Dict[str, Any]) -> bool:
    return r.get("category") in INVALID_CATEGORIES


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pass rate over the GRADED cases: a case the PC could not run
    (harness_error) is counted under "invalid", never as a failure."""
    n = len(rows)
    graded = [r for r in rows if not _invalid(r)]
    passed = sum(1 for r in graded if r.get("passed"))
    by_tier = {}
    for t in TIERS:
        rs = [r for r in graded if r.get("tier") == t]
        if rs:
            by_tier[t] = f"{sum(1 for r in rs if r.get('passed'))}/{len(rs)}"
    secs = [r.get("seconds", 0) for r in rows]
    rates = [r["gen_tok_s"] for r in rows if r.get("gen_tok_s")]
    return {
        "cases": n, "graded": len(graded), "invalid": n - len(graded),
        "passed": passed,
        "pass_rate": round(passed / len(graded), 3) if graded else None,
        "first_call_passes": sum(1 for r in graded if r.get("passed")
                                 and r.get("model_calls") == 1),
        "by_tier": by_tier,
        "categories": dict(Counter(r.get("category", "?") for r in rows
                                   if not r.get("passed"))),
        "median_seconds": round(statistics.median(secs), 2) if secs else None,
        "total_seconds": round(sum(secs), 1),
        "model_calls": sum(r.get("model_calls", 0) for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "gen_tok_s": round(statistics.median(rates), 1) if rates else None,
    }


def format_table(report: Dict[str, Any]) -> str:
    out = []
    meta = report.get("meta", {})
    model = meta.get("model") or ", ".join(meta.get("served_by") or []) \
        or "?"
    out.append(f"model: {model}  backend: "
               f"{meta.get('backend', '?')}  pipeline: "
               f"{meta.get('pipeline', 'baseline')}  n_ctx: "
               f"{meta.get('n_ctx', '?')}")
    speed = meta.get("speed") or {}
    if speed:
        out.append(f"speed: {speed.get('gen_tok_s')} tok/s reply, "
                   f"{speed.get('prompt_tok_s')} tok/s prompt, cold "
                   f"{speed.get('cold_s')} s, placement "
                   f"{speed.get('placement')} ({speed.get('vram_pct')}% "
                   f"in VRAM)")
    for suite in SUITES:
        if suite not in report:
            continue
        for p, rows in enumerate(report[suite]["passes"], 1):
            out.append(f"\n{suite.upper()} pass {p}")
            out.append(f"{'id':<4} {'tier':<8} {'result':<6} {'category':<17} "
                       f"{'calls':>5} {'p_tok':>6} {'o_tok':>6} {'tok/s':>6} "
                       f"{'sec':>7}  detail")
            for r in rows:
                res = ("N/A" if _invalid(r) else
                       "PASS" if r.get("passed") else "FAIL")
                rate = r.get("gen_tok_s")
                out.append(
                    f"{r['id']:<4} {str(r.get('tier', '')):<8} {res:<6} "
                    f"{r.get('category', ''):<17} {r.get('model_calls', 0):>5} "
                    f"{r.get('prompt_tokens', 0):>6} "
                    f"{r.get('output_tokens', 0):>6} "
                    f"{(f'{rate:.0f}' if rate else '-'):>6} "
                    f"{r.get('seconds', 0):>7.1f}  "
                    f"{(r.get('detail') or '')[:90]}")
            s = summarise(rows)
            line = (f"  passed {s['passed']}/{s['graded']} "
                    f"(tiers {s['by_tier']}), failures {s['categories']}, "
                    f"median {s['median_seconds']}s/case, "
                    f"{s['model_calls']} calls")
            if s["invalid"]:
                line += (f"\n  INVALID: {s['invalid']} case(s) could not be "
                         f"graded — the PC could not start the test "
                         f"process (see child_proc); excluded from the rate")
            out.append(line)
        if suite == "docs":
            for ds in report["docs"].get("summary_docs") or []:
                out.append(f"  docs: accuracy {ds.get('rate')}, questions "
                           f"{ds.get('questions')}, citations right "
                           f"{ds.get('citations_right')}, 'not covered' "
                           f"right {ds.get('not_covered_right')}, code "
                           f"{ds.get('code')}, reordered "
                           f"{ds.get('reordered')}, page [1] cited "
                           f"{ds.get('cites_page_1')}, cited pages right "
                           f"{ds.get('cited_pages_right')}")
    if meta.get("interrupted"):
        out.append(f"\nINTERRUPTED: {meta['interrupted']}")
    return "\n".join(out)


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(APP_ROOT), capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _docs_ids(only: str) -> Optional[List[str]]:
    wanted = [s.strip() for s in (only or "").split(",") if s.strip()]
    if not wanted:
        return None
    return [w for w in wanted if re.fullmatch(r"[qncr]\d\d", w)]


def _line(suite: str, p: int, r: Dict[str, Any]) -> str:
    res = "N/A " if _invalid(r) else ("PASS" if r["passed"] else "FAIL")
    rate = r.get("gen_tok_s")
    return (f"{suite:<4} p{p} {r['id']:<4} {res} "
            f"{r.get('category', ''):<16} {r.get('seconds', 0):>6.1f}s "
            f"x{r.get('model_calls', 0)}"
            + (f" {rate:.0f}tok/s" if rate else "")
            + f" {(r.get('detail') or '')[:80]}")


def run(suites: Sequence[str], backend: Backend, *, only: str = "",
        passes: int = 1, vault: Optional[Path] = None, generate: bool = True,
        runtime_python: Optional[str] = None, test_python: Optional[str] = None,
        strategy: Optional[str] = None, pipeline: str = "baseline",
        docs_items: Any = None,
        progress: Optional[Callable[[str], None]] = None,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        deadline: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run the suites ``passes`` times. Returns the whole report.

    ``pipeline`` "new" needs ``backend`` to be an EngineTap (installed here
    if it is not). ``on_update(report)`` is called after every case — the
    CLI rewrites its JSON there, so an interrupted run keeps what it
    measured. ``deadline`` (time.monotonic()) and ``should_stop`` stop it
    starting new cases."""
    from council_core import child_proc
    say = progress or (lambda _s: None)
    if strategy is None:
        strategy = "codebehind" if pipeline == "new" else "baseline"
    installed = False
    if pipeline == "new" and isinstance(backend, EngineTap) and \
            backend._engine is None:                       # noqa: SLF001
        backend.install()
        installed = True
    mem0 = child_proc.memory_status().get("commit_free_mb")
    report: Dict[str, Any] = {"meta": {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "commit": _git_head(),
        "python": sys.version.split()[0], "passes": passes,
        "pipeline": pipeline,
        "describe": ({"profile": "the Designer's own (see each row)",
                      "role": "coder"} if pipeline == "new" else
                     {"temperature": 0.1, "num_predict": 1800,
                      "role": "coder"}),
        "code": ({"strategy": strategy, "role": "coder"}
                 if strategy in PIPELINE_STRATEGIES else
                 {"strategy": strategy, "temperature": CODE_TEMPERATURE,
                  "num_predict": CODE_NUM_PREDICT, "role": CODE_ROLE}),
        "commit_free_mb": {"start": mem0, "min": mem0}}}
    report["meta"].update(meta or {})

    def stopping() -> Optional[str]:
        if should_stop is not None and should_stop():
            return "stopped"
        if deadline is not None and time.monotonic() > deadline:
            return "time budget used up"
        return None

    def done_one() -> None:
        free = child_proc.memory_status().get("commit_free_mb")
        m = report["meta"]["commit_free_mb"]
        if free is not None and (m["min"] is None or free < m["min"]):
            m["min"] = free
        if on_update is not None:
            try:
                on_update(report)
            except Exception:                             # noqa: BLE001
                pass

    try:
        if "gui" in suites:
            cases = select(load_gui_cases(), only)
            report["gui"] = {"passes": []}
            for p in range(passes):
                rows: List[Dict[str, Any]] = []
                report["gui"]["passes"].append(rows)
                for c in cases:
                    why = stopping()
                    if why:
                        report["meta"]["interrupted"] = why
                        break
                    r = run_gui_case(c, backend, vault=vault,
                                     generate=generate,
                                     runtime_python=runtime_python,
                                     pipeline=pipeline,
                                     should_stop=should_stop)
                    rows.append(r)
                    say(_line("gui", p + 1, r))
                    report["gui"]["summary"] = [
                        summarise(rs) for rs in report["gui"]["passes"]]
                    done_one()
            report["gui"]["summary"] = [summarise(rs)
                                        for rs in report["gui"]["passes"]]
        if "code" in suites:
            cases = select(load_code_cases(), only)
            report["code"] = {"passes": []}
            for p in range(passes):
                rows = []
                report["code"]["passes"].append(rows)
                for c in cases:
                    why = stopping()
                    if why:
                        report["meta"]["interrupted"] = why
                        break
                    r = run_code_case(c, backend, strategy=strategy,
                                      python=test_python, vault=vault,
                                      should_stop=should_stop)
                    rows.append(r)
                    say(_line("code", p + 1, r))
                    report["code"]["summary"] = [
                        summarise(rs) for rs in report["code"]["passes"]]
                    done_one()
            report["code"]["summary"] = [summarise(rs)
                                         for rs in report["code"]["passes"]]
        if "docs" in suites:
            ids = _docs_ids(only)
            items = docs_items if docs_items is not None else (
                ids if ids is not None else "all")
            report["docs"] = {"passes": [], "summary_docs": []}
            if items:
                for p in range(passes):
                    why = stopping()
                    if why:
                        report["meta"]["interrupted"] = why
                        break
                    got = run_docs(backend, items=items,
                                   should_stop=should_stop, progress=say)
                    report["docs"]["passes"].append(got["rows"])
                    report["docs"]["summary_docs"].append(got["summary"])
                    for r in got["rows"]:
                        say(_line("docs", p + 1, r))
                    done_one()
            report["docs"]["summary"] = [summarise(rs)
                                         for rs in report["docs"]["passes"]]
    except KeyboardInterrupt:
        report["meta"]["interrupted"] = "KeyboardInterrupt"
    finally:
        if installed:
            backend.uninstall()
    report["meta"].update(backend.describe())
    # Per-call cost; the replies themselves are already in each case's row.
    report["meta"]["calls"] = [{k: v for k, v in asdict(c).items()
                                if k != "reply"} for c in backend.calls]
    return report


def replay(old: Dict[str, Any], *, vault: Optional[Path] = None,
           generate: bool = True, runtime_python: Optional[str] = None,
           test_python: Optional[str] = None,
           progress: Optional[Callable[[str], None]] = None
           ) -> Dict[str, Any]:
    """Re-grade a RECORDED run with the current pipeline and no model.

    Each case's recorded replies are fed back, in order — through
    ScriptedBackend for the baseline, through a scripted EngineTap for the
    new pipeline (so Describe, the code-behind writer and docs_qa all run
    for real around them). This measures a change to what happens AFTER the
    model answers — parsing, deterministic repairs, the gates — on real
    model output, in seconds. It cannot measure a change to what the model
    is ASKED: a new prompt would have drawn different replies. A case that
    now needs more replies than were recorded fails as model_error ("ran
    out of replies"); token counts and seconds in a replay are not model
    costs. A new-pipeline GUI case is replayed with the profile it was
    recorded with (mode, best-of-N, schema)."""
    from council_core import designer_project as dp
    say = progress or (lambda _s: None)
    cases = {"gui": {c["id"]: c for c in load_gui_cases()},
             "code": {c["id"]: c for c in load_code_cases()}}
    meta = dict(old.get("meta") or {})
    meta.update(replayed=time.strftime("%Y-%m-%d %H:%M:%S"),
                replay_commit=_git_head())
    meta.pop("calls", None)
    pipeline = meta.get("pipeline", "baseline")
    report: Dict[str, Any] = {"meta": meta}
    strategy = (meta.get("code") or {}).get("strategy", "baseline")
    for suite in ("gui", "code"):
        if suite not in old:
            continue
        report[suite] = {"passes": []}
        for p, rows in enumerate(old[suite]["passes"], 1):
            out = []
            for row in rows:
                case = cases[suite].get(row["id"])
                if case is None:
                    continue
                replies = row.get("replies") or []
                if pipeline == "new":
                    be: Backend = EngineTap(list(replies))
                    saved = {k: os.environ.get(k) for k in (
                        dp.ENV_MODE, dp.ENV_BEST_OF, dp.ENV_CONSTRAINED)}
                    prof = row.get("profile") or {}
                    if prof.get("mode"):
                        os.environ[dp.ENV_MODE] = str(prof["mode"])
                    if prof.get("n_best"):
                        os.environ[dp.ENV_BEST_OF] = str(prof["n_best"])
                    if prof.get("constrained") is not None:
                        os.environ[dp.ENV_CONSTRAINED] = \
                            "1" if prof["constrained"] else "0"
                    try:
                        with be:
                            r = (run_gui_case(case, be, vault=vault,
                                              generate=generate,
                                              runtime_python=runtime_python,
                                              pipeline="new")
                                 if suite == "gui" else
                                 run_code_case(case, be, strategy=strategy,
                                               python=test_python,
                                               vault=vault,
                                               n_best=row.get("n_best")))
                    finally:
                        for k, v in saved.items():
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v
                else:
                    be = ScriptedBackend(replies)
                    if suite == "gui":
                        r = run_gui_case(case, be, vault=vault,
                                         generate=generate,
                                         runtime_python=runtime_python)
                    else:
                        r = run_code_case(case, be, strategy=strategy,
                                          python=test_python)
                r["recorded"] = {"passed": row.get("passed"),
                                 "category": row.get("category")}
                out.append(r)
                say(f"{suite:<4} p{p} {r['id']:<4} "
                    f"{'PASS' if r['passed'] else 'FAIL'} "
                    f"{r.get('category', ''):<16} (recorded "
                    f"{'PASS' if row.get('passed') else row.get('category')})")
            report[suite]["passes"].append(out)
        report[suite]["summary"] = [summarise(rs)
                                    for rs in report[suite]["passes"]]
    if "docs" in old:
        # docs_qa asks the model in a fixed order per item, and the MCP
        # server's search is deterministic: the recorded replies, in order,
        # answer the same items again.
        report["docs"] = {"passes": [], "summary_docs": []}
        for p, rows in enumerate(old["docs"].get("passes") or [], 1):
            replies = [x for r in rows for x in (r.get("replies") or [])]
            tap = EngineTap(replies)
            with tap:
                got = run_docs(tap, items=[r["id"] for r in rows])
            recorded = {r["id"]: r for r in rows}
            for r in got["rows"]:
                was = recorded.get(r["id"]) or {}
                r["recorded"] = {"passed": was.get("passed"),
                                 "category": was.get("category")}
                say(_line("docs", p, r))
            report["docs"]["passes"].append(got["rows"])
            report["docs"]["summary_docs"].append(got["summary"])
        report["docs"]["summary"] = [summarise(rs)
                                     for rs in report["docs"]["passes"]]
    return report


# ============================================================
# Soak: many child processes back to back (the 0xC0000142 check)
# ============================================================

#: A known-good K1 handler, for the soak's hidden tests.
SOAK_HANDLER = '''
def on_btn_add(self, *args) -> None:
    a = self.ports.first_number.get()
    b = self.ports.second_number.get()
    if a is None or b is None:
        self.ports.result.set("Invalid input")
        return
    self.ports.result.set(f"{a + b:g}")
'''
#: A known-good wireframe for the soak's runtime probes (the S4 to-do app).
SOAK_WIREFRAME = {"window": {"title": "To-do"}, "shapes": [
    {"kind": "entry", "label": "New task", "x": 16, "y": 16, "w": 560,
     "h": 32},
    {"kind": "button", "label": "Add", "x": 592, "y": 16, "w": 120, "h": 32},
    {"kind": "listbox", "label": "Tasks", "x": 16, "y": 64, "w": 696,
     "h": 400},
    {"kind": "button", "label": "Remove", "x": 16, "y": 480, "w": 120,
     "h": 32},
    {"kind": "button", "label": "Clear all", "x": 152, "y": 480, "w": 120,
     "h": 32}]}


def soak(n: int, vault: Path, *, python: Optional[str] = None,
         progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """``n`` child processes back to back, alternating a Qt runtime probe of
    a generated app and a hidden code test — the two kinds that exited
    0xC0000142 late in the phi4 run — through the same child_proc path the
    benchmark uses. Counts results, infra failures, leaked descendants,
    timeouts, and the lowest commit headroom seen."""
    import gui_describe as gd
    import gui_projects
    import run_describe_prompts as rdp
    from council_core import child_proc
    from council_core import designer_project as dp
    say = progress or (lambda _s: None)
    python = python or sys.executable
    name = rdp.fresh_name("Soak", vault)
    made = dp.create(name, "standalone", vault, "qt")
    if not made.ok:
        raise RuntimeError(made.message)
    pdir = gui_projects.project_path(name, vault)
    chk = gd.check_reply(json.dumps(SOAK_WIREFRAME))
    if not chk.ok:
        raise RuntimeError(f"soak wireframe: {chk.faults}")
    gen = rdp.generate_project(name, pdir, chk.shapes, vault)
    if not gen["ok"]:
        raise RuntimeError(f"soak generate: {gen['detail']}")
    case = next(c for c in load_code_cases() if c["id"] == "K1")
    src, _cat, _f = assemble_handlers(SOAK_HANDLER, "on_btn_add")
    procs0 = child_proc.process_count()
    stats: Dict[str, Any] = {"n": n, "ok": 0, "failed": 0, "infra": 0,
                             "timeouts": 0, "leaked": 0, "probe_ok": 0,
                             "test_ok": 0, "failures": [],
                             "commit_free_min_mb": None,
                             "processes_before": procs0}
    t0 = time.perf_counter()
    for i in range(n):
        if i % 2 == 0:
            ran = rdp.run_generated(pdir, python)
            ok, why = ran["ok"], ran.get("infra") or ran["detail"]
            stats["leaked"] += int(ran.get("leaked") or 0)
            if ran.get("infra"):
                stats["infra"] += 1
            if "timed out" in str(ran.get("detail")):
                stats["timeouts"] += 1
            stats["probe_ok"] += int(ok)
            kind = "probe"
        else:
            verdict, detail = run_hidden_test(src, case, python=python)
            ok, why = verdict == "ok", detail
            stats["infra"] += int(verdict == "infra")
            stats["timeouts"] += int(verdict == "timeout")
            stats["test_ok"] += int(ok)
            kind = "test"
        stats["ok" if ok else "failed"] += 1
        if not ok:
            stats["failures"].append(f"{i + 1} {kind}: {why}"[:300])
        free = child_proc.memory_status().get("commit_free_mb")
        if free is not None and (stats["commit_free_min_mb"] is None
                                 or free < stats["commit_free_min_mb"]):
            stats["commit_free_min_mb"] = free
        if (i + 1) % 10 == 0 or not ok:
            say(f"soak {i + 1}/{n}: ok {stats['ok']}, failed "
                f"{stats['failed']}, infra {stats['infra']}, leaked "
                f"{stats['leaked']}, commit free {free} MB")
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    stats["processes_after"] = child_proc.process_count()
    return stats


# ============================================================
# "Check this PC" (the Models tab) — see council_core.pc_check
# ============================================================

def check_this_pc(models: Optional[Sequence[str]] = None,
                  vault_dir: Any = None,
                  on_progress: Optional[Callable[[str], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None
                  ) -> Dict[str, Any]:
    """Measure the installed models on THIS PC — warm speed, where the
    weights sit, and a quick reliability probe per model — and rank them per
    role (US-made only). {ok, message, rows, report_path}; writes
    <vault>/model_bench.json. BLOCKING — the Models tab runs it on a worker
    (model_jobs.check_this_pc). See council_core.pc_check."""
    from council_core import pc_check
    return pc_check.check_this_pc(models=models, vault_dir=vault_dir,
                                  on_progress=on_progress,
                                  should_stop=should_stop)


# ============================================================
# CLI
# ============================================================

def _backend_from_args(args: argparse.Namespace) -> Backend:
    kind = args.backend or ("ollama" if os.environ.get(
        "COUNCIL_BENCH_OLLAMA_MODEL") else "gguf")
    if kind == "ollama":
        model = args.ollama_model or os.environ.get("COUNCIL_BENCH_OLLAMA_MODEL")
        if not model:
            raise SystemExit("--backend ollama needs --ollama-model")
        return OllamaBackend(model, host=args.ollama_host)
    if args.gguf:
        os.environ["COUNCIL_GGUF_PATH"] = str(Path(args.gguf).resolve())
    if not os.environ.get("COUNCIL_GGUF_PATH"):
        raise SystemExit("set COUNCIL_GGUF_PATH or pass --gguf (or use "
                         "--backend ollama, or --model ollama:<name>)")
    os.environ["COUNCIL_BACKEND"] = "gguf"
    return EngineBackend()


def _warm_up(model: str) -> Dict[str, Any]:
    """One short call on the coder role before the suites: it loads the
    model (a cold load is 27-64 s here and would be charged to the first
    case) and lets the engine learn the Ollama window it sends, so the
    first Describe is budgeted like the rest."""
    import council_engine
    t0 = time.perf_counter()
    try:
        council_engine.local_chat(
            [{"role": "user", "content": "Reply with the one word: ready"}],
            role=CODE_ROLE, num_predict=8, temperature=0.0, timeout=600)
    except Exception as exc:                              # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300],
                "seconds": round(time.perf_counter() - t0, 2)}
    s = council_engine.last_call_stats(CODE_ROLE) or {}
    return {"seconds": round(time.perf_counter() - t0, 2),
            "load_s": s.get("load_s"), "model": s.get("model"),
            "backend": s.get("backend"), "num_ctx": s.get("num_ctx")}


def _has_pyside(python: str) -> bool:
    if os.path.normcase(os.path.abspath(python)) == \
            os.path.normcase(os.path.abspath(sys.executable)):
        import importlib.util
        return importlib.util.find_spec("PySide6") is not None
    return True


def _write_json(path: Optional[str], data: Dict[str, Any]) -> None:
    if not path:
        return
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m council_core.llm_bench",
        description="Can a local model build the GUI, the code behind it, "
                    "and answer from documentation — and how fast, here?")
    ap.add_argument("--model", default="",
                    help="ollama:<name> (or a GGUF path): the model EVERY "
                         "role uses, set the app's way (a model_slots.json "
                         "in the scratch vault)")
    ap.add_argument("--suites", default="",
                    help="comma-separated: gui,code,docs (default with "
                         "--model: all three; else from --suite)")
    ap.add_argument("--suite", choices=("gui", "code", "all"), default="all",
                    help="(older flag) all = gui,code")
    ap.add_argument("--pipeline", choices=PIPELINES, default="",
                    help="new (what the user runs; default with --model) or "
                         "baseline (the first measurements' pipeline)")
    ap.add_argument("--backend", choices=("gguf", "ollama"), default="",
                    help="baseline without --model: gguf (in process) or a "
                         "direct Ollama call (comparison only)")
    ap.add_argument("--gguf", default="", help="sets COUNCIL_GGUF_PATH")
    ap.add_argument("--ollama-model", default="")
    ap.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    ap.add_argument("--only", default="",
                    help="comma-separated case ids (S1..C4, K1..K8, q01..)")
    ap.add_argument("--quick", action="store_true",
                    help="the check's subset: S2, K1, q01, q04")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--strategy", default="",
                    choices=[""] + sorted(STRATEGIES) +
                    list(PIPELINE_STRATEGIES),
                    help="code suite (default: codebehind for --pipeline "
                         "new, baseline for baseline)")
    ap.add_argument("--speed", action="store_true",
                    help="measure warm speed and placement first")
    ap.add_argument("--check", action="store_true",
                    help="\"Check this PC\" for --model: speed, placement, "
                         "the quick probe and the time estimates")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--vault", default="",
                    help="scratch vault (default: a new short temp folder)")
    ap.add_argument("--keep-vault", action="store_true",
                    help="do not delete the scratch vault afterwards")
    ap.add_argument("--no-generate", action="store_true",
                    help="grade the wireframe only; do not Generate it")
    ap.add_argument("--runtime-python", default="",
                    help="a Python with PySide6 to construct each generated "
                         "app offscreen (default: this one, if it has "
                         "PySide6)")
    ap.add_argument("--no-runtime", action="store_true",
                    help="skip constructing the generated apps")
    ap.add_argument("--test-python", default="",
                    help="the Python that runs hidden code tests "
                         "(default: this one)")
    ap.add_argument("--max-minutes", type=float, default=0.0,
                    help="start no new case after this long (0: no limit)")
    ap.add_argument("--no-unload", action="store_true",
                    help="leave the Ollama model loaded afterwards")
    ap.add_argument("--out", default="", help="write the JSON report here "
                    "(rewritten after every case)")
    ap.add_argument("--replay", default="",
                    help="re-grade this recorded report with the current "
                         "pipeline and NO model (see replay())")
    ap.add_argument("--soak", type=int, default=0,
                    help="run N child processes back to back (runtime "
                         "probes and hidden tests) and report failures")
    args = ap.parse_args(argv)

    own_vault = not args.vault
    vault = Path(args.vault or tempfile.mkdtemp(prefix="lbv_"))
    vault.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32" and len(str(vault.resolve())) > MAX_VAULT_CHARS:
        # Measured: a vault under a 170-character scratch folder made
        # Generate fail with "The filename or extension is too long" — a
        # generated project nests its backups and ui/ ~120 characters deep.
        raise SystemExit(f"--vault path is {len(str(vault.resolve()))} "
                         f"characters; keep it under {MAX_VAULT_CHARS} "
                         f"(Windows MAX_PATH)")
    # BEFORE the engine is imported: the slot file, the GPU-crash sentinel
    # and the main model are all resolved from here.
    os.environ["COUNCIL_VAULT_ROOT"] = str(vault.resolve())
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(APP_ROOT))
    say = (lambda s: print(s, flush=True))
    runtime_python = None if args.no_runtime else (
        args.runtime_python or sys.executable)
    if runtime_python and not _has_pyside(runtime_python):
        runtime_python = None
    rc = 0
    try:
        if args.soak:
            stats = soak(args.soak, vault, python=args.test_python or None,
                         progress=say)
            _write_json(args.out, {"soak": stats})
            print(json.dumps({k: v for k, v in stats.items()
                              if k != "failures"}, indent=1))
            for f in stats["failures"][:20]:
                print("  " + f)
            return 0 if stats["failed"] == 0 else 1
        if args.replay:
            old = json.loads(Path(args.replay).read_text(encoding="utf-8"))
            report = replay(old, vault=vault, generate=not args.no_generate,
                            runtime_python=runtime_python,
                            test_python=args.test_python or None,
                            progress=say)
            _write_json(args.out, report)
            print()
            print(format_table(report))
            return 0
        if args.check:
            if not args.model:
                raise SystemExit("--check needs --model")
            from council_core import pc_check
            row = pc_check.check_one(args.model, vault, say=say,
                                     runtime_python=runtime_python)
            _write_json(args.out, {"check": row})
            print(json.dumps({k: v for k, v in row.items()
                              if k not in ("report",)}, indent=1,
                             default=str))
            return 0 if not row.get("error") else 1
        pipeline = args.pipeline or ("new" if args.model else "baseline")
        if args.suites:
            suites = tuple(s.strip() for s in args.suites.split(",")
                           if s.strip())
            bad = [s for s in suites if s not in SUITES]
            if bad:
                raise SystemExit(f"unknown suite(s) {bad}; use {SUITES}")
        elif args.model:
            suites = SUITES
        else:
            suites = ("gui", "code") if args.suite == "all" else (args.suite,)
        only = args.only or (",".join(QUICK_GUI + QUICK_CODE + QUICK_DOCS)
                             if args.quick else "")
        meta: Dict[str, Any] = {"vault": str(vault)}
        if args.model:
            from council_core import bench_engine
            bench_engine.use_model(vault, args.model)
            meta["model"] = args.model
            if args.speed:
                meta["speed"] = bench_engine.speed_probe(args.model, say=say)
            elif not args.no_warmup:
                meta["warmup"] = _warm_up(args.model)
                say(f"warm-up: {meta['warmup']}")
        if pipeline == "new":
            backend: Backend = EngineTap()
        elif args.model:
            backend = EngineBackend(model=args.model)
        else:
            backend = _backend_from_args(args)
        deadline = (time.monotonic() + args.max_minutes * 60
                    if args.max_minutes > 0 else None)
        if args.out:
            say(f"report: {args.out} (rewritten after every case)")
        report = run(suites, backend, only=only, passes=args.passes,
                     vault=vault, generate=not args.no_generate,
                     runtime_python=runtime_python,
                     test_python=args.test_python or None,
                     strategy=args.strategy or None, pipeline=pipeline,
                     progress=say,
                     on_update=lambda rep: _write_json(args.out, rep),
                     deadline=deadline, meta=meta)
        _write_json(args.out, report)
        print()
        print(format_table(report))
        if report["meta"].get("interrupted"):
            rc = 2
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        rc = 2
    finally:
        if args.model and not args.no_unload:
            try:
                from council_core import bench_engine, local_models
                if local_models.is_ollama_id(args.model):
                    bench_engine.unload(args.model)
            except Exception:                             # noqa: BLE001
                pass
        if own_vault and not args.keep_vault:
            shutil.rmtree(vault, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
