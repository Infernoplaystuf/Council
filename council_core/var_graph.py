"""
council_core.var_graph — how the variables in a Python script feed each other.

Point it at a script and it answers the question a bug hunt starts with: "this
value is wrong — what could have made it wrong?" Every variable is a node;
every place one value is computed from another is an edge, with the line it
happens on. Walk the edges backwards from the wrong value (upstream) and you
have every variable that could have influenced it; walk forwards (downstream)
and you have everything a bad value will spread to.

It reads the source with `ast` and never runs it, so it is safe on any script,
including one that does not run any more. It is static, so it is an
over-approximation in some places and blind in others:

* An `if` that picks between two assignments links both (that is the point —
  either could be the culprit).
* A call into a function the project defines (this file, or another one
  opened with it) links the arguments to the parameters and the function's
  return to the result. A call to anything else (`len(x)`, `np.mean(x)`)
  links its arguments straight to the result: outside packages are never
  followed.
* `x.append(v)`, `x[i] = v` and `x.attr = v` count as writing to `x`.
* `self.attr` is one node per class - per family of classes, when they
  inherit from each other in the project (App(HandlerMixin, MainUi)).
* Values that move through containers, `getattr`, files on disk,
  environment variables or queues are not followed.

ACROSS FILES (:func:`analyze_paths`, a folder or several scripts)
Imports between the scripts are resolved (absolute, relative, and bare
names of scripts that put their folder on sys.path), and on top of plain
calls a value is followed through:

* GUI ports: `self.ports.<name>.get()` / `.set(v)` / `ports["name"]` and
  the generated Ports class are one node per port, so a GUI Designer app
  traces from the widget through handlers.py into the attached script;
* `subprocess.run([python, "worker.py", a, b])` into worker.py's
  `sys.argv[1]`, `sys.argv[2]` (and argparse, which reads all of it);
* `Thread(target=f, args=(a,))`, `partial(f, a)`, `pool.submit(f, a)`;
* `from config import RATE` and `config.RATE`.

Condition edges ("this `if` decided whether that line ran") are recorded with
kind ``condition`` and are off unless asked for: they matter for "why did this
branch run", and they triple the size of the graph.

Toolkit-free: the Qt tab (council_qt.tabs.var_graph) and the command line
(`python -m council_core.var_graph script.py --up total`) both use this.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import sys
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

MODULE = "<module>"

#: Methods of the generated Ports object, which are not ports themselves.
_PORTS_METHODS = frozenset({"read", "apply", "clear", "get", "set", "items",
                            "keys", "values", "names", "all"})
RETURN = "<return>"

#: Method calls that change the object they are called on.
MUTATORS = frozenset({
    "append", "extend", "insert", "update", "add", "setdefault", "pop",
    "popitem", "remove", "discard", "clear", "appendleft", "extendleft",
    "sort", "reverse", "write", "writelines", "put", "push", "fill",
    "set", "__setitem__",
})

#: subprocess functions that start another script.
SPAWNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: Folders never scanned for project files: environments, caches, VCS, and
#: the GUI Designer's own backups.
SKIP_DIRS = frozenset({".git", "__pycache__", "venv", ".venv", "env", ".env",
                       "site-packages", "node_modules", ".backups", ".trash",
                       "build", "dist", ".mypy_cache", ".pytest_cache",
                       ".tox", ".idea", ".vscode"})

#: Scope every GUI port lives in: one node per port, whichever file reads or
#: sets it (the generated ports.py, app.py, handlers.py).
PORTS = "<gui ports>"

#: Names the analysis treats as defined everywhere.
BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                       "__builtins__", "__spec__",
                                       "__loader__", "__package__"}


@dataclass
class Var:
    """One node: a variable in a scope."""
    id: str                 # "<scope>::<name>", unique in the graph
    name: str               # what the source calls it: total, self.rate
    scope: str              # <module>, func, Class.method, Class
    kind: str               # variable | param | attribute | return | import | port | argv
    defined: List[int] = field(default_factory=list)   # lines it is written (in `file`)
    used: List[int] = field(default_factory=list)      # lines it is read (in `file`)
    module: str = ""        # dotted module name; "" when one file is analysed
    file: str = ""          # the file it lives in
    elsewhere: List[str] = field(default_factory=list)  # "file:line" in other files

    @property
    def local_scope(self) -> str:
        """The scope without its module prefix: 'main', 'Cart.total'."""
        if self.module and self.scope.startswith(self.module + ":"):
            return self.scope[len(self.module) + 1:]
        return self.scope

    @property
    def label(self) -> str:
        local = self.local_scope
        where = f" · {self.module}" if self.module else ""
        if self.kind == "port":
            return f"{self.name}  (GUI port)"
        if self.kind == "return":
            return f"{self.module + '.' if self.module else ''}{local}() return"
        if local == MODULE:
            return f"{self.name}  ({self.module})" if self.module else self.name
        return f"{self.name}  ({local}{where})"


@dataclass
class Edge:
    """`src` flows into `dst` on `line`."""
    src: str
    dst: str
    kind: str               # assign | augassign | arg | return | iter | with | mutate | condition
    line: int
    code: str = ""          # the source line, stripped
    file: str = ""          # the file `line` is in


@dataclass
class Finding:
    """Something in the graph that often turns out to be the bug."""
    kind: str               # undefined | unused | shadowed-param
    name: str
    scope: str
    line: int
    message: str
    file: str = ""


@dataclass
class Step:
    """One hop of a trace: reached `var` at `depth` through `edge`."""
    var: Var
    depth: int
    edge: Optional[Edge]


class _Scope:
    def __init__(self, name: str, kind: str, parent: Optional["_Scope"],
                 node: Optional[ast.AST] = None, cls: Optional[str] = None):
        self.name = name
        self.kind = kind            # module | function | class
        self.parent = parent
        self.node = node
        self.cls = cls              # for a method: its class's qualified name
        self.locals: Set[str] = set()
        self.params: List[str] = []
        self.globals: Set[str] = set()
        self.nonlocals: Set[str] = set()
        self.self_name: Optional[str] = None
        # name -> (module prefix, attribute or None): what an import bound
        # here, when it is a file of the project.
        self.imports: Dict[str, Tuple[str, Optional[str]]] = {}


def _target_names(target: ast.AST) -> Iterable[ast.AST]:
    """The Name/Attribute/Subscript leaves of an assignment target."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)
    else:
        yield target


class VarGraph:
    """The graph for one file (:func:`analyze`) or many
    (:func:`analyze_paths`). With many, every scope is prefixed by its module
    ('handlers:App.on_scan') and imports between the files are followed."""

    def __init__(self, source: str = "", path: str = "<string>",
                 conditions: bool = False):
        self.source = source            # the first file's, for one-file callers
        self.filename = path
        self.conditions = conditions
        self.sources: Dict[str, str] = {}             # file -> source
        self._lines: Dict[str, List[str]] = {}        # file -> its lines
        self.modules: Dict[str, str] = {}             # dotted module -> file
        self.vars: Dict[str, Var] = {}
        self.edges: List[Edge] = []
        self.findings: List[Finding] = []
        self._in: Dict[str, List[Edge]] = {}
        self._out: Dict[str, List[Edge]] = {}
        self._scopes: Dict[ast.AST, _Scope] = {}
        self._funcs: Dict[str, _Scope] = {}       # qualname -> function scope
        self._classes: Dict[str, _Scope] = {}     # qualname -> class scope
        self._edge_keys: Set[tuple] = set()
        self._unresolved: List[Tuple[str, int, str, str]] = []
        self._instances: Dict[str, str] = {}      # var id -> class it holds
        self._module_scopes: Dict[str, _Scope] = {}   # module prefix -> its scope
        self._class_parent: Dict[str, str] = {}       # union-find over inheritance
        self._outside_bases: Set[str] = set()         # classes with a base not in the project
        self._clobbered: Dict[str, int] = {}          # param id -> line it is replaced
        self._file_of_prefix: Dict[str, str] = {}
        self._pending_imports: List[Tuple[str, str, str, int]] = []
        self._cur_file = path
        self._cur_module = ""

    def family(self, cls: str) -> str:
        """The class whose node a `self.x` of `cls` uses: classes related by
        inheritance in the project share their attributes, so App(Mixin, Ui)
        reading self.x that Ui set is one node, not three."""
        parent = self._class_parent.get(cls, cls)
        if parent == cls:
            return cls
        root = self.family(parent)
        self._class_parent[cls] = root
        return root

    def family_members(self, cls: str) -> List[str]:
        root = self.family(cls)
        return [c for c in self._classes if self.family(c) == root]

    @property
    def lines(self) -> List[str]:
        return self._lines.get(self.filename, [])

    def source_of(self, file: str) -> str:
        return self.sources.get(file, "")

    @property
    def files(self) -> List[str]:
        return list(self.sources)
    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def find(self, text: str) -> List[Var]:
        """Variables whose name contains `text` (case-insensitive), exact
        matches first."""
        t = text.strip().lower()
        hits = [v for v in self.vars.values() if t in v.name.lower()
                or t in v.id.lower()]
        hits.sort(key=lambda v: (v.name.lower() != t, v.kind == "return",
                                 v.name.lower(), v.scope))
        return hits

    def resolve(self, ref: str) -> Var:
        """A node from its id ('func::x'), 'func.x', 'module.func.x', a
        module-level 'x' / 'module.x', or a bare name that only one variable
        has. Closer forms win; a form that fits several is an error."""
        if ref in self.vars:
            return self.vars[ref]
        vs = list(self.vars.values())
        tiers = [
            lambda v: ref in (f"{v.scope}.{v.name}",
                              f"{v.module}.{v.local_scope}.{v.name}",
                              f"{v.module}.{v.name}" if v.local_scope == MODULE else None),
            lambda v: ref == f"{v.local_scope}.{v.name}"
            or (v.local_scope == MODULE and v.name == ref),
            lambda v: v.name == ref,
        ]
        for fits in tiers:
            same = [v for v in vs if fits(v)]
            if len(same) == 1:
                return same[0]
            if same:
                raise KeyError(f"{ref!r} is ambiguous: "
                               + ", ".join(v.id for v in same[:8]))
        raise KeyError(f"no variable called {ref!r}")

    def _resolve_module(self, name: str, importer: str, level: int = 0
                        ) -> Optional[str]:
        """The project module an import names, or None for anything outside
        the project (numpy, the standard library ...). `importer` is the
        importing module; `level` the leading dots of a relative import."""
        if not self.modules:
            return None
        if level:
            base = importer.split(".")
            is_pkg = Path(self.modules.get(importer, "")).name == "__init__.py"
            base = base if is_pkg else base[:-1]
            if level > 1:
                base = base[:-(level - 1)] if level - 1 <= len(base) else []
            full = ".".join(base + ([name] if name else []))
            return full if full in self.modules else None
        if name in self.modules:
            return name
        # A script folder put on sys.path (the GUI Designer's main.py does,
        # and so do most hand-made launchers) imports its siblings by their
        # bare names: match on the tail when exactly one file has it.
        tail = [m for m in self.modules if m.endswith("." + name)]
        return tail[0] if len(tail) == 1 else None

    def fed_by(self, var_id: str, conditions: Optional[bool] = None) -> List[Edge]:
        return [e for e in self._in.get(var_id, ()) if self._keep(e, conditions)]

    def feeds(self, var_id: str, conditions: Optional[bool] = None) -> List[Edge]:
        return [e for e in self._out.get(var_id, ()) if self._keep(e, conditions)]

    def upstream(self, var_id: str, max_depth: Optional[int] = None,
                 conditions: Optional[bool] = None) -> List[Step]:
        """Everything that can influence `var_id`, nearest first."""
        return self._walk(var_id, self.fed_by, "src", max_depth, conditions)

    def downstream(self, var_id: str, max_depth: Optional[int] = None,
                   conditions: Optional[bool] = None) -> List[Step]:
        """Everything `var_id` can influence, nearest first."""
        return self._walk(var_id, self.feeds, "dst", max_depth, conditions)

    def path(self, src_id: str, dst_id: str,
             conditions: Optional[bool] = None) -> List[Edge]:
        """The shortest chain of edges from `src_id` to `dst_id`, or []."""
        prev: Dict[str, Edge] = {}
        seen = {src_id}
        queue = deque([src_id])
        while queue:
            cur = queue.popleft()
            if cur == dst_id:
                out: List[Edge] = []
                while cur != src_id:
                    out.append(prev[cur])
                    cur = prev[cur].src
                return out[::-1]
            for e in self.feeds(cur, conditions):
                if e.dst not in seen:
                    seen.add(e.dst)
                    prev[e.dst] = e
                    queue.append(e.dst)
        return []

    def to_dict(self) -> dict:
        return {"path": self.filename, "files": self.files,
                "vars": [asdict(v) | {"label": v.label} for v in self.vars.values()],
                "edges": [asdict(e) for e in self.edges],
                "findings": [asdict(f) for f in self.findings]}

    def to_dot(self, focus: Optional[Iterable[str]] = None) -> str:
        """Graphviz source; `focus` limits it to those node ids."""
        keep = set(focus) if focus is not None else set(self.vars)
        shape = {"param": "box", "return": "doubleoctagon", "import": "note",
                 "attribute": "component"}
        out = ["digraph vars {", "  rankdir=LR;",
               '  node [fontname="Helvetica", shape=ellipse];']
        for vid in sorted(keep):
            v = self.vars[vid]
            out.append(f'  {json.dumps(vid)} [label={json.dumps(v.label)}, '
                       f'shape={shape.get(v.kind, "ellipse")}];')
        for e in self.edges:
            if e.src in keep and e.dst in keep and self._keep(e, None):
                style = ', style=dashed' if e.kind == "condition" else ""
                out.append(f'  {json.dumps(e.src)} -> {json.dumps(e.dst)} '
                           f'[label="{e.kind} L{e.line}"{style}];')
        out.append("}")
        return "\n".join(out)

    def _keep(self, e: Edge, conditions: Optional[bool]) -> bool:
        want = self.conditions if conditions is None else conditions
        return want or e.kind != "condition"

    def _walk(self, start, step, attr, max_depth, conditions) -> List[Step]:
        seen = {start}
        out: List[Step] = []
        queue = deque([(start, 0)])
        while queue:
            cur, depth = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            for e in step(cur, conditions):
                nxt = getattr(e, attr)
                if nxt in seen:
                    continue
                seen.add(nxt)
                out.append(Step(self.vars[nxt], depth + 1, e))
                queue.append((nxt, depth + 1))
        return out

    # ------------------------------------------------------------------
    # building
    # ------------------------------------------------------------------
    def _var(self, scope: str, name: str, kind: str) -> Var:
        vid = f"{scope}::{name}"
        v = self.vars.get(vid)
        if v is None:
            module = scope.split(":", 1)[0] if self.modules and ":" in scope else ""
            file = self._file_of_prefix.get(module, self._cur_file) \
                if scope != PORTS else ""
            v = self.vars[vid] = Var(vid, name, scope, kind, module=module,
                                     file=file)
        elif kind in ("param", "import", "attribute", "return") and v.kind == "variable":
            v.kind = kind
        return v

    def _note(self, v: Var, line: int, written: bool) -> None:
        """Record a read or write of `v` on `line` of the file being linked."""
        if v.kind == "port" and self.modules:
            # A port has no home file: name the file with every line.
            v.elsewhere.append(f"{'set' if written else 'read'} "
                               f"{Path(self._cur_file).name}:{line}")
        elif not v.file or v.file == self._cur_file:
            (v.defined if written else v.used).append(line)
        else:
            v.elsewhere.append(f"{'set' if written else 'read'} "
                               f"{Path(self._cur_file).name}:{line}")

    def _edge(self, src: str, dst: str, kind: str, line: int) -> None:
        if src == dst and kind not in ("augassign", "mutate"):
            return
        key = (src, dst, kind, line, self._cur_file)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        lines = self._lines.get(self._cur_file, [])
        code = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        e = Edge(src, dst, kind, line, code, self._cur_file)
        self.edges.append(e)
        self._in.setdefault(dst, []).append(e)
        self._out.setdefault(src, []).append(e)


def analyze(source: str, path: str = "<string>",
            conditions: bool = False) -> VarGraph:
    """Build the graph for `source`. Raises SyntaxError if it does not parse."""
    return _build({"": (path, source)}, conditions, strict=True)


def analyze_file(path, conditions: bool = False) -> VarGraph:
    p = Path(path)
    return analyze(p.read_text(encoding="utf-8", errors="replace"), str(p),
                   conditions)


def project_files(paths: Iterable) -> Dict[str, str]:
    """dotted module name -> file, for every .py under `paths` (files or
    folders). A folder's files are named from that folder down
    ('ui/ports.py' -> 'ui.ports'; a package's __init__.py is the package);
    a lone file by its stem. Environments, caches and backups are skipped."""
    out: Dict[str, str] = {}
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            out.setdefault(p.stem, str(p))
            continue
        if not p.is_dir():
            raise FileNotFoundError(f"no such file or folder: {p}")
        for f in sorted(p.rglob("*.py")):
            rel = f.relative_to(p)
            if any(part in SKIP_DIRS or part.startswith(".")
                   for part in rel.parts[:-1]):
                continue
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1] or [p.name]
            out.setdefault(".".join(parts), str(f))
    return out


def analyze_paths(paths: Iterable, conditions: bool = False) -> VarGraph:
    """One graph over several scripts: files, folders, or both. Imports
    between them are followed; imports of anything else (numpy, PySide6, the
    standard library) are not. A file that does not parse is skipped and
    reported as a finding. One file on its own is the same as
    :func:`analyze_file`."""
    mods = project_files(paths)
    if not mods:
        raise FileNotFoundError("no Python files found")
    if len(mods) == 1:
        return analyze_file(next(iter(mods.values())), conditions)
    files = {}
    for mod, f in mods.items():
        files[mod] = (f, Path(f).read_text(encoding="utf-8", errors="replace"))
    return _build(files, conditions, strict=False)


def _build(files: Dict[str, Tuple[str, str]], conditions: bool,
           strict: bool) -> VarGraph:
    """`files` is module -> (path, source); module "" means a single file
    with unprefixed scopes."""
    first_path, first_src = next(iter(files.values()))
    g = VarGraph(first_src, first_path, conditions)
    multi = "" not in files
    if multi:
        g.filename = str(Path(os.path.commonpath(
            [f for f, _s in files.values()])))
    trees = {}
    broken: List[Finding] = []
    for mod, (path, src) in files.items():
        g.sources[path] = src
        g._lines[path] = src.splitlines()
        if multi:
            g.modules[mod] = path
            g._file_of_prefix[mod] = path
        try:
            trees[mod] = ast.parse(src, filename=path)
        except SyntaxError as exc:
            if strict:
                raise
            broken.append(Finding("syntax", Path(path).name, mod, exc.lineno or 0,
                                  f"{Path(path).name} does not parse "
                                  f"(line {exc.lineno}): {exc.msg} - skipped",
                                  path))
    for mod, tree in trees.items():
        g._cur_file, g._cur_module = files[mod][0], mod
        _Collector(g, mod).run(tree)
    _join_class_families(g)
    for mod, tree in trees.items():
        g._cur_file, g._cur_module = files[mod][0], mod
        _Linker(g, mod).run(tree)
    _link_imports(g)
    _find_problems(g)
    g.findings[:0] = broken
    return g


def _join_class_families(g: VarGraph) -> None:
    """Union each class with the bases the project defines. The subclass
    becomes the family's name, so App(HandlerMixin, MainUi) is 'App'."""
    for qual, sc in list(g._classes.items()):
        mod_scope = sc.parent
        while mod_scope is not None and mod_scope.kind != "module":
            mod_scope = mod_scope.parent
        prefix = mod_scope.name[:-len(MODULE)] if mod_scope else ""
        for base in sc.node.bases:
            target = None
            if isinstance(base, ast.Name):
                imp = mod_scope.imports.get(base.id) if mod_scope else None
                if imp and imp[1]:
                    target = f"{imp[0]}:{imp[1]}"
                else:
                    target = prefix + base.id
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and mod_scope is not None:
                imp = mod_scope.imports.get(base.value.id)
                if imp and imp[1] is None:
                    target = f"{imp[0]}:{base.attr}"
            if target in g._classes:
                g._class_parent[g.family(target)] = g.family(qual)
            elif not (isinstance(base, ast.Name) and base.id == "object"):
                g._outside_bases.add(qual)


def _link_imports(g: VarGraph) -> None:
    """`from config import RATE`: config's RATE flows into this file's RATE.
    Done after every file is linked, so it does not matter which came first."""
    for src, dst, file, line in g._pending_imports:
        if src in g.vars:
            g._cur_file = file
            g._edge(src, dst, "import", line)
    # sys.argv[i] is part of sys.argv; argparse reads the whole of it.
    for v in list(g.vars.values()):
        if v.kind == "argv" and v.name != "sys.argv":
            whole = g.vars.get(f"{v.scope}::sys.argv")
            if whole is not None:
                g._cur_file = whole.file
                g._edge(v.id, whole.id, "argv", whole.used[0] if whole.used else 0)


# ----------------------------------------------------------------------
# pass 1: which names live in which scope
# ----------------------------------------------------------------------
class _Collector(ast.NodeVisitor):
    def __init__(self, g: VarGraph, module: str = ""):
        self.g = g
        self.module = module
        self.prefix = f"{module}:" if module else ""
        self.stack: List[_Scope] = []

    def run(self, tree: ast.Module) -> None:
        mod = _Scope(self.prefix + MODULE, "module", None, tree)
        self.g._scopes[tree] = mod
        self.g._module_scopes[self.module] = mod
        self.stack.append(mod)
        self.generic_visit(tree)

    @property
    def cur(self) -> _Scope:
        return self.stack[-1]

    def _qual(self, name: str) -> str:
        return self.prefix + name if self.cur.kind == "module" \
            else f"{self.cur.name}.{name}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(d)
        self.cur.locals.add(node.name)
        sc = _Scope(self._qual(node.name), "class", self.cur, node)
        self.g._scopes[node] = sc
        self.g._classes[sc.name] = sc
        self.stack.append(sc)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    def visit_FunctionDef(self, node) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(d)
        self.cur.locals.add(node.name)
        cls = self.cur.name if self.cur.kind == "class" else None
        sc = _Scope(self._qual(node.name), "function", self.cur, node, cls)
        a = node.args
        sc.params = [x.arg for x in a.posonlyargs + a.args]
        if a.vararg:
            sc.params.append(a.vararg.arg)
        sc.params += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            sc.params.append(a.kwarg.arg)
        sc.locals.update(sc.params)
        is_static = any(isinstance(d, ast.Name) and d.id == "staticmethod"
                        for d in node.decorator_list)
        if cls and sc.params and not is_static:
            sc.self_name = sc.params[0]
        self.g._scopes[node] = sc
        self.g._funcs[sc.name] = sc
        self.stack.append(sc)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.body)

    def visit_Global(self, node: ast.Global) -> None:
        self.cur.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.cur.nonlocals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.cur.locals.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.cur.locals.add(bound)
            # `import pkg.mod` binds pkg; `import pkg.mod as m` binds m to mod.
            target = alias.name if alias.asname else bound
            mod = self.g._resolve_module(target, self.module)
            if mod is not None:
                self.cur.imports[bound] = (mod, None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self.g._resolve_module(node.module or "", self.module, node.level) \
            if (node.module or node.level) else None
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            self.cur.locals.add(bound)
            sub = self.g._resolve_module(
                f"{node.module}.{alias.name}" if node.module else alias.name,
                self.module, node.level)
            if sub is not None:                    # from pkg import submodule
                self.cur.imports[bound] = (sub, None)
            elif base is not None:
                self.cur.imports[bound] = (base, alias.name)

    def visit_MatchAs(self, node) -> None:
        if node.name:
            self.cur.locals.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node) -> None:
        if node.name:
            self.cur.locals.add(node.name)

    def visit_MatchMapping(self, node) -> None:
        if node.rest:
            self.cur.locals.add(node.rest)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.cur.locals.add(node.name)
        self.generic_visit(node)

    def _comp(self, node) -> None:
        # Comprehension targets are their own scope in Python 3; leaving them
        # out of the enclosing locals keeps `[x for x in xs]` from inventing an
        # `x` in the function.
        for gen in node.generators:
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comp


# ----------------------------------------------------------------------
# pass 2: the edges
# ----------------------------------------------------------------------
class _Linker:
    def __init__(self, g: VarGraph, module: str = ""):
        self.g = g
        self.module = module
        self.prefix = f"{module}:" if module else ""
        self.stack: List[_Scope] = []
        self.cond: List[Set[str]] = []        # names in enclosing if/while tests
        self.comp: List[Set[str]] = []        # comprehension-bound names
        self.reads: List[Tuple[str, int, str]] = []   # unresolved: (name, line, scope)

    def run(self, tree: ast.Module) -> None:
        self.stack.append(self.g._scopes[tree])
        self._body(tree.body)
        # Names read but defined nowhere, once per scope, for the findings.
        seen: Set[Tuple[str, str]] = set()
        for name, line, scope in self.reads:
            if (name, scope) not in seen:
                seen.add((name, scope))
                self.g._unresolved.append((name, line, scope, self.g._cur_file))

    @property
    def cur(self) -> _Scope:
        return self.stack[-1]

    # -- name resolution --------------------------------------------------
    def _owner(self, name: str) -> Optional[_Scope]:
        sc = self.cur
        if name in sc.globals:
            return self._module()
        if name in sc.nonlocals:
            sc = sc.parent
        first = True
        while sc is not None:
            # Class bodies are not visible from the methods inside them.
            if sc.kind == "class" and not first:
                sc = sc.parent
                continue
            if name in sc.locals:
                return sc
            first = False
            sc = sc.parent
        return None

    def _module(self) -> _Scope:
        sc = self.cur
        while sc.parent is not None:
            sc = sc.parent
        return sc

    def _kind_for(self, sc: _Scope, name: str) -> str:
        if name in sc.params:
            return "param"
        return "variable"

    def _read_name(self, name: str, line: int) -> Optional[str]:
        if any(name in c for c in self.comp):
            return None
        sc = self._owner(name)
        if sc is not None and name == sc.self_name:
            return None         # `self` is the object, not a value; self.x is
        if sc is None:
            if name not in BUILTINS:
                self.reads.append((name, line, self.cur.name))
            return None
        v = self.g._var(sc.name, name, self._kind_for(sc, name))
        self.g._note(v, line, False)
        return v.id

    def _write_name(self, name: str, line: int, kind: str = "variable") -> str:
        sc = self._owner(name) or self.cur
        v = self.g._var(sc.name, name,
                        "param" if name in sc.params else kind)
        self.g._note(v, line, True)
        return v.id

    # -- across files ----------------------------------------------------
    def _ref(self, node: ast.AST) -> Optional[Tuple[str, str]]:
        """What a Name or dotted Attribute names, when it is something of the
        project: ('module', prefix) or ('qual', qualified name). The qualified
        name is how _funcs/_classes key a function or class."""
        if isinstance(node, ast.Name):
            sc = self._owner(node.id)
            if sc is None:
                return None
            if node.id in sc.imports:
                mod, attr = sc.imports[node.id]
                return ("module", mod) if attr is None else ("qual", f"{mod}:{attr}")
            return ("qual", self.prefix + node.id if sc.kind == "module"
                    else f"{sc.name}.{node.id}")
        if isinstance(node, ast.Attribute):
            sa = self._self_attr(node)
            if sa:
                return "qual", f"{sa[0]}.{sa[1][5:]}"
            base = self._ref(node.value) if isinstance(
                node.value, (ast.Name, ast.Attribute)) else None
            if base and base[0] == "module":
                sub = f"{base[1]}.{node.attr}"
                if sub in self.g.modules:
                    return "module", sub
                return "qual", f"{base[1]}:{node.attr}"
        return None

    def _module_var(self, node: ast.AST) -> Optional[Var]:
        """config.RATE, when config is a file of the project and RATE one of
        its module-level variables (not a function or class)."""
        if not isinstance(node, ast.Attribute):
            return None
        base = self._ref(node.value) if isinstance(
            node.value, (ast.Name, ast.Attribute)) else None
        if not base or base[0] != "module":
            return None
        mod = base[1]
        scope = self.g._module_scopes.get(mod)
        qual = f"{mod}:{node.attr}"
        if scope is None or node.attr not in scope.locals \
                or qual in self.g._funcs or qual in self.g._classes:
            return None
        return self.g._var(scope.name, node.attr, "variable")

    def _port(self, node: ast.AST) -> Optional[str]:
        """The GUI port `node` names: self.ports.<name>, ports.<name>,
        ports["name"], or self.<name> inside the generated Ports class."""
        holder, name = None, None
        if isinstance(node, ast.Attribute):
            holder, name = node.value, node.attr
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            holder, name = node.value, node.slice.value
        if holder is None or name in _PORTS_METHODS:
            return None
        if (isinstance(holder, ast.Attribute) and holder.attr == "ports") or \
                (isinstance(holder, ast.Name) and holder.id == "ports"):
            return name
        if isinstance(node, ast.Attribute) and isinstance(holder, ast.Name):
            sc = self.cur
            while sc is not None and sc.kind != "function":
                sc = sc.parent
            if sc and sc.cls and sc.self_name == holder.id \
                    and sc.cls.rsplit(".", 1)[-1].rsplit(":", 1)[-1].endswith("Ports"):
                return name
        return None

    def _port_var(self, name: str) -> Var:
        return self.g._var(PORTS, f"ports.{name}", "port")

    def _argv(self, node: ast.AST) -> Optional[Var]:
        """sys.argv, or sys.argv[i] with a constant i: what a script was
        started with."""
        def is_argv(n):
            return (isinstance(n, ast.Attribute) and n.attr == "argv"
                    and isinstance(n.value, ast.Name) and n.value.id == "sys")
        if is_argv(node):
            return self.g._var(self.prefix + MODULE, "sys.argv", "argv")
        if isinstance(node, ast.Subscript) and is_argv(node.value) \
                and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int):
            return self.g._var(self.prefix + MODULE,
                               f"sys.argv[{node.slice.value}]", "argv")
        return None

    def _self_attr(self, node: ast.AST) -> Optional[Tuple[str, str]]:
        """('Class', 'self.x') when `node` is self.x inside a method of Class,
        or obj.x where obj was last assigned `Class(...)`."""
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            sc = self.cur
            while sc is not None and sc.kind != "function":
                sc = sc.parent
            if sc and sc.self_name and node.value.id == sc.self_name \
                    and self._owner(node.value.id) is sc:
                return self.g.family(sc.cls), f"self.{node.attr}"
            owner = self._owner(node.value.id)
            if owner is not None:
                cls = self.g._instances.get(f"{owner.name}::{node.value.id}")
                if cls:
                    return self.g.family(cls), f"self.{node.attr}"
        return None

    def _instance_var(self, node: ast.AST) -> Optional[str]:
        """The node id of `obj` in obj.x when obj holds an instance of a class
        in this file — so the object itself still shows the write."""
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = self._owner(node.value.id)
            if owner is not None:
                vid = f"{owner.name}::{node.value.id}"
                if vid in self.g._instances:
                    return vid
        return None

    def _class_of_call(self, node: Optional[ast.AST]) -> Optional[str]:
        """'Cart' when `node` is Cart(...) for a class defined in the project."""
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            r = self._ref(node.func)
            if r and r[0] == "qual" and r[1] in self.g._classes:
                return r[1]
        return None

    # -- reading expressions ---------------------------------------------
    def deps(self, node: Optional[ast.AST]) -> Set[str]:
        """Node ids of every variable `node` reads, linking any calls it makes
        on the way."""
        out: Set[str] = set()
        if node is None:
            return out
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                vid = self._read_name(node.id, node.lineno)
                if vid:
                    out.add(vid)
            return out
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            special = self._special(node)
            if special is not None:
                self.g._note(special, node.lineno, False)
                return {special.id}
        if isinstance(node, ast.Attribute):
            sa = self._self_attr(node)
            if sa:
                v = self.g._var(sa[0], sa[1], "attribute")
                self.g._note(v, node.lineno, False)
                out.add(v.id)
                if self._instance_var(node):
                    self.deps(node.value)       # record the read of obj
                return out
            return self.deps(node.value)
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Lambda):
            self.comp.append({a.arg for a in node.args.args + node.args.kwonlyargs})
            out |= self.deps(node.body)
            self.comp.pop()
            return out
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            return self._comprehension(node)
        if isinstance(node, ast.NamedExpr):
            src = self.deps(node.value)
            dst = self._write_name(node.target.id, node.lineno)
            for s in src:
                self.g._edge(s, dst, "assign", node.lineno)
            self._condition_edges(dst, node.lineno)
            return {dst}
        for child in ast.iter_child_nodes(node):
            out |= self.deps(child)
        return out

    def _special(self, node: ast.AST) -> Optional[Var]:
        """A port, another file's module variable, or sys.argv."""
        port = self._port(node)
        if port:
            return self._port_var(port)
        return self._module_var(node) or self._argv(node)

    def _comprehension(self, node) -> Set[str]:
        out: Set[str] = set()
        bound: Set[str] = set()
        self.comp.append(bound)
        for gen in node.generators:
            out |= self.deps(gen.iter)
            for t in _target_names(gen.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
            for cond in gen.ifs:
                out |= self.deps(cond)
        if isinstance(node, ast.DictComp):
            out |= self.deps(node.key) | self.deps(node.value)
        else:
            out |= self.deps(node.elt)
        self.comp.pop()
        return out

    def _callee(self, func: ast.AST) -> Optional[_Scope]:
        """The function scope a call lands in, when the project defines it
        (this file, or another one it imports)."""
        r = self._ref(func) if isinstance(func, (ast.Name, ast.Attribute)) else None
        if not r or r[0] != "qual":
            return None
        name = r[1]
        if name in self.g._classes:
            return self._method(name, "__init__")
        if name in self.g._funcs:
            return self.g._funcs[name]
        cls, _dot, meth = name.rpartition(".")
        return self._method(cls, meth) if cls in self.g._classes else None

    def _method(self, cls: str, meth: str) -> Optional[_Scope]:
        """`meth` of `cls`, or of another class in its family (a mixin's
        method called as self.meth() from the class it is mixed into)."""
        own = self.g._funcs.get(f"{cls}.{meth}")
        if own is not None:
            return own
        for c in self.g.family_members(cls):
            f = self.g._funcs.get(f"{c}.{meth}")
            if f is not None:
                return f
        return None

    def _link_args(self, target: _Scope, arg_deps: List[Set[str]],
                   kw_deps: List[Tuple[Optional[str], Set[str]]], line: int,
                   kind: str = "arg") -> None:
        """Positional and keyword arguments into `target`'s parameters."""
        params = list(target.params)
        if target.self_name and params:
            params = params[1:]
        for i, d in enumerate(arg_deps):
            if i < len(params):
                p = self.g._var(target.name, params[i], "param")
                for s in d:
                    self.g._edge(s, p.id, kind, line)
        for kname, d in kw_deps:
            if kname and kname in target.params:
                p = self.g._var(target.name, kname, "param")
                for s in d:
                    self.g._edge(s, p.id, kind, line)

    def _deferred_call(self, node: ast.Call, line: int) -> None:
        """Calls that run a function later with arguments given now:
        Thread(target=f, args=(a,)), partial(f, a), pool.submit(f, a)."""
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
            node.func.id if isinstance(node.func, ast.Name) else ""
        kws = {k.arg: k.value for k in node.keywords if k.arg}
        if "target" in kws:
            target = self._callee(kws["target"])
            if target is None:
                return
            argv = kws.get("args")
            elts = argv.elts if isinstance(argv, (ast.Tuple, ast.List)) else []
            kw = kws.get("kwargs")
            kwd = [(k.value, self.deps(v)) for k, v in zip(kw.keys, kw.values)
                   if isinstance(k, ast.Constant)] if isinstance(kw, ast.Dict) else []
            self._link_args(target, [self.deps(e) for e in elts], kwd, line)
        elif fname in ("partial", "submit", "start_new_thread", "run_in_executor") \
                and node.args:
            skip = 1 if fname == "run_in_executor" else 0   # loop.run_in_executor(pool, f, ...)
            if len(node.args) <= skip:
                return
            target = self._callee(node.args[skip])
            if target is None:
                return
            rest = node.args[skip + 1:]
            if fname == "start_new_thread" and rest and isinstance(rest[0], ast.Tuple):
                rest = rest[0].elts
            self._link_args(target, [self.deps(a) for a in rest],
                            [(k.arg, self.deps(k.value)) for k in node.keywords
                             if k.arg], line)

    def _spawn(self, node: ast.Call, line: int) -> None:
        """subprocess.run([python, "script.py", a, b]): a is the script's
        sys.argv[1], b its sys.argv[2] - when script.py is in the project."""
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
            node.func.id if isinstance(node.func, ast.Name) else ""
        if fname not in SPAWNERS or not node.args \
                or not isinstance(node.args[0], (ast.List, ast.Tuple)):
            return
        elts = node.args[0].elts
        for k, elt in enumerate(elts):
            script = next((c.value for c in ast.walk(elt)
                           if isinstance(c, ast.Constant) and isinstance(c.value, str)
                           and c.value.endswith((".py", ".pyw"))), None)
            if script is None:
                continue
            base = Path(script).name
            hits = [m for m, f in self.g.modules.items() if Path(f).name == base]
            if len(hits) != 1:
                return
            scope = f"{hits[0]}:{MODULE}"
            for j in range(k + 1, len(elts)):
                dst = self.g._var(scope, f"sys.argv[{j - k}]", "argv")
                for s in self.deps(elts[j]):
                    self.g._edge(s, dst.id, "argv", line)
            return

    def _call(self, node: ast.Call) -> Set[str]:
        line = node.lineno
        target = self._callee(node.func)
        arg_deps = [self.deps(a.value if isinstance(a, ast.Starred) else a)
                    for a in node.args]
        kw_deps = [(k.arg, self.deps(k.value)) for k in node.keywords]
        out: Set[str] = set()
        if target is not None:
            if isinstance(node.func, ast.Name):
                self._read_name(node.func.id, line)     # the name is used
            self._link_args(target, arg_deps, kw_deps, line)
            if self._class_of_call(node):
                # Cart(a, b): the new object is made from its arguments.
                for d in arg_deps + [d for _k, d in kw_deps]:
                    out |= d
                return out
            ret = self.g._var(target.name, RETURN, "return")
            out.add(ret.id)
            if isinstance(node.func, ast.Attribute):
                out |= self.deps(node.func.value)
            return out
        self._deferred_call(node, line)
        if self.g.modules:
            self._spawn(node, line)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "parse_args" \
                and not node.args:
            # argparse reads sys.argv itself
            whole = self.g._var(self.prefix + MODULE, "sys.argv", "argv")
            self.g._note(whole, line, False)
            out.add(whole.id)
        out |= self.deps(node.func)
        for d in arg_deps:
            out |= d
        for _k, d in kw_deps:
            out |= d
        # x.append(v): v flows into x
        if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
            for obj in self._write_targets(node.func.value, line, mutate=True):
                for d in arg_deps + [d for _k, d in kw_deps]:
                    for s in d:
                        self.g._edge(s, obj, "mutate", line)
        return out

    # -- writing ---------------------------------------------------------
    def _write_targets(self, target: ast.AST, line: int,
                       mutate: bool = False) -> List[str]:
        """Node ids an assignment to `target` writes. `a.b = v` and `a[i] = v`
        write `a`; for a subscript, the index is read too and flows in."""
        out: List[str] = []
        for t in _target_names(target):
            if isinstance(t, ast.Name):
                if mutate:
                    vid = self._read_name(t.id, line)
                    if vid:
                        self.g._note(self.g.vars[vid], line, True)
                        out.append(vid)
                else:
                    out.append(self._write_name(t.id, line))
            elif isinstance(t, (ast.Attribute, ast.Subscript)) \
                    and (self._port(t) or self._module_var(t)):
                v = self._special(t)
                self.g._note(v, line, True)
                out.append(v.id)
            elif isinstance(t, ast.Attribute):
                sa = self._self_attr(t)
                if sa:
                    v = self.g._var(sa[0], sa[1], "attribute")
                    self.g._note(v, line, True)
                    out.append(v.id)
                    if self._instance_var(t):
                        out += self._write_targets(t.value, line, mutate=True)
                else:
                    out += self._write_targets(t.value, line, mutate=True)
            elif isinstance(t, ast.Subscript):
                objs = self._write_targets(t.value, line, mutate=True)
                for s in self.deps(t.slice):
                    for o in objs:
                        self.g._edge(s, o, "mutate", line)
                out += objs
        return out

    def _assign(self, targets: List[ast.AST], value: Optional[ast.AST],
                line: int, kind: str = "assign",
                extra: Iterable[str] = ()) -> None:
        src = self.deps(value) | set(extra)
        cls = self._class_of_call(value)
        for target in targets:
            plain = isinstance(target, (ast.Name, ast.Tuple, ast.List,
                                        ast.Starred))
            dsts = self._write_targets(target, line)
            if cls and isinstance(target, ast.Name):
                for d in dsts:
                    self.g._instances[d] = cls
            for dst in dsts:
                # `x = 0` replaces a parameter; `x = int(x)` only reshapes it.
                if kind == "assign" and plain and dst not in src \
                        and self.g.vars[dst].kind == "param":
                    self.g._clobbered.setdefault(dst, line)
                # x.a = v / x[i] = v changes x; self.a = v is a plain write.
                k = kind
                direct = self.g.vars[dst].kind in ("attribute", "port") or (
                    isinstance(target, ast.Attribute) and self._module_var(target))
                if kind == "assign" and not plain and not direct:
                    k = "mutate"
                for s in src:
                    self.g._edge(s, dst, k, line)
                self._condition_edges(dst, line)

    def _condition_edges(self, dst: str, line: int) -> None:
        for names in self.cond:
            for s in names:
                self.g._edge(s, dst, "condition", line)

    # -- statements ------------------------------------------------------
    def _body(self, stmts: List[ast.stmt]) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, node: ast.stmt) -> None:
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                self.deps(d)
            sc = self.g._scopes[node]
            for p in sc.params:
                if p != sc.self_name:
                    self.g._note(self.g._var(sc.name, p, "param"), node.lineno, True)
            # defaults flow into their parameters
            a = node.args
            pos = a.posonlyargs + a.args
            for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                for s in self.deps(d):
                    self.g._edge(s, f"{sc.name}::{p.arg}", "assign", line)
            for p, d in zip(a.kwonlyargs, a.kw_defaults):
                if d is not None:
                    for s in self.deps(d):
                        self.g._edge(s, f"{sc.name}::{p.arg}", "assign", line)
            self.stack.append(sc)
            saved, self.cond = self.cond, []
            self._body(node.body)
            self.cond = saved
            self.stack.pop()
        elif isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases:
                self.deps(d)
            self.stack.append(self.g._scopes[node])
            saved, self.cond = self.cond, []
            self._body(node.body)
            self.cond = saved
            self.stack.pop()
        elif isinstance(node, ast.Assign):
            self._assign(node.targets, node.value, line)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._assign([node.target], node.value, line)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                cur = self._read_name(node.target.id, line)
                self._assign([node.target], node.value, line, "augassign",
                             [cur] if cur else [])
            else:
                old = self.deps(node.target)
                self._assign([node.target], node.value, line, "augassign", old)
        elif isinstance(node, ast.Return):
            sc = self.cur
            if sc.kind == "function":
                ret = self.g._var(sc.name, RETURN, "return")
                self.g._note(ret, line, True)
                for s in self.deps(node.value):
                    self.g._edge(s, ret.id, "return", line)
                self._condition_edges(ret.id, line)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._assign([node.target], node.iter, line, "iter")
            self._branch(set(), node.body + node.orelse)
        elif isinstance(node, (ast.If, ast.While)):
            test = self.deps(node.test)
            self._branch(test, node.body)
            self._branch(test, node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    self._assign([item.optional_vars], item.context_expr,
                                 line, "with")
                else:
                    self.deps(item.context_expr)
            self._body(node.body)
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            self._body(node.body)
            for h in node.handlers:
                self.deps(h.type)
                if h.name:
                    self._write_name(h.name, h.lineno)
                self._body(h.body)
            self._body(node.orelse)
            self._body(node.finalbody)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name == "*":
                    continue
                dst = self._write_name(name, line, "import")
                sc = self._owner(name)
                mod, attr = (sc.imports.get(name, (None, None)) if sc else (None, None))
                if mod is not None and attr is not None:
                    # from config import RATE: config's RATE flows in here.
                    # Linked once every file is done (see _link_imports).
                    self.g._pending_imports.append(
                        (f"{mod}:{MODULE}::{attr}", dst, self.g._cur_file, line))
        elif isinstance(node, ast.Match):
            subject = self.deps(node.subject)
            for case in node.cases:
                for n in ast.walk(case.pattern):
                    bound = getattr(n, "name", None) or getattr(n, "rest", None)
                    if isinstance(bound, str):
                        dst = self._write_name(bound, line)
                        for s in subject:
                            self.g._edge(s, dst, "assign", line)
                self._branch(subject | self.deps(case.guard), case.body)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                self.deps(t)
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.Pass,
                               ast.Break, ast.Continue)):
            pass
        else:
            # Expr, Raise, Assert and anything new: read everything in it,
            # which also links the calls it makes.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    self.stmt(child)
                else:
                    self.deps(child)

    def _branch(self, test: Set[str], body: List[ast.stmt]) -> None:
        if not body:
            return
        self.cond.append(test)
        self._body(body)
        self.cond.pop()


# ----------------------------------------------------------------------
# findings
# ----------------------------------------------------------------------
def _find_problems(g: VarGraph) -> None:
    def where(v: Var) -> str:
        return f"{v.local_scope}" + (f" ({v.module})" if v.module else "")

    for v in g.vars.values():
        if v.kind in ("return", "attribute", "port", "argv"):
            continue
        if v.defined and not v.used and not g._out.get(v.id) \
                and v.local_scope != MODULE and not v.name.startswith("_") \
                and v.kind != "param":
            g.findings.append(Finding(
                "unused", v.name, v.scope, v.defined[0],
                f"'{v.name}' is assigned in {where(v)} but never read", v.file))
    for vid, later in g._clobbered.items():
        v = g.vars[vid]
        g.findings.append(Finding(
            "shadowed-param", v.name, v.scope, later,
            f"parameter '{v.name}' of {where(v)} is overwritten on line "
            f"{later} without using it; the caller's value is lost "
            "after that", v.file))
    for v in g.vars.values():
        if v.kind == "attribute" and not v.defined and not v.elsewhere \
                and v.used and v.scope in g._classes:
            attr = v.name[5:]
            members = g.family_members(v.scope)
            if any(f"{c}::{attr}" in g.vars or f"{c}.{attr}" in g._funcs
                   for c in members):
                continue
            # A base the project does not define (QWidget, threading.Thread)
            # may well set it.
            if any(c in g._outside_bases for c in members):
                continue
            g.findings.append(Finding(
                "undefined", v.name, v.scope, v.used[0],
                f"'{v.name}' is read in class {where(v)} but never assigned "
                "(set from outside, or a typo?)", v.file))
    for name, line, scope, file in g._unresolved:
        mod = scope.split(":", 1)[0] if g.modules and ":" in scope else ""
        local = scope[len(mod) + 1:] if mod else scope
        g.findings.append(Finding(
            "undefined", name, scope, line,
            f"'{name}' is read in {local}{f' ({mod})' if mod else ''} but "
            "never assigned or imported (typo, or a star-import?)", file))
    g.findings.sort(key=lambda f: (f.file, f.line, f.kind))


# ----------------------------------------------------------------------
# text report / command line
# ----------------------------------------------------------------------
def format_trace(g: VarGraph, var: Var, direction: str = "up",
                 max_depth: Optional[int] = None,
                 conditions: Optional[bool] = None) -> str:
    """A tree of what feeds (up) or is fed by (down) `var`, one edge a line."""
    lines = [f"{var.label}   [{var.kind}; written L{_lines(var.defined)}; "
             f"read L{_lines(var.used)}"
             + (f"; also {', '.join(var.elsewhere[:6])}" if var.elsewhere else "")
             + "]"]
    step = g.fed_by if direction == "up" else g.feeds
    other = "src" if direction == "up" else "dst"
    arrow = "<-" if direction == "up" else "->"

    expanded: Set[str] = {var.id}

    def rec(vid: str, depth: int, prefix: str) -> None:
        if max_depth is not None and depth >= max_depth:
            return
        edges = sorted(step(vid, conditions), key=lambda e: (e.line, getattr(e, other)))
        for i, e in enumerate(edges):
            last = i == len(edges) - 1
            nxt = getattr(e, other)
            v = g.vars[nxt]
            # A node is expanded once; later mentions point back to it.
            again = nxt in expanded and bool(step(nxt, conditions))
            lines.append(f"{prefix}{'└─' if last else '├─'}{arrow} {v.label}  "
                         f"[{e.kind} {_at(g, e)}: {e.code}]"
                         f"{'  (see above)' if again else ''}")
            if nxt not in expanded:
                expanded.add(nxt)
                rec(nxt, depth + 1, prefix + ("   " if last else "│  "))

    rec(var.id, 0, "  ")
    return "\n".join(lines)


def trace_report(g: VarGraph, var: Var, max_depth: Optional[int] = None,
                 conditions: Optional[bool] = None) -> str:
    """The whole trace of `var` as plain text, for a bug report or notes:
    what it is, what feeds it, what it feeds, and the findings on that path.
    The two trees are format_trace's, so they read the same as --up/--down."""
    want = g.conditions if conditions is None else conditions
    up = g.upstream(var.id, max_depth, want)
    down = g.downstream(var.id, max_depth, want)
    where = f" in {Path(var.file).name}" if var.file else ""
    seen = [f"written on {_lines(var.defined)}", f"read on {_lines(var.used)}"]
    if var.elsewhere:
        seen.append("also " + ", ".join(dict.fromkeys(var.elsewhere)))
    if g.modules:
        scripts = f"{len(g.modules)} files under {g.filename}"
    else:
        scripts = g.filename
    hops = "all" if max_depth is None else f"{max_depth} hop{'s' if max_depth != 1 else ''}"
    out = ["Variable Map trace",
           "",
           f"Variable    : {var.label}",
           f"Kind        : {var.kind}{where}; " + "; ".join(seen),
           f"Scripts     : {scripts}",
           f"Depth       : {hops}; if/while conditions {'on' if want else 'off'}",
           f"Reach       : {len(up)} variables can affect it; it can affect "
           f"{len(down)}",
           ""]
    for title, direction in (("WHAT FEEDS IT (where its value comes from)", "up"),
                             ("WHAT IT FEEDS (where its value goes)", "down")):
        body = format_trace(g, var, direction, max_depth, want).splitlines()[1:]
        out += [title, *(body or ["  (nothing)"]), ""]
    on_path = {var.id} | {s.var.id for s in up + down}
    found = [f for f in g.findings if f"{f.scope}::{f.name}" in on_path]
    if found:
        out.append("WORTH A LOOK ON THIS PATH")
        for f in found:
            at = f"{Path(f.file).name}:{f.line}" if g.modules else f"L{f.line}"
            out.append(f"  {at:<22s} {f.message}")
        out.append("")
    return "\n".join(out)


def write_text(path, text: str) -> Path:
    """Write `text` as UTF-8 via a temp file in the same folder, then swap it
    in: a failed write leaves the old file, never half of a new one."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def _at(g: VarGraph, e: Edge) -> str:
    """'L12', or 'handlers.py:12' when the graph spans several files."""
    return f"{Path(e.file).name}:{e.line}" if g.modules else f"L{e.line}"


def _lines(nums: List[int]) -> str:
    u = sorted(set(nums))
    if not u:
        return " -"
    return ",".join(str(n) for n in u[:8]) + ("…" if len(u) > 8 else "")


def summary(g: VarGraph) -> str:
    kinds: Dict[str, int] = {}
    for v in g.vars.values():
        kinds[v.kind] = kinds.get(v.kind, 0) + 1
    scopes = len({v.scope for v in g.vars.values()})
    parts = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    shown = sum(1 for e in g.edges if g._keep(e, None))
    return (f"{len(g.vars)} nodes ({parts}) in {scopes} scopes, "
            f"{shown} links, {len(g.findings)} things worth a look")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m council_core.var_graph",
        description="Show how the variables in a Python script - or several "
                    "scripts that import or launch each other - feed each other.")
    ap.add_argument("script", nargs="+",
                    help="a .py file, or a folder (every script in it is "
                         "linked; imports of outside packages are not followed)")
    ap.add_argument("--up", metavar="VAR", help="what feeds VAR (where a bad value came from)")
    ap.add_argument("--down", metavar="VAR", help="what VAR feeds (where a bad value goes)")
    ap.add_argument("--path", nargs=2, metavar=("FROM", "TO"),
                    help="the shortest chain from FROM to TO")
    ap.add_argument("--trace", metavar="VAR",
                    help="both directions plus the findings on the way, as a "
                         "report to save or paste")
    ap.add_argument("--out", metavar="FILE",
                    help="with --trace: write the report to FILE instead of printing it")
    ap.add_argument("--find", metavar="TEXT", help="list variables matching TEXT")
    ap.add_argument("--depth", type=int, default=None)
    ap.add_argument("--conditions", action="store_true",
                    help="also link if/while tests to what they guard")
    ap.add_argument("--dot", action="store_true", help="print Graphviz DOT")
    ap.add_argument("--json", action="store_true", help="print the graph as JSON")
    args = ap.parse_args(argv)

    try:
        g = analyze_paths(args.script, args.conditions)
    except SyntaxError as exc:
        print(f"{exc.filename} does not parse: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        if args.json:
            print(json.dumps(g.to_dict(), indent=1))
        elif args.dot:
            focus = None
            ref = args.up or args.down
            if ref:
                v = g.resolve(ref)
                steps = g.upstream(v.id, args.depth) if args.up else g.downstream(v.id, args.depth)
                focus = {v.id} | {s.var.id for s in steps}
            print(g.to_dot(focus))
        elif args.trace:
            report = trace_report(g, g.resolve(args.trace), args.depth)
            if args.out:
                print(f"wrote {write_text(args.out, report)}")
            else:
                print(report)
        elif args.up or args.down:
            v = g.resolve(args.up or args.down)
            print(format_trace(g, v, "up" if args.up else "down", args.depth))
        elif args.path:
            a, b = g.resolve(args.path[0]), g.resolve(args.path[1])
            chain = g.path(a.id, b.id)
            if not chain:
                print(f"no chain from {a.label} to {b.label}")
            for e in chain:
                print(f"{g.vars[e.src].label} -> {g.vars[e.dst].label}  "
                      f"[{e.kind} {_at(g, e)}: {e.code}]")
        elif args.find:
            for v in g.find(args.find):
                print(f"{v.id:40s} {v.kind:10s} written L{_lines(v.defined)}  "
                      f"read L{_lines(v.used)}"
                      + (f"  also {', '.join(v.elsewhere[:4])}" if v.elsewhere else ""))
        else:
            print(f"{g.filename}: {summary(g)}")
            if g.modules:
                print(f"  {len(g.modules)} files: " + ", ".join(sorted(g.modules)))
            for f in g.findings:
                at = f"{Path(f.file).name}:{f.line}" if g.modules else f"L{f.line}"
                print(f"  {at:<22s} {f.kind:15s} {f.message}")
            print("\nTry --up NAME, --down NAME, --trace NAME, --path A B, "
                  "--find TEXT, --dot")
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
