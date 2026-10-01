"""
council_core.llm_bench — can a LOCAL model build the GUI, and the code behind it?

    python -m council_core.llm_bench --suite all --backend gguf \
        --gguf C:/models/phi-3.5-mini.gguf --out bench.json
    python -m council_core.llm_bench --suite code --backend ollama \
        --ollama-model phi3.5:latest --only K1,K4

Two fixed suites, graded by machine, run through the Council's REAL paths:

GUI SUITE (tests/data/llm_bench/gui_cases.json)
    Twelve plain-English app descriptions (4 simple, 4 medium, 4 complex).
    Each goes through council_core.designer_project.describe — the "Describe
    it" button: gui_describe's prompt, checks, deterministic repairs and up to
    three model rounds — with the model call made exactly as
    designer_project.describe_model_call makes it (role "coder", temperature
    0.1, 1800 tokens). The wireframe it returns is then graded HERE,
    independently: run_describe_prompts.structural_problems (palette kinds,
    inside the canvas, no sibling overlap, gui_spec.validate) and the case's
    own acceptance criteria (required kinds, counts, labels, props, ports).
    Optionally it is saved and Generated in a scratch vault (the Generate
    button's own code, policy gate included) and the generated Qt app is
    constructed offscreen in a subprocess.

CODE-BEHIND SUITE (tests/data/llm_bench/code_cases.json)
    Eight small apps — ports with kinds and types, one button, what the button
    must do — each with a HIDDEN test that runs the produced handler against
    stand-in ports (council_core.bench_ports) in a fresh interpreter. The
    Designer has no path today for a model to write a handler body: it writes
    TODO stubs or script-link calls (gui_emit.handler_stub) and gui_describe
    refuses every code-bearing key. So the BASELINE strategy is the simplest
    honest one: ask council_engine.local_chat once with BASELINE_CODE_PROMPT.
    Every strategy is graded by the same gates, in order, first failure wins:

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

WHAT IS RECORDED PER CASE
    pass/fail and why (category + detail), model calls, repair rounds, prompt
    and output tokens, wall seconds, and every model reply. Output: one JSON
    report plus a short table.

REPLAY
    --replay report.json re-grades a recorded run with the CURRENT code and
    no model, feeding each case its recorded replies. A change to parsing,
    deterministic repair or the gates is measured on real model output in
    seconds; a change to the prompt is not (it would draw other replies).

MODELS ARE OPT-IN
    Nothing here loads a model on import, and the unit tests drive every path
    with ScriptedBackend. A real model runs only from this CLI.

    --backend gguf    council_engine.local_chat — the app's own path
                      (COUNCIL_GGUF_PATH, or --gguf, which sets it)
    --backend ollama  a localhost Ollama /api/chat call with the engine's own
                      Ollama options. FOR COMPARISON ONLY: the app's
                      local_chat is GGUF-only (council_engine._council_backend
                      returns "gguf").

The scratch vault is a fresh temp folder unless --vault is given, and
COUNCIL_VAULT_ROOT is pointed at it before the engine is imported, so a run
never touches the user's vault.
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

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "tests" / "data" / "llm_bench"
GUI_CASES = DATA_DIR / "gui_cases.json"
CODE_CASES = DATA_DIR / "code_cases.json"
PORTS_MODULE = Path(__file__).resolve().parent / "bench_ports.py"

TIERS = ("simple", "medium", "complex")

#: The code-behind call. Same temperature and role as Describe it; a handler
#: is short, so 1200 tokens is generous and still leaves a 4k context room.
CODE_TEMPERATURE, CODE_NUM_PREDICT, CODE_ROLE = 0.1, 1200, "coder"
TEST_TIMEOUT_S = 20
#: A scratch vault deeper than this cannot hold a generated project on
#: Windows without hitting MAX_PATH (see main()).
MAX_VAULT_CHARS = 110
#: Each reply is kept in the report up to this length (an 1800-token reply
#: is ~6000 characters).
MAX_REPLY_CHARS = 12000

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
# Model backends
# ============================================================

@dataclass
class CallStat:
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    hit_limit: bool = False
    error: str = ""
    #: The reply itself, so a run can be re-graded offline after a pipeline
    #: change (feed a case's replies back through ScriptedBackend).
    reply: str = ""


class Backend:
    """A chat call that records what it cost. Subclasses implement _chat."""

    name = "backend"

    def __init__(self) -> None:
        self.calls: List[CallStat] = []

    def chat(self, messages: List[Dict[str, str]], *, temperature: float,
             num_predict: int, role: Optional[str] = None) -> str:
        stat = CallStat()
        t0 = time.perf_counter()
        try:
            text, p_tok, o_tok, hit = self._chat(
                messages, temperature=temperature, num_predict=num_predict,
                role=role)
        except Exception as exc:
            stat.seconds = round(time.perf_counter() - t0, 3)
            stat.error = repr(exc)[:300]
            self.calls.append(stat)
            raise
        stat.seconds = round(time.perf_counter() - t0, 3)
        stat.prompt_tokens, stat.output_tokens = int(p_tok), int(o_tok)
        stat.hit_limit = bool(hit)
        stat.reply = str(text)[:MAX_REPLY_CHARS]
        self.calls.append(stat)
        return text

    def _chat(self, messages, *, temperature, num_predict, role):
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name}


def _estimate(text: str) -> int:
    return max(1, (len(text or "") + 3) // 4) if text else 0


class ScriptedBackend(Backend):
    """Canned replies, in order — the unit tests' model. A reply that is an
    Exception instance is raised instead of returned."""

    name = "scripted"

    def __init__(self, replies: Sequence[Any]):
        super().__init__()
        self.replies = list(replies)
        self.prompts: List[str] = []

    def _chat(self, messages, *, temperature, num_predict, role):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("scripted backend ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply), _estimate(prompt), _estimate(str(reply)), False


class EngineBackend(Backend):
    """council_engine.local_chat — the call every Council feature makes.

    Token counts come from council_engine.estimate_tokens, which is exact
    (the loaded model's own tokenizer) whenever no other call holds the lock
    — always, in a benchmark run. They count the message text, not the chat
    template around it (a few tokens)."""

    name = "gguf"

    def __init__(self) -> None:
        super().__init__()
        import council_engine                       # loads nothing yet
        self.engine = council_engine

    def _chat(self, messages, *, temperature, num_predict, role):
        text = self.engine.local_chat(messages, temperature=temperature,
                                      num_predict=num_predict, role=role)
        p = sum(self.engine.estimate_tokens(m.get("content") or "")
                for m in messages)
        o = self.engine.estimate_tokens(text)
        return text, p, o, o >= num_predict - 2

    def describe(self) -> Dict[str, Any]:
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
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "options": {"temperature": float(temperature),
                               "num_predict": int(num_predict),
                               "num_ctx": 8192, "num_gpu": 99,
                               "num_keep": 128}}
        req = urllib.request.Request(
            self.host + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = (data.get("message") or {}).get("content", "") or ""
        return (text, data.get("prompt_eval_count", 0),
                data.get("eval_count", 0), data.get("done_reason") == "length")

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name, "model": self.model, "host": self.host,
                "num_ctx": 8192}


def _since(backend: Backend, start: int) -> Dict[str, Any]:
    calls = backend.calls[start:]
    return {"model_calls": len(calls),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "model_seconds": round(sum(c.seconds for c in calls), 2),
            "hit_token_limit": sum(1 for c in calls if c.hit_limit),
            "replies": [c.reply for c in calls]}


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


def run_gui_case(case: Dict[str, Any], backend: Backend, *,
                 vault: Optional[Path] = None, generate: bool = True,
                 runtime_python: Optional[str] = None) -> Dict[str, Any]:
    """One description through Describe it, then graded (and optionally
    Generated and run). Never raises."""
    from council_core import designer_project as dp
    import run_describe_prompts as rdp

    row: Dict[str, Any] = {"id": case["id"], "tier": case["tier"],
                           "name": case["name"], "passed": False}
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
    row["repair_rounds"] = max(0, row["attempts"] - 1)
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
        row.update(category="count_bounds" if counts and len(counts) == len(misses)
                   else "missing_required", detail="; ".join(misses)[:300],
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
                row.update(category="runtime_failed", detail=ran["detail"])
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
                    workdir: Optional[Path] = None) -> Tuple[str, str]:
    """('ok' | 'test_failure' | 'timeout', detail). The handler runs in a
    FRESH interpreter, in a scratch folder, with dialogs and displays off."""
    own = workdir is None
    work = Path(workdir or tempfile.mkdtemp(prefix="llm_bench_case_"))
    try:
        work.mkdir(parents=True, exist_ok=True)
        (work / "handlers.py").write_text(handlers_src, encoding="utf-8")
        shutil.copyfile(PORTS_MODULE, work / "bench_ports.py")
        spec = {"ports": case["ports"], "test": case["test_source"]
                if "test_source" in case else _test_source(case),
                "fake_clock": bool(case.get("fake_clock")),
                "sys_path": [str(APP_ROOT)] if case.get("linked") else []}
        (work / "case.json").write_text(json.dumps(spec), encoding="utf-8")
        env = dict(os.environ)
        env.update({"QT_QPA_PLATFORM": "offscreen", "COUNCIL_NO_DIALOGS": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8"})
        env.pop("PYTHONPATH", None)
        try:
            proc = subprocess.run(
                [python or sys.executable, "bench_ports.py", "case.json"],
                cwd=str(work), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return "timeout", f"the hidden test did not finish in {timeout}s"
        lines = proc.stdout.splitlines()
        if "BENCH_OK" in lines:
            return "ok", ""
        verdict = next((ln for ln in lines if ln.startswith("BENCH_FAIL")), "")
        if not verdict:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [
                f"exit {proc.returncode}"]
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
    chk.category = verdict
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


def run_code_case(case: Dict[str, Any], backend: Backend, *,
                  strategy: str = "baseline", python: Optional[str] = None,
                  timeout: int = TEST_TIMEOUT_S) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": case["id"], "tier": case["tier"],
                           "name": case["name"], "passed": False,
                           "strategy": strategy}
    start = len(backend.calls)
    t0 = time.perf_counter()
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
# Reports
# ============================================================

def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    by_tier = {}
    for t in TIERS:
        rs = [r for r in rows if r.get("tier") == t]
        if rs:
            by_tier[t] = f"{sum(1 for r in rs if r.get('passed'))}/{len(rs)}"
    secs = [r.get("seconds", 0) for r in rows]
    return {
        "cases": n, "passed": passed,
        "pass_rate": round(passed / n, 3) if n else None,
        "by_tier": by_tier,
        "categories": dict(Counter(r.get("category", "?") for r in rows
                                   if not r.get("passed"))),
        "median_seconds": round(statistics.median(secs), 2) if secs else None,
        "total_seconds": round(sum(secs), 1),
        "model_calls": sum(r.get("model_calls", 0) for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
    }


def format_table(report: Dict[str, Any]) -> str:
    out = []
    meta = report.get("meta", {})
    out.append(f"model: {meta.get('model', '?')}  backend: "
               f"{meta.get('backend', '?')}  n_ctx: {meta.get('n_ctx', '?')}")
    for suite in ("gui", "code"):
        if suite not in report:
            continue
        for p, rows in enumerate(report[suite]["passes"], 1):
            out.append(f"\n{suite.upper()} pass {p}")
            out.append(f"{'id':<4} {'tier':<8} {'result':<6} {'category':<17} "
                       f"{'calls':>5} {'p_tok':>6} {'o_tok':>6} {'sec':>7}  "
                       f"detail")
            for r in rows:
                out.append(
                    f"{r['id']:<4} {r['tier']:<8} "
                    f"{'PASS' if r.get('passed') else 'FAIL':<6} "
                    f"{r.get('category', ''):<17} {r.get('model_calls', 0):>5} "
                    f"{r.get('prompt_tokens', 0):>6} "
                    f"{r.get('output_tokens', 0):>6} "
                    f"{r.get('seconds', 0):>7.1f}  "
                    f"{(r.get('detail') or '')[:90]}")
            s = summarise(rows)
            out.append(f"  passed {s['passed']}/{s['cases']} "
                       f"(tiers {s['by_tier']}), failures {s['categories']}, "
                       f"median {s['median_seconds']}s/case, "
                       f"{s['model_calls']} calls")
    return "\n".join(out)


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(APP_ROOT), capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def run(suites: Sequence[str], backend: Backend, *, only: str = "",
        passes: int = 1, vault: Optional[Path] = None, generate: bool = True,
        runtime_python: Optional[str] = None, test_python: Optional[str] = None,
        strategy: str = "baseline",
        progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the suites ``passes`` times. Returns the whole report."""
    say = progress or (lambda _s: None)
    report: Dict[str, Any] = {"meta": {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "commit": _git_head(),
        "python": sys.version.split()[0], "passes": passes,
        "describe": {"temperature": 0.1, "num_predict": 1800, "role": "coder"},
        "code": {"strategy": strategy, "temperature": CODE_TEMPERATURE,
                 "num_predict": CODE_NUM_PREDICT, "role": CODE_ROLE}}}
    if "gui" in suites:
        cases = select(load_gui_cases(), only)
        report["gui"] = {"passes": []}
        for p in range(passes):
            rows = []
            for c in cases:
                r = run_gui_case(c, backend, vault=vault, generate=generate,
                                 runtime_python=runtime_python)
                rows.append(r)
                say(f"gui  p{p + 1} {r['id']:<4} "
                    f"{'PASS' if r['passed'] else 'FAIL'} "
                    f"{r.get('category', ''):<16} {r.get('seconds', 0):>6.1f}s "
                    f"x{r.get('model_calls', 0)} {(r.get('detail') or '')[:80]}")
            report["gui"]["passes"].append(rows)
        report["gui"]["summary"] = [summarise(rs)
                                    for rs in report["gui"]["passes"]]
    if "code" in suites:
        cases = select(load_code_cases(), only)
        report["code"] = {"passes": []}
        for p in range(passes):
            rows = []
            for c in cases:
                r = run_code_case(c, backend, strategy=strategy,
                                  python=test_python)
                rows.append(r)
                say(f"code p{p + 1} {r['id']:<4} "
                    f"{'PASS' if r['passed'] else 'FAIL'} "
                    f"{r.get('category', ''):<16} {r.get('seconds', 0):>6.1f}s "
                    f"x{r.get('model_calls', 0)} {(r.get('detail') or '')[:80]}")
            report["code"]["passes"].append(rows)
        report["code"]["summary"] = [summarise(rs)
                                     for rs in report["code"]["passes"]]
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

    Each case's recorded replies are fed back, in order, through
    ScriptedBackend. This measures a change to what happens AFTER the model
    answers — parsing, deterministic repairs, the gates — on real model
    output, in seconds. It cannot measure a change to what the model is
    ASKED: a new prompt would have drawn different replies. A case that now
    needs more replies than were recorded fails as model_error ("ran out of
    replies"); token counts and seconds in a replay are not model costs."""
    say = progress or (lambda _s: None)
    cases = {"gui": {c["id"]: c for c in load_gui_cases()},
             "code": {c["id"]: c for c in load_code_cases()}}
    meta = dict(old.get("meta") or {})
    meta.update(replayed=time.strftime("%Y-%m-%d %H:%M:%S"),
                replay_commit=_git_head())
    meta.pop("calls", None)
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
                be = ScriptedBackend(row.get("replies") or [])
                if suite == "gui":
                    r = run_gui_case(case, be, vault=vault, generate=generate,
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
    return report


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
                         "--backend ollama)")
    os.environ["COUNCIL_BACKEND"] = "gguf"
    return EngineBackend()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m council_core.llm_bench",
        description="Can a local model build the GUI, and the code behind it?")
    ap.add_argument("--suite", choices=("gui", "code", "all"), default="all")
    ap.add_argument("--backend", choices=("gguf", "ollama"), default="")
    ap.add_argument("--gguf", default="", help="sets COUNCIL_GGUF_PATH")
    ap.add_argument("--ollama-model", default="")
    ap.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--strategy", default="baseline",
                    choices=sorted(STRATEGIES))
    ap.add_argument("--vault", default="",
                    help="scratch vault (default: a new temp folder)")
    ap.add_argument("--no-generate", action="store_true",
                    help="grade the wireframe only; do not Generate it")
    ap.add_argument("--runtime-python", default="",
                    help="a Python with PySide6, to construct each generated "
                         "app offscreen (skipped when empty)")
    ap.add_argument("--test-python", default="",
                    help="the Python that runs hidden code tests "
                         "(default: this one)")
    ap.add_argument("--out", default="", help="write the JSON report here")
    ap.add_argument("--replay", default="",
                    help="re-grade this recorded report with the current "
                         "pipeline and NO model (see replay())")
    args = ap.parse_args(argv)

    vault = Path(args.vault or tempfile.mkdtemp(prefix="llm_bench_vault_"))
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
    if args.replay:
        old = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        report = replay(old, vault=vault, generate=not args.no_generate,
                        runtime_python=args.runtime_python or None,
                        test_python=args.test_python or None,
                        progress=lambda s: print(s, flush=True))
    else:
        backend = _backend_from_args(args)
        suites = ("gui", "code") if args.suite == "all" else (args.suite,)
        report = run(suites, backend, only=args.only, passes=args.passes,
                     vault=vault, generate=not args.no_generate,
                     runtime_python=args.runtime_python or None,
                     test_python=args.test_python or None,
                     strategy=args.strategy,
                     progress=lambda s: print(s, flush=True))
    report["meta"]["vault"] = str(vault)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str),
                                  encoding="utf-8")
    print()
    print(format_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
