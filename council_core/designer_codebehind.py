"""
council_core.designer_codebehind — "Write it with the model…", minus the
widgets.

WHAT THIS IS
The project side of gui_codebehind. gui_codebehind is pure: it takes a
Target and an injected model and smoke runner and hands back a checked
function. This module knows the PROJECT: it reads the wireframe into a spec
to find the widget, its handler and every port with its real type; picks the
signature from the ports the user wired; gathers the grounding (a shortlist
of existing functions, documentation when the task names a package); runs
the candidates in gui_smoke's sandbox under the project's OWN interpreter;
and, only after the person has read the diff and pressed Accept, writes.

NOTHING IS WRITTEN UNTIL ACCEPT
plan() and run() read files and nothing else — the smoke run works in a
temp sandbox. apply() is the only writer, and it refuses when the file it
is about to change is not the file the model was shown: the sha256 taken
when the job started is compared with what is on disk now. Then it backs up
(gui_projects.backup, which copies logic.py too), writes a temp file and
os.replace()s it, and records the written text's sha256 in the manifest
(Manifest.ai_handlers). That record is what makes a model-written body
"the generator's own" for as long as nobody edits it: plan_handlers may
then remove it with its widget, and a later write may replace it — and the
moment one character changes, it is the user's, and neither happens.

THE LINK IS SET THE WAY A PERSON SETS IT
Function mode returns the script link (logic.<function>, the inputs, the
outputs); the Designer applies it through the Scene exactly as the Wiring
group's Apply does — undoable, marks the project dirty — so gui_describe's
rule holds: no model-authored import target reaches a project without a
person accepting it.

THE MODEL CALL
role "coder", as Describe uses. seed / stop / should_stop are passed to
council_engine.local_chat and dropped one by one if the engine in this build
does not take them (TypeError), so this works before and after the engine's
structured-output work lands.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: Which council role writes code. Describe uses the same one.
ROLE = "coder"
TIMEOUT = 180
#: How long one smoke run may take, interpreter start included.
SMOKE_TIMEOUT = 8.0

#: The Council's own folder, which a linked project's main.py puts on
#: sys.path — and so does the smoke run.
APP_ROOT = Path(__file__).resolve().parent.parent

MODE_LABELS = {"function": "Function in logic.py (recommended)",
               "handler": "Handler body"}

#: Words that mean "the sample file should be a CSV / an image / text".
_CSV_WORDS = ("csv", "table", "column", "columns", "spreadsheet", "row",
              "rows")
_IMAGE_WORDS = ("image", "images", "frame", "frames", "png", "jpg", "jpeg",
                "picture", "photo", "brightness", "pixel", "pixels")
_TEXT_WORDS = ("text", "log", "notes", "txt")
_OUT_WORDS = ("out", "output", "save", "export", "dest", "destination",
              "target", "result")


@dataclass
class Request:
    """What the user asked for, from the Wiring group."""
    project_dir: Any
    shapes: Sequence[Any]
    shape_id: str
    instruction: str
    mode: str = "function"
    #: Function mode: port names passed in, in order. Empty = the widget's
    #: current link's, else guessed from the instruction.
    inputs: Sequence[str] = ()
    #: Function mode: port -> result key ("" = the port's own name).
    outputs: Dict[str, str] = field(default_factory=dict)
    #: Function mode: the function's name. "" = derived from the label.
    function: str = ""
    #: First-round samples; None = from the model's size.
    n_best: Optional[int] = None


@dataclass
class Plan:
    """Everything decided before a model is asked anything."""
    ok: bool = False
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    request: Optional[Request] = None
    target: Any = None                 # gui_codebehind.Target
    project_dir: Optional[Path] = None
    mode: str = "function"
    shape_id: str = ""
    widget: str = ""
    handler: str = ""
    label: str = ""
    kind: str = ""
    #: The file this writes into, and its text and sha256 when planned.
    file: str = ""
    before: str = ""
    before_sha: str = ""
    #: What the write replaces: "a new function", "the TODO stub", ...
    replacing: str = ""
    #: Function mode: the script link Accept sets on the widget.
    link: Dict[str, Any] = field(default_factory=dict)
    project_mode: str = "linked"
    toolkit: str = "qt"
    python: str = ""
    #: handler mode: the ports, as gui_smoke rows.
    port_rows: List[Dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Review:
    """A finished job: what would be written, and why it may be."""
    plan: Plan
    result: Any                        # gui_codebehind.CodeResult
    after: str = ""
    diff: str = ""
    #: The method/function text as written (wrapped, for a handler).
    written: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.plan.ok and self.result is not None
                    and self.result.ok and self.after)

    def report(self) -> List[str]:
        """The lines the review dialog and the log show."""
        r = self.result
        out = [f"{self.plan.label or self.plan.widget}: "
               f"{'function ' + self.plan.link.get('function', '') + '() in logic.py' if self.plan.mode == 'function' else self.plan.handler + ' in handlers.py'}"
               f" — replaces {self.plan.replacing}"]
        if r is None:
            return out + self.plan.problems
        out.append(f"{r.attempts} model call(s), {r.seconds:.1f} s"
                   + (f", model {self.model}" if self.model else ""))
        out += [f"  {g}" for g in r.gates]
        if r.ok and self.plan.mode == "function":
            link = self.plan.link
            out.append(f"Accept wires it: logic.{link.get('function')}("
                       f"{', '.join(link.get('inputs') or [])}) → "
                       f"{', '.join(f'{p}←{k}' for p, k in (link.get('outputs') or {}).items())}"
                       f" — then Generate rewrites the handler stub")
        for n in r.notes:
            out.append(f"note: {n}")
        if not r.ok:
            out.append("NOT OFFERED — the best attempt still had:")
            out += [f"  - {e}" for e in r.errors]
        return out


@dataclass
class Applied:
    ok: bool
    message: str = ""
    link: Dict[str, Any] = field(default_factory=dict)
    backup: str = ""
    file: str = ""


# ============================================================
# The model call
# ============================================================

def default_model_call(prompt: str, *, seed: Optional[int] = None,
                       temperature: float = 0.2,
                       should_stop: Optional[Callable[[], bool]] = None
                       ) -> str:
    """The code writer's call. Separated so a test never reaches a model.

    seed, stop and should_stop are offered to local_chat and dropped, one
    at a time, when this build's engine does not take them."""
    import council_engine
    import gui_codebehind as gcb
    base = dict(messages=[{"role": "user", "content": prompt}],
                temperature=temperature, num_predict=gcb.NUM_PREDICT,
                timeout=TIMEOUT, role=ROLE)
    extras = {"seed": seed, "stop": list(gcb.STOPS),
              "should_stop": should_stop}
    extras = {k: v for k, v in extras.items() if v is not None}
    while True:
        try:
            return council_engine.local_chat(**base, **extras)
        except TypeError as exc:
            m = re.search(r"unexpected keyword argument '(\w+)'", str(exc))
            if not m or m.group(1) not in extras:
                raise
            extras.pop(m.group(1))


def model_name() -> str:
    """The coder role's model, as the engine last reported it, or ""."""
    try:
        import council_engine
        stats = council_engine.last_call_stats(role=ROLE)
        return str((stats or {}).get("model") or "")
    except Exception:                                    # noqa: BLE001
        return ""


def coder_params_b() -> Optional[float]:
    """The coder role's model size in billions of parameters, or None.

    From the engine's model list when it has one (Ollama /api/show and GGUF
    metadata), else from a "3.8B" / "8b" in the file or model name."""
    ident = ""
    try:
        from council_core import model_slots
        cfg = model_slots.current()
        slot = cfg.slots.get(cfg.slot_for(ROLE))
        if slot is not None:
            ident = slot.resolved_path(os.environ.get("COUNCIL_GGUF_PATH", ""))
    except Exception:                                    # noqa: BLE001
        ident = ""
    try:
        import council_engine
        for m in council_engine.list_local_models() or []:
            if ident and m.get("id") in (ident, f"ollama:{ident}"):
                if m.get("params_b"):
                    return float(m["params_b"])
    except Exception:                                    # noqa: BLE001
        pass
    found = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])",
                      Path(ident).name if ident else "")
    return float(found.group(1)) if found else None


def n_ctx_for_coder() -> Optional[int]:
    """The coder slot's real window, when the engine is already loaded. Never
    imports council_engine just to ask: a stub-model test must not pay for
    it, and gui_codebehind's 4096 default is the safe answer."""
    engine = sys.modules.get("council_engine")
    if engine is None:
        return None
    try:
        from council_core import model_slots
        return int(engine.effective_n_ctx(model_slots.slot_for_role(ROLE)))
    except Exception:                                    # noqa: BLE001
        return None


# ============================================================
# Documentation (the DOCS track's docs_qa, when it is there)
# ============================================================

def packages_named(instruction: str, requires: Sequence[str]) -> List[str]:
    """Third-party packages the instruction names: the project's declared
    ones, the always-allowed four, and anything written as `import x` or
    `x.something(`."""
    import gui_policy
    text = str(instruction or "")
    low = text.lower()
    cands = list(gui_policy.as_requires(requires)) + [
        "numpy", "pandas", "PIL", "pillow", "matplotlib"]
    found = [c for c in cands
             if re.search(rf"\b{re.escape(c.lower())}\b", low)]
    for m in re.finditer(r"\bimport\s+([A-Za-z_][\w]*)|\b([A-Za-z_]\w*)\.\w+\(",
                         text):
        name = m.group(1) or m.group(2)
        if name and name not in found and name not in (
                sys.stdlib_module_names if hasattr(sys, "stdlib_module_names")
                else ()) and not gui_policy.is_council_module(name) \
                and name not in ("self", "np", "pd", "plt"):
            found.append(name)
    out: List[str] = []
    for f in found:
        f = "PIL" if f.lower() == "pillow" else f
        if f not in out:
            out.append(f)
    return out


def docs_for(instruction: str, requires: Sequence[str],
             max_chars: int = 3000) -> Tuple[List[Dict[str, str]], str]:
    """(snippets, a note) from council_core.docs_qa when the task names a
    package and that module exists. Never raises; [] is the normal answer."""
    packages = packages_named(instruction, requires)
    if not packages:
        return [], ""
    try:
        from council_core import docs_qa
    except Exception:                                    # noqa: BLE001
        return [], ""
    try:
        got = docs_qa.docs_context(instruction, packages=tuple(packages),
                                   max_chars=int(max_chars))
    except TypeError:
        try:
            got = docs_qa.docs_context(instruction)
        except Exception:                                # noqa: BLE001
            return [], ""
    except Exception as exc:                             # noqa: BLE001
        return [], f"documentation lookup failed: {exc!r}"
    out = []
    for s in got or []:
        if isinstance(s, dict) and str(s.get("text") or "").strip():
            out.append({k: str(s.get(k) or "") for k in
                        ("server", "source", "title", "text")})
    note = (f"documentation: {len(out)} excerpt(s) for "
            f"{', '.join(packages)}" if out else "")
    return out, note


# ============================================================
# Plan
# ============================================================

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


_GENERIC = frozenset({"go", "run", "ok", "start", "apply", "button", "do",
                      "click", "press", "submit", "execute", "action"})
_FILLER = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "on",
                     "for", "with", "from", "into", "it", "its", "this",
                     "that", "show", "put", "then", "all", "each", "every",
                     "when", "my", "me", "please"})


def function_name(label: str, instruction: str, widget: str) -> str:
    """count_images for a button "Count images"; from the instruction when
    the label says nothing ("Go"); the widget's name as a last resort."""
    base = _slug(label)
    if not base or base in _GENERIC or len(base) < 4:
        words = [w for w in re.findall(r"[a-z0-9]+", instruction.lower())
                 if w not in _FILLER][:3]
        base = "_".join(words)
    if not base:
        base = f"{widget}_logic"
    if base[0].isdigit():
        base = f"n{base}"
    base = base[:48].rstrip("_") or "logic_function"
    import builtins
    import keyword
    # `def list(...)` or `def sum(...)` would shadow the builtin inside
    # logic.py for every other function in it.
    if keyword.iskeyword(base) or hasattr(builtins, base):
        base += "_fn"
    return base


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in _FILLER}


def _mentions(instruction: str, port: Any, label: str) -> bool:
    """Whether the instruction names this port — its name, its widget's
    label, or a word of either (whole words: a port "go" is not "going")."""
    text = str(instruction or "").lower()

    def says(phrase: str) -> bool:
        phrase = phrase.lower().strip()
        return bool(phrase) and re.search(
            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
    name_words = set(port.name.lower().split("_")) - _FILLER
    return bool(says(port.name) or says(label or "")
                or (_words(instruction) & name_words))


def _sample_for(port: Any, widget: Any, instruction: str) -> Any:
    """What a smoke run passes for this port (gui_smoke.SAMPLE_TOKENS or a
    literal)."""
    props = dict(getattr(widget, "props", None) or {}) if widget else {}
    # Whole WORDS, the widget's own first: "csv_file" is a CSV even when
    # the instruction also mentions an image, and "layout" is not "out".
    named = set(re.findall(r"[a-z]+", f"{port.name} "
                           f"{getattr(widget, 'label', '') or ''}".lower()))
    said = set(re.findall(r"[a-z]+", str(instruction or "").lower()))

    def file_token() -> str:
        for pool in (named, said):
            if pool & set(_CSV_WORDS):
                return "<CSV>"
            if pool & set(_IMAGE_WORDS):
                return "<PNG>"
            if pool & set(_TEXT_WORDS):
                return "<TXT>"
        return "<PNG>"
    if port.kind == "file_picker":
        mode = str(props.get("mode") or "file")
        if mode == "save":
            return "<SAVE>"
        if mode == "folder":
            return "<OUT>" if named & set(_OUT_WORDS) else "<FOLDER>"
        return file_token()
    if port.type == "path" or (port.kind == "entry" and named & {
            "folder", "dir", "path", "file"}):
        if named & set(_OUT_WORDS):
            return "<OUT>"
        if "file" in named:
            return file_token()
        return "<FOLDER>"
    if port.binder == "list":
        return ["alpha", "beta"]
    if port.binder == "table":
        return [["a", "1"], ["b", "2"]]
    if port.binder == "tab":
        return 0
    if port.kind == "radiobutton":
        return port.default if port.default is not None else (
            port.choices[0] if port.choices else "a")
    if port.kind == "combobox":
        values = list(props.get("values") or [])
        return str(values[0]) if values else "sample"
    if port.kind in ("spinbox", "scale"):
        lo, hi = props.get("from_", 0), props.get("to", 100)
        try:
            mid = (float(lo) + float(hi)) / 2
        except (TypeError, ValueError):
            mid = 3
        return int(mid) if port.type == "int" else float(mid)
    if port.type == "int":
        return 3
    if port.type == "float":
        return 0.5
    if port.type == "bool" or port.kind == "checkbutton":
        return True
    if port.kind == "text":
        return "sample text\nsecond line"
    return "sample"


def _param_type(port: Any) -> str:
    import gui_codebehind as gcb
    if port.binder in ("list", "table"):
        return "list"
    if port.binder == "tab":
        return "int"
    return gcb.PARAM_TYPES.get(port.type, "str")


def _param_what(port: Any, widget: Any) -> str:
    label = (getattr(widget, "label", "") or port.name) if widget else \
        port.name
    lab = f'"{label}"'
    props = dict(getattr(widget, "props", None) or {}) if widget else {}
    if port.kind == "file_picker":
        mode = str(props.get("mode") or "file")
        return {"folder": f"a folder path the user picked in {lab}",
                "save": f"a file path the user chose to SAVE to in {lab} — "
                        f"the only place it may write",
                }.get(mode, f"a file path the user picked in {lab}")
    if port.binder == "list":
        return f"the list of items selected in {lab} (list of str)"
    if port.binder == "table":
        return f"the selected rows of the table {lab} (list of tuples)"
    if port.binder == "tab":
        return "the index of the open tab"
    if port.kind == "checkbutton":
        return f"True/False from the checkbox {lab}"
    if port.kind in ("combobox", "radiobutton"):
        opts = list(props.get("values") or []) if port.kind == "combobox" \
            else list(port.choices)
        return (f"the option chosen in {lab}"
                + (f" (one of: {', '.join(map(str, opts[:8]))})"
                   if opts else ""))
    if port.kind in ("spinbox", "scale"):
        return f"a number from the {port.kind} {lab}"
    if port.type in ("int", "float"):
        return f"a number the user typed in {lab}"
    if port.type == "path":
        return f"a path the user typed in {lab}"
    if port.kind == "text":
        return f"the text in {lab}"
    return f"the text in {lab}"


#: For guessing a signature from the instruction alone: kinds a person sets
#: (passed in) and kinds that show a result (filled).
_INPUT_KINDS = frozenset({"entry", "checkbutton", "radiobutton", "combobox",
                          "spinbox", "scale", "file_picker", "scrubber",
                          "notebook"})
_OUTPUT_KINDS = frozenset({"label", "listbox", "treeview", "text",
                           "progressbar", "image_canvas", "log_pane",
                           "status_bar"})

_CHECKS = {"label": "text", "entry": "text", "text": "text",
           "status_bar": "text", "log_pane": "text", "combobox": "text",
           "listbox": "list", "treeview": "rows", "image_canvas": "image",
           "progressbar": "number", "spinbox": "number", "scale": "number",
           "checkbutton": "bool", "notebook": "number"}


def _output_what(port: Any, widget: Any) -> str:
    label = (getattr(widget, "label", "") or port.name) if widget else \
        port.name
    lab = f'"{label}"'
    return {
        "log_pane": f"text, appended as a line to the log {lab}",
        "status_bar": f"text, shown in the status bar {lab}",
        "listbox": f"a list of strings, shown as the items of {lab}",
        "treeview": f"a list of rows (each a tuple of cell values), shown in "
                    f"the table {lab}",
        "image_canvas": f"a PIL.Image (or numpy array) shown in {lab} — "
                        f"never a file path",
        "progressbar": f"a number 0-100 shown by the progress bar {lab}",
        "checkbutton": f"True/False, ticks the checkbox {lab}",
        "spinbox": f"a number, shown in {lab}",
        "scale": f"a number, shown in {lab}",
    }.get(port.kind, f"shown as text in {lab}")


def plan(request: Request) -> Plan:
    """Decide everything a model will not: the widget, the signature, the
    grounding, the file and what in it may be replaced. Reads files; writes
    nothing. Never raises — a problem is a Plan with ok=False."""
    t0 = time.perf_counter()
    out = Plan(request=request, mode=request.mode, shape_id=request.shape_id)
    try:
        _plan(out, request)
    except Exception as exc:                             # noqa: BLE001
        out.ok = False
        out.problems.append(f"could not plan the code: {exc!r}")
    out.seconds = time.perf_counter() - t0
    return out


def _plan(out: Plan, req: Request) -> None:
    import gui_codebehind as gcb
    import gui_emit
    import gui_layout
    import gui_policy
    import gui_projects
    import gui_shapes
    import gui_spec
    import python_envs

    from . import designer_wiring

    if req.mode not in gcb.MODES:
        out.problems.append(f"unknown mode {req.mode!r}")
        return
    if not str(req.instruction or "").strip():
        out.problems.append("Say what it should do first — the instruction "
                            "is empty.")
        return
    if not req.project_dir:
        out.problems.append("Open or create a project first.")
        return
    pdir = Path(req.project_dir)
    out.project_dir = pdir
    manifest = gui_projects.load_manifest(pdir)
    if manifest.detached:
        out.problems.append("This project is detached — the Designer no "
                            "longer writes its code.")
        return
    project = gui_shapes.load_gspec(pdir / gui_projects.GSPEC_NAME)
    shapes = list(req.shapes)
    tree = gui_layout.infer(shapes, project.canvas.w, project.canvas.h)
    spec = gui_spec.build(
        shapes, tree, None, registry=manifest.widget_names,
        port_registry=manifest.port_names or {}, project=manifest.name,
        mode=manifest.mode, title=project.window.title,
        requires=getattr(project, "requires", []) or [])
    w = spec.by_shape(req.shape_id)
    if w is None:
        out.problems.append("Select the widget to write code for.")
        return
    out.widget, out.handler, out.kind = w.name, w.handler or "", w.kind
    out.label = w.label or w.name
    if w.kind not in gui_spec.COMMAND_KINDS:
        out.problems.append(f"A {w.kind} does not run code — only buttons "
                            f"and other controls that are pressed or "
                            f"changed do.")
        return
    out.project_mode = manifest.mode or "linked"
    out.toolkit = gui_projects.toolkit_for(pdir)
    resolved = python_envs.resolve(manifest.python)
    out.python = resolved.python or ""
    if resolved.error:
        out.notes.append(f"the smoke run is skipped: {resolved.error}")
    requires = list(spec.requires)
    local = sorted(set(gui_policy.project_modules(pdir)) | {gcb.MODULE})
    by_sid = {s.id: s for s in shapes}
    port_by = {p.name: p for p in spec.ports}

    def widget_of(port):
        return spec.by_shape(port.shape_ids[0]) if port.shape_ids else None

    rows = []
    for p in spec.ports:
        wd = widget_of(p)
        rows.append(gcb.PortRow(name=p.name, kind=p.kind, type=p.type,
                                binder=p.binder, writer=p.writer,
                                label=(wd.label if wd else "") or "",
                                sample=_sample_for(p, wd, req.instruction)))
    out.port_rows = [dict(name=r.name, kind=r.kind, type=r.type,
                          binder=r.binder, writer=r.writer, sample=r.sample)
                     for r in rows]
    target = gcb.Target(mode=req.mode, instruction=req.instruction.strip(),
                        label=out.label, kind=w.kind,
                        project_mode=out.project_mode, toolkit=out.toolkit,
                        requires=requires, local_modules=local)

    if req.mode == "function":
        _plan_function(out, req, target, spec, w, port_by, widget_of,
                       manifest, pdir)
    else:
        _plan_handler(out, req, target, spec, w, rows, manifest, pdir,
                      gui_emit)
    if out.problems:
        return

    # ---- grounding -------------------------------------------------
    modules = []
    if out.project_mode == "linked":
        modules += sorted(gui_policy.LINKED_MODULES)
    modules += [m for m in local if m not in gui_policy.PROJECT_MODULES]
    infos = []
    for m in modules:
        root = pdir if m in local else None
        info = designer_wiring.module_info(m, root)
        if m == gcb.MODULE and req.mode == "function":
            # Not the function being written: "call it rather than
            # rewriting it" about itself is a recursion invitation.
            import dataclasses
            info = dataclasses.replace(info, functions=tuple(
                f for f in info.functions if f.name != target.name))
        infos.append((m, info))
    # What the task SAYS, not the port names: an output port called
    # capture_status pulled frame_camera.status() to the top of the list
    # (measured on Typhon) — a word the result is shown in is not a thing
    # to call.
    target.shortlist = gcb.shortlist(f"{req.instruction} {out.label}", infos)
    budget = gcb.budget_chars(n_ctx_for_coder())
    docs, note = docs_for(req.instruction, requires,
                          max_chars=min(6000, budget // 3))
    target.docs = docs
    if note:
        out.notes.append(note)
    out.target = target
    out.ok = True


def _plan_function(out: Plan, req: Request, target: Any, spec: Any, w: Any,
                   port_by: Dict[str, Any], widget_of, manifest: Any,
                   pdir: Path) -> None:
    import gui_codebehind as gcb
    from . import designer_wiring

    link = designer_wiring.normalise(w.script)
    inputs = [p for p in (req.inputs or link.get("inputs") or [])]
    outputs = dict(req.outputs or link.get("outputs") or {})

    def named(p) -> bool:
        wd = widget_of(p)
        return _mentions(req.instruction, p, wd.label if wd else "")
    # Guessed only with no rows and no link: the widgets the instruction
    # names, an input kind passed in, a display kind filled.
    if not req.inputs and not link:
        inputs = [p.name for p in spec.ports if p.kind in _INPUT_KINDS
                  and ((p.shape_ids and p.shape_ids[0] == w.shape_id)
                       or named(p))]
    if not outputs and not link:
        outputs = {p.name: p.name for p in spec.ports
                   if p.kind in _OUTPUT_KINDS and p.name not in inputs
                   and named(p)}
        if not outputs:
            out.problems.append(
                "Choose where the result is shown: add an output row (port ← "
                "key) in the Wiring group, or name the widget in the "
                "instruction.")
            return
    params = []
    for name in inputs:
        p = port_by.get(name)
        if p is None:
            out.problems.append(f"Input {name!r} is not a port in this "
                                f"project.")
            continue
        if p.binder in ("proxy", "event"):
            out.problems.append(f"Input {name!r} is a {p.kind}, which has no "
                                f"value to pass in.")
            continue
        wd = widget_of(p)
        params.append(gcb.Param(name=name, type=_param_type(p), port=name,
                                kind=p.kind, what=_param_what(p, wd),
                                sample=_sample_for(p, wd, req.instruction)))
    outs = []
    for port, key in outputs.items():
        p = port_by.get(port)
        if p is None:
            out.problems.append(f"Output {port!r} is not a port in this "
                                f"project.")
            continue
        if p.binder == "event" or p.writer == "figure_for_drawing":
            out.problems.append(f"Output {port!r} is a {p.kind}, which "
                                f"cannot show a result.")
            continue
        key = str(key or "").strip() or port
        if not key.isidentifier():
            key = re.sub(r"\W+", "_", key).strip("_") or port
        outs.append(gcb.Output(key=key, port=port, kind=p.kind,
                               check=_CHECKS.get(p.kind, "any"),
                               what=_output_what(p, widget_of(p))))
    if out.problems:
        return
    logic = pdir / gcb.LOGIC_FILE
    before = logic.read_text(encoding="utf-8", errors="replace") \
        if logic.is_file() else ""
    if _parse_problem(before, gcb.LOGIC_FILE, out):
        return
    taken = set(gcb.top_level_names(before))
    ai = dict(getattr(manifest, "ai_handlers", {}) or {})
    wanted = (req.function.strip()
              or (link.get("function") if link.get("module") == gcb.MODULE
                  else "")
              or function_name(out.label, req.instruction, w.name))
    import keyword
    # "class" is an identifier to isidentifier() and `def class(` never
    # parses: every candidate would fail the shape gate.
    if not wanted.isidentifier() or keyword.iskeyword(wanted):
        out.problems.append(f"{wanted!r} is not a valid function name.")
        return
    name, replacing = wanted, "nothing — a new function in logic.py"
    if wanted in taken:
        text = gcb.function_text(before, wanted)
        rec = ai.get(f"{gcb.MODULE}.{wanted}") or {}
        # Unedited model output is replaceable only for the widget that
        # uses it: two buttons labelled alike derive the same name, and
        # replacing the other one's function (with another signature)
        # breaks that button's link the moment this one is accepted.
        users = [o.label or o.name for o in spec.widgets
                 if o is not w and _links_to(o.script, wanted)]
        if text is not None and rec.get("sha256") == gcb.sha(text) \
                and not users:
            replacing = f"the model's earlier {wanted}() (unedited)"
        else:
            i = 2
            while f"{wanted}_{i}" in taken:
                i += 1
            name = f"{wanted}_{i}"
            out.notes.append(
                f"another widget ({', '.join(users[:3])}) is wired to it — "
                f"{wanted}() in logic.py stays as it is, and this one is "
                f"{name}()" if users else
                f"logic.py already has a {wanted}() that the model did not "
                f"write (or that was edited), so this one is {name}()")
    target.name = name
    target.params = params
    target.outputs = outs
    target.module_names = [n for n in taken if n != name]
    out.file = gcb.LOGIC_FILE
    out.before = before
    out.before_sha = gcb.sha(before)
    out.replacing = replacing
    out.link = {"module": gcb.MODULE, "function": name,
                "inputs": [p.name for p in params],
                "outputs": {o.port: o.key for o in outs}}


def _parse_problem(source: str, filename: str, out: Plan) -> bool:
    """Refuse a file that does not parse before any model is asked: every
    candidate is spliced into it and smoke-run with it, so each would fail
    on a line the model was never shown, and the repairs would be spent on
    it."""
    try:
        ast.parse(source or "")
    except SyntaxError as exc:
        out.problems.append(f"{filename} does not parse (line {exc.lineno}: "
                            f"{exc.msg}) — fix it first; the model's code "
                            f"goes into that file.")
        return True
    return False


def _links_to(script: Any, function: str) -> bool:
    """Whether a widget's script link calls logic.<function>."""
    import gui_codebehind as gcb
    from . import designer_wiring
    link = designer_wiring.normalise(script)
    return link.get("module") == gcb.MODULE and \
        link.get("function") == function


def _plan_handler(out: Plan, req: Request, target: Any, spec: Any, w: Any,
                  rows: List[Any], manifest: Any, pdir: Path,
                  gui_emit: Any) -> None:
    import gui_codebehind as gcb
    if w.script:
        out.problems.append(
            f"{out.label} is wired to {gui_emit.link_text(w.script)}, and "
            f"Generate writes its handler from that link. Use Function mode, "
            f"or remove the wiring first.")
        return
    handlers = pdir / "handlers.py"
    if not handlers.is_file():
        out.problems.append("Generate the project first — handlers.py does "
                            "not exist yet.")
        return
    before = handlers.read_text(encoding="utf-8", errors="replace")
    if _parse_problem(before, "handlers.py", out):
        return
    name = w.handler
    copies = gui_emit._handler_methods(ast.parse(before))[0].get(name, [])
    if len(copies) > 1:
        # handler_text() is None for this, which would read as "new" — and
        # a third copy appended last would win over the user's.
        out.problems.append(
            f"{name} is defined more than once in handlers.py (lines "
            f"{', '.join(str(a) for a, _b in copies)}) — keep one, then "
            f"try again.")
        return
    current = gui_emit.handler_text(before, name)
    ai = dict(getattr(manifest, "ai_handlers", {}) or {})
    if current is None:
        replacing = f"nothing — {name} is new"
    else:
        rec = ai.get(name) or {}
        if rec.get("sha256") == gcb.sha(current):
            replacing = f"the model's earlier {name} (unedited)"
        elif gui_emit.is_generated_stub(name, before):
            replacing = f"the untouched stub {name}"
        else:
            out.problems.append(
                f"{name} in handlers.py has been edited by hand, so the "
                f"Designer will not rewrite it. Use Function mode, or delete "
                f"the method to start over.")
            return
    target.name = name
    target.ports = rows
    target.handlers = sorted(set(spec.handlers) | {"on_close"})
    out.file = "handlers.py"
    out.before = before
    out.before_sha = gcb.sha(before)
    out.replacing = replacing


# ============================================================
# Run
# ============================================================

def wrap_handler(code: str, name: str, label: str, instruction: str,
                 model: str = "") -> str:
    """The method as it goes into handlers.py: indented into HandlerMixin,
    with the docstring that says who wrote it and the same failure envelope
    handler_stub writes — clear what it fills, report_error in the window."""
    import textwrap
    tree = ast.parse(code)
    fn = tree.body[0]
    lines = code.rstrip("\n").split("\n")
    first = fn.body[0]
    doc = ""
    start = first.lineno
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        doc = " ".join(first.value.value.split())
        start = first.end_lineno + 1
    body = "\n".join(lines[start - 1:]) if start <= len(lines) else ""
    body = textwrap.dedent(body).strip("\n")
    if not body.strip():
        body = "pass"
    filled = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set"
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Attribute)
                and node.func.value.value.attr == "ports"):
            port = node.func.value.attr
            if port not in filled:
                filled.append(port)
    # No double quotes or backslashes inside the docstring: a '"' at the end
    # would close it early, and "C:\new" would be an escape sequence.
    said = " ".join(str(instruction or "").split()).replace('"', "'")
    said = said.replace("\\", "/")
    doc = doc.replace('"', "'").replace("\\", "/")
    who = f"the local model ({model})" if model else "the local model"
    out = [f"    def {name}(self, *args) -> None:",
           f'        """Written by {who} from: "{said[:300]}".']
    if doc and doc.lower() not in said.lower():
        out += ["", f"        {doc[:200]}"]
    out += ["",
            "        UNREVIEWED — edit freely; once edited, the Designer never",
            '        rewrites it."""',
            "        try:"]
    out += [("            " + ln) if ln.strip() else "" for ln in
            body.split("\n")]
    out += ["        except Exception as exc:"]
    if filled:
        out.append("            self.clear_ports("
                   + ", ".join(repr(p) for p in filled) + ")")
    out.append(f"            self.report_error({label or name!r}, exc)")
    return "\n".join(out) + "\n"


def splice_handler(source: str, name: str, method: str) -> str:
    """handlers.py with ``method`` replacing ``name`` or added as the last
    method of the class that holds the handlers (HandlerMixin).

    Into the CLASS, not onto the end of the file: after a module-level
    helper the user added below the class, an indented def appended at the
    end becomes a function nested inside that helper — it parses, passes
    policy, and the button does nothing (_propose re-checks this)."""
    import gui_emit
    import ast as _ast
    tree = _ast.parse(source)
    spans, _nodes = gui_emit._handler_methods(tree)
    if name in spans and len(spans[name]) == 1:
        a, b = spans[name][0]
        return gui_emit._spliced(source, [(a, b, method)])
    classes = [n for n in tree.body if isinstance(n, _ast.ClassDef)]
    holder = next((c for c in classes if c.name == "HandlerMixin"),
                  classes[0] if classes else None)
    if holder is None:
        text = source if source.endswith("\n") else source + "\n"
        return text + "\n" + method
    lines = source.splitlines(keepends=True)
    head = "".join(lines[:holder.end_lineno])
    head = head if head.endswith("\n") else head + "\n"
    return head + "\n" + method + "".join(lines[holder.end_lineno:])


def _smoke_runner(p: Plan) -> Callable[[Any], Any]:
    """gui_codebehind's gate 5 for this project: the candidate, spliced into
    a COPY of its file, run in gui_smoke's sandbox."""
    import gui_codebehind as gcb
    import gui_smoke

    from . import designer_wiring

    root = APP_ROOT if p.project_mode == "linked" else None
    target = p.target
    # The project's own modules go into the sandbox too: logic.py (or a
    # handler) may import a helper the user wrote beside it, and a smoke
    # run without it would fail on an import the real app satisfies.
    own: Dict[str, str] = {}
    for m in designer_wiring.local_modules(p.project_dir):
        path = p.project_dir / f"{m}.py"
        if path.is_file():
            own[path.name] = path.read_text(encoding="utf-8",
                                            errors="replace")

    def run(cand: Any) -> Any:
        if not p.python:
            res = gui_smoke.SmokeResult(skipped="no interpreter for this "
                                                "project")
            return res
        if p.mode == "function":
            after = gcb.splice_function(p.before, target.name, cand.code)
            fakes = gui_smoke.fakes_for(gui_smoke.imported_roots(after), root)
            return gui_smoke.smoke_function(
                {**own, gcb.LOGIC_FILE: after}, target.name,
                [x.sample for x in target.params],
                {o.key: o.check for o in target.outputs},
                app_root=root, python=p.python, fakes=fakes,
                timeout=SMOKE_TIMEOUT)
        method = wrap_handler(cand.code, target.name, p.label,
                              target.instruction)
        after = splice_handler(p.before, target.name, method)
        files = dict(own)
        fakes = gui_smoke.fakes_for(gui_smoke.imported_roots(method), root)
        return gui_smoke.smoke_handler(after, target.name, p.port_rows,
                                       app_root=root, python=p.python,
                                       files=files, fakes=fakes,
                                       timeout=SMOKE_TIMEOUT)
    return run


def _catalogue(p: Plan) -> Callable[[str], Any]:
    from . import designer_wiring
    local = set(p.target.local_modules) if p.target else set()

    def look(module: str) -> Any:
        root = p.project_dir if module.split(".")[0] in local else None
        return designer_wiring.module_info(module, root)
    return look


def run(p: Plan, *, model_call: Optional[Callable[..., str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        smoke: bool = True, n_best: Optional[int] = None,
        n_ctx: Optional[int] = None) -> Review:
    """Ask the model, gate every candidate, and build the diff. Writes
    NOTHING to the project. Never raises."""
    import gui_codebehind as gcb
    if not p.ok or p.target is None:
        res = gcb.CodeResult(mode=p.mode, errors=list(p.problems))
        return Review(p, res)
    try:
        call = model_call or default_model_call
        if n_best is None:
            n_best = (p.request.n_best if p.request and p.request.n_best
                      else gcb.default_n_best(coder_params_b()
                                              if model_call is None else None))
        result = gcb.write(
            p.target, call, smoke=_smoke_runner(p) if smoke else None,
            catalogue=_catalogue(p), n_best=n_best,
            n_ctx=n_ctx or (n_ctx_for_coder() if model_call is None
                            else None),
            should_stop=should_stop, on_progress=on_progress)
        result.notes = list(p.notes) + list(result.notes)
        review = Review(p, result)
        if model_call is None:
            review.model = model_name()
        if result.ok:
            _propose(review)
        return review
    except Exception as exc:                             # noqa: BLE001
        res = gcb.CodeResult(mode=p.mode,
                             errors=[f"the code writer failed: {exc!r}"])
        return Review(p, res)


def _propose(review: Review) -> None:
    """The whole file as it would be, its diff, and one more policy pass over
    the result — the gate Run will apply to it."""
    import gui_codebehind as gcb
    import gui_policy
    p, r = review.plan, review.result
    if p.mode == "function":
        after = gcb.splice_function(p.before, p.target.name, r.code)
        written = gcb.function_text(after, p.target.name) or r.code
    else:
        written = wrap_handler(r.code, p.target.name, p.label,
                               p.target.instruction, review.model)
        after = splice_handler(p.before, p.target.name, written)
    try:
        ast.parse(after)
    except SyntaxError as exc:
        r.ok = False
        r.errors = [f"the spliced {p.file} does not parse: line {exc.lineno}: "
                    f"{exc.msg}"]
        return
    if p.mode == "handler":
        import gui_emit
        if gui_emit.handler_text(after, p.target.name) is None:
            # Not a method of the window's class once spliced (no class to
            # put it in): written, it would never run.
            r.ok = False
            r.errors = [f"{p.target.name} would not be a method of the "
                        f"window's class in {p.file} — nothing to write"]
            return
    ok, errs = gui_policy.validate(
        after, p.project_mode,
        gui_policy.as_requires(p.target.requires) + p.target.local_modules,
        toolkit=p.toolkit)
    if not ok:
        r.ok = False
        r.errors = [f"{p.file}: {e}" for e in errs]
        return
    review.after = after
    review.written = written
    review.diff = gcb.unified_diff(p.before, after, p.file)


# ============================================================
# Apply — the only writer
# ============================================================

def apply(review: Review) -> Applied:
    """Write the accepted code. Refuses if the file changed since the model
    was shown it. Backs up first, writes atomically, records the sha256.
    Never raises."""
    try:
        return _apply(review)
    except Exception as exc:                             # noqa: BLE001
        return Applied(False, f"not written: {exc!r}")


def _apply(review: Review) -> Applied:
    import gui_codebehind as gcb
    import gui_emit
    import gui_projects
    p = review.plan
    if not review.ok:
        return Applied(False, "nothing to apply — the code did not pass")
    path = p.project_dir / p.file
    now = path.read_text(encoding="utf-8", errors="replace") \
        if path.is_file() else ""
    if gcb.sha(now) != p.before_sha:
        return Applied(False, f"{p.file} changed since the model started — "
                              f"nothing was written. Run it again.")
    manifest = gui_projects.load_manifest(p.project_dir)
    backup = gui_projects.backup(p.project_dir)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(review.after, encoding="utf-8")
    os.replace(tmp, path)
    after = path.read_text(encoding="utf-8")
    if p.mode == "function":
        key = f"{gcb.MODULE}.{p.target.name}"
        text = gcb.function_text(after, p.target.name) or ""
    else:
        key = p.target.name
        text = gui_emit.handler_text(after, p.target.name) or ""
    record = {"kind": p.mode, "file": p.file, "sha256": gcb.sha(text),
              "widget": p.shape_id, "instruction": p.target.instruction[:500],
              "model": review.model or "",
              "written": time.strftime("%Y-%m-%d %H:%M:%S")}
    manifest.ai_handlers = dict(getattr(manifest, "ai_handlers", {}) or {})
    manifest.ai_handlers[key] = record
    gui_projects.save_manifest(p.project_dir, manifest)
    if p.mode == "function":
        msg = (f"wrote {p.target.name}() into logic.py (backup in "
               f"{backup.name}) — wired {p.label} to it; Save, then Generate "
               f"to rewrite its handler")
        return Applied(True, msg, dict(p.link), str(backup), str(path))
    return Applied(True, f"wrote {p.target.name} into handlers.py (backup in "
                         f"{backup.name})", {}, str(backup), str(path))
