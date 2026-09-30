"""
council_core.designer_wiring — what a button RUNS, decided with no toolkit.

WHAT THIS IS
The Designer's wiring editor, minus the widgets. A wired button carries a
`script` link in the .gspec —

    {"module": "frame_camera", "function": "start",
     "inputs": ["capture_folder", "exposure", "gain", "frame_rate"],
     "outputs": {"capture_status": "summary"}}

— and Generate turns it into a handler that calls start() with those ports'
values and writes result["summary"] into the capture_status port. Until now
the only way to make or change one was to edit the JSON by hand, because the
Properties panel had no rows for it at all. This module answers everything the
panel has to decide: which modules a link may name, what each one offers,
which ports a link may read or fill, whether a link as typed will generate,
and what a port rename does to every link that names it.

NEVER IMPORTS THE MODULE IT DESCRIBES
frame_camera reaches for camera SDKs; any linked module's top level runs when
it is imported. Listing a module's functions by importing it would run that
top level inside the Council every time a button is selected. So everything
here is read by PARSING the source (gui_spec.parsed_module, which also caches
the tree) — the same reading gui_spec.validate does at Generate, so the editor
and the validator cannot disagree about what a module offers.

WHY THE CHECKS ARE WORDED FOR A PERSON
gui_spec.validate is the final gate and says what is wrong in a generator's
terms ("script input 'x' names no port"). The panel says it BEFORE anything is
saved, next to the control that caused it, so it says it in the user's terms:
"Input 'x' is not a port in this project." Both run; neither replaces the
other.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: What an empty link is. `{}` in the .gspec means "this button runs its
#: handler stub and nothing else".
NO_LINK: Dict[str, Any] = {}


# ============================================================
# Which kinds can carry a link
# ============================================================

def linkable(kind: str) -> bool:
    """Whether a shape of ``kind`` can run a function when used.

    gui_spec's COMMAND_KINDS is the answer, not a copy of it: a link on any
    other kind is refused at Generate ("put it on a button"), so offering the
    editor there would let the user build something that cannot generate.
    """
    import gui_spec
    return kind in gui_spec.COMMAND_KINDS


# ============================================================
# Modules and functions, by parsing
# ============================================================

@dataclass(frozen=True)
class FunctionInfo:
    """One public top-level function of a linkable module."""
    name: str
    #: Positional parameters, in order — a link passes its inputs positionally.
    params: Tuple[str, ...] = ()
    #: How many of `params` must be given (those without a default).
    required: int = 0
    #: True when it takes *args, so any number of inputs is legal.
    varargs: bool = False
    #: The docstring's first line.
    summary: str = ""
    #: Keys of the dict literals it returns — what an output can be fed from.
    result_keys: Tuple[str, ...] = ()
    line: int = 0

    def signature(self) -> str:
        """"start(folder, exposure=…, gain=…, frame_rate=…)"."""
        parts = [p if i < self.required else f"{p}=…"
                 for i, p in enumerate(self.params)]
        if self.varargs:
            parts.append("*…")
        return f"{self.name}({', '.join(parts)})"


@dataclass(frozen=True)
class ModuleInfo:
    """What a module offers a script link, or why that cannot be known."""
    name: str
    #: False for a module with no source beside the Council (a vendor package
    #: from `requires`). Its functions cannot be listed; a link to it is only
    #: checked by the policy gate, exactly as gui_spec treats it.
    found: bool = False
    functions: Tuple[FunctionInfo, ...] = ()
    #: COUNCIL_REQUIRED_PORTS — ports the module looks up by name at runtime.
    required_ports: Tuple[str, ...] = ()

    def function(self, name: str) -> Optional[FunctionInfo]:
        return next((f for f in self.functions if f.name == name), None)

    @property
    def function_names(self) -> List[str]:
        return [f.name for f in self.functions]


def module_info(module: str, root: Any = None) -> ModuleInfo:
    """Parse ``module``'s source for what a link can call. Never imports it.

    Cheap to call repeatedly: the parse is cached on the source text in
    gui_spec, so selecting the same wired button twice parses nothing.
    """
    import gui_spec

    name = str(module or "").strip()
    if not name or not all(p.isidentifier() for p in name.split(".")):
        return ModuleInfo(name)
    tree = gui_spec.parsed_module(name, root)
    if tree is None:
        return ModuleInfo(name)
    return _describe(name, tree)


#: module name -> (the parsed tree it was read from, ModuleInfo). gui_spec
#: hands back the SAME tree object until the source text changes, so an
#: identity check on it is a free cache key — and holding the tree here means
#: its id can never be reused by a newer one while the entry exists.
_DESCRIBED: Dict[str, Tuple[ast.Module, ModuleInfo]] = {}


def _describe(name: str, tree: ast.Module) -> ModuleInfo:
    import gui_spec

    held = _DESCRIBED.get(name)
    if held is not None and held[0] is tree:
        return held[1]
    defs = _top_level_functions(tree.body)
    by_name = {node.name: node for node in defs}
    functions = tuple(_function_info(node, by_name) for node in defs
                      if not node.name.startswith("_"))
    info = ModuleInfo(name, True, functions,
                      gui_spec.required_ports_in(tree))
    _DESCRIBED[name] = (tree, info)
    return info


def _top_level_functions(stmts: Sequence[ast.stmt]) -> List[ast.FunctionDef]:
    """Functions defined at module level, including inside if/try/with.

    `try: from fast import scan / except ImportError: def scan(...)` is how a
    module offers a function with a fallback, and gui_spec accepts a link to
    it — so the list must too. A name defined twice is listed once.
    """
    out: List[ast.FunctionDef] = []
    seen = set()
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in seen:
                seen.add(node.name)
                out.append(node)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            blocks = [node.body, getattr(node, "orelse", []),
                      getattr(node, "finalbody", [])]
            blocks += [h.body for h in getattr(node, "handlers", [])]
            for block in blocks:
                for inner in _top_level_functions(block):
                    if inner.name not in seen:
                        seen.add(inner.name)
                        out.append(inner)
    return out


def _function_info(node: ast.FunctionDef,
                   by_name: Dict[str, ast.FunctionDef]) -> FunctionInfo:
    args = node.args
    positional = [a.arg for a in list(args.posonlyargs) + list(args.args)]
    doc = ast.get_docstring(node) or ""
    summary = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return FunctionInfo(
        name=node.name, params=tuple(positional),
        required=len(positional) - len(args.defaults),
        varargs=args.vararg is not None, summary=summary,
        result_keys=tuple(_result_keys(node, by_name)),
        line=getattr(node, "lineno", 0))


def _result_keys(node: ast.FunctionDef, by_name: Dict[str, ast.FunctionDef],
                 depth: int = 0) -> List[str]:
    """The string keys of every dict this function visibly returns.

    `return {"summary": ..., "view": ...}` and `return dict(summary=...)` are
    read directly; `return other()` where other is in the same module is
    followed ONE level (shutdown() returns disconnect()). Anything else — a
    variable, a call into another module — contributes nothing, and the key
    field stays free text: these are SUGGESTIONS, never a restriction.
    """
    keys: List[str] = []

    def add(key: Any) -> None:
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)

    for ret in _own_returns(node):
        value = ret.value
        if isinstance(value, ast.Dict):
            for k in value.keys:
                if isinstance(k, ast.Constant):
                    add(k.value)
        elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id == "dict":
                for kw in value.keywords:
                    add(kw.arg)
            elif depth == 0 and value.func.id in by_name \
                    and value.func.id != node.name:
                for k in _result_keys(by_name[value.func.id], by_name, 1):
                    add(k)
    return keys


def _own_returns(node: ast.AST) -> Iterable[ast.Return]:
    """Return statements of THIS function, not of functions nested in it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Return) and child.value is not None:
            yield child
        yield from _own_returns(child)


def linkable_modules(mode: str = "linked",
                     requires: Sequence[str] = ()) -> List[str]:
    """The modules the Module dropdown offers, most useful first.

    The Council modules a linked app may reach (gui_policy.LINKED_MODULES —
    frame_camera among them), then the project's declared packages. That is
    what gui_spec will accept as a link's module; the stdlib is technically
    allowed too, but "os.getcwd on a button" is not what anyone opens this
    dropdown for, and the box stays editable for whoever means it.
    """
    import gui_policy

    names: List[str] = []
    if mode == "linked":
        names.extend(sorted(gui_policy.LINKED_MODULES))
    for raw in gui_policy.as_requires(requires):
        root = str(raw).strip()
        if root and root not in names and root not in gui_policy.DENIED_MODULES:
            names.append(root)
    return names


# ============================================================
# Ports a link can name
# ============================================================

@dataclass(frozen=True)
class PortInfo:
    """One port as a link sees it."""
    name: str
    kind: str
    #: May be an INPUT: it has a value to read.
    readable: bool
    #: May be an OUTPUT: it can show a result.
    showable: bool
    shape_ids: Tuple[str, ...] = ()


def project_ports(shapes: Sequence[Any],
                  registry: Optional[Dict[str, str]] = None) -> List[PortInfo]:
    """Every port the wireframe will have, with what a link may do with it.

    gui_ports.build_ports is the one place port names are decided — explicit
    names reserved first, then the manifest's registry, then derived — so this
    asks it rather than guessing from `shape.port`. ``registry`` is the
    project's manifest port_names; without it a port whose name was never
    typed is shown as derived from today's label, which is what a NEW project
    would get.

    The input/output rules are gui_spec's: an image canvas, chart or button
    has nothing to READ (proxy / event binders), and a button or chart cannot
    SHOW a result.
    """
    import gui_ports

    ports = gui_ports.build_ports(list(shapes),
                                  parents=radio_parents(shapes),
                                  registry=registry or {})
    out = []
    for p in ports:
        out.append(PortInfo(
            name=p.name, kind=p.kind,
            readable=p.binder not in ("proxy", "event"),
            showable=not (p.binder == "event" or (
                p.binder == "proxy" and p.writer == "figure_for_drawing")),
            shape_ids=tuple(p.shape_ids)))
    return out


def port_names_by_key(shapes: Sequence[Any],
                      registry: Optional[Dict[str, str]] = None
                      ) -> Dict[str, str]:
    """shape id (or radio group key) -> port name — the manifest's shape.

    Compared before and after an edit, this is what says a port was RENAMED
    rather than deleted and another added: the key is the shape, and it did
    not change.
    """
    import gui_ports

    parents = radio_parents(shapes)
    ports = gui_ports.build_ports(list(shapes), parents=parents,
                                  registry=registry or {})
    return gui_ports.registry_for(ports, parents=parents)


def radio_parents(shapes: Sequence[Any], tol: int = 4) -> Dict[str, str]:
    """radio id -> the smallest container around it. Nothing else.

    build_ports uses parents for ONE thing — keying radio groups, so two
    "size" groups in two panels stay two ports. designer_scene's
    containment_map answers for every shape and is O(n²): 3.5 ms on Typhon's
    58 shapes, paid on every selection. Radios only is O(radios × shapes),
    and nothing at all for a wireframe with none.
    """
    from gui_shapes import is_container

    radios = [s for s in shapes if getattr(s, "kind", "") == "radiobutton"]
    if not radios:
        return {}
    boxes = [s for s in shapes if is_container(getattr(s, "kind", ""))]
    out: Dict[str, str] = {}
    for radio in radios:
        best = None
        for box in boxes:
            if box.id != radio.id and box.contains(radio, tol) and (
                    best is None or box.area < best.area):
                best = box
        if best is not None:
            out[radio.id] = best.id
    return out


# ============================================================
# A link: reading, checking, describing
# ============================================================

def normalise(link: Any) -> Dict[str, Any]:
    """A link as the .gspec stores it, or {} for "not wired".

    Order is kept (inputs are positional; outputs in the order the user put
    them) and blanks are dropped, so a half-filled output row does not become
    a port named "".
    """
    if not isinstance(link, dict) or not link:
        return {}
    module = str(link.get("module") or "").strip()
    function = str(link.get("function") or "").strip()
    if not module and not function:
        return {}
    inputs = [str(p).strip() for p in (link.get("inputs") or [])
              if str(p).strip()]
    outputs: Dict[str, str] = {}
    raw = link.get("outputs") or {}
    if isinstance(raw, dict):
        for port, key in raw.items():
            port, key = str(port).strip(), str(key if key is not None
                                               else "").strip()
            if port:
                outputs[port] = key
    out: Dict[str, Any] = {"module": module, "function": function,
                           "inputs": inputs, "outputs": outputs}
    # The single-output form an older .gspec may carry. Kept, not converted:
    # it generates a different handler (set(result) rather than a key).
    if link.get("output"):
        out["output"] = str(link["output"]).strip()
    return out


def describe(link: Any) -> str:
    """One line a person reads: "frame_camera.start(capture_folder, …) →
    capture_status". "" for no link."""
    link = normalise(link)
    if not link:
        return ""
    call = f"{link['module']}.{link['function']}({', '.join(link['inputs'])})"
    targets = list(link["outputs"]) + ([link["output"]]
                                       if link.get("output") else [])
    return call + (f" → {', '.join(targets)}" if targets else "")


def problems(link: Any, ports: Sequence[PortInfo], kind: str = "button",
             mode: str = "linked", requires: Sequence[str] = (),
             info: Optional[ModuleInfo] = None) -> List[str]:
    """Everything that would stop this link generating, in plain words.

    Empty for a good link AND for no link — removing the wiring is always
    allowed. Checked before anything is saved, so the user fixes it where they
    typed it rather than reading it back from Generate's log.

    ``info`` is the link module's ModuleInfo when the caller already has it —
    the panel re-checks on every keystroke and has just read it.
    """
    link = normalise(link)
    if not link:
        return []
    out: List[str] = []
    if not linkable(kind):
        out.append(f"A {kind} cannot run a function — only buttons and "
                   f"other controls that are pressed or changed can.")
    module, function = link["module"], link["function"]
    if not module:
        out.append("Choose the module the function lives in.")
    elif not all(p.isidentifier() for p in module.split(".")):
        out.append(f"{module!r} is not a module name.")
    elif module.split(".")[0] not in _allowed(mode, requires):
        out.append(f"{module} is not a module this app may import in "
                   f"{mode} mode. Add it to the packages it needs (click an "
                   f"empty part of the canvas), or pick one from the list.")
    if info is None or info.name != module:
        info = module_info(module) if module else ModuleInfo("")
    fn = info.function(function) if info.found else None
    if not function:
        out.append("Choose the function to run.")
    elif not function.isidentifier():
        out.append(f"{function!r} is not a function name.")
    elif info.found and fn is None:
        out.append(f"{module} has no function {function!r}.")

    by_name = {p.name: p for p in ports}
    for name in link["inputs"]:
        port = by_name.get(name)
        if port is None:
            out.append(f"Input {name!r} is not a port in this project.")
        elif not port.readable:
            out.append(f"Input {name!r} is a {port.kind}, which has no value "
                       f"to pass in.")
    if fn is not None and not fn.varargs:
        given = len(link["inputs"])
        if given > len(fn.params):
            out.append(f"{fn.name} takes {len(fn.params)} input(s) and "
                       f"{given} are wired.")
        elif given < fn.required:
            missing = ", ".join(fn.params[given:fn.required])
            out.append(f"{fn.name} needs {fn.required} input(s) — wire "
                       f"{missing} too.")
    targets = list(link["outputs"].items())
    if link.get("output"):
        targets.append((link["output"], "(whole result)"))
    for name, key in targets:
        port = by_name.get(name)
        if port is None:
            out.append(f"Output {name!r} is not a port in this project.")
        elif not port.showable:
            out.append(f"Output {name!r} is a {port.kind}, which cannot "
                       f"show a result.")
        if not key:
            out.append(f"Output {name!r} needs the result key it shows.")
    return out


#: (mode, requires) -> gui_policy.allowed_modules. That set includes every
#: stdlib module name, which is 0.7 ms to rebuild — and the panel re-checks
#: the link on every keystroke.
_ALLOWED: Dict[Tuple[str, Tuple[str, ...]], frozenset] = {}


def _allowed(mode: str, requires: Sequence[str]) -> frozenset:
    import gui_policy

    key = (str(mode), tuple(gui_policy.as_requires(requires)))
    if key not in _ALLOWED:
        _ALLOWED[key] = frozenset(gui_policy.allowed_modules(*key))
    return _ALLOWED[key]


def match_parameters(fn: FunctionInfo, port_names: Iterable[str]
                     ) -> Tuple[List[str], List[str]]:
    """(inputs, parameters left unmatched) filled from parameter names.

    A link passes its inputs POSITIONALLY, so this matches a PREFIX of the
    parameters and stops at the first one it cannot fill: wiring start's
    exposure into its folder slot because folder had no match would be worse
    than wiring nothing. A parameter matches a port of the same name, else
    the one port whose name ends in "_<parameter>" — Typhon's start(folder,
    ...) takes capture_folder, which an exact match alone would miss.
    """
    names = list(port_names)
    wired: List[str] = []
    for param in fn.params:
        if param in names:
            wired.append(param)
            continue
        close = [n for n in names if n.endswith(f"_{param}")]
        if len(close) == 1:
            wired.append(close[0])
            continue
        break
    return wired, list(fn.params[len(wired):fn.required])


# ============================================================
# Renames
# ============================================================

def rename_ports(shapes: Sequence[Any], renames: Dict[str, str]) -> List[str]:
    """Point every link that names an old port at its new name. In place.

    Returns the ids of the shapes that changed. Script links (inputs, outputs,
    the single `output`) and sequence links (`drives`) both name ports, and a
    rename that updated one and not the other would turn a working link into
    "names no port" at the next Generate — the refusal the user would then
    have to trace back to a rename they made somewhere else.
    """
    renames = {old: new for old, new in (renames or {}).items()
               if old and new and old != new}
    if not renames:
        return []
    changed: List[str] = []
    for shape in shapes:
        touched = False
        script = getattr(shape, "script", None)
        if isinstance(script, dict) and script:
            inputs = script.get("inputs")
            if isinstance(inputs, list):
                renamed = [renames.get(str(p), p) for p in inputs]
                if renamed != inputs:
                    script["inputs"], touched = renamed, True
            outputs = script.get("outputs")
            if isinstance(outputs, dict) and any(str(p) in renames
                                                 for p in outputs):
                script["outputs"] = {renames.get(str(p), p): k
                                     for p, k in outputs.items()}
                touched = True
            single = script.get("output")
            if single and str(single) in renames:
                script["output"], touched = renames[str(single)], True
        drives = getattr(shape, "drives", None)
        if isinstance(drives, dict):
            for role, port in list(drives.items()):
                if isinstance(port, str) and port in renames:
                    drives[role], touched = renames[port], True
        if touched:
            changed.append(shape.id)
    return changed


def renames_between(before: Dict[str, str],
                    after: Dict[str, str]) -> Dict[str, str]:
    """old name -> new name for every key whose port name changed."""
    return {old: after[key] for key, old in before.items()
            if key in after and after[key] != old}


def missing_required(shapes: Sequence[Any],
                     registry: Optional[Dict[str, str]] = None
                     ) -> List[Tuple[str, str]]:
    """(module, port) for every port a linked module needs and the wireframe
    lacks — what Generate will refuse on, known the moment it happens."""
    modules = sorted({str((getattr(s, "script", None) or {}).get("module")
                          or "").strip()
                      for s in shapes if getattr(s, "script", None)} - {""})
    if not modules:
        return []
    have = {p.name for p in project_ports(shapes, registry)}
    out = []
    for module in modules:
        for need in module_info(module).required_ports:
            if need not in have:
                out.append((module, need))
    return out
