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
* A call into a function defined in the same file links the arguments to the
  parameters and the function's return to the result. A call to anything else
  (`len(x)`, `np.mean(x)`) links its arguments straight to the result.
* `x.append(v)`, `x[i] = v` and `x.attr = v` count as writing to `x`.
* `self.attr` is one node per class, shared by all its methods.
* Values that move through containers, `getattr`, globals in other modules, or
  callbacks are not followed.

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
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

MODULE = "<module>"
RETURN = "<return>"

#: Method calls that change the object they are called on.
MUTATORS = frozenset({
    "append", "extend", "insert", "update", "add", "setdefault", "pop",
    "popitem", "remove", "discard", "clear", "appendleft", "extendleft",
    "sort", "reverse", "write", "writelines", "put", "push", "fill",
    "set", "__setitem__",
})

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
    kind: str               # variable | param | attribute | return | import | global
    defined: List[int] = field(default_factory=list)   # lines it is written
    used: List[int] = field(default_factory=list)      # lines it is read

    @property
    def label(self) -> str:
        if self.kind == "return":
            return f"{self.scope}() return"
        if self.scope == MODULE:
            return self.name
        return f"{self.name}  ({self.scope})"


@dataclass
class Edge:
    """`src` flows into `dst` on `line`."""
    src: str
    dst: str
    kind: str               # assign | augassign | arg | return | iter | with | mutate | condition
    line: int
    code: str = ""          # the source line, stripped


@dataclass
class Finding:
    """Something in the graph that often turns out to be the bug."""
    kind: str               # undefined | unused | shadowed-param
    name: str
    scope: str
    line: int
    message: str


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
    """The graph for one source file. Build with :func:`analyze`."""

    def __init__(self, source: str, path: str = "<string>",
                 conditions: bool = False):
        self.source = source
        self.filename = path
        self.conditions = conditions
        self.lines = source.splitlines()
        self.vars: Dict[str, Var] = {}
        self.edges: List[Edge] = []
        self.findings: List[Finding] = []
        self._in: Dict[str, List[Edge]] = {}
        self._out: Dict[str, List[Edge]] = {}
        self._scopes: Dict[ast.AST, _Scope] = {}
        self._funcs: Dict[str, _Scope] = {}       # qualname -> function scope
        self._classes: Dict[str, _Scope] = {}     # qualname -> class scope
        self._edge_keys: Set[Tuple[str, str, str, int]] = set()
        self._unresolved: List[Tuple[str, int, str]] = []
        self._instances: Dict[str, str] = {}      # var id -> class it holds

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
        """A node from an id ('func::x'), a 'func.x'/'x' style reference, or a
        bare name if exactly one variable has it."""
        if ref in self.vars:
            return self.vars[ref]
        for v in self.vars.values():
            if f"{v.scope}.{v.name}" == ref or (v.scope == MODULE and v.name == ref):
                return v
        same = [v for v in self.vars.values() if v.name == ref]
        if len(same) == 1:
            return same[0]
        if not same:
            raise KeyError(f"no variable called {ref!r}")
        raise KeyError(f"{ref!r} is ambiguous: "
                       + ", ".join(v.id for v in same))

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
        return {"path": self.filename,
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
            v = self.vars[vid] = Var(vid, name, scope, kind)
        elif kind in ("param", "import", "attribute", "return") and v.kind == "variable":
            v.kind = kind
        return v

    def _edge(self, src: str, dst: str, kind: str, line: int) -> None:
        if src == dst and kind not in ("augassign", "mutate"):
            return
        key = (src, dst, kind, line)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        code = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        e = Edge(src, dst, kind, line, code)
        self.edges.append(e)
        self._in.setdefault(dst, []).append(e)
        self._out.setdefault(src, []).append(e)


def analyze(source: str, path: str = "<string>",
            conditions: bool = False) -> VarGraph:
    """Build the graph for `source`. Raises SyntaxError if it does not parse."""
    tree = ast.parse(source, filename=path)
    g = VarGraph(source, path, conditions)
    _Collector(g).run(tree)
    _Linker(g).run(tree)
    _find_problems(g)
    return g


def analyze_file(path, conditions: bool = False) -> VarGraph:
    p = Path(path)
    return analyze(p.read_text(encoding="utf-8", errors="replace"), str(p),
                   conditions)


# ----------------------------------------------------------------------
# pass 1: which names live in which scope
# ----------------------------------------------------------------------
class _Collector(ast.NodeVisitor):
    def __init__(self, g: VarGraph):
        self.g = g
        self.stack: List[_Scope] = []

    def run(self, tree: ast.Module) -> None:
        mod = _Scope(MODULE, "module", None, tree)
        self.g._scopes[tree] = mod
        self.stack.append(mod)
        self.generic_visit(tree)

    @property
    def cur(self) -> _Scope:
        return self.stack[-1]

    def _qual(self, name: str) -> str:
        return name if self.cur.kind == "module" else f"{self.cur.name}.{name}"

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

    def visit_Import(self, node) -> None:
        for alias in node.names:
            self.cur.locals.add((alias.asname or alias.name).split(".")[0])

    visit_ImportFrom = visit_Import

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
    def __init__(self, g: VarGraph):
        self.g = g
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
                self.g._unresolved.append((name, line, scope))

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
        v.used.append(line)
        return v.id

    def _write_name(self, name: str, line: int, kind: str = "variable") -> str:
        sc = self._owner(name) or self.cur
        v = self.g._var(sc.name, name,
                        "param" if name in sc.params else kind)
        v.defined.append(line)
        return v.id

    def _self_attr(self, node: ast.AST) -> Optional[Tuple[str, str]]:
        """('Class', 'self.x') when `node` is self.x inside a method of Class,
        or obj.x where obj was last assigned `Class(...)`."""
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            sc = self.cur
            while sc is not None and sc.kind != "function":
                sc = sc.parent
            if sc and sc.self_name and node.value.id == sc.self_name \
                    and self._owner(node.value.id) is sc:
                return sc.cls, f"self.{node.attr}"
            owner = self._owner(node.value.id)
            if owner is not None:
                cls = self.g._instances.get(f"{owner.name}::{node.value.id}")
                if cls:
                    return cls, f"self.{node.attr}"
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
        """'Cart' when `node` is Cart(...) for a class defined in this file."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            sc = self._owner(node.func.id)
            if sc is not None:
                name = node.func.id if sc.kind == "module" else f"{sc.name}.{node.func.id}"
                if name in self.g._classes:
                    return name
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
        if isinstance(node, ast.Attribute):
            sa = self._self_attr(node)
            if sa:
                v = self.g._var(sa[0], sa[1], "attribute")
                v.used.append(node.lineno)
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
        """The function scope a call lands in, when it is defined in this file."""
        name = None
        if isinstance(func, ast.Name):
            sc = self._owner(func.id)
            if sc is None:
                return None
            name = func.id if sc.kind == "module" else f"{sc.name}.{func.id}"
        else:
            sa = self._self_attr(func)
            if sa:
                name = f"{sa[0]}.{sa[1][5:]}"
        if name is None:
            return None
        if name in self.g._classes:
            return self.g._funcs.get(f"{name}.__init__")
        return self.g._funcs.get(name)

    def _call(self, node: ast.Call) -> Set[str]:
        line = node.lineno
        target = self._callee(node.func)
        arg_deps = [self.deps(a.value if isinstance(a, ast.Starred) else a)
                    for a in node.args]
        kw_deps = [(k.arg, self.deps(k.value)) for k in node.keywords]
        out: Set[str] = set()
        if target is not None:
            params = list(target.params)
            if target.self_name and params:
                params = params[1:]
            for i, d in enumerate(arg_deps):
                if i < len(params):
                    p = self.g._var(target.name, params[i], "param")
                    for s in d:
                        self.g._edge(s, p.id, "arg", line)
            for kname, d in kw_deps:
                if kname and kname in target.params:
                    p = self.g._var(target.name, kname, "param")
                    for s in d:
                        self.g._edge(s, p.id, "arg", line)
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
                        self.g.vars[vid].defined.append(line)
                        out.append(vid)
                else:
                    out.append(self._write_name(t.id, line))
            elif isinstance(t, ast.Attribute):
                sa = self._self_attr(t)
                if sa:
                    v = self.g._var(sa[0], sa[1], "attribute")
                    v.defined.append(line)
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
                # x.a = v / x[i] = v changes x; self.a = v is a plain write.
                k = kind
                if kind == "assign" and not plain \
                        and self.g.vars[dst].kind != "attribute":
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
                    self.g._var(sc.name, p, "param").defined.append(node.lineno)
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
                ret.defined.append(line)
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
                name = (alias.asname or alias.name).split(".")[0]
                if name != "*":
                    self._write_name(name, line, "import")
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
    for v in g.vars.values():
        if v.kind in ("return", "attribute"):
            continue
        if v.defined and not v.used and not g._out.get(v.id) \
                and v.scope != MODULE and not v.name.startswith("_") \
                and v.kind != "param":
            g.findings.append(Finding(
                "unused", v.name, v.scope, v.defined[0],
                f"'{v.name}' is assigned in {v.scope} but never read"))
    for v in g.vars.values():
        if v.kind == "param" and v.defined and len(set(v.defined)) > 1:
            later = sorted(set(v.defined))[1]
            g.findings.append(Finding(
                "shadowed-param", v.name, v.scope, later,
                f"parameter '{v.name}' of {v.scope} is overwritten on line "
                f"{later}; callers' value is lost after that"))
    attrs_written = {v.id for v in g.vars.values()
                     if v.kind == "attribute" and v.defined}
    for v in g.vars.values():
        if v.kind == "attribute" and not v.defined and v.used \
                and v.id not in attrs_written:
            cls_var = f"{v.scope}::{v.name[5:]}"
            if cls_var in g.vars or v.scope not in g._classes:
                continue
            g.findings.append(Finding(
                "undefined", v.name, v.scope, v.used[0],
                f"'{v.name}' is read in class {v.scope} but never assigned "
                "there (inherited, set from outside, or a typo?)"))
    for name, line, scope in g._unresolved:
        g.findings.append(Finding(
            "undefined", name, scope, line,
            f"'{name}' is read in {scope} but never assigned anywhere in "
            "this file (typo, or a star-import?)"))
    g.findings.sort(key=lambda f: (f.line, f.kind))


# ----------------------------------------------------------------------
# text report / command line
# ----------------------------------------------------------------------
def format_trace(g: VarGraph, var: Var, direction: str = "up",
                 max_depth: Optional[int] = None,
                 conditions: Optional[bool] = None) -> str:
    """A tree of what feeds (up) or is fed by (down) `var`, one edge a line."""
    lines = [f"{var.label}   [{var.kind}; written L{_lines(var.defined)}; "
             f"read L{_lines(var.used)}]"]
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
                         f"[{e.kind} L{e.line}: {e.code}]"
                         f"{'  (see above)' if again else ''}")
            if nxt not in expanded:
                expanded.add(nxt)
                rec(nxt, depth + 1, prefix + ("   " if last else "│  "))

    rec(var.id, 0, "  ")
    return "\n".join(lines)


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
        description="Show how the variables in a Python script feed each other.")
    ap.add_argument("script")
    ap.add_argument("--up", metavar="VAR", help="what feeds VAR (where a bad value came from)")
    ap.add_argument("--down", metavar="VAR", help="what VAR feeds (where a bad value goes)")
    ap.add_argument("--path", nargs=2, metavar=("FROM", "TO"),
                    help="the shortest chain from FROM to TO")
    ap.add_argument("--find", metavar="TEXT", help="list variables matching TEXT")
    ap.add_argument("--depth", type=int, default=None)
    ap.add_argument("--conditions", action="store_true",
                    help="also link if/while tests to what they guard")
    ap.add_argument("--dot", action="store_true", help="print Graphviz DOT")
    ap.add_argument("--json", action="store_true", help="print the graph as JSON")
    args = ap.parse_args(argv)

    try:
        g = analyze_file(args.script, args.conditions)
    except SyntaxError as exc:
        print(f"{args.script} does not parse: {exc}", file=sys.stderr)
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
                      f"[{e.kind} L{e.line}: {e.code}]")
        elif args.find:
            for v in g.find(args.find):
                print(f"{v.id:40s} {v.kind:10s} written L{_lines(v.defined)}  "
                      f"read L{_lines(v.used)}")
        else:
            print(f"{g.filename}: {summary(g)}")
            for f in g.findings:
                print(f"  L{f.line:<5d} {f.kind:15s} {f.message}")
            print("\nTry --up NAME, --down NAME, --path A B, --find TEXT, --dot")
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
