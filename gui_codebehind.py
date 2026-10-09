"""
gui_codebehind.py — a local model writes the code behind ONE widget, and
nothing it writes is offered to the user until it has passed every gate.

PURE. Stdlib plus the designer's own pure modules (gui_policy, and
council_core.designer_wiring for what a module offers). No toolkit, no
council_engine, no subprocess: the model call and the smoke run are both
INJECTED, so every path below — a good reply, prose around a fence, a cut-off
reply, a hallucinated port, a model that raises — runs against a scripted
stub. A test AST-checks the imports, as tests/test_gui_describe.py does for
gui_describe.

LOGIC-FUNCTION-FIRST
The recommended mode does not let the model touch handlers.py at all. It
writes ONE pure function — `def count_images(folder: str) -> dict` — into the
project's logic.py, with the signature fixed by the widget's input ports and
the result keys fixed by its output ports. The widget then gets an ordinary
script link to logic.<function>, and the next Generate turns its untouched
TODO stub into the deterministic linked stub (gui_emit.handler_stub): port
reads, the error envelope, report_error and clear_ports are all code that was
already tested before any model existed. A small model only has to do the one
thing small models are measurably good at — one function, one signature,
named keys back — and the function can be run on sample data with no window
at all.

The second mode, "handler body", is for UI-only glue (enable this button,
copy that field). It writes a method body, wrapped deterministically in the
same try/report_error envelope, and only over a stub nobody has edited.

THE GATES, CHEAPEST FIRST
Each costs milliseconds except the last, and a candidate stops at the first
gate it fails — so a bad candidate never pays for a 1 s subprocess:

    0 extract     the ```python block; a reply that stops mid-function is
                  named as cut off, not as a syntax error
    1 shape       exactly one def, the right name and parameters. Fixed
                  deterministically where the fix cannot change meaning: a
                  wrong-named single def is renamed, parameters reordered,
                  helpers and top-level imports moved inside, example calls
                  and `if __name__` blocks dropped
    2 policy      gui_policy.validate (the gate Run enforces) plus what code
                  behind a GUI must not do even where an app may: import a
                  toolkit or pyplot, input(), global, chdir, sys.exit, an
                  endless loop, a broad except that hides the failure
    3 references  every linked-module function exists with that arity (a
                  unique close match is fixed, as gui_describe fixes kinds);
                  every returned dict carries the keys the links read;
                  handler mode: every self.ports.X exists and is used the
                  way its widget allows
    4 names       undefined names; a known module (np, Path, Image...) gets
                  its import added, anything else goes back to the model
    5 smoke       the injected sandboxed run (gui_smoke)

THE LOOP
describe()'s contract (gui_describe): NEVER RAISES, a model that raises is a
result, and a worse round never replaces a better one. First round: n_best
samples with different seeds and temperatures, stopping at the first that
passes everything (one that passes only with a caveat — a ValueError on the
sample data chosen for the task — is held while samples remain, and offered
if none does better). Then up to max_repairs rounds from the BEST candidate so
far, each quoting the exact fault text and line, plus a hint keyed on the
fault (the chart recipe for the chart TypeError, the real port names for a
missing port...). A repair that returns the same text again is retried with
a higher temperature and a new seed, because three identical rounds is what
an unvaried retry produced for Describe (the M2 fixture).
"""
from __future__ import annotations

import ast
import builtins
import dataclasses
import difflib
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gui_policy

# ============================================================
# Constants
# ============================================================

MODES = ("function", "handler")
#: The project module model-written functions go into.
MODULE = "logic"
LOGIC_FILE = "logic.py"

#: One function is 10-40 lines; ~12 tokens a line on the tokenizers measured
#: for Describe. 700 leaves room for a docstring without truncating a
#: reasonable function, and is what the map's budget assumed.
NUM_PREDICT = 700
N_CTX = 4096
SLACK_TOKENS = 256
#: Measured for Describe's prompt (gui_describe.CHARS_PER_TOKEN); this prompt
#: is denser in identifiers, so the same conservative figure is kept.
CHARS_PER_TOKEN = 2.9

#: Stops that can only come AFTER the function. Never a bare ``` — it can
#: match the OPENING fence and return nothing (the map's warning).
STOPS = ("\nif __name__", "\n# Example usage")

#: First-round sampling: seed i+1 at these temperatures.
TEMPERATURES = (0.2, 0.45, 0.7)
REPAIR_TEMPERATURE = 0.1
MAX_REPAIRS = 3
SHORTLIST = 6
MAX_FAULTS = 8
MAX_SHOWN_CHARS = 2500

STAGE_EXTRACT, STAGE_SHAPE, STAGE_POLICY, STAGE_REFS, STAGE_NAMES, \
    STAGE_SMOKE, STAGE_OK = range(7)
STAGE_NAMES_TEXT = ("extract", "shape", "policy", "references", "names",
                    "smoke run", "ok")

#: Toolkits code behind a GUI must not import: the app owns the window.
TOOLKIT_ROOTS = frozenset({"tkinter", "_tkinter", "PySide6", "PySide2",
                           "PyQt5", "PyQt6", "wx", "kivy", "qtpy"})

#: Names a candidate may use without importing, and the import that makes
#: them work — added inside the function, deterministically, instead of
#: spending a model round on "NameError: name 'np' is not defined".
AUTO_IMPORTS: Dict[str, str] = {
    "np": "import numpy as np", "numpy": "import numpy",
    "pd": "import pandas as pd", "pandas": "import pandas",
    "Path": "from pathlib import Path",
    "Image": "from PIL import Image", "ImageStat": "from PIL import ImageStat",
    "ImageOps": "from PIL import ImageOps",
    "Figure": "from matplotlib.figure import Figure",
    "Counter": "from collections import Counter",
    "defaultdict": "from collections import defaultdict",
    "OrderedDict": "from collections import OrderedDict",
    "dataclass": "from dataclasses import dataclass",
    "datetime": "import datetime",
}
for _m in ("math", "re", "json", "csv", "os", "glob", "statistics", "time",
           "itertools", "collections", "functools", "random", "string", "io",
           "fnmatch", "textwrap", "struct", "zlib", "hashlib", "decimal",
           "fractions", "bisect", "heapq", "operator", "pathlib"):
    AUTO_IMPORTS[_m] = f"import {_m}"
for _t in ("List", "Dict", "Optional", "Any", "Tuple", "Sequence", "Iterable",
           "Union", "Set"):
    AUTO_IMPORTS[_t] = f"from typing import {_t}"

#: What a script-link input passes, by port type, as a Python annotation.
PARAM_TYPES = {"path": "str", "str": "str", "int": "int", "float": "float",
               "bool": "bool", "rows": "list", "image": "Any",
               "figure": "Any", "event": "Any"}

#: self.<attr> a handler body may use besides self.ports and on_* handlers.
SELF_ATTRS = frozenset({"ports", "report_error", "clear_ports",
                        "request_close"})
#: A handler's OWN state between clicks lives under this prefix — a
#: stopwatch's start time, the list as it was before a filter. Anything else
#: on self is the app's, and a model writing to it (self.ports = ...,
#: self.request_close = ...) would break the window; this prefix cannot
#: collide with anything the emitter generates.
PRIVATE_PREFIX = "_ai_"

_BUILTINS = frozenset(dir(builtins))

#: Interpreter internals code behind a widget never needs: live frames (and
#: the attributes that read one), the garbage collector, and the hooks that
#: watch the interpreter. gui_policy denies __globals__ and globals() for the
#: same reason; these are the routes it does not spell. In the smoke run
#: each reaches the harness's own state — its fence and its verdict live in
#: the calling frame — and in the app they reach the window's.
INTROSPECTION_ATTRS = frozenset({
    "_getframe", "_current_frames", "currentframe", "settrace", "setprofile",
    "addaudithook", "f_locals", "f_globals", "f_back", "f_builtins",
    "f_trace", "tb_frame", "gi_frame", "cr_frame", "ag_frame"})
INTROSPECTION_MODULES = frozenset({"gc", "sys.monitoring"})

#: Modules gui_policy admits as stdlib that code behind a widget has no use
#: for: the OS's process/handle layer (_winapi, nt, posix, msvcrt,
#: _posixsubprocess), the C half of ctypes (gui_policy denies ctypes by
#: name, not _ctypes), and the hooks that run code later or elsewhere —
#: atexit, signal, faulthandler. Each also ran outside the smoke fence's
#: view (an exit handler after it, a signal around it).
LOW_LEVEL_MODULES = frozenset({"_winapi", "nt", "posix", "msvcrt",
                               "_posixsubprocess", "_ctypes", "atexit",
                               "signal", "faulthandler"})
#: Threads and worker processes by any spelling: a ProcessPoolExecutor's
#: worker is a second Python no fence watches, and in the app the window
#: cannot report what a worker raises.
WORKER_MODULES = frozenset({"_thread", "concurrent"})
#: ctypes by another door: gui_policy denies `import ctypes`, but numpy
#: hands it over as np.ctypeslib.ctypes. The smoke fence cannot refuse
#: native calls (libraries load native code lazily), so these names are
#: refused wherever they are spelled.
CTYPES_NAMES = frozenset({"ctypes", "ctypeslib", "windll", "cdll", "oledll",
                          "pydll", "pythonapi", "WinDLL", "CDLL", "OleDLL",
                          "PyDLL", "LoadLibrary"})
WORKER_NAMES = frozenset({"Thread", "ThreadPoolExecutor",
                          "ProcessPoolExecutor", "start_new_thread"})
#: inspect's frame-returning calls, by receiver (np.stack is not one).
INSPECT_FRAMES = frozenset({"stack", "trace", "currentframe",
                            "getouterframes", "getinnerframes", "getframeinfo"})


# ============================================================
# What the model is asked to write
# ============================================================

@dataclass(frozen=True)
class Param:
    """One parameter of a logic function: a port's value, passed in."""
    name: str
    type: str = "str"          # the annotation
    port: str = ""
    kind: str = ""
    what: str = ""             # 'a folder path the user picked in "Folder"'
    sample: Any = "sample"     # what the smoke run passes (gui_smoke tokens)


@dataclass(frozen=True)
class Output:
    """One key of the returned dict, and the port that shows it."""
    key: str
    port: str = ""
    kind: str = ""
    check: str = "any"         # gui_smoke.CHECKS
    what: str = ""             # 'shown as text in "Status"'


@dataclass(frozen=True)
class PortRow:
    """One port as a handler body may use it."""
    name: str
    kind: str
    type: str = "str"
    binder: str = "var"
    writer: str = ""
    label: str = ""
    sample: Any = None

    @property
    def readable(self) -> bool:
        return self.binder not in ("proxy", "event")


@dataclass(frozen=True)
class Ref:
    """One existing function the model may call, shown with its types."""
    module: str
    name: str
    signature: str             # "scan_report(folder: Any) -> Dict[str, Any]"
    summary: str = ""
    keys: Tuple[str, ...] = ()
    params: Tuple[str, ...] = ()
    required: int = 0
    varargs: bool = False
    #: When it returns an instance of a class in its module: that class and
    #: what may be read off it ("total", "summary()").
    returns_class: str = ""
    members: Tuple[str, ...] = ()


@dataclass
class Target:
    """Everything the writer needs to know, gathered by the caller."""
    mode: str = "function"
    #: The function (function mode) or handler (handler mode) to write.
    name: str = ""
    instruction: str = ""
    label: str = ""
    kind: str = "button"
    params: List[Param] = field(default_factory=list)
    outputs: List[Output] = field(default_factory=list)
    #: handler mode: every port of the window.
    ports: List[PortRow] = field(default_factory=list)
    #: handler mode: the other handlers a body may call (self.on_x()).
    handlers: List[str] = field(default_factory=list)
    project_mode: str = "linked"
    toolkit: str = "qt"
    requires: List[str] = field(default_factory=list)
    #: The project's own modules (logic, a helper the user wrote).
    local_modules: List[str] = field(default_factory=list)
    shortlist: List[Ref] = field(default_factory=list)
    #: Documentation snippets: {server, source, title, text}.
    docs: List[Dict[str, str]] = field(default_factory=list)
    #: Names logic.py already binds at its top level (function mode), which
    #: the candidate may use.
    module_names: List[str] = field(default_factory=list)

    @property
    def keys(self) -> List[str]:
        return [o.key for o in self.outputs]

    @property
    def port_names(self) -> List[str]:
        return [p.name for p in self.ports]

    def signature(self) -> str:
        if self.mode == "handler":
            return f"def {self.name}(self, *args) -> None:"
        params = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"def {self.name}({params}) -> dict:"


@dataclass
class Gate:
    """One line of the gate report the review dialog shows."""
    stage: int
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        name = STAGE_NAMES_TEXT[self.stage]
        return f"{name}: {mark}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class Candidate:
    """One reply, taken through every gate it could reach."""
    stage: int = STAGE_EXTRACT
    faults: List[str] = field(default_factory=list)
    code: str = ""             # the normalised function / method
    raw: str = ""
    notes: List[str] = field(default_factory=list)
    gates: List[Gate] = field(default_factory=list)
    smoke: Any = None
    seed: Optional[int] = None
    temperature: float = 0.0
    seconds: float = 0.0       # the model call
    gate_ms: float = 0.0       # the static gates

    @property
    def ok(self) -> bool:
        return self.stage == STAGE_OK and not self.faults

    def rank(self) -> Tuple[int, int]:
        return (self.stage, -len(self.faults))


@dataclass
class CodeResult:
    """What write() hands back. ``code`` is empty unless ``ok`` — a candidate
    that failed a gate is never offered for applying; ``best`` keeps it for
    the log."""
    ok: bool = False
    mode: str = "function"
    name: str = ""
    code: str = ""
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    gates: List[str] = field(default_factory=list)
    attempts: int = 0
    raw: str = ""
    smoke: Any = None
    stopped: bool = False
    seconds: float = 0.0
    prompt_chars: int = 0
    calls: List[Dict[str, Any]] = field(default_factory=list)
    best: Optional[Candidate] = None


# ============================================================
# Prompt
# ============================================================

def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def budget_chars(n_ctx: Optional[int] = None,
                 num_predict: int = NUM_PREDICT) -> int:
    """How many prompt characters fit beside the reply in ``n_ctx``."""
    n = int(n_ctx or N_CTX)
    return int(max(512, n - num_predict - SLACK_TOKENS) * CHARS_PER_TOKEN)


_ROLE_FN = ("You write ONE Python function for an offline desktop app. Reply "
            "with exactly one ```python block containing exactly one def. "
            "No prose.")
_ROLE_HANDLER = ("You write ONE method of an offline desktop app's window — "
                 "the code that runs when a widget is used. Reply with "
                 "exactly one ```python block containing exactly one def. "
                 "No prose.")

_NEVER = ("NEVER: delete, move or rename files; run programs; use the "
          "network; eval/exec; input(); print() to show results; open "
          "windows or import tkinter, PySide6 or matplotlib.pyplot; write "
          "anywhere except inside a folder or file the user chose. Never "
          "wait or loop forever — the window is frozen while this runs.")

EXAMPLE_FUNCTION = '''def count_images(folder: str) -> dict:
    """Count the PNG and JPEG files in a folder."""
    from pathlib import Path
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"{folder} is not a folder")
    names = sorted(p.name for p in root.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    return {"count": len(names), "names": names}'''

EXAMPLE_HANDLER = '''def on_btn_count(self, *args) -> None:
    """Count the PNG files in the chosen folder."""
    from pathlib import Path
    folder = Path(self.ports.folder.get())
    names = sorted(p.name for p in folder.glob("*.png"))
    self.ports.files.set(names)
    self.ports.status.set(f"{len(names)} PNG file(s)")'''

CHART_RECIPE = ("fig = self.ports.{p}.widget.figure_for_drawing(); "
                "ax = fig.add_subplot(111); ax.plot(...); "
                "self.ports.{p}.widget.redraw()  — never .set(fig)")


def _q(text: str, cap: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap - 1] + "…"


def importable_modules(target: Target) -> List[str]:
    """The modules YOU MAY IMPORT names one by one — the app modules (a
    linked project), this project's own, its declared packages — and every
    module a shortlisted function comes from. A name among them that the
    code reads as `name.function(...)` and never imports is safe to import:
    gate 4 adds the import, and the policy gate reads it again."""
    out = sorted(gui_policy.LINKED_MODULES) \
        if target.project_mode == "linked" else []
    out += [r for r in gui_policy.as_requires(target.requires)
            if r not in gui_policy.THIRD_PARTY and r.isidentifier()]
    out += [m for m in target.local_modules
            if m not in gui_policy.PROJECT_MODULES and m != MODULE]
    out += [r.module for r in target.shortlist if r.module.isidentifier()]
    return _dedupe(out)


def _allowed_text(target: Target) -> str:
    third = "numpy, PIL (Pillow), pandas, matplotlib.figure"
    linked = sorted(gui_policy.LINKED_MODULES) \
        if target.project_mode == "linked" else []
    extra = [r for r in gui_policy.as_requires(target.requires)
             if r not in gui_policy.THIRD_PARTY]
    local = [m for m in target.local_modules
             if m not in gui_policy.PROJECT_MODULES and m != MODULE]
    parts = [f"YOU MAY IMPORT (inside the function): the Python standard "
             f"library, {third}"]
    if extra:
        parts.append(f"; this app's declared packages: {', '.join(extra)}")
    if linked:
        parts.append(f"; these app modules: {', '.join(linked)}")
    if local:
        parts.append(f"; this project's modules: {', '.join(local)}")
    return "".join(parts) + "."


def _shortlist_text(refs: Sequence[Ref]) -> str:
    if not refs:
        return ""
    lines = ["USEFUL EXISTING FUNCTIONS (call them rather than rewriting "
             "them):"]
    for r in refs:
        lines.append(f"from {r.module} import {r.name}")
        lines.append(f"    {r.signature}"
                     + (f"  # {_q(r.summary, 90)}" if r.summary else ""))
        if r.keys:
            lines.append(f"    returns a dict with keys: {', '.join(r.keys)}")
        elif r.members:
            lines.append(f"    returns a {r.returns_class} with: "
                         f"{', '.join(r.members[:12])}")
    return "\n".join(lines)


def _docs_text(docs: Sequence[Dict[str, str]], cap: int) -> str:
    if not docs or cap <= 200:
        return ""
    out = ["DOCUMENTATION (excerpts — use the APIs exactly as written here):"]
    used = len(out[0])
    for d in docs:
        head = (f"--- {d.get('title') or d.get('source') or 'doc'}"
                + (f" ({d.get('server')})" if d.get("server") else ""))
        text = str(d.get("text") or "").strip()
        room = cap - used - len(head) - 2
        if room < 120:
            break
        if len(text) > room:
            text = text[:room - 1] + "…"
        out += [head, text]
        used += len(head) + len(text) + 2
    return "\n".join(out) if len(out) > 1 else ""


def _signature_block(target: Target) -> str:
    lines = ["SIGNATURE (use exactly this line):", target.signature()]
    for p in target.params:
        if p.what:
            lines.append(f"  {p.name}: {p.what}")
    return "\n".join(lines)


def _return_block(target: Target) -> str:
    lines = ["RETURN a dict with exactly these keys:"]
    for o in target.outputs:
        lines.append(f'  "{o.key}"  {o.what}' if o.what else f'  "{o.key}"')
    # A raise becomes an error dialog and CLEARED outputs (the stub's
    # envelope), so a validation message the task wants shown in a label
    # was never seen — measured: 4 of 6 failing code cases on llama3.1:8b.
    # No sample wording here: given one ("Enter a number"), llama3.1:8b
    # wrote exactly that instead of the message each task asked for
    # (measured, 3 of 8 cases).
    lines.append('If an INPUT is missing or invalid (a blank box, text where '
                 'a number belongs), do not raise: return the dict with the '
                 "message for the user in the key shown as text — the TASK's "
                 'own wording, word for word, when it gives one — and empty '
                 'values in the others.')
    lines.append('Raise ValueError("<a plain sentence for the user>") only '
                 'for a failure the user cannot fix by changing an input (a '
                 'file that cannot be read) — and never return 0 or "" to '
                 'hide a failure.')
    return "\n".join(lines)


def port_line(p: PortRow) -> str:
    """How a handler body may use one port — the binder's real methods."""
    lab = f' "{_q(p.label, 30)}"' if p.label else ""
    base = f"self.ports.{p.name}"
    if p.binder == "event":
        return f"{base}  {p.kind}{lab} — no value; only .enable(True/False)"
    if p.writer == "figure_for_drawing":
        return f"{base}  chart{lab}: " + CHART_RECIPE.format(p=p.name)
    if p.writer == "set_image":
        return (f"{base}.set(pil_image)  image{lab} — a PIL.Image or numpy "
                f"array, never a file path; .set(None) clears it")
    if p.writer == "append":
        return f"{base}.set(text)  log{lab} — appends one line"
    if p.writer == "set":
        return f"{base}.set(text)  status bar{lab}"
    if p.binder == "list":
        return (f"{base}.items() -> list of str (all), .get() -> list "
                f"(selected), .set(list_of_str)  listbox{lab}")
    if p.binder == "table":
        return (f"{base}.rows() -> list of tuples (all), .get() -> list "
                f"(selected), .set(list_of_rows)  table{lab}")
    if p.binder == "tab":
        return f"{base}.get() -> int, .set(index)  tabs{lab}"
    typ = PARAM_TYPES.get(p.type, "str")
    if p.kind in ("label", "progressbar"):
        what = "number 0-100" if p.kind == "progressbar" else "text"
        return f"{base}.set({what})  {p.kind}{lab}"
    if typ in ("int", "float") and p.kind not in ("spinbox", "scale"):
        # A typed box that is blank, or holds text that is no number, is
        # read as None (gui_emit_qt._coerce) — measured: the models wrote
        # float(...) and crashed on a blank box.
        typ += " or None (blank, or not a number)"
    return f"{base}.get() -> {typ}, .set(value)  {p.kind}{lab}"


#: A task whose every press works from the data as it was at the start:
#: "the items of the ORIGINAL list", "every original item again". K2 says
#: exactly that, and four of five models filtered whatever the LAST press
#: had left instead (2026-10-05) — which one press cannot show, so the
#: smoke run presses again (gui_smoke, handler mode) and the prompt says it.
_FROM_ORIGINAL = re.compile(
    r"\b(?:original|unfiltered)\s+(?:list|items?|data|rows?|table|values?"
    r"|entries|text|order)\b|\b(?:every|each|all)\s+(?:the\s+)?original\b"
    r"|\bfrom\s+the\s+(?:original|full)\s", re.IGNORECASE)


def restarts_from_original(instruction: str) -> bool:
    """Whether every press of the task starts from the ORIGINAL data."""
    return bool(_FROM_ORIGINAL.search(instruction or ""))


def _original_recipe(target: Target) -> str:
    """How to keep the original, on this window's own list or table."""
    held = [p for p in target.ports if p.binder in ("list", "table")]
    if not held:
        return (f"keep the data as it is on the first press in "
                f"self.{PRIVATE_PREFIX}original (when getattr(self, "
                f"\"{PRIVATE_PREFIX}original\", None) is None), and work "
                f"from that copy on every press")
    p = held[0]
    read = "items()" if p.binder == "list" else "rows()"
    return (f"on the first press keep the original — `if getattr(self, "
            f"\"{PRIVATE_PREFIX}original\", None) is None: "
            f"self.{PRIVATE_PREFIX}original = self.ports.{p.name}.{read}` — "
            f"and work from self.{PRIVATE_PREFIX}original on every press")


def _private_ok(attr: str) -> bool:
    """self._ai_<name>: the handler's own state between clicks."""
    rest = attr[len(PRIVATE_PREFIX):] if attr.startswith(PRIVATE_PREFIX) \
        else ""
    return bool(rest) and rest.isidentifier()


def _ports_block(target: Target, mention: Sequence[str] = ()) -> str:
    rows = list(target.ports)
    if mention:
        rows.sort(key=lambda p: (p.name not in mention, p.name))
    lines = ["THE WINDOW'S PORTS (read and write the window ONLY through "
             "these):"] + [f"  {port_line(p)}" for p in rows]
    lines.append("Every port also has .enable(True/False) and .clear().")
    lines.append("A blank or invalid input is the user's to fix: set the "
                 "message in the output label instead of raising — the "
                 "TASK's own wording, word for word, when it gives one. Do "
                 "not catch other exceptions — the app shows them to the "
                 "user.")
    lines.append(f"To remember a value between clicks, keep it on "
                 f"self.{PRIVATE_PREFIX}<name> (e.g. self.{PRIVATE_PREFIX}"
                 f"start = time.monotonic()) and read it with getattr(self, "
                 f"\"{PRIVATE_PREFIX}start\", None) — never any other "
                 f"self.<name>.")
    if restarts_from_original(target.instruction):
        lines.append("Repeated presses start from the ORIGINAL data, never "
                     "from what the last press left in the window: "
                     + _original_recipe(target) + ".")
    return "\n".join(lines)


def _sections(target: Target, budget: int) -> List[Tuple[str, str]]:
    """(name, text) in prompt order. Named so shedding can say what went."""
    task = (f'TASK\nThe {target.kind} "{_q(target.label, 40) or target.name}"'
            f" in this app must: {target.instruction.strip()}")
    if target.mode == "handler":
        head = [("role", _ROLE_HANDLER), ("task", task),
                ("signature", "SIGNATURE (use exactly this line):\n"
                              + target.signature()),
                ("ports", _ports_block(target, _mentioned(target)))]
        example = ("example", "EXAMPLE of the shape (a different task):\n"
                              "```python\n" + EXAMPLE_HANDLER + "\n```")
    else:
        head = [("role", _ROLE_FN), ("task", task),
                ("signature", _signature_block(target)),
                ("return", _return_block(target))]
        example = ("example", "EXAMPLE of the shape (a different task):\n"
                              "```python\n" + EXAMPLE_FUNCTION + "\n```")
    out = head + [("imports", _allowed_text(target))]
    if target.shortlist:
        out.append(("shortlist", _shortlist_text(target.shortlist)))
    out.append(("never", _NEVER))
    out.append(example)
    fixed = sum(len(t) + 2 for _n, t in out)
    if target.docs:
        docs = _docs_text(target.docs, max(0, budget - fixed - 120))
        if docs:
            out.insert(len(head) + 1, ("docs", docs))
    return out


def _mentioned(target: Target) -> List[str]:
    """Ports the instruction names (by port name or label), shown first."""
    text = f"{target.instruction} {target.label}".lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    out = []
    for p in target.ports:
        if p.name.lower() in words or (p.label and p.label.lower() in text):
            out.append(p.name)
    return out


def _join(sections: Sequence[Tuple[str, str]], tail: str) -> str:
    return "\n\n".join(t for _n, t in sections if t) + "\n\n" + tail


_TAIL = "Reply with the ```python block only."


def build_prompt(target: Target, budget: Optional[int] = None
                 ) -> Tuple[str, List[str]]:
    """(prompt, what was shed to fit ``budget`` characters)."""
    budget = int(budget or budget_chars())
    secs = _sections(target, budget)
    return _fit(secs, _TAIL, budget, target)


def _fit(secs: List[Tuple[str, str]], tail: str, budget: int,
         target: Target) -> Tuple[str, List[str]]:
    """Shed, in order, what a small model can best do without: the docs
    (already sized to the room left), shortlist entries from the bottom,
    the worked example, then — handler mode — ports the task never names."""
    shed: List[str] = []
    prompt = _join(secs, tail)
    names = [n for n, _t in secs]
    if target.docs and "docs" not in names:
        shed.append("documentation")         # no room was left for any
    refs = list(target.shortlist)
    while len(prompt) > budget:
        if "docs" in names:
            secs = [(n, t) for n, t in secs if n != "docs"]
            shed.append("documentation")
        elif refs:
            refs.pop()
            secs = [(n, _shortlist_text(refs) if n == "shortlist" else t)
                    for n, t in secs]
            if not refs:
                shed.append("the useful-functions list")
        elif "example" in names:
            secs = [(n, t) for n, t in secs if n != "example"]
            shed.append("the worked example")
        elif target.mode == "handler" and "ports" in names and \
                "ports the task does not name" not in shed:
            keep = _mentioned(target)
            if not keep or len(keep) == len(target.ports):
                break
            slim = dataclasses.replace(
                target, ports=[p for p in target.ports if p.name in keep])
            secs = [(n, _ports_block(slim, keep) if n == "ports" else t)
                    for n, t in secs]
            shed.append("ports the task does not name")
        else:
            break
        names = [n for n, _t in secs]
        prompt = _join(secs, tail)
    if refs and len(refs) < len(target.shortlist):
        shed.append(f"{len(target.shortlist) - len(refs)} useful function(s)")
    return prompt, shed


def numbered(code: str, cap: int = MAX_SHOWN_CHARS) -> str:
    lines = (code or "").split("\n")
    width = len(str(len(lines)))
    out = "\n".join(f"{i + 1:>{width}} | {ln}" for i, ln in enumerate(lines))
    return out if len(out) <= cap else out[:cap] + "\n… (cut)"


def hints_for(faults: Sequence[str], target: Target) -> List[str]:
    """Targeted advice keyed on the fault text — what a small model needs
    beyond the error itself."""
    text = "\n".join(faults)
    out: List[str] = []

    def add(h: str) -> None:
        if h not in out:
            out.append(h)

    modules = importable_modules(target)
    for name in re.findall(r"name '([A-Za-z_][A-Za-z0-9_]*)' is not defined"
                           r"|undefined name '([A-Za-z_][A-Za-z0-9_]*)'",
                           text):
        n = name[0] or name[1]
        if n in AUTO_IMPORTS:
            add(f"add `{AUTO_IMPORTS[n]}` inside the function")
        elif n in modules:
            # "define image_stats, or use a parameter" was the hint for
            # `image_stats.image_pixel_stats(path)` — the very form K4's task
            # words it in; all five of qwen2.5-coder's replies kept the call
            # and never imported (2026-10-05).
            add(f"add `import {n}` inside the function — {n} is one of the "
                f"modules under YOU MAY IMPORT")
        elif f"'{n}' — it is used as a module" in text:
            add(f"{n} is used as a module but never imported: import it "
                f"inside the function if it is under YOU MAY IMPORT, or use "
                f"a module that is")
        else:
            add(f"define {n} before using it, or use one of the parameters "
                f"({', '.join(p.name for p in target.params) or 'self.ports'})")
    for attr in re.findall(r"has no attribute '(" + PRIVATE_PREFIX
                           + r"[A-Za-z0-9_]+)'", text):
        # The handler's own state, read on a press before any press set it:
        # qwen2.5's K2 read self._ai_original_fruits on the FIRST press and
        # got the bare AttributeError back with no hint, three repairs
        # running — each returned the same code (2026-10-05).
        add(f"self.{attr} does not exist until a press sets it: read it with "
            f"getattr(self, \"{attr}\", None), and when that gives None, set "
            f"it first (on the first press)")
    if "start from the ORIGINAL data" in text:
        add(_original_recipe(target))
    if "raised again as ValueError" in text:
        add("fix the line that failed; never turn a crash into ValueError — "
            "catch only the error a bad input causes (e.g. `except "
            "ValueError:` around int(text)), and let any other error raise")
    if "no port" in text and target.ports:
        add("the ports are exactly: "
            + ", ".join(p.name for p in target.ports))
    if "figure_for_drawing" in text or "chart" in text:
        charts = [p.name for p in target.ports
                  if p.writer == "figure_for_drawing"] or ["chart"]
        add("draw a chart like this: " + CHART_RECIPE.format(p=charts[0]))
    if "writes outside" in text:
        add("write files only inside a folder or file the user chose — a "
            "parameter — never to a fixed path")
    if "did not finish" in text or "timed out" in text or "endless" in text:
        add("no waiting and no endless loops: it must finish in a second on "
            "small inputs")
    if "has no key" in text or "lacks the key" in text or "not a dict" in text:
        add("every return must be a dict with exactly these keys: "
            + ", ".join(repr(k) for k in target.keys))
    if "shows text, but got a list" in text:
        add("a label shows text: join a list first, e.g. ', '.join(names)")
    if "split into letters" in text or "takes a list of items" in text:
        add("a listbox takes a LIST of strings, not one string")
    if "No module named" in text or "is not on the" in text:
        add("use only the modules listed under YOU MAY IMPORT")
    if "positional argument" in text or "takes" in text and "argument" in text:
        sigs = [r.signature for r in target.shortlist]
        if sigs:
            add("call existing functions with their real signatures: "
                + "; ".join(sigs[:3]))
    if "cut off" in text:
        add("write a shorter function — fewer comments, no example usage")
    if "broad except" in text:
        add("let errors raise (or raise ValueError with a plain sentence); "
            "do not catch Exception and carry on")
    for h in _value_hints(text):
        add(h)
    syntax = _SYNTAX.search(text)
    if syntax:
        add(f"line {syntax.group(1)} is not valid Python: "
            + ("close every (, [ and { on the line that opens it — write a "
               "long dict one key per line, ending with its }"
               if _BRACKETS.search(syntax.group(2)) else
               "a name is one word with no spaces, arguments are separated "
               "by commas, and a line that opens a block ends with ':'"))
    if not out and faults:
        # Every fault goes back with a HOW TO FIX: 9 of the 22 repair
        # prompts of the 2026-10-05 run had none (replayed offline).
        add("fix exactly what WHAT IS WRONG names, on the line it names, "
            "and keep everything else as it is")
    return out[:6]


#: A syntax fault as gate 1 words it: "line 7: '{' was never closed: ...".
_SYNTAX = re.compile(r"^line (\d+): (invalid (?:syntax|character|decimal "
                     r"literal).*|.*was never closed|unmatched .*|"
                     r".*does not match opening .*|unterminated .*|"
                     r"expected .*|unexpected indent|unindent .*|"
                     r"cannot assign to .*|f-string.*)", re.MULTILINE)
_BRACKETS = re.compile(r"never closed|unmatched|does not match opening")

#: What a value of a built-in type is called in a hint.
_TYPE_WORDS = {"int": "an int", "float": "a float", "str": "a string",
               "bool": "a bool", "list": "a list", "dict": "a dict",
               "tuple": "a tuple", "set": "a set", "bytes": "bytes"}


def _value_hints(text: str) -> List[str]:
    """An attribute a built-in value does not have, said about the name on
    the failing line: phi3.5's K7 called age.is_integer() on the int a
    number box gives (no int has it on Python 3.11) and got the bare
    AttributeError back, three repairs running (2026-10-05)."""
    out = []
    for typ, attr, line in re.findall(
            r"'(\w+)' object has no attribute '(\w+)'"
            r"(?: \(at [^:\s]+:\d+: ([^\n]*))?", text):
        word = _TYPE_WORDS.get(typ)
        if word is None and typ != "NoneType":
            continue
        used = re.search(r"([A-Za-z_][\w.]*(?:\[[^\]]*\])?)\s*\.\s*"
                         + re.escape(attr) + r"\b", line or "")
        name = used.group(1) if used else "the value"
        if typ == "NoneType":
            out.append(f"{name} is None there (a blank input, or a call "
                       f"that returned nothing): test `if {name} is None:` "
                       f"before using it")
        else:
            out.append(f"{name} is {word} there, and {word} has no "
                       f".{attr} — use only what {word} has")
    return out


def repair_prompt(target: Target, cand: Candidate, budget: Optional[int] = None
                  ) -> str:
    """The best candidate so far, its faults with lines, targeted hints, and
    the grounding again — re-budgeted so the faults always fit."""
    budget = int(budget or budget_chars())
    faults = list(cand.faults)
    shown = faults[:MAX_FAULTS]
    more = len(faults) - len(shown)
    wrong = "WHAT IS WRONG:\n" + "\n".join(f"- {f}" for f in shown) + (
        f"\n- (and {more} more)" if more > 0 else "")
    hints = hints_for(faults, target)
    fix = ("HOW TO FIX:\n" + "\n".join(f"- {h}" for h in hints)) if hints \
        else ""
    returned = ("WHAT YOU RETURNED (line numbers added):\n"
                + numbered(cand.code or cand.raw))
    secs = _sections(target, budget)
    keep = [(n, t) for n, t in secs if n in ("role", "task", "signature",
                                             "return", "ports")]
    rest = [(n, t) for n, t in secs if n not in dict(keep)]
    head = keep + [("returned", returned), ("wrong", wrong), ("fix", fix)]
    tail = ("Reply with the corrected function in one ```python block. "
            "No prose.")
    prompt, _shed = _fit(head + rest, tail, budget, target)
    return prompt


# ============================================================
# Gate 0 — extract
# ============================================================

_CODE_START = re.compile(r"^\s*(def |async def |import |from \S+ import |@|"
                         r"class )")


def extract_code(raw: str) -> Tuple[str, bool]:
    """(the code, whether its fence was never closed).

    Prefers a fenced block that holds a def, then a ```python one, then any;
    with no fence, everything from the first line that looks like code."""
    text = str(raw or "").replace("\r\n", "\n").expandtabs(4)
    blocks: List[Tuple[str, str, bool]] = []
    pos = 0
    while True:
        i = text.find("```", pos)
        if i < 0:
            break
        eol = text.find("\n", i)
        if eol < 0:
            break
        lang = text[i + 3:eol].strip().lower()
        j = text.find("```", eol + 1)
        if j < 0:
            blocks.append((lang, text[eol + 1:], False))
            break
        blocks.append((lang, text[eol + 1:j], True))
        pos = j + 3
    if blocks:
        with_def = [b for b in blocks if re.search(r"^\s*def ", b[1], re.M)]
        if len(with_def) > 1:
            # A helper in a block of its own: join them, and the shape gate
            # nests the helpers inside the one function.
            return ("\n\n".join(b[1].strip("\n") for b in with_def),
                    not all(b[2] for b in with_def))
        pick = (with_def or [b for b in blocks if b[0] in ("python", "py")]
                or blocks)
        _lang, body, closed = pick[0]
        return body.strip("\n"), not closed
    lines = text.split("\n")
    for k, line in enumerate(lines):
        if _CODE_START.match(line):
            return "\n".join(lines[k:]).strip("\n"), False
    return "", False


def _parse_prefix(code: str) -> Tuple[Optional[ast.Module], str, int]:
    """(tree, code, lines dropped) — the whole code, or the longest prefix
    that parses when what follows is prose at column 0 ("This function
    counts…"). (None, code, 0) when no prefix with a def parses."""
    try:
        return ast.parse(code), code, 0
    except SyntaxError as exc:
        err_line = exc.lineno or 0
    lines = code.split("\n")
    for end in range(min(err_line, len(lines)) - 1, 0, -1):
        if lines[end].strip() and not lines[end][:1].isspace():
            head = "\n".join(lines[:end]).rstrip()
            try:
                tree = ast.parse(head)
            except SyntaxError:
                continue
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for n in ast.walk(tree)):
                return tree, head, len(lines) - end
    return None, code, 0


def _looks_cut(code: str, exc: SyntaxError, unclosed: bool) -> bool:
    msg = str(getattr(exc, "msg", "") or "")
    last = len(code.rstrip("\n").split("\n"))
    at_end = (exc.lineno or 0) >= last - 1
    return unclosed or (at_end and any(
        k in msg for k in ("never closed", "unexpected EOF",
                           "expected an indented block",
                           "unterminated", "EOF while")))


# ============================================================
# Gate 1 — shape
# ============================================================

def _header_end(fn: ast.FunctionDef) -> int:
    """The line the def's header (signature) ends on."""
    end = fn.lineno
    a = fn.args
    for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                + [x for x in (a.vararg, a.kwarg) if x]):
        end = max(end, getattr(arg, "end_lineno", end) or end)
    for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
        end = max(end, getattr(d, "end_lineno", end) or end)
    if fn.returns is not None:
        end = max(end, fn.returns.end_lineno or end)
    return end


def _segment_lines(lines: List[str], node: ast.AST) -> List[str]:
    first = min([node.lineno] + [d.lineno for d in
                                 getattr(node, "decorator_list", [])])
    return lines[first - 1:node.end_lineno]


def _body_lines(code: str, lines: List[str], fn: ast.FunctionDef
                ) -> Tuple[List[str], str, int]:
    """(body lines as written, the body's indent, how many leading lines are
    the docstring) — comments inside the body kept."""
    first = fn.body[0]
    hend = _header_end(fn)
    if first.lineno <= hend:
        # `def f(x): return x` — the body shares the header's line.
        body = []
        for stmt in fn.body:
            seg = ast.get_source_segment(code, stmt) or ast.unparse(stmt)
            body.extend("    " + ln for ln in seg.split("\n"))
        indent = "    "
    else:
        body = lines[hend:fn.end_lineno]
        indent = " " * first.col_offset
    doc_lines = 0
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        doc_lines = max(0, (first.end_lineno or first.lineno) - hend) \
            if first.lineno > hend else 1
    return body, indent, doc_lines


def _src(code: str, node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    return ast.get_source_segment(code, node) or ast.unparse(node)


def shape_function(code: str, target: Target
                   ) -> Tuple[str, List[str], List[str]]:
    """Gate 1 for function mode: (normalised code, faults, notes)."""
    faults: List[str] = []
    notes: List[str] = []
    tree = ast.parse(code)
    lines = code.split("\n")
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            faults.append(f"line {n.lineno}: no classes — write one function "
                          f"named {target.name}")
    if not defs:
        faults.append(f"there is no function — write `{target.signature()}`")
        return code, faults, notes
    main = next((d for d in defs if d.name == target.name), None)
    if main is None:
        wanted = [p.name for p in target.params]
        same = [d for d in defs if [a.arg for a in d.args.args] == wanted]
        if len(defs) == 1:
            main = defs[0]
        elif len(same) == 1:
            main = same[0]
        else:
            faults.append(f"{len(defs)} functions and none is named "
                          f"{target.name} — write exactly one, named "
                          f"{target.name} (put helpers inside it)")
            return code, faults, notes
        notes.append(f"renamed {main.name}() to {target.name}()")
    if isinstance(main, ast.AsyncFunctionDef):
        faults.append(f"line {main.lineno}: not async — a plain def")
    if main.decorator_list:
        faults.append(f"line {main.lineno}: no decorators")
    if faults:
        return code, faults, notes

    helpers = [d for d in defs if d is not main]
    moved: List[str] = []
    for node in tree.body:
        if node is main or isinstance(node, ast.ClassDef):
            continue
        if node in helpers:
            moved.append("\n".join(_segment_lines(lines, node)))
            notes.append(f"moved helper {node.name}() inside {target.name}()")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            moved.append(_src(code, node))
            notes.append(f"moved `{_q(_src(code, node), 50)}` inside the "
                         f"function")
        elif (isinstance(node, (ast.Assign, ast.AnnAssign))
              and _is_literal(node.value)):
            moved.append(_src(code, node))
            notes.append(f"moved the constant on line {node.lineno} inside "
                         f"the function")
        elif (isinstance(node, ast.Expr) and isinstance(node.value,
                                                        ast.Constant)):
            continue                                    # a module docstring
        else:
            notes.append(f"dropped line {node.lineno} "
                         f"(`{_q(_src(code, node).splitlines()[0], 50)}`) — "
                         f"only the function is kept")

    # ---- parameters ------------------------------------------------
    a = main.args
    model = [x.arg for x in list(a.posonlyargs) + list(a.args)]
    n_req = len(model) - len(a.defaults)
    defaults = {x.arg: d for x, d in zip(
        (list(a.posonlyargs) + list(a.args))[n_req:], a.defaults)}
    if model and model[0] == "self":
        model = model[1:]
        n_req = max(0, n_req - 1)
        notes.append("dropped `self` — this is a plain function")
    wanted = [p.name for p in target.params]
    aliases: List[Tuple[str, str]] = []
    keep_extra: List[str] = []
    if model != wanted:
        extra = [m for m in model if m not in wanted]
        missing = [t for t in wanted if t not in model]
        for m, t in zip(extra, missing):
            aliases.append((m, t))
        for m in extra[len(missing):]:
            if m in defaults:
                keep_extra.append(f"{m}={_src(code, defaults[m])}")
            else:
                faults.append(
                    f"line {main.lineno}: it takes `{m}`, which no widget "
                    f"passes — the signature is exactly "
                    f"`{target.signature()}`")
        for t in missing[len(extra):]:
            notes.append(f"added parameter {t} (from the {_port_of(target, t)}"
                         f" widget) — the body does not use it")
        if aliases:
            notes.append("renamed parameter(s) to the port names: "
                         + ", ".join(f"{m} -> {t}" for m, t in aliases))
        elif not extra and not missing:
            notes.append("put the parameters in the order the widgets "
                         "pass them")
    if a.vararg or a.kwarg:
        notes.append("dropped *args/**kwargs — a link passes exactly the "
                     "listed parameters")
    if a.kwonlyargs:
        for x, d in zip(a.kwonlyargs, a.kw_defaults):
            if d is None:
                faults.append(f"line {main.lineno}: keyword-only `{x.arg}` "
                              f"has no default and nothing passes it")
    if faults:
        return code, faults, notes

    body, indent, doc_n = _body_lines(code, lines, main)
    insert: List[str] = []
    for chunk in moved:
        insert.extend(indent + ln if ln.strip() else ln
                      for ln in chunk.split("\n"))
    insert.extend(f"{indent}{m} = {t}" for m, t in aliases)
    params = [f"{p.name}: {p.type}" for p in target.params] + keep_extra
    header = f"def {target.name}({', '.join(params)}) -> dict:"
    if doc_n == 0 and target.instruction.strip():
        # Double quotes and backslashes out: an instruction ending in '"'
        # would close the docstring early, and "C:\new" would be an escape.
        doc = _q(target.instruction, 70).replace('"', "'").replace("\\", "/")
        # A pasted instruction may carry a zero-width space; the policy
        # gate refuses those, and the model could never "fix" this line.
        doc = _INVISIBLE.sub("", doc)
        body = [f'{indent}"""{doc}"""'] + body
        doc_n = 1
    new = [header] + body[:doc_n] + insert + body[doc_n:]
    out = "\n".join(new).rstrip() + "\n"
    try:
        ast.parse(out)
    except SyntaxError as exc:
        faults.append(f"after tidying it does not parse (line {exc.lineno}: "
                      f"{exc.msg}) — write the function again, plainly")
        return code, faults, notes
    return out, faults, notes


def _port_of(target: Target, param: str) -> str:
    p = next((x for x in target.params if x.name == param), None)
    return (p.port or param) if p else param


def _is_literal(node: Optional[ast.AST]) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def shape_handler(code: str, target: Target
                  ) -> Tuple[str, List[str], List[str]]:
    """Gate 1 for handler mode: one method, `(self, *args)`."""
    faults: List[str] = []
    notes: List[str] = []
    tree = ast.parse(code)
    lines = code.split("\n")
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        inner = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
        pick = [n for n in inner if n.name == target.name] or inner[:1]
        if pick and not fns:
            seg = _segment_lines(lines, pick[0])
            pad = pick[0].col_offset
            code = "\n".join(ln[pad:] if ln[:pad].strip() == "" else ln
                             for ln in seg)
            notes.append(f"took {pick[0].name}() out of class {cls.name}")
            return shape_handler(code, target)
    if not fns:
        stmts = [n for n in tree.body if not isinstance(
            n, (ast.Import, ast.ImportFrom, ast.ClassDef))]
        if not stmts and not tree.body:
            faults.append(f"there is no code — write `{target.signature()}`")
            return code, faults, notes
        body = "\n".join("    " + ln if ln.strip() else ln
                         for ln in code.split("\n"))
        code = f"def {target.name}(self, *args) -> None:\n{body}\n"
        notes.append("wrapped the statements in the method")
        return shape_handler(code, target)
    main = next((d for d in fns if d.name == target.name), None)
    if main is None:
        if len(fns) != 1:
            faults.append(f"{len(fns)} functions and none is named "
                          f"{target.name} — write exactly one method")
            return code, faults, notes
        main = fns[0]
        notes.append(f"renamed {main.name}() to {target.name}()")
    if main.decorator_list:
        faults.append(f"line {main.lineno}: no decorators")
        return code, faults, notes
    moved: List[str] = []
    for node in tree.body:
        if node is main:
            continue
        if isinstance(node, ast.FunctionDef):
            moved.append("\n".join(_segment_lines(lines, node)))
            notes.append(f"moved helper {node.name}() inside {target.name}()")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            moved.append(_src(code, node))
            notes.append(f"moved `{_q(_src(code, node), 50)}` inside the "
                         f"method")
        elif not (isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Constant)):
            notes.append(f"dropped line {node.lineno} — only the method is "
                         f"kept")
    names = [x.arg for x in main.args.args]
    if not names or names[0] != "self":
        notes.append("added `self` as the first parameter")
    body, indent, doc_n = _body_lines(code, lines, main)
    insert: List[str] = []
    for chunk in moved:
        insert.extend(indent + ln if ln.strip() else ln
                      for ln in chunk.split("\n"))
    new = [target.signature()] + body[:doc_n] + insert + body[doc_n:]
    out = "\n".join(new).rstrip() + "\n"
    try:
        ast.parse(out)
    except SyntaxError as exc:
        faults.append(f"after tidying it does not parse (line {exc.lineno}: "
                      f"{exc.msg})")
        return code, faults, notes
    return out, faults, notes


# ============================================================
# Gate 2 — policy
# ============================================================

def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
    return any(isinstance(n, ast.Name) and n.id in ("Exception",
                                                    "BaseException")
               for n in names)


def _reports(handler: ast.ExceptHandler) -> bool:
    """Whether a broad except still lets the failure be seen: it raises,
    returns an "error" key, reports — or skips ONE item of a loop
    (`skipped += 1; continue`), the per-file tolerance frame_timing itself
    uses: one unreadable frame in a thousand must not abort a scan."""
    for node in ast.walk(handler):
        if isinstance(node, (ast.Raise, ast.Continue)):
            return True
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == "error"
                   for k in node.value.keys):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "report_error":
            return True
    return False


#: Characters that make the diff a person reviews differ from the code that
#: runs: bidirectional overrides/isolates ("Trojan Source") and zero-width
#: or invisible marks. Code behind a widget never needs them literally.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                        "\u2066-\u2069\ufeff]")

_NO_EXIT = ("no exit() — it would close the whole app; raise ValueError "
            "instead")
_NO_WORKERS = ("no threads or worker processes — return the result; the app "
               "decides where it runs")


def policy_faults(code: str, target: Target) -> List[str]:
    """Gate 2: gui_policy (the gate Run enforces) and what code behind a GUI
    must not do even where an app may."""
    importable = (gui_policy.as_requires(target.requires)
                  + list(target.local_modules))
    ok, errs = gui_policy.validate(code, target.project_mode, importable,
                                   toolkit=target.toolkit)
    faults = list(errs)
    for i, text in enumerate(code.split("\n"), 1):
        m = _INVISIBLE.search(text)
        if m:
            faults.append(f"line {i}: an invisible or direction-control "
                          f"character (U+{ord(m.group()):04X}) — the review "
                          f"would not show this line as it runs; remove it "
                          f"(write \\u{ord(m.group()):04x} if it is meant)")
    tree = ast.parse(code)
    faults += _introspection_faults(tree)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        roots: List[str] = []
        if isinstance(node, ast.Import):
            roots = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots = [node.module]
        for r in roots:
            if r.split(".")[0] in TOOLKIT_ROOTS:
                faults.append(f"line {line}: no {r.split('.')[0]} — the app "
                              f"owns the window; "
                              + ("return values instead" if target.mode ==
                                 "function" else "use self.ports"))
            elif r.startswith("matplotlib.pyplot"):
                faults.append(f"line {line}: no matplotlib.pyplot (it opens "
                              f"windows) — use matplotlib.figure.Figure")
            elif r.split(".")[0] in WORKER_MODULES:
                faults.append(f"line {line}: {_NO_WORKERS}")
            elif r.split(".")[0] in LOW_LEVEL_MODULES:
                faults.append(f"line {line}: no {r.split('.')[0]} — code "
                              f"behind a widget has no use for the OS's or "
                              f"the interpreter's low-level hooks; return "
                              f"values instead")
        if isinstance(node, ast.ImportFrom) and node.module == "matplotlib" \
                and any(a.name == "pyplot" for a in node.names):
            faults.append(f"line {line}: no matplotlib.pyplot — use "
                          f"matplotlib.figure.Figure")
        if (isinstance(node, ast.Attribute) and node.attr in CTYPES_NAMES) \
                or (isinstance(node, ast.ImportFrom) and any(
                    a.name in CTYPES_NAMES for a in node.names)):
            faults.append(f"line {line}: no ctypes (native calls) — code "
                          f"behind a widget uses Python libraries")
        if isinstance(node, ast.ImportFrom) and not node.level:
            for a in node.names:
                if a.name in WORKER_NAMES:
                    faults.append(f"line {line}: {_NO_WORKERS}")
                elif a.name in ("_exit", "abort", "exit") and \
                        (node.module or "") in ("os", "sys", "nt", "posix"):
                    faults.append(f"line {line}: {_NO_EXIT}")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            faults.append(f"line {line}: no global/nonlocal — use local "
                          f"variables and return the result")
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else "")
            recv = f.value.id if (isinstance(f, ast.Attribute) and
                                  isinstance(f.value, ast.Name)) else ""
            if isinstance(f, ast.Name) and name == "input":
                faults.append(f"line {line}: no input() — the values come "
                              f"from the parameters")
            elif name in ("exit", "quit") and isinstance(f, ast.Name) or (
                    name == "_exit") or (
                    name in ("exit", "abort") and recv in ("sys", "os")):
                faults.append(f"line {line}: {_NO_EXIT}")
            elif name == "chdir":
                faults.append(f"line {line}: no chdir — it changes the whole "
                              f"app's working folder; use full paths")
            elif name == "sleep" and node.args and isinstance(
                    node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, (int, float)) and \
                    node.args[0].value > 1:
                faults.append(f"line {line}: no sleep({node.args[0].value}) — "
                              f"the window is frozen while this runs")
            elif name in WORKER_NAMES or recv in ("threading", "_thread"):
                faults.append(f"line {line}: {_NO_WORKERS}")
        elif isinstance(node, ast.While) and _always_true(node.test):
            if not any(isinstance(n, (ast.Break, ast.Return, ast.Raise))
                       for n in ast.walk(node)):
                faults.append(f"line {line}: `while True` with no break or "
                              f"return never ends — the window would freeze")
        elif isinstance(node, ast.ExceptHandler) and _is_broad(node) \
                and not _reports(node):
            faults.append(f"line {line}: a broad except hides the failure — "
                          f"let it raise (or raise ValueError with a plain "
                          f"sentence)" + (" ; the app reports exceptions"
                                          if target.mode == "handler" else ""))
    return _dedupe(faults)


def _attr_targets(node: ast.AST):
    """The attribute targets of an assignment/del target, tuples unpacked."""
    if isinstance(node, ast.Attribute):
        yield node
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _attr_targets(elt)
    elif isinstance(node, ast.Starred):
        yield from _attr_targets(node.value)


def _root(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _introspection_faults(tree: ast.AST) -> List[str]:
    """Frames, the garbage collector, interpreter hooks (INTROSPECTION_*),
    and changing a module the code imported — `os.path.realpath = ...`,
    setattr(json, ...). Patching a module changes it for the whole app (and,
    in the smoke run, for the harness checking this code)."""
    faults: List[str] = []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update((a.asname or a.name).split(".")[0]
                            for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names
                            if a.name != "*")
    why = ("— code behind a widget has no use for the interpreter's "
           "internals; return values instead")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in INTROSPECTION_MODULES or \
                        a.name in INTROSPECTION_MODULES:
                    faults.append(f"line {line}: no {a.name} {why}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in INTROSPECTION_MODULES or \
                    node.module in INTROSPECTION_MODULES:
                faults.append(f"line {line}: no {node.module} {why}")
            for a in node.names:
                if a.name in INTROSPECTION_ATTRS or (
                        node.module == "inspect" and a.name in INSPECT_FRAMES):
                    faults.append(f"line {line}: no {a.name} {why}")
        elif isinstance(node, ast.Attribute) and \
                node.attr in INTROSPECTION_ATTRS:
            faults.append(f"line {line}: no .{node.attr} {why}")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "inspect" and f.attr in INSPECT_FRAMES:
                faults.append(f"line {line}: no inspect.{f.attr}() {why}")
            elif isinstance(f, ast.Name) and f.id in ("setattr", "delattr") \
                    and node.args and _root(node.args[0]) in imported:
                faults.append(f"line {line}: do not change the module "
                              f"{_root(node.args[0])} — it is shared by the "
                              f"whole app")
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for t in targets:
            for attr in _attr_targets(t):
                if _root(attr) in imported:
                    faults.append(f"line {line}: do not change the module "
                                  f"{_root(attr)} — it is shared by the "
                                  f"whole app")
    return _dedupe(faults)


def _always_true(test: ast.AST) -> bool:
    return isinstance(test, ast.Constant) and bool(test.value) is True


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


# ============================================================
# Gate 3 — references
# ============================================================

def _ref_index(target: Target) -> Dict[Tuple[str, str], Ref]:
    return {(r.module, r.name): r for r in target.shortlist}


def _module_functions(module: str, target: Target,
                      catalogue: Optional[Callable[[str], Any]]) -> Any:
    """designer_wiring.ModuleInfo for ``module`` via the caller's
    ``catalogue`` (which knows the project folder), else None."""
    if catalogue is None:
        return None
    try:
        return catalogue(module)
    except Exception:                                    # noqa: BLE001
        return None


def _checkable(module: str, target: Target) -> bool:
    root = module.split(".")[0]
    return (root in gui_policy.LINKED_MODULES
            or root in target.local_modules) and root not in \
        gui_policy.PROJECT_MODULES


def reference_fixes(code: str, target: Target,
                    catalogue: Optional[Callable[[str], Any]] = None
                    ) -> Tuple[str, List[str], List[str]]:
    """Gate 3: (code with unique close matches fixed, faults, notes)."""
    faults: List[str] = []
    notes: List[str] = []
    tree = ast.parse(code)
    bound: Dict[str, Tuple[str, str]] = {}     # local name -> (module, func)
    modules: Dict[str, str] = {}               # alias -> module
    fixes: List[Tuple[str, str]] = []          # regex, replacement
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                not node.level and _checkable(node.module, target):
            info = _module_functions(node.module, target, catalogue)
            if info is None or not getattr(info, "found", False):
                continue
            have = list(info.function_names)
            for a in node.names:
                if a.name in have or a.name == "*":
                    bound[a.asname or a.name] = (node.module, a.name)
                    continue
                close = difflib.get_close_matches(a.name, have, n=2,
                                                  cutoff=0.75)
                if len(close) == 1:
                    fixes.append((rf"(from\s+{re.escape(node.module)}\s+"
                                  rf"import\s+[^\n]*?)\b{re.escape(a.name)}\b"
                                  rf"(?!\s+as\b)",
                                  rf"\g<1>{close[0]} as {a.name}"
                                  if not a.asname else
                                  rf"\g<1>{close[0]}"))
                    bound[a.asname or a.name] = (node.module, close[0])
                    notes.append(f"{node.module} has no {a.name}(); used "
                                 f"{close[0]}()")
                else:
                    faults.append(
                        f"line {node.lineno}: {node.module} has no function "
                        f"{a.name!r}" + (f" (it has: {', '.join(have[:12])})"
                                         if have else ""))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _checkable(a.name, target):
                    modules[a.asname or a.name] = a.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        mod = func = ""
        if isinstance(f, ast.Name) and f.id in bound:
            mod, func = bound[f.id]
        elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
              and f.value.id in modules):
            mod, func = modules[f.value.id], f.attr
            info = _module_functions(mod, target, catalogue)
            if info is not None and getattr(info, "found", False) and \
                    func not in info.function_names:
                close = difflib.get_close_matches(
                    func, list(info.function_names), n=2, cutoff=0.75)
                if len(close) == 1:
                    fixes.append((rf"\b{re.escape(f.value.id)}\."
                                  rf"{re.escape(func)}\b",
                                  f"{f.value.id}.{close[0]}"))
                    notes.append(f"{mod} has no {func}(); used {close[0]}()")
                    func = close[0]
                else:
                    faults.append(f"line {node.lineno}: {mod} has no function "
                                  f"{func!r}")
                    continue
        if not mod:
            continue
        info = _module_functions(mod, target, catalogue)
        fn = info.function(func) if info is not None and getattr(
            info, "found", False) else None
        if fn is None or fn.varargs:
            continue
        given = len(node.args) + sum(1 for k in node.keywords
                                     if k.arg in fn.params)
        if any(isinstance(x, ast.Starred) for x in node.args) or any(
                k.arg is None for k in node.keywords):
            continue
        if given > len(fn.params) or given < fn.required:
            want = (f"{fn.required}" if fn.required == len(fn.params)
                    else f"{fn.required}-{len(fn.params)}")
            faults.append(f"line {node.lineno}: {mod}.{fn.signature()} takes "
                          f"{want} argument(s); this passes {given}")
    for pattern, repl in fixes:
        code, n = re.subn(pattern, repl, code, count=1)
        if not n:
            faults.append("an import names a function its module does not "
                          "have — " + (notes.pop() if notes else "fix it"))
    if target.mode == "function":
        faults += _return_faults(code, target)
    else:
        code, more_f, more_n = _port_faults(code, target)
        faults += more_f
        notes += more_n
    return code, _dedupe(faults), notes


def _own_returns(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Return):
            yield child
        yield from _own_returns(child)


def returned_keys(code: str, name: str) -> Optional[List[str]]:
    """The keys every dict-literal return of ``name`` carries in common, or
    None when no return can be read (a variable — the smoke run checks)."""
    tree = ast.parse(code)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == name), None)
    if fn is None:
        return None
    sets = []
    for r in _own_returns(fn):
        if isinstance(r.value, ast.Dict):
            sets.append([k.value for k in r.value.keys
                         if isinstance(k, ast.Constant)])
    if not sets:
        return None
    common = [k for k in sets[0] if all(k in s for s in sets[1:])]
    return common


def _return_faults(code: str, target: Target) -> List[str]:
    tree = ast.parse(code)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)), None)
    if fn is None:
        return []
    rets = list(_own_returns(fn))
    keys = target.keys
    faults: List[str] = []
    valued = [r for r in rets if r.value is not None and not (
        isinstance(r.value, ast.Constant) and r.value.value is None)]
    if not valued:
        faults.append(f"it never returns a value — end with "
                      f"return {{{', '.join(repr(k) + ': ...' for k in keys)}}}")
        return faults
    # A function that cannot produce the result passed every gate before
    # these two checks: `return {"error": "..."}` alone is a soft pass in
    # the smoke run, and so is `raise ValueError(...)` with a dead return
    # under it — a refusal or a placeholder, offered with Accept enabled.
    want = f"return {{{', '.join(repr(k) + ': ...' for k in keys)}}}"
    if all(_is_error_dict(r.value) for r in valued):
        faults.append(f"every return is an error — at least one must give "
                      f"the result: {want}")
    first_return = min(r.lineno for r in rets)
    for stmt in fn.body:
        if isinstance(stmt, ast.Raise) and stmt.lineno < first_return:
            faults.append(f"line {stmt.lineno}: it always raises here, "
                          f"before any return — compute the result and "
                          f"{want}; raise ValueError only when the input is "
                          f"wrong")
            break
    for r in valued:
        v = r.value
        if isinstance(v, ast.Dict):
            have = [k.value for k in v.keys if isinstance(k, ast.Constant)]
            if None in [k for k in v.keys]:
                continue                                 # {**other}: unknown
            if "error" in have:
                continue
            missing = [k for k in keys if k not in have]
            if missing:
                faults.append(f"line {r.lineno}: the returned dict lacks the "
                              f"key(s) {', '.join(map(repr, missing))} — every "
                              f"return needs {', '.join(map(repr, keys))}")
        elif isinstance(v, (ast.Constant, ast.List, ast.Tuple, ast.JoinedStr,
                            ast.Set, ast.ListComp, ast.BinOp)) or (
                isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id in _NOT_DICT_CALLS):
            what = (f"{v.func.id}(...)" if isinstance(v, ast.Call)
                    else type(v).__name__.lower())
            faults.append(f"line {r.lineno}: it returns {what}, not a dict "
                          f"— return "
                          f"{{{', '.join(repr(k) + ': ...' for k in keys)}}}")
    return faults


def _is_error_dict(node: Optional[ast.AST]) -> bool:
    """`{"error": ...}` written as a literal — the failure path, not a
    result."""
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "error" for k in node.keys)


#: Builtins whose result is certainly not a dict — `return len(names)`.
_NOT_DICT_CALLS = frozenset({"len", "str", "int", "float", "bool", "sum",
                             "sorted", "list", "tuple", "set", "round", "max",
                             "min", "abs", "repr", "format"})

_PORT_REF = re.compile(r"\bself\.ports\.([A-Za-z_][A-Za-z0-9_]*)")


def _port_faults(code: str, target: Target
                 ) -> Tuple[str, List[str], List[str]]:
    """self.ports.X exists and is used as its widget allows; self.<attr> is
    part of the app."""
    faults: List[str] = []
    notes: List[str] = []
    names = target.port_names
    for bad in sorted(set(_PORT_REF.findall(code)) - set(names)):
        close = difflib.get_close_matches(bad, names, n=2, cutoff=0.7)
        if len(close) == 1:
            code = re.sub(rf"\bself\.ports\.{re.escape(bad)}\b",
                          f"self.ports.{close[0]}", code)
            notes.append(f"there is no port {bad}; used {close[0]}")
        else:
            faults.append(f"there is no port {bad!r} (ports: "
                          f"{', '.join(names) or 'none'})")
    by = {p.name: p for p in target.ports}
    tree = ast.parse(code)
    allowed_self = set(SELF_ATTRS) | set(target.handlers)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        holder = node.value
        # self.<attr>
        if isinstance(holder, ast.Name) and holder.id == "self" and \
                node.attr not in allowed_self and \
                not node.attr.startswith("browse_") and \
                not _private_ok(node.attr):
            faults.append(f"line {node.lineno}: self.{node.attr} is not part "
                          f"of the app — use self.ports.<name>, or "
                          f"self.{PRIVATE_PREFIX}<name> to remember a value "
                          f"between clicks")
            continue
        # self.ports.X.<method>
        if not (isinstance(holder, ast.Attribute)
                and isinstance(holder.value, ast.Attribute)
                and holder.value.attr == "ports"
                and isinstance(holder.value.value, ast.Name)
                and holder.value.value.id == "self"):
            continue
        p = by.get(holder.attr)
        if p is None:
            continue
        m = node.attr
        line = node.lineno
        if m == "get" and not p.readable:
            faults.append(f"line {line}: {p.name} is a {p.kind} — it has no "
                          f"value to get()")
        elif m == "set" and p.binder == "event":
            faults.append(f"line {line}: {p.name} is a {p.kind} — it has no "
                          f"value to set(); use .enable(True/False)")
        elif m == "set" and p.writer == "figure_for_drawing":
            faults.append(f"line {line}: {p.name} is a chart — .set(fig) "
                          f"raises TypeError; "
                          + CHART_RECIPE.format(p=p.name))
        elif m == "items" and p.binder != "list":
            faults.append(f"line {line}: {p.name} is a {p.kind}; only a "
                          f"listbox has items()")
        elif m == "rows" and p.binder != "table":
            faults.append(f"line {line}: {p.name} is a {p.kind}; only a table "
                          f"has rows()")
        elif m == "widget" and p.writer != "figure_for_drawing":
            faults.append(f"line {line}: do not reach {p.name}'s Qt widget — "
                          f"use the port's own methods")
        elif m not in ("get", "set", "items", "rows", "widget", "enable",
                       "clear", "on_change", "on_fire", "fire", "name",
                       "kind", "type", "path", "count"):
            faults.append(f"line {line}: ports have no .{m}() — use get(), "
                          f"set(), enable() or clear()")
    return code, faults, notes


# ============================================================
# Gate 4 — names
# ============================================================

def _annotation_nodes(fn: ast.AST) -> set:
    out = set()
    for node in ast.walk(fn):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in (list(a.posonlyargs) + list(a.args)
                        + list(a.kwonlyargs)
                        + [x for x in (a.vararg, a.kwarg) if x]):
                if arg.annotation is not None:
                    anns.append(arg.annotation)
            if node.returns is not None:
                anns.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            out.update(id(n) for n in ast.walk(ann))
    return out


def undefined_names(code: str, extra: Sequence[str] = ()) -> List[Tuple[str,
                                                                        int]]:
    """(name, first line) for every name read in ``code`` that nothing binds.

    Scopes are flattened (a name bound anywhere counts as bound): the cost is
    a missed fault the smoke run then catches, never a false one."""
    tree = ast.parse(code)
    bound = set(extra) | _BUILTINS | {"__file__", "__name__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx,
                                                     (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and \
                getattr(node, "name", None):
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    skip = _annotation_nodes(tree)
    out: List[Tuple[str, int]] = []
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound and id(node) not in skip \
                and node.id not in seen:
            seen.add(node.id)
            out.append((node.id, node.lineno))
    out.sort(key=lambda x: x[1])
    return out


def _used_as_module(tree: ast.AST) -> set:
    """Names every read of which is `name.attr` — the way a module is
    used (`image_stats.image_pixel_stats(path)`)."""
    bases = {id(n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    dotted, bare = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            (dotted if id(n) in bases else bare).add(n.id)
    return dotted - bare


def name_fixes(code: str, target: Target) -> Tuple[str, List[str], List[str]]:
    """Gate 4: (code with safe imports added, faults, notes).

    A name that is a module the app may import, read as `name.function()`,
    gets its import like `np` does — the form a task usually words it in
    ("call image_stats.image_pixel_stats(path)"). Before, only the bare
    `image_pixel_stats(path)` was fixed, and qwen2.5-coder's K4 spent all
    five calls on the dotted one (2026-10-05)."""
    faults: List[str] = []
    notes: List[str] = []
    extra = list(target.module_names) + list(target.handlers)
    missing = undefined_names(code, extra)
    if not missing:
        return code, faults, notes
    by_func: Dict[str, List[str]] = {}
    for r in target.shortlist:
        by_func.setdefault(r.name, []).append(r.module)
    imports: List[str] = []
    tree = ast.parse(code)
    fn = tree.body[0] if tree.body else None
    all_bound = sorted({n.id for n in ast.walk(tree)
                        if isinstance(n, ast.Name)
                        and isinstance(n.ctx, ast.Store)}
                       | {a.arg for a in ast.walk(tree)
                          if isinstance(a, ast.arg)})
    as_module = _used_as_module(tree)
    modules = set(importable_modules(target))
    for name, line in missing:
        if name in AUTO_IMPORTS:
            imports.append(AUTO_IMPORTS[name])
            notes.append(f"added `{AUTO_IMPORTS[name]}`")
        elif len(by_func.get(name, [])) == 1:
            stmt = f"from {by_func[name][0]} import {name}"
            imports.append(stmt)
            notes.append(f"added `{stmt}`")
        elif name in modules and name in as_module:
            stmt = f"import {name}"
            imports.append(stmt)
            notes.append(f"added `{stmt}`")
        else:
            close = difflib.get_close_matches(name, all_bound, n=1,
                                              cutoff=0.8)
            faults.append(f"line {line}: undefined name '{name}'"
                          + (" — it is used as a module but never imported"
                             if name in as_module else "")
                          + (f" — did you mean '{close[0]}'?" if close
                             else ""))
    if imports and fn is not None and isinstance(fn, ast.FunctionDef):
        lines = code.split("\n")
        first = fn.body[0]
        doc = (isinstance(first, ast.Expr)
               and isinstance(first.value, ast.Constant)
               and isinstance(first.value.value, str))
        at = (first.end_lineno if doc else first.lineno - 1)
        if first.lineno <= _header_end(fn):
            return code, faults + [f"line {fn.lineno}: undefined name(s) "
                                   f"{', '.join(n for n, _l in missing)}"], \
                notes
        indent = " " * first.col_offset
        lines[at:at] = [indent + s for s in _dedupe(imports)]
        code = "\n".join(lines)
    return code, faults, notes


# ============================================================
# All the static gates
# ============================================================

def check(raw: str, target: Target,
          catalogue: Optional[Callable[[str], Any]] = None) -> Candidate:
    """Take one reply through gates 0-4. The smoke run is write()'s."""
    t0 = time.perf_counter()
    cand = Candidate(raw=raw if isinstance(raw, str) else str(raw or ""))
    try:
        _check(cand, target, catalogue)
    except Exception as exc:                             # noqa: BLE001
        cand.faults.append(f"the checker failed on this reply: {exc!r}")
    cand.gate_ms = (time.perf_counter() - t0) * 1000
    return cand


def _check(cand: Candidate, target: Target,
           catalogue: Optional[Callable[[str], Any]]) -> None:
    code, unclosed = extract_code(cand.raw)
    if not code.strip():
        cand.stage = STAGE_EXTRACT
        cand.faults = ["the reply contained no Python code — reply with one "
                       "```python block"]
        cand.gates.append(Gate(STAGE_EXTRACT, False, cand.faults[0]))
        return
    tree, code2, dropped = _parse_prefix(code)
    if tree is None:
        try:
            ast.parse(code)
        except SyntaxError as exc:
            cand.code = code
            cand.stage = STAGE_SHAPE
            if _looks_cut(code, exc, unclosed):
                cand.faults = ["the reply stops mid-function — it looks cut "
                               "off; write the whole function, shorter if "
                               "needed"]
            else:
                bad = code.split("\n")[(exc.lineno or 1) - 1] \
                    if exc.lineno else ""
                cand.faults = [f"line {exc.lineno}: {exc.msg}"
                               + (f": {bad.strip()}" if bad.strip() else "")]
            cand.gates.append(Gate(STAGE_EXTRACT, True))
            cand.gates.append(Gate(STAGE_SHAPE, False, cand.faults[0]))
            return
    if dropped:
        cand.notes.append(f"dropped {dropped} line(s) of prose after the code")
    code = code2
    cand.gates.append(Gate(STAGE_EXTRACT, True,
                           "fence was never closed" if unclosed else ""))

    shaper = shape_handler if target.mode == "handler" else shape_function
    code, faults, notes = shaper(code, target)
    cand.code, cand.notes = code, cand.notes + notes
    if faults:
        cand.stage, cand.faults = STAGE_SHAPE, faults
        cand.gates.append(Gate(STAGE_SHAPE, False, "; ".join(faults)))
        return
    cand.gates.append(Gate(STAGE_SHAPE, True, "; ".join(notes)))

    faults = policy_faults(code, target)
    if faults:
        cand.stage, cand.faults = STAGE_POLICY, faults
        cand.gates.append(Gate(STAGE_POLICY, False, "; ".join(faults[:3])))
        return
    cand.gates.append(Gate(STAGE_POLICY, True))

    code, faults, notes = reference_fixes(code, target, catalogue)
    cand.code, cand.notes = code, cand.notes + notes
    if faults:
        cand.stage, cand.faults = STAGE_REFS, faults
        cand.gates.append(Gate(STAGE_REFS, False, "; ".join(faults[:3])))
        return
    cand.gates.append(Gate(STAGE_REFS, True, "; ".join(notes)))

    code, faults, notes = name_fixes(code, target)
    cand.code, cand.notes = code, cand.notes + notes
    if faults:
        cand.stage, cand.faults = STAGE_NAMES, faults
        cand.gates.append(Gate(STAGE_NAMES, False, "; ".join(faults[:3])))
        return
    if notes:
        # An added import is new code: the policy gate reads it too.
        late = policy_faults(code, target)
        if late:
            cand.stage, cand.faults = STAGE_POLICY, late
            cand.gates.append(Gate(STAGE_POLICY, False, "; ".join(late[:3])))
            return
    cand.gates.append(Gate(STAGE_NAMES, True, "; ".join(notes)))
    cand.stage = STAGE_SMOKE


# ============================================================
# The loop
# ============================================================

ModelCall = Callable[..., str]


def _call(model_call: ModelCall, prompt: str, seed: int,
          temperature: float, should_stop: Optional[Callable[[], bool]]
          ) -> str:
    """The model, with seed/temperature/should_stop when it takes them.

    A stub written as `lambda prompt: ...` is a valid model call; so is
    designer_codebehind's, which forwards everything to local_chat."""
    try:
        return model_call(prompt, seed=seed, temperature=temperature,
                          should_stop=should_stop)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
    return model_call(prompt)


def _smoke(cand: Candidate, smoke: Optional[Callable[[Candidate], Any]]
           ) -> None:
    """Gate 5, in place."""
    if smoke is None:
        cand.stage = STAGE_OK
        cand.gates.append(Gate(STAGE_SMOKE, True, "not run (no smoke runner)"))
        return
    try:
        res = smoke(cand)
    except Exception as exc:                             # noqa: BLE001
        res = None
        cand.notes.append(f"the smoke run could not be made: {exc!r}")
    cand.smoke = res
    if res is None or getattr(res, "skipped", ""):
        why = getattr(res, "skipped", "") or "it could not start"
        cand.stage = STAGE_OK
        cand.notes.append(f"not smoke-run: {why}")
        cand.gates.append(Gate(STAGE_SMOKE, True, f"skipped — {why}"))
        return
    if getattr(res, "ok", False):
        cand.stage = STAGE_OK
        cand.notes.extend(getattr(res, "notes", []) or [])
        if getattr(res, "soft", ""):
            cand.notes.append(res.soft)
        cand.gates.append(Gate(STAGE_SMOKE, True, res.summary()))
        return
    cand.stage = STAGE_SMOKE
    cand.faults = list(res.faults()) or ["the smoke run failed"]
    cand.gates.append(Gate(STAGE_SMOKE, False, "; ".join(cand.faults[:3])))


def _accept(res: CodeResult, cand: Candidate) -> None:
    """``cand`` is what write() offers."""
    res.ok = True
    res.code = cand.code
    res.raw = cand.raw
    res.smoke = cand.smoke
    res.notes += cand.notes
    res.gates = [g.line() for g in cand.gates]
    res.best = cand


def default_n_best(params_b: Optional[float]) -> int:
    """Samples in the first round, from the model's size. Small models vary
    most run to run (Phi-4's H1/H3 failed at 3 rounds and passed on a rerun),
    so they get the most draws; a 14B+ model gets one."""
    if params_b is None:
        return 1
    if params_b <= 4.5:
        return 3
    if params_b <= 9.5:
        return 2
    return 1


def write(target: Target, model_call: Optional[ModelCall], *,
          smoke: Optional[Callable[[Candidate], Any]] = None,
          catalogue: Optional[Callable[[str], Any]] = None,
          n_best: int = 1, max_repairs: int = MAX_REPAIRS,
          n_ctx: Optional[int] = None,
          should_stop: Optional[Callable[[], bool]] = None,
          on_progress: Optional[Callable[[str], None]] = None) -> CodeResult:
    """Write the code behind one widget. NEVER RAISES.

    No model -> ok=False and zero calls (the designer must work with no
    model loaded). A model that raises -> ok=False with the exception and
    the best candidate so far. Exhausting the rounds -> ok=False, the best
    candidate's faults, and NO code."""
    res = CodeResult(mode=target.mode, name=target.name)
    t0 = time.perf_counter()
    try:
        _write(res, target, model_call, smoke, catalogue, n_best, max_repairs,
               n_ctx, should_stop, on_progress)
    except Exception as exc:                             # noqa: BLE001
        res.ok = False
        res.code = ""
        res.errors.append(f"the code writer failed unexpectedly: {exc!r}")
    res.seconds = time.perf_counter() - t0
    return res


#: Model-call failures that another call cannot fix: no server, no model.
_FATAL_CALL_ERRORS = ("BackendUnavailable", "LocalChatTimeout")
_FATAL_CALL_TEXT = ("has no model", "not installed", "no ollama server",
                    "no model is loaded", "cuda out of memory")


def _worth_retrying(exc: BaseException, failures: List[str]) -> bool:
    """Whether a failed model call deserves another try. Records it.

    Not when the cause outlives the call (no server, no model, out of
    memory — the next call fails the same way, only later), and not when the
    same failure has just happened twice (it is not going to change)."""
    text = repr(exc)
    failures.append(text)
    if type(exc).__name__ in _FATAL_CALL_ERRORS:
        return False
    if any(t in text.lower() for t in _FATAL_CALL_TEXT):
        return False
    return not (len(failures) >= 2 and failures[-1] == failures[-2])


def _say(on_progress, text: str) -> None:
    if on_progress is not None:
        try:
            on_progress(text)
        except Exception:                                # noqa: BLE001
            pass


def _write(res: CodeResult, target: Target, model_call, smoke, catalogue,
           n_best, max_repairs, n_ctx, should_stop, on_progress) -> None:
    if target.mode not in MODES:
        res.errors = [f"unknown mode {target.mode!r}"]
        return
    if not target.instruction.strip():
        res.errors = ["say what it should do first — the instruction is "
                      "empty"]
        return
    if not target.name.isidentifier():
        res.errors = [f"{target.name!r} is not a valid function name"]
        return
    if target.mode == "function" and not target.outputs:
        res.errors = ["pick at least one output — a port the result is shown "
                      "in"]
        return
    if model_call is None:
        res.errors = ["no model available to write code — load a model, or "
                      "write the function by hand"]
        return
    budget = budget_chars(n_ctx)
    prompt, shed = build_prompt(target, budget)
    res.prompt_chars = len(prompt)
    if len(prompt) > budget:
        # Everything sheddable is gone and it still does not fit: what is
        # left is the instruction (or, in handler mode, the ports it names).
        # Sent anyway, the engine's clamp cuts the MIDDLE out of the one
        # message — the signature — on every call of the loop.
        size = (f"(the prompt would be {len(prompt)} characters; about "
                f"{budget} fit beside the reply)")
        if target.mode == "handler" and \
                len(target.instruction) < budget // 3:
            # A short instruction on a big window: the port list is what
            # does not fit, and it shrinks to the ports the task names.
            res.errors = [f"this window has {len(target.ports)} ports — too "
                          f"many to list in the model's window {size}. Name "
                          f"the widgets it should use in the instruction "
                          f"(only those are then listed), or use Function "
                          f"mode"]
        else:
            res.errors = [f"the instruction is too long for the model's "
                          f"window {size} — say it in a few sentences"]
        return
    if shed:
        res.notes.append("to fit the model's context the prompt left out: "
                         + ", ".join(shed))
    stopped = (lambda: bool(should_stop and should_stop()))
    best: Optional[Candidate] = None
    #: A candidate that passed SOFTLY (a deliberate ValueError or an error
    #: key on the sample data) while first-round samples remain. The sample
    #: data was chosen for this task, so a soft pass is a yellow flag — a
    #: candidate globbing *.tif in a folder of PNGs is one — and a clean
    #: pass from the next seed is preferred. Offered when none comes; never
    #: spent repairs on.
    fallback: Optional[Candidate] = None
    seen: Dict[str, int] = {}
    n_best = max(1, min(int(n_best or 1), len(TEMPERATURES)))
    plan: List[Tuple[str, int, float]] = [
        ("sample", i + 1, TEMPERATURES[i]) for i in range(n_best)]
    plan += [("repair", 100 + i, REPAIR_TEMPERATURE)
             for i in range(max(0, int(max_repairs)))]
    bump = 0.0
    call_failures: List[str] = []
    for step, (kind, seed, temp) in enumerate(plan):
        temp = min(0.9, temp + bump)
        seed = seed + int(bump * 100)
        if stopped():
            res.stopped = True
            res.errors = ["stopped"] + (best.faults if best else [])
            break
        if kind == "repair":
            if fallback is not None:
                _accept(res, fallback)
                return
            if best is None:
                break
            prompt = repair_prompt(target, best, budget)
        _say(on_progress, f"{'candidate' if kind == 'sample' else 'repair'} "
                          f"{res.attempts + 1}: asking the model…")
        c0 = time.perf_counter()
        try:
            raw = _call(model_call, prompt, seed, temp, should_stop)
        except Exception as exc:                         # noqa: BLE001
            res.attempts += 1
            if stopped():
                # The engine answers should_stop() by raising
                # (GenerationCancelled): that is the Stop the user pressed,
                # not a model failure.
                res.stopped = True
                res.errors = ["stopped"]
            elif fallback is not None:
                # A held soft pass passed every gate; a later sample's
                # failure does not take it away.
                fallback.notes.append(f"a further sample was not drawn — "
                                      f"the model call failed: {exc!r}")
                _accept(res, fallback)
                return
            elif _worth_retrying(exc, call_failures) and step + 1 < len(plan):
                # A failure that is about THIS generation (measured: Ollama's
                # "prediction aborted, token repeat limit reached" on the
                # first of three samples) used to end the whole attempt with
                # two samples and the repairs unspent. New dice, next step.
                res.notes.append(f"a model call failed ({exc!r}); trying "
                                 f"again with different settings")
                bump = min(0.6, bump + 0.3)
                continue
            else:
                res.errors = [f"the model call failed: {exc!r}"]
            if best is not None:
                # The best candidate's gate report, as when the rounds run
                # out: a replay that runs out of recorded replies (or a
                # model that stops answering) still says how far it got.
                res.errors += best.faults
                res.raw = best.raw
                res.gates = [g.line() for g in best.gates]
                res.smoke = best.smoke
                res.best = best
            return
        res.attempts += 1
        secs = time.perf_counter() - c0
        digest = hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()
        if digest in seen:
            # The same answer again (Describe's M2 fixture: three rounds of
            # it). The next call gets new dice, since the prompt alone did
            # not move it.
            seen[digest] += 1
            bump = min(0.6, bump + 0.3)
        else:
            seen[digest] = 0
        cand = check(raw, target, catalogue)
        cand.seed, cand.temperature, cand.seconds = seed, temp, secs
        if cand.stage == STAGE_SMOKE and not stopped():
            _say(on_progress, f"smoke-running candidate {res.attempts}…")
            _smoke(cand, smoke)
        res.calls.append({"kind": kind, "seed": seed, "temperature": temp,
                          "seconds": round(secs, 3),
                          "stage": STAGE_NAMES_TEXT[cand.stage],
                          "faults": len(cand.faults),
                          "gate_ms": round(cand.gate_ms, 2),
                          "prompt_chars": len(prompt)})
        _say(on_progress, f"  {STAGE_NAMES_TEXT[cand.stage]}"
                          + ("" if cand.ok else
                             f" — {cand.faults[0] if cand.faults else ''}"))
        if cand.ok:
            more = kind == "sample" and step + 1 < n_best
            if more and getattr(cand.smoke, "soft", ""):
                fallback = fallback or cand
                _say(on_progress, "  passed with a caveat — trying the next "
                                  "sample for a clean pass")
                continue
            _accept(res, cand)
            return
        if best is None or cand.rank() >= best.rank():
            best = cand
    if fallback is not None and not res.stopped:
        _accept(res, fallback)
        return
    res.ok = False
    res.code = ""
    if best is not None:
        if not res.errors:
            res.errors = list(best.faults)
        res.raw = best.raw
        res.notes += best.notes
        res.gates = [g.line() for g in best.gates]
        res.smoke = best.smoke
        res.best = best


# ============================================================
# Grounding: a shortlist of existing functions
# ============================================================

_STOP = frozenset("""a an the and or of to in on for with from by into it its
this that these those is are be as at all any each every when then than show
shows display put get set make the button click press value values result
results file files list number""".split())


def _words(text: str) -> set:
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(text or "")):
        for part in re.split(r"_|(?<=[a-z])(?=[A-Z])", w):
            part = part.lower()
            if len(part) > 2 and part not in _STOP:
                out.add(part)
                if part.endswith("s") and len(part) > 4:
                    out.add(part[:-1])
    return out


def shortlist(query: str, modules: Sequence[Tuple[str, Any]],
              limit: int = SHORTLIST) -> List[Ref]:
    """The ``limit`` functions of ``modules`` (each a (name, ModuleInfo))
    whose names, summaries, parameters and result keys share the most words
    with ``query``. Nothing scores on a word they do not share: an empty
    list is a fine answer, better than six irrelevant signatures a small
    model will dutifully call.

    Words are weighted by how RARE they are across the catalogue (an IDF):
    "folder" is in dozens of signatures and says little; "png" or "csv" is
    in a few and says a lot. Then only entries scoring at least a third of
    the best are kept — measured on "count the png files in the capture
    folder" against the 15 linked modules, unweighted counting put
    frame_camera.status (it shares "capture") third."""
    want = _words(query)
    entries = []
    for module, info in modules:
        if info is None or not getattr(info, "found", False):
            continue
        for fn in info.functions:
            have = _words(" ".join([fn.name, fn.summary, " ".join(fn.params),
                                    " ".join(fn.result_keys)]))
            entries.append((module, info, fn, have, _words(fn.name)))
    df: Dict[str, int] = {}
    for _m, _i, _f, have, _n in entries:
        for w in have:
            df[w] = df.get(w, 0) + 1
    total = max(1, len(entries))

    def idf(word: str) -> float:
        return math.log((total + 1) / (df.get(word, 0) + 1)) + 0.1

    scored: List[Tuple[float, int, Ref]] = []
    order = 0
    for module, info, fn, have, name_words in entries:
        hits = want & have
        if not hits:
            continue
        score = sum(idf(w) for w in hits) + sum(idf(w) for w in
                                               want & name_words)
        if fn.result_keys:
            score *= 1.1
        sig = fn.typed_signature() if hasattr(fn, "typed_signature") \
            else fn.signature()
        ret = str(getattr(fn, "returns", "") or "")
        members = tuple(info.members(ret)) \
            if ret.isidentifier() and hasattr(info, "members") else ()
        scored.append((score, order, Ref(
            module=module, name=fn.name, signature=sig,
            summary=fn.summary, keys=tuple(fn.result_keys),
            params=tuple(fn.params), required=fn.required,
            varargs=fn.varargs, returns_class=ret if members else "",
            members=members)))
        order += 1
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return []
    floor = scored[0][0] / 3.0
    return [r for s, _o, r in scored[:limit] if s >= floor]


# ============================================================
# Splicing into logic.py
# ============================================================

LOGIC_HEADER = '''"""Functions behind this app's widgets.

Written by the GUI Designer's code writer (and by you). A widget calls one
of these through its script link, and Generate writes the handler that does
it. This file is YOURS: the Designer only adds functions, and replaces one
only when it is the model's own, unedited (its fingerprint is in the
manifest). Edit freely — once edited, a function is never rewritten.
"""
from __future__ import annotations
'''


def function_span(source: str, name: str) -> Optional[Tuple[int, int]]:
    """(first, last) line of top-level ``def name``, decorators included."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == name:
            first = min([node.lineno] + [d.lineno for d in
                                         node.decorator_list])
            return first, node.end_lineno
    return None


def function_text(source: str, name: str) -> Optional[str]:
    span = function_span(source, name)
    if span is None:
        return None
    lines = source.splitlines(keepends=True)
    text = "".join(lines[span[0] - 1:span[1]])
    return text if text.endswith("\n") else text + "\n"


def sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def splice_function(source: str, name: str, code: str) -> str:
    """logic.py with ``code`` replacing ``def name`` or appended to it."""
    code = code.rstrip("\n") + "\n"
    if not (source or "").strip():
        return LOGIC_HEADER + "\n\n" + code
    span = function_span(source, name)
    if span is None:
        return source.rstrip("\n") + "\n\n\n" + code
    lines = source.splitlines(keepends=True)
    lines[span[0] - 1:span[1]] = [code]
    return "".join(lines)


def top_level_names(source: str) -> List[str]:
    """Names logic.py binds at its top level (other functions, imports)."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out += [(a.asname or a.name).split(".")[0] for a in node.names
                    if a.name != "*"]
        elif isinstance(node, ast.Assign):
            out += [t.id for t in node.targets if isinstance(t, ast.Name)]
    return out


def unified_diff(before: str, after: str, filename: str) -> str:
    return "".join(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"{filename} (now)", tofile=f"{filename} (proposed)"))
