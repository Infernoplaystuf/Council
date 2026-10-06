"""
nx_ground.py — does a simplnx script (or a pipeline value) name things that
EXIST in the installed package, with arguments of the right type?

Runs in the APP env: pure ast + the catalog nx_introspect built from the
installed binary. Nothing here imports simplnx or runs the script.

nx_policy.validate_script answers "may the app run this?" — imports, shell,
the two code-execution filters. It never asked whether the script calls real
things, so it accepted

    r = nx.TotallyMadeUpFilter.execute(bogus_param='x')

as readily as a correct script. Measured with llama3.1:8b through the Qt
"Write pipeline" button: 12 of 12 scripts accepted, 0 of 12 ran — `nx` never
imported, filters instantiated and given attributes, invented APIs
(ds.add_filter, nx.ImageGeometry), a keyword the filter does not take.

check_script() catches those before anything runs, from the catalog alone:

  * every name a script reads must be defined or imported (`nx` used without
    `import simplnx as nx` is a NameError on line one);
  * every nx.<Name> must exist in that module, and every filter must be the
    module's own (ReadAngDataFilter lives in orientationanalysis, not nx);
  * a filter is called as nx.<Filter>.execute(...), never instantiated;
  * every execute() keyword must be a real parameter of THAT filter, and
    data_structure must be passed (the one parameter with no default);
  * every value whose type can be read off the source is checked against the
    catalog's type for that parameter — the binding casts nothing: a str for a
    DataPath, an int for a NumericType, a float for an int, a dict for a
    compound parameter all raise "Unable to cast" at run time (each measured
    against the installed build);
  * attributes read off ds, an execute() result or a compound parameter
    object must exist on that class (ds.add_filter does not), and a compound
    object's attributes are type-checked when assigned;
  * the DataStructure is not lost: ds[path] = ... (no item assignment),
    ds = nx.X.execute(data_structure=ds, ...) (that is the result, not ds),
    and ds[path] passed where a DataPath goes (that is the object, not the
    path) — the three a re-run with llama3.1:8b still produced.

Each error names the exact unknown thing and the nearest real ones, and
`suggest` carries the real filters the script reached for, so the repair
prompt can show their signatures.

Which filter a call runs, and which keywords it passes, are followed through
every way a script holds them (_Flow): an alias however it is bound (plain,
annotated, walrus, tuple unpacking, rebound — checked against the binding
that reaches the call when straight-line code makes that certain, else
against every value it may hold, needing to fit one), a list / tuple / dict
of filters and an index into it, a loop over filters, `A if c else B`, a
helper `def run(f, **kw): f.execute(...)` (checked per call site; a method's
callers are unknown, so it is reported), and `**params` built from a dict
literal or dict(...). These
all used to skip the check: `F, H = nx.A, nx.B; F.execute(bogus=1)` and
`nx.A.execute(data_structure=ds, **{'dims': ...})` were accepted and died
with TypeError in simplnx. What it still cannot follow — a filter or a
keyword set computed at run time — is an error, not a pass: the app does
not run what it could not check, and the message says how to write it
checkably (each filter by name, each parameter as a keyword).

What it cannot see: a VALUE it cannot read off the source (a function's
return, a number read from a file) passes; so does a path that does not
name anything in YOUR data (the model wrote 'Data Container/Feature Data'
for a file holding 'DataContainer/Cell Feature Data') and a value simplnx
refuses on its own terms (start_import_row=0). Those fail when simplnx runs
the script — simplnx is still the final gate, and only a trial run on real
input could catch them earlier.
"""
from __future__ import annotations

import ast
import builtins
import difflib
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import nx_policy

MODULE_ALIASES = {"simplnx": "nx", "orientationanalysis": "nxor",
                  "itkimageprocessing": "nxitk"}
NX_MODULES = frozenset(MODULE_ALIASES)

# Before catalog schema 2 there was no filter_attrs; these are the installed
# build's (dir() of any filter class).
_FILTER_ATTRS_FALLBACK = frozenset({
    "ExecuteResult", "Message", "MessageHandler", "PreflightResult",
    "PreflightValue", "ProgressMessage", "execute", "execute2", "human_name",
    "name", "parameters_version", "preflight2", "uuid"})

# execute2 / preflight2 are INSTANCE methods (self: IFilter) taking **kwargs
# with no parameter list. Called on the class, as a script must (a filter is
# never instantiated), they fail every time: "incompatible function
# arguments" (measured on the installed build).
_INSTANCE_ONLY = frozenset({"execute2", "preflight2"})
_RUN_ATTRS = frozenset({"execute"}) | _INSTANCE_ONLY

_IMPORT_HINT = {"nx": "import simplnx as nx",
                "nxor": "import orientationanalysis as nxor",
                "nxitk": "import itkimageprocessing as nxitk",
                "np": "import numpy as np",
                "Path": "from pathlib import Path",
                "json": "import json", "math": "import math", "re": "import re"}

# Plus what every module has without a builtin of that name.
_BUILTINS = frozenset(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__",
    "__package__", "__builtins__", "__annotations__", "__cached__"}
_SIMPLE = {"int", "float", "bool", "str"}


# ============================================================
# Types: what the catalog says a parameter is
# ============================================================

def classify(type_str: str, catalog: dict) -> Tuple[str, Any]:
    """('datapath'|'enum'|'int'|'float'|'bool'|'str'|'path'|'list'|'optional'
    |'object'|'ds'|'any', detail) for a type string from the catalog."""
    t = (type_str or "").strip()
    if not t:
        return "any", None
    enums = catalog.get("enums") or {}
    if t in enums:
        return "enum", t
    m = re.fullmatch(r"(?:list|List)\[(.*)\]", t)
    if m:
        return "list", m.group(1).strip()
    m = re.fullmatch(r"Annotated\[(.*?),\s*FixedSize\(\d+\)\]", t)
    if m:
        return classify(m.group(1), catalog)
    m = re.fullmatch(r"Optional\[(.*)\]", t)
    if m:
        return "optional", m.group(1).strip()
    if t in _SIMPLE:
        return t, None
    if t == "os.PathLike":
        return "path", None
    if t == "DataPath" or t.endswith(".DataPath"):
        return "datapath", None
    if t.endswith(".DataStructure"):
        return "ds", None
    if t in (catalog.get("classes") or {}) or re.fullmatch(
            r"(simplnx|orientationanalysis|itkimageprocessing)"
            r"(\.[A-Za-z_]\w*)+", t):
        return "object", t
    return "any", None


def py_name(path: str) -> str:
    """'simplnx.NumericType' -> 'nx.NumericType' (how a script spells it)."""
    mod, _, rest = path.partition(".")
    return f"{MODULE_ALIASES.get(mod, mod)}.{rest}" if rest else path


def _members(catalog: dict, enum_key: str) -> List[str]:
    m = (catalog.get("enums") or {}).get(enum_key) or {}
    return [m[k] for k in sorted(m, key=lambda x: int(x))] if all(
        str(k).lstrip("-").isdigit() for k in m) else list(m.values())


def how_to_write(type_str: str, catalog: dict) -> str:
    """A correct value of this type, as source."""
    kind, arg = classify(type_str, catalog)
    if kind == "datapath":
        return "nx.DataPath('Group/Array')"
    if kind == "enum":
        mem = _members(catalog, arg)
        return f"{py_name(arg)}.{mem[0] if mem else '<member>'}"
    if kind == "int":
        return "an int such as 3"
    if kind == "float":
        return "a number such as 1.5"
    if kind == "bool":
        return "True or False"
    if kind == "str":
        return "a string"
    if kind == "path":
        return "a path string such as 'C:/data/out.dream3d'"
    if kind == "list":
        return f"a list of {how_to_write(arg, catalog)}"
    if kind == "optional":
        return f"None or {how_to_write(arg, catalog)}"
    if kind == "object":
        return construct_hint(arg, catalog)
    return "a value"


def construct_hint(class_path: str, catalog: dict) -> str:
    """How to build a compound parameter value, from the catalog's record of
    the class: its constructor, or default-construct and set its
    properties."""
    rec = (catalog.get("classes") or {}).get(class_path)
    name = py_name(class_path)
    if not rec:
        return f"a {name} object"
    inits = rec.get("init") or []
    props = rec.get("props") or {}
    if [] in inits or not inits:
        settable = [f"{p} ({t})" for p, t in props.items()
                    if p not in (rec.get("readonly") or [])
                    and p.isidentifier()]
        return (f"a {name} object: v = {name}(), then set "
                + ", ".join(f"v.{s}" for s in settable[:12]) + ", and pass v")
    sig = ", ".join(f"{p['name']}: {p.get('type')}" for p in inits[0])
    return f"a {name} object: {name}({sig})"


# ============================================================
# Pipeline JSON values (nx_generate.validate)
# ============================================================

def json_value_problem(value: Any, type_str: str,
                       catalog: dict) -> Optional[str]:
    """Why ``value`` (from a .d3dpipeline-shaped args dict, envelope already
    unwrapped) cannot be a ``type_str``, or None. Mirrors what the
    transpiler can render into something the binding accepts."""
    kind, arg = classify(type_str, catalog)
    if kind in ("any", "ds"):
        return None
    if value is None:
        return None if kind == "optional" else "is null — give it a value"
    if kind == "optional":
        return json_value_problem(value, arg, catalog)
    if kind == "datapath":
        if isinstance(value, str) or (isinstance(value, list) and all(
                isinstance(x, str) for x in value)):
            return None
        return (f"must be a data path string such as 'Group/Array', got "
                f"{type(value).__name__} {value!r}"[:200])
    if kind == "enum":
        m = (catalog.get("enums") or {}).get(arg) or {}
        if isinstance(value, int) and not isinstance(value, bool) and (
                value in m or str(value) in m):
            return None
        opts = ", ".join(f"{k} ({v})" for k, v in list(m.items())[:12])
        return f"must be one of {opts}; got {value!r}"[:300]
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return None
        return f"must be an int, got {type(value).__name__} {value!r}"[:200]
    if kind == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return None
        return f"must be a number, got {type(value).__name__} {value!r}"[:200]
    if kind == "bool":
        if isinstance(value, bool) or value in (0, 1):
            return None
        return f"must be true or false, got {value!r}"[:200]
    if kind in ("str", "path"):
        if isinstance(value, str):
            return None
        return f"must be a string, got {type(value).__name__} {value!r}"[:200]
    if kind == "list":
        if not isinstance(value, list):
            return (f"must be a list ({type_str}), got "
                    f"{type(value).__name__} {value!r}")[:200]
        for i, x in enumerate(value):
            why = json_value_problem(x, arg, catalog)
            if why:
                return f"element {i} {why}"
        return None
    if kind == "object":
        if isinstance(value, dict):
            return None
        return (f"must be an object ({type_str}), got "
                f"{type(value).__name__} {value!r}")[:200]
    return None


# ============================================================
# Nearest real names
# ============================================================

def _split_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ",
                  str(s or ""))


def nearest_filters(name: str, catalog: dict, n: int = 3) -> List[dict]:
    """The permitted filters a misspelt / invented filter name most likely
    meant: spelling first, then the words in it (retrieve's lexical score).
    Never a denied filter — the repair prompt must not hand one over."""
    permitted = nx_policy.permitted_filters(catalog)
    by_attr = {f["py_attr"]: f for f in permitted if f.get("py_attr")}
    out = [by_attr[x] for x in difflib.get_close_matches(
        name, list(by_attr), n=n, cutoff=0.6)]
    if len(out) < n:
        import nx_generate                 # lazy: nx_generate imports us
        words = _split_camel(re.sub(r"Filter$", "", name or ""))
        for e in nx_generate.retrieve(catalog, words, k=n):
            if e not in out:
                out.append(e)
    return out[:n]


def _near(name: str, pool, n: int = 3, cutoff: float = 0.55) -> List[str]:
    return difflib.get_close_matches(name, list(pool), n=n, cutoff=cutoff)


def _and_nearest(name: str, pool, n: int = 3) -> str:
    near = _near(name, pool, n)
    return f" Nearest: {', '.join(near)}." if near else ""


# ============================================================
# Which value a name may hold (for "which filter does this call run")
# ============================================================

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMP_NODES = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
# Methods that change a dict / list / set in place: one changed after it is
# built cannot be read off its literal (`fs = []; fs.append(nx.X)`).
_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear",
                       "__setitem__", "__delitem__", "append", "extend",
                       "insert", "remove", "sort", "reverse", "add",
                       "discard", "__iadd__"})
# Builtins whose elements are the elements of their argument(s).
_PASS_THROUGH = frozenset({"list", "tuple", "reversed", "sorted", "set",
                           "frozenset", "iter"})
_MAX_ENVS = 64          # loop iterations x call sites checked per call
_MAX_DEPTH = 8          # bindings followed through


def _src(node, n: int = 60) -> str:
    try:
        s = ast.unparse(node)
    except Exception:                                     # noqa: BLE001
        s = "..."
    return s if len(s) <= n else s[:n - 3] + "..."


class _Flow:
    """Every value a name in a script may hold, scope by scope.

    The checker used to resolve a name only through ONE plain assignment
    (`F = nx.X`). Any other binding — `F, H = ...`, `F: T = ...`, `F := ...`,
    a second `F = ...`, `for F in (...)`, `fs[0]`, `fs['g']`, `A if c else
    B`, a helper's argument — made the call unresolvable, and an
    unresolvable call was not checked at all: not its keywords, not their
    values. This follows each of them.

    A binding is a record per (scope, name): ("bind", value, path, is_iter)
    — the name takes ``value`` (or, is_iter, each element of it) indexed by
    ``path`` for tuple unpacking — or ("def", node), ("import",), or
    ("unknown", why).

    An ENV binds what depends on how the code is reached: a loop variable to
    one element (one env per iteration) and a function's parameters to one
    call site's arguments (one env per call site), so `for f, kw in steps:
    f.execute(**kw)` and `def run(f, **kw)` called twice are checked pair by
    pair, never filter A against filter B's keywords. Keys are (id(scope),
    name); values ("expr", node, env) | ("kwargs", keywords, spreads, env) |
    ("unknown", why).
    """

    def __init__(self, tree: ast.AST):
        self.tree = tree
        self.parent: Dict[ast.AST, ast.AST] = {}
        for p in ast.walk(tree):
            for ch in ast.iter_child_nodes(p):
                self.parent[ch] = p
        self.records: Dict[Tuple[int, str], List[tuple]] = {}
        # Parallel to records: (the statement that binds, whether the name
        # is that statement's own target) for each record.
        self.where: Dict[Tuple[int, str], List[Tuple[Any, bool]]] = {}
        self.mutated: set = set()
        self._globals: Dict[int, set] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                self._globals.setdefault(id(self.scope_of(node)),
                                         set()).update(node.names)
        self._collect()

    # ---- scopes ------------------------------------------------------------
    def scope_of(self, node) -> ast.AST:
        """The function (or the module) whose names ``node`` reads.
        A function's defaults and decorators run in the enclosing scope."""
        child, p = node, self.parent.get(node)
        while p is not None:
            if isinstance(p, _SCOPE_NODES) and not isinstance(
                    child, ast.arguments) and child not in getattr(
                    p, "decorator_list", ()) \
                    and child is not getattr(p, "returns", None):
                return p
            child, p = p, self.parent.get(p)
        return self.tree

    @staticmethod
    def params(fn) -> List[str]:
        a = fn.args
        out = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
        if a.vararg:
            out.append(a.vararg.arg)
        if a.kwarg:
            out.append(a.kwarg.arg)
        return out

    # ---- pass 1: every binding ---------------------------------------------
    def _add(self, at, name: str, rec: tuple) -> None:
        scope = self.scope_of(at)
        if scope is not self.tree and name in self._globals.get(id(scope), ()):
            scope = self.tree
        self.records.setdefault((id(scope), name), []).append(rec)
        stmt = self.stmt_of(at)
        self.where.setdefault((id(scope), name), []).append(
            (stmt, self._is_target(stmt, at)))

    def stmt_of(self, node):
        """The statement ``node`` is part of (itself, if it is one)."""
        while node is not None and not isinstance(node, ast.stmt):
            node = self.parent.get(node)
        return node

    @staticmethod
    def _is_target(stmt, at) -> bool:
        """Is ``at`` what ``stmt`` itself binds — an assignment's target, an
        import, a def — rather than a walrus or a comprehension inside it?"""
        if at is stmt:
            return isinstance(stmt, (ast.Import, ast.ImportFrom,
                                     ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef))
        if isinstance(stmt, ast.Assign):
            roots = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            roots = [stmt.target]
        else:
            return False
        return any(n is at for r in roots for n in ast.walk(r))

    def within(self, node, container) -> bool:
        while node is not None:
            if node is container:
                return True
            node = self.parent.get(node)
        return False

    def reaching(self, use: ast.Name, recs: List[tuple]
                 ) -> Optional[List[tuple]]:
        """The bindings of a REBOUND name that reach ``use``, when straight-
        line code makes that certain; None when it does not (a branch, a
        loop, a binding in an enclosing scope), and then every binding counts.

        `F = nx.A`, then `F = nx.B`, then `F.execute(...)` runs B. Counting
        every binding made the call pass if it fit EITHER filter, so one of
        A's keywords passed to B was accepted — and so was a `params` dict
        with a typo whenever a later `params = {...}` happened to fit; both
        raise TypeError in simplnx."""
        key = (id(self.scope_of(use)), use.id)
        if self.records.get(key) is not recs or len(recs) < 2:
            return None                   # bound in an enclosing scope
        where = self.where.get(key) or []
        stmts = [w[0] for w in where]
        u = self.stmt_of(use)
        if u is None or any(self.within(s, u) for s in stmts):
            return None                   # bound in the same statement
        cur = u
        while True:
            par = self.parent.get(cur)
            if par is None:
                return None
            block = next((getattr(par, f) for f in ("body", "orelse",
                                                    "finalbody")
                          if isinstance(getattr(par, f, None), list)
                          and any(x is cur for x in getattr(par, f))), None)
            if block is not None:
                i = next(j for j, x in enumerate(block) if x is cur)
                for s in reversed(block[:i]):
                    direct = [r for r, (st, d) in zip(recs, where)
                              if st is s and d]
                    if direct:
                        return direct
                    if any(self.within(st, s) for st in stmts):
                        return None       # bound inside a branch or a loop
            if isinstance(par, _SCOPE_NODES + (ast.Module, ast.ClassDef)):
                return None               # nothing binds it before the use
            if isinstance(par, (ast.For, ast.AsyncFor, ast.While)):
                # The loop comes round again: a binding anywhere in it (its
                # target, a statement after the use) may be the one.
                if any(self.within(st, par) for st in stmts):
                    return None
            elif any(self.within(st, par) and not any(
                    self.within(st, x) for x in (block or [cur]))
                    for st in stmts):
                return None               # bound in a test or another branch
            cur = par

    def _bind(self, target, value, path: tuple, is_iter: bool) -> None:
        if isinstance(target, ast.Name):
            self._add(target, target.id, ("bind", value, path, is_iter))
        elif isinstance(target, (ast.Tuple, ast.List)):
            if any(isinstance(e, ast.Starred) for e in target.elts):
                self._unknown(target, "it is bound by a starred unpacking")
                return
            for i, e in enumerate(target.elts):
                self._bind(e, value, path + (i,), is_iter)
        elif isinstance(target, ast.Starred):
            self._unknown(target, "it is bound by a starred unpacking")
        # an Attribute / Subscript target binds no name

    def _unknown(self, target, why: str) -> None:
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                self._add(n, n.id, ("unknown", why))

    def _collect(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    self._bind(t, node.value, (), False)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._bind(node.target, node.value, (), False)
            elif isinstance(node, ast.NamedExpr):
                self._bind(node.target, node.value, (), False)
            elif isinstance(node, ast.AugAssign):
                self._unknown(node.target, "it is changed in place")
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._bind(node.target, node.iter, (), True)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for it in node.items:
                    if it.optional_vars is not None:
                        self._unknown(it.optional_vars,
                                      "it is bound by a with statement")
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self._add(node, node.name, ("unknown", "it is an exception"))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                self._add(node, node.name, ("def", node))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        self._add(node, a.asname or a.name.split(".")[0],
                                  ("import",))
            elif type(node).__name__ in ("MatchAs", "MatchStar") \
                    and getattr(node, "name", None):
                self._add(node, node.name, ("unknown", "it is bound by match"))
            elif type(node).__name__ == "MatchMapping" \
                    and getattr(node, "rest", None):
                self._add(node, node.rest, ("unknown", "it is bound by match"))
            if isinstance(node, ast.Subscript) and isinstance(
                    node.ctx, (ast.Store, ast.Del)) \
                    and isinstance(node.value, ast.Name):
                self.mutated.add(node.value.id)
            elif isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in _MUTATORS \
                    and isinstance(node.func.value, ast.Name):
                self.mutated.add(node.func.value.id)

    # ---- lookups -----------------------------------------------------------
    def lookup(self, name: str, at, env) -> Tuple[str, Any]:
        """("env", binding) | ("recs", [records]) | ("free", None) for the
        name ``name`` read at ``at``."""
        scope = self.scope_of(at)
        while True:
            key = (id(scope), name)
            if env and key in env:
                return "env", env[key]
            if isinstance(scope, _SCOPE_NODES) and name in self.params(scope):
                what = (f"{scope.name}()" if isinstance(scope, _FUNC_NODES)
                        else "a lambda")
                return "env", ("unknown", f"{name} is an argument of {what}, "
                               f"which is not called in a way this check can "
                               f"follow")
            recs = self.records.get(key)
            if recs:
                return "recs", recs
            if scope is self.tree:
                return "free", None
            scope = self.scope_of(scope)

    def values(self, expr, env, strict: bool = True, depth: int = 0):
        """([(leaf, strict, env)], [why]) — every expression ``expr`` may
        evaluate to, followed through names, `a if c else b` and indexing
        into a literal list / tuple / dict. ``strict`` is False for the
        values of a REBOUND name: which one a given call sees depends on
        the order the code runs in, so it only needs one to fit."""
        if depth > _MAX_DEPTH:
            return [], ["it is bound through too many steps"]
        if isinstance(expr, ast.Name):
            kind, data = self.lookup(expr.id, expr, env)
            if kind == "env":
                return self._binding_values(data, strict, depth)
            if kind == "recs":
                if any(r[0] == "import" for r in data):
                    return [(expr, strict, env)], []
                data = self.reaching(expr, data) or data
                loose = len(data) > 1
                out, whys = [], []
                for rec in data:
                    leaves, w = self._record_values(rec, env,
                                                    strict and not loose,
                                                    depth + 1)
                    out += leaves
                    whys += w
                return out, whys
            return [], [f"{expr.id} is not assigned in the script"]
        if isinstance(expr, ast.NamedExpr):
            return self.values(expr.value, env, strict, depth + 1)
        if isinstance(expr, ast.IfExp):
            a, wa = self.values(expr.body, env, strict, depth + 1)
            b, wb = self.values(expr.orelse, env, strict, depth + 1)
            return a + b, wa + wb
        if isinstance(expr, ast.Subscript):
            return self._subscript_values(expr, env, strict, depth)
        return [(expr, strict, env)], []

    def _binding_values(self, b: tuple, strict: bool, depth: int):
        if b[0] == "expr":
            return self.values(b[1], b[2], strict, depth + 1)
        if b[0] == "kwargs":
            return [], ["it is a function's **keywords"]
        return [], [b[1]]

    def _record_values(self, rec: tuple, env, strict: bool, depth: int):
        if rec[0] == "unknown":
            return [], [rec[1]]
        if rec[0] == "def":
            return [(rec[1], strict, env)], []
        _kind, value, path, is_iter = rec
        if is_iter:
            cur, whys = self.elements(value, env, strict, depth + 1)
        else:
            cur, whys = [(value, strict, env)], []
        for idx in path:
            nxt = []
            for node, st, e in cur:
                els, w = self.elements(node, e, st, depth + 1, index=idx)
                nxt += els
                whys += w
            cur = nxt
        out = []
        for node, st, e in cur:
            leaves, w = self.values(node, e, st, depth + 1)
            out += leaves
            whys += w
        return out, whys

    def elements(self, expr, env, strict: bool, depth: int,
                 index: Optional[int] = None):
        """The elements of a literal list / tuple / set ``expr`` evaluates
        to (only element ``index`` when given): ([(node, strict, env)],
        [why]). enumerate / zip / d.items() and list(...)-style wrappers
        of a literal are followed too."""
        if depth > _MAX_DEPTH:
            return [], ["it is bound through too many steps"]
        if isinstance(expr, ast.Name) and expr.id in self.mutated:
            return [], [f"{expr.id} is changed after it is built"]
        conts, whys = self.values(expr, env, strict, depth + 1)
        out = []
        for c, st, e in conts:
            if isinstance(c, ast.Call):
                items, w = self._call_elements(c, e, st, depth + 1)
                whys += w
                if items is not None:
                    if index is None:
                        out += items
                    elif -len(items) <= index < len(items):
                        out.append(items[index])
                    else:
                        whys.append("it unpacks a sequence of another length")
                    continue
            if isinstance(c, (ast.List, ast.Tuple, ast.Set)):
                if any(isinstance(x, ast.Starred) for x in c.elts):
                    whys.append("it comes out of a *-unpacked sequence")
                    continue
                if index is None:
                    out += [(x, st, e) for x in c.elts]
                elif -len(c.elts) <= index < len(c.elts):
                    out.append((c.elts[index], st, e))
                else:
                    whys.append("it unpacks a sequence of another length")
            elif isinstance(c, ast.Dict) and index is None:
                whys.append("it iterates over a dict's keys")
            else:
                whys.append("it comes out of a sequence built at run time")
        return out, whys

    def _call_elements(self, call: ast.Call, env, strict: bool, depth: int):
        """(elements, [why]) of enumerate(x) / zip(a, b) / d.items() /
        d.values() / d.keys() / list(x)-style calls over literals, with
        synthesized tuples where Python yields them; (None, []) for any
        other call."""
        f = call.func
        if isinstance(f, ast.Name) and not call.keywords \
                and self.lookup(f.id, call, env)[0] == "free":
            if f.id in _PASS_THROUGH and len(call.args) == 1:
                return self.elements(call.args[0], env, strict, depth + 1)
            if f.id == "enumerate" and len(call.args) in (1, 2):
                start = 0
                if len(call.args) == 2:
                    a = call.args[1]
                    if not (isinstance(a, ast.Constant)
                            and isinstance(a.value, int)):
                        return [], ["enumerate starts at a computed index"]
                    start = a.value
                els, whys = self.elements(call.args[0], env, strict,
                                          depth + 1)
                return [(ast.Tuple(elts=[ast.Constant(start + i), x],
                                   ctx=ast.Load()), st, e)
                        for i, (x, st, e) in enumerate(els)], whys
            if f.id == "zip" and call.args:
                cols, whys = [], []
                for a in call.args:
                    els, w = self.elements(a, env, strict, depth + 1)
                    whys += w
                    cols.append(els)
                if whys:
                    return [], whys
                rows = []
                for row in zip(*cols):
                    # the elements may come from different envs; zip pairs
                    # literals, which need none
                    rows.append((ast.Tuple(elts=[x for x, _s, _e in row],
                                           ctx=ast.Load()),
                                 all(s for _x, s, _e in row), row[0][2]))
                return rows, []
        if isinstance(f, ast.Attribute) and not call.args \
                and f.attr in ("items", "values", "keys"):
            conts, whys = self.values(f.value, env, strict, depth + 1)
            if isinstance(f.value, ast.Name) and f.value.id in self.mutated:
                return [], [f"{f.value.id} is changed after it is built"]
            out = []
            for c, st, e in conts:
                if not isinstance(c, ast.Dict) or any(k is None
                                                      for k in c.keys):
                    whys.append("it iterates a dict built at run time")
                    continue
                for k, v in zip(c.keys, c.values):
                    x = {"items": ast.Tuple(elts=[k, v], ctx=ast.Load()),
                         "values": v, "keys": k}[f.attr]
                    out.append((x, st, e))
            return out, whys
        return None, []

    def _subscript_values(self, expr: ast.Subscript, env, strict: bool,
                          depth: int):
        sl = expr.slice
        if isinstance(sl, ast.Slice):
            return [], ["it is a slice"]
        if isinstance(expr.value, ast.Name) and expr.value.id in self.mutated:
            return [], [f"{expr.value.id} is changed after it is built"]
        key = sl.value if isinstance(sl, ast.Constant) else None
        conts, whys = self.values(expr.value, env, strict, depth + 1)
        out = []
        for c, st, e in conts:
            if isinstance(c, (ast.List, ast.Tuple)):
                if any(isinstance(x, ast.Starred) for x in c.elts):
                    whys.append("it indexes a *-unpacked sequence")
                    continue
                if key is None:
                    pick = list(c.elts)        # any element may be the one
                elif isinstance(key, int) and not isinstance(key, bool) \
                        and -len(c.elts) <= key < len(c.elts):
                    pick = [c.elts[key]]
                else:
                    whys.append(f"index {key!r} is not in that sequence")
                    continue
            elif isinstance(c, ast.Dict):
                if any(k is None for k in c.keys):
                    whys.append("it indexes a dict built with **")
                    continue
                if key is None:
                    pick = list(c.values)
                else:
                    pick = [v for k, v in zip(c.keys, c.values)
                            if isinstance(k, ast.Constant) and k.value == key]
                    if not pick:
                        whys.append(f"key {key!r} is not in that dict")
                        continue
            else:
                whys.append("it indexes something built at run time")
                continue
            for p in pick:
                leaves, w = self.values(p, e, st, depth + 1)
                out += leaves
                whys += w
        return out, whys

    # ---- ** keyword sets -----------------------------------------------------
    def kw_options(self, expr, env, strict: bool = True, depth: int = 0):
        """([(items, strict)], [why]): each keyword set the dict ``expr``
        may hold, as [(key, value node)]; a why for every part that cannot
        be read off the source."""
        if depth > _MAX_DEPTH:
            return [], ["it is built through too many steps"]
        if isinstance(expr, ast.Name):
            if expr.id in self.mutated:
                return [], [f"{expr.id} is changed after it is built"]
            kind, data = self.lookup(expr.id, expr, env)
            if kind == "env":
                if data[0] == "kwargs":
                    _k, kws, spreads, oenv = data
                    return self._spread_into(
                        [([(kw.arg, kw.value) for kw in kws], strict)],
                        spreads, oenv, depth)
                if data[0] == "expr":
                    return self.kw_options(data[1], data[2], strict, depth + 1)
                return [], [data[1]]
            if kind == "recs":
                if any(r[0] == "import" for r in data):
                    return [], [f"{expr.id} is imported"]
                data = self.reaching(expr, data) or data
                loose = len(data) > 1
                opts, whys = [], []
                for rec in data:
                    leaves, w = self._record_values(rec, env,
                                                    strict and not loose,
                                                    depth + 1)
                    whys += w
                    for leaf, st, e in leaves:
                        o, w2 = self.kw_options(leaf, e, st, depth + 1)
                        opts += o
                        whys += w2
                return opts, whys
            return [], [f"{expr.id} is not assigned in the script"]
        if isinstance(expr, ast.Dict):
            base, spreads = [], []
            for k, v in zip(expr.keys, expr.values):
                if k is None:
                    spreads.append(v)
                elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                    base.append((k.value, v))
                else:
                    return [], ["a key is not a plain string"]
            return self._spread_into([(base, strict)], spreads, env, depth)
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
                and expr.func.id == "dict" and not expr.args \
                and self.lookup("dict", expr, env)[0] == "free":
            base = [(k.arg, k.value) for k in expr.keywords if k.arg]
            spreads = [k.value for k in expr.keywords if k.arg is None]
            return self._spread_into([(base, strict)], spreads, env, depth)
        if isinstance(expr, (ast.IfExp, ast.Subscript, ast.NamedExpr)):
            leaves, whys = self.values(expr, env, strict, depth + 1)
            opts = []
            for leaf, st, e in leaves:
                o, w = self.kw_options(leaf, e, st, depth + 1)
                opts += o
                whys += w
            return opts, whys
        return [], ["it is built at run time"]

    def _spread_into(self, opts, spreads, env, depth: int):
        whys: List[str] = []
        for s in spreads:
            sub, w = self.kw_options(s, env, True, depth + 1)
            whys += w
            opts = [(a + b, sa and sb) for a, sa in opts
                    for b, sb in sub][:_MAX_ENVS]
        return opts, whys

    # ---- how the code around a call is reached ----------------------------
    def envs_for(self, node, depth: int = 0) -> List[dict]:
        """One env per way the code around ``node`` runs it: per iteration
        of each enclosing loop over a literal sequence, per call site of the
        enclosing function. [] when it never runs (a function nobody
        calls)."""
        choices: List[List[dict]] = []
        child, p = node, self.parent.get(node)
        fn = None
        while p is not None:
            if isinstance(p, (ast.For, ast.AsyncFor)) and child in p.body:
                frs = self._loop_frags(p.target, p.iter, depth)
                if frs:
                    choices.append(frs)
            elif isinstance(p, _COMP_NODES) and child in (
                    getattr(p, "elt", None), getattr(p, "key", None),
                    getattr(p, "value", None)):
                for gen in p.generators:
                    frs = self._loop_frags(gen.target, gen.iter, depth)
                    if frs:
                        choices.append(frs)
            elif isinstance(p, _FUNC_NODES) and not isinstance(
                    child, ast.arguments) and child not in p.decorator_list:
                fn = p
                break
            elif isinstance(p, ast.Lambda):
                break
            child, p = p, self.parent.get(p)
        if fn is not None and self.params(fn):
            sites = self.call_sites(fn)
            if sites is not None:
                if not sites:
                    return []
                frags: List[dict] = []
                for s in sites:
                    frags += self.bind_call(fn, s, depth)
                if not frags:
                    return []
                choices.append(frags)
        envs: List[dict] = [{}]
        for frs in choices:
            envs = [{**e, **f} for e in envs for f in frs][:_MAX_ENVS]
        return envs

    def _loop_frags(self, target, iter_node, depth: int) -> List[dict]:
        els, whys = self.elements(iter_node, None, True, depth + 1)
        if whys or not els:
            return []          # the records' union (and its whys) apply
        out = []
        for el, _st, e in els[:_MAX_ENVS]:
            frag: Dict[Tuple[int, str], tuple] = {}
            self._unpack_into(frag, target, el, e, depth)
            out.append(frag)
        return out

    def _unpack_into(self, frag: dict, target, value, env, depth: int) -> None:
        if isinstance(target, ast.Name):
            frag[(id(self.scope_of(target)), target.id)] = ("expr", value, env)
            return
        if isinstance(target, (ast.Tuple, ast.List)) and not any(
                isinstance(x, ast.Starred) for x in target.elts):
            leaves, whys = self.values(value, env, True, depth + 1)
            if not whys and len(leaves) == 1 and isinstance(
                    leaves[0][0], (ast.Tuple, ast.List)) \
                    and len(leaves[0][0].elts) == len(target.elts) \
                    and not any(isinstance(x, ast.Starred)
                                for x in leaves[0][0].elts):
                for t, v in zip(target.elts, leaves[0][0].elts):
                    self._unpack_into(frag, t, v, leaves[0][2], depth)
                return
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                frag[(id(self.scope_of(n)), n.id)] = (
                    "unknown", f"{n.id} is unpacked from something built at "
                               f"run time")

    def call_sites(self, fn) -> Optional[List[ast.Call]]:
        """Every `fn(...)` call, or None when fn is also used some other way
        (passed to map(), decorated, rebound) — its callers are unknown.
        A method is called through an object (r.run(...)), never by its bare
        name, so its callers are unknown too: taken as "nobody calls it",
        `class R: def run(self, f, **k): f.execute(...)` was never checked."""
        if fn.decorator_list or isinstance(self.parent.get(fn), ast.ClassDef):
            return None
        recs = self.records.get((id(self.scope_of(fn)), fn.name)) or []
        if len(recs) != 1:
            return None
        sites = []
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Name) and n.id == fn.name \
                    and isinstance(n.ctx, ast.Load):
                kind, data = self.lookup(n.id, n, None)
                if kind != "recs" or data is not recs:
                    continue                    # another binding of the name
                par = self.parent.get(n)
                if isinstance(par, ast.Call) and par.func is n:
                    sites.append(par)
                else:
                    return None
        return sites

    def bind_call(self, fn, site: ast.Call, depth: int) -> List[dict]:
        """fn's parameters bound to ``site``'s arguments, once per way the
        call site itself is reached."""
        outer = self.envs_for(site, depth + 1) if depth < 2 else [{}]
        a = fn.args
        pos = a.posonlyargs + a.args
        named = {x.arg for x in pos + a.kwonlyargs}
        out = []
        for oenv in outer[:8]:
            frag: Dict[Tuple[int, str], tuple] = {}

            def key(name):
                return (id(fn), name)
            star = False
            for i, arg in enumerate(site.args):
                if isinstance(arg, ast.Starred):
                    star = True
                    break
                if i < len(pos):
                    frag[key(pos[i].arg)] = ("expr", arg, oenv)
            extra, spreads = [], []
            for kw in site.keywords:
                if kw.arg is None:
                    spreads.append(kw.value)
                elif kw.arg in named:
                    frag[key(kw.arg)] = ("expr", kw.value, oenv)
                else:
                    extra.append(kw)
            if not star and not spreads:
                for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                    frag.setdefault(key(p.arg), ("expr", d, None))
                for p, d in zip(a.kwonlyargs, a.kw_defaults):
                    if d is not None:
                        frag.setdefault(key(p.arg), ("expr", d, None))
            for p in pos + a.kwonlyargs:
                frag.setdefault(key(p.arg), (
                    "unknown", f"{p.arg} is passed to {fn.name}() in a way "
                               f"this check cannot follow"))
            if a.vararg:
                frag[key(a.vararg.arg)] = (
                    "unknown", f"{a.vararg.arg} collects {fn.name}()'s extra "
                               f"positional arguments")
            if a.kwarg:
                frag[key(a.kwarg.arg)] = (
                    ("kwargs", extra, spreads, oenv) if not star else
                    ("unknown", f"{fn.name}() is called with *arguments"))
            out.append(frag)
        return out


# ============================================================
# Scripts (nx_generate.write_script)
# ============================================================

class _Checker:
    def __init__(self, tree: ast.AST, catalog: dict):
        self.tree = tree
        self.cat = catalog
        self.errors: List[str] = []
        self._seen = set()
        self.suggest: List[dict] = []
        self.filters: Dict[str, Dict[str, dict]] = {}
        for f in catalog.get("filters", []):
            if f.get("py_attr"):
                self.filters.setdefault(f.get("module") or "simplnx",
                                        {})[f["py_attr"]] = f
        self.module_names = {m: set(v) for m, v in
                             (catalog.get("module_names") or {}).items()}
        self.loaded = set(catalog.get("modules_loaded") or self.filters)
        self.classes = catalog.get("classes") or {}
        self.enums = catalog.get("enums") or {}
        self.filter_attrs = set(catalog.get("filter_attrs")
                                or _FILTER_ATTRS_FALLBACK)
        self.mod_alias: Dict[str, str] = {}       # 'nx' -> 'simplnx'
        self.from_names: Dict[str, Tuple[str, str]] = {}   # 'Path'->(pathlib, Path)
        self.other_alias: Dict[str, str] = {}     # 'np' -> 'numpy'
        self.known: Dict[str, ast.AST] = {}       # name -> its only value
        self.rebound: Dict[str, List[ast.AST]] = {}   # name -> its values
        self.flow = _Flow(tree)        # every value a name may hold

    # ---- reporting ---------------------------------------------------
    def err(self, node, msg: str) -> None:
        line = getattr(node, "lineno", "?")
        key = (line, msg)
        if key in self._seen:
            return
        self._seen.add(key)
        self.errors.append(f"line {line}: {msg}")

    def want(self, entry: dict) -> None:
        if entry and entry not in self.suggest \
                and not nx_policy.is_denied(entry.get("uuid")):
            self.suggest.append(entry)

    # ---- pass 1: imports and bindings ----------------------------------
    def collect(self) -> None:
        stores: Counter = Counter()
        simple: Dict[str, ast.AST] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    if a.name in NX_MODULES:
                        self.mod_alias[local] = a.name
                        self._check_installed(node, a.name)
                    else:
                        self.other_alias[local] = a.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    if a.name == "*":
                        if mod in NX_MODULES:
                            self.err(node, f"`from {mod} import *` hides what "
                                     f"the script calls; write `import {mod} "
                                     f"as {MODULE_ALIASES[mod]}` and "
                                     f"{MODULE_ALIASES[mod]}.<Name>")
                        continue
                    self.from_names[a.asname or a.name] = (mod, a.name)
                    if mod in NX_MODULES:
                        self._check_installed(node, mod)
                        self._check_module_attr(node, mod, a.name)
            elif isinstance(node, ast.Name) and isinstance(
                    node.ctx, (ast.Store, ast.Del)):
                stores[node.id] += 1
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                simple.setdefault(node.targets[0].id, []).append(node.value)
        for name, values in simple.items():
            if stores[name] == 1:
                self.known[name] = values[0]
            elif stores[name] == len(values):
                # Rebound only by plain assignments (`result = ...` after
                # every filter): its class is known if they all agree.
                self.rebound[name] = values

    def class_of_name(self, name: str, depth: int = 0) -> Optional[str]:
        """The class every assignment of ``name`` gives it, or None."""
        values = self.rebound.get(name)
        if not values or depth > 4:
            return None
        kinds = [self.infer(v, depth + 1) for v in values]
        k = kinds[0]
        if k and k[0] == "object" and k[1] in self.classes                 and all(x == k for x in kinds):
            return k[1]
        return None

    def _check_installed(self, node, mod: str) -> None:
        if self.loaded and mod not in self.loaded:
            self.err(node, f"{mod} is not installed in the DREAM3D-NX env "
                     f"(installed: {', '.join(sorted(self.loaded))})")

    def bound_names(self) -> set:
        out = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(
                    node.ctx, (ast.Store, ast.Del)):
                out.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.arg):
                out.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    out.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                out.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                out.update(node.names)
            elif type(node).__name__ in ("MatchAs", "MatchStar") \
                    and getattr(node, "name", None):
                out.add(node.name)
            elif type(node).__name__ == "MatchMapping" \
                    and getattr(node, "rest", None):
                out.add(node.rest)
        return out

    # ---- resolving names -----------------------------------------------
    def ref(self, node, depth: int = 0) -> Optional[Tuple[str, List[str]]]:
        """(module, [attr, attr...]) for an expression rooted at an nx
        module ((mod, []) for the module itself), else None."""
        if depth > 4:
            return None
        if isinstance(node, ast.Name):
            if node.id in self.mod_alias:
                return self.mod_alias[node.id], []
            if node.id in self.from_names:
                mod, attr = self.from_names[node.id]
                if mod in NX_MODULES:
                    return mod, [attr]
            v = self.known.get(node.id)
            if v is not None and isinstance(v, (ast.Name, ast.Attribute)):
                return self.ref(v, depth + 1)
            return None
        if isinstance(node, ast.Attribute):
            base = self.ref(node.value, depth + 1)
            if base is None:
                return None
            return base[0], base[1] + [node.attr]
        return None

    def filter_entry(self, mod: str, attr: str) -> Optional[dict]:
        return (self.filters.get(mod) or {}).get(attr)

    def class_of_ref(self, mod: str, parts: List[str]) -> Optional[str]:
        key = ".".join([mod] + parts)
        return key if key in self.classes else None

    # ---- what a value is -------------------------------------------------
    def infer(self, node, depth: int = 0):
        """(kind, detail) of an expression when the source says it, else None."""
        if depth > 4 or node is None:
            return None
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ("bool", None)
            if isinstance(v, int):
                return ("int", None)
            if isinstance(v, float):
                return ("float", None)
            if isinstance(v, str):
                return ("str", None)
            if v is None:
                return ("none", None)
            return ("other", type(v).__name__)
        if isinstance(node, ast.JoinedStr):
            return ("str", None)
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.USub, ast.UAdd)):
            k = self.infer(node.operand, depth + 1)
            return k if k and k[0] in ("int", "float") else None
        if isinstance(node, (ast.List, ast.Tuple)):
            return ("list", [None if isinstance(e, ast.Starred)
                             else self.infer(e, depth + 1) for e in node.elts])
        if isinstance(node, ast.Dict):
            return ("dict", None)
        if isinstance(node, ast.Set):
            return ("set", None)
        if isinstance(node, ast.Call):
            f = node.func
            r = self.ref(f)
            if r is not None:
                mod, parts = r
                if parts == ["DataPath"]:
                    return ("datapath", None)
                cls = self.class_of_ref(mod, parts)
                if cls:
                    return ("object", cls)
                if len(parts) == 2 and parts[1] == "execute" \
                        and self.filter_entry(mod, parts[0]):
                    return ("object", "simplnx.IFilter.ExecuteResult")
                return None
            if isinstance(f, ast.Name):
                if f.id in ("str", "repr"):
                    return ("str", None)
                if f.id in ("int", "len", "round"):
                    return ("int", None)
                if f.id == "float":
                    return ("float", None)
                if f.id == "bool":
                    return ("bool", None)
                if f.id == "dict":
                    return ("dict", None)
                if self.from_names.get(f.id, ("", ""))[0] == "pathlib":
                    return ("path", None)
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and self.other_alias.get(f.value.id) == "pathlib":
                return ("path", None)
            return None
        if isinstance(node, ast.Attribute):
            r = self.ref(node)
            if r is not None and len(r[1]) >= 2:
                mod, parts = r
                key = ".".join([mod] + parts[:-1])
                if key in self.enums:
                    return ("enum", key)
            return None
        if isinstance(node, ast.Name):
            v = self.known.get(node.id)
            if v is not None:
                return self.infer(v, depth + 1)
            return None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and self._class_of_value(node.value, depth + 1) \
                == "simplnx.DataStructure":
            # ds[path] is the OBJECT stored there (an ImageGeom, an array),
            # not its path. Measured: passing it as a DataPath parameter
            # raised "Unable to cast ... ImageGeom to ... DataPath".
            return ("dataobject", None)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = self.infer(node.left, depth + 1), self.infer(node.right,
                                                               depth + 1)
            if a and b and a[0] == b[0] == "str":
                return ("str", None)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            a = self.infer(node.left, depth + 1)
            if a and a[0] == "path":
                return ("path", None)
        return None

    @staticmethod
    def what(kind) -> str:
        k, d = kind
        return {"bool": "a bool", "int": "an int", "float": "a float",
                "str": "a str", "none": "None", "list": "a list",
                "dict": "a dict", "set": "a set", "datapath": "a DataPath",
                "path": "a pathlib.Path",
                "dataobject": "ds[...] — that is the object stored at the "
                              "path, not the path; pass nx.DataPath('...') "
                              "itself"}.get(
            k, f"a {py_name(d)}" if k in ("enum", "object") and d
            else f"a {d or k}")

    def value_problem(self, kind, type_str: str) -> Optional[str]:
        """Why a value of ``kind`` cannot be passed as ``type_str`` — each
        case measured against the installed build's casts — or None."""
        if kind is None:
            return None
        t, arg = classify(type_str, self.cat)
        k = kind[0]
        if t in ("any", "ds") or k == "other":
            return None
        if t == "optional":
            return None if k == "none" else self.value_problem(kind, arg)
        ok = {
            "datapath": {"datapath"},
            "int": {"int", "bool"},
            "float": {"int", "float", "bool"},
            "bool": {"bool", "int", "float", "none"},
            "str": {"str"},
            "path": {"str", "path"},
            "list": {"list"},
        }.get(t)
        if t == "enum":
            if k == "enum" and kind[1] == arg:
                return None
            extra = (" (the binding refuses a plain int)" if k == "int"
                     else "")
            mem = _members(self.cat, arg)
            return (f"is a {arg}: write {py_name(arg)}.<member>, one of "
                    f"{', '.join(mem[:12])} — not {self.what(kind)}{extra}")
        if t == "object":
            if k == "object" and (
                    kind[1] == arg or not self.classes
                    or arg in ((self.classes.get(kind[1]) or {})
                               .get("bases") or [])):
                return None          # the class, or a subclass of it
            return (f"is a {arg}, not {self.what(kind)} (a "
                    f"{'dict is' if k == 'dict' else 'value like that is'} "
                    f"not converted). Build "
                    f"{construct_hint(arg, self.cat)}")
        if ok is None:
            return None
        if k not in ok:
            hint = (" (wrap the string: nx.DataPath('...'))"
                    if t == "datapath" and k == "str" else "")
            art = "an" if type_str[:1] in "aeiou" else "a"
            return (f"is {art} {type_str}: write "
                    f"{how_to_write(type_str, self.cat)}, not "
                    f"{self.what(kind)}{hint}")
        if t == "list":
            for i, ek in enumerate(kind[1] or []):
                why = self.value_problem(ek, arg)
                if why:
                    return f"is a {type_str}; element {i} {why}"
        return None

    # ---- pass 2: the checks ---------------------------------------------
    def _check_module_attr(self, node, mod: str, attr: str) -> bool:
        """Is ``attr`` a real name in module ``mod``? Reports if not."""
        names = self.module_names.get(mod)
        here = self.filter_entry(mod, attr)
        if here is not None:
            return True
        elsewhere = [m for m, fs in self.filters.items()
                     if attr in fs and m != mod]
        if elsewhere:
            m = elsewhere[0]
            self.err(node, f"{attr} is not in {mod}; it is in {m}: "
                     f"`import {m} as {MODULE_ALIASES.get(m, m)}` and call "
                     f"{MODULE_ALIASES.get(m, m)}.{attr}.execute(...)")
            self.want(self.filter_entry(m, attr))
            return False
        if names is not None:
            if attr in names:
                return True
        elif not attr.endswith("Filter"):
            return True          # no module listing (old catalog): unknowable
        alias = MODULE_ALIASES.get(mod, mod)
        if attr in MODULE_ALIASES.values():
            m = next(k for k, v in MODULE_ALIASES.items() if v == attr)
            self.err(node, f"{mod} has no {attr!r} to import: `{attr}` is "
                     f"the module itself — write `import {m} as {attr}`")
            return False
        if attr.endswith("Filter"):
            near = nearest_filters(attr, self.cat)
            for e in near:
                self.want(e)
            hint = (" Nearest real filters: " + ", ".join(
                f"{MODULE_ALIASES.get(e.get('module'), 'nx')}.{e['py_attr']} "
                f"({e.get('human_name')})" for e in near) + "."
                    if near else "")
            self.err(node, f"{alias}.{attr} does not exist in the installed "
                     f"{mod}.{hint}")
        else:
            self.err(node, f"{alias}.{attr} does not exist in the installed "
                     f"{mod}.{_and_nearest(attr, names or [])}")
        return False

    def check_names(self) -> None:
        bound = self.bound_names()
        reported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id not in bound and node.id not in _BUILTINS \
                    and node.id not in reported:
                reported.add(node.id)
                fix = _IMPORT_HINT.get(node.id)
                self.err(node, f"name {node.id!r} is used but never defined"
                         + (f" — add `{fix}` at the top" if fix
                            else " or imported"))

    def check_attributes(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Attribute):
                continue
            r = self.ref(node)
            if r is None:
                self._check_object_attr(node)
                continue
            mod, parts = r
            if len(parts) == 1:
                self._check_module_attr(node, mod, parts[0])
            elif len(parts) >= 2:
                head = self.filter_entry(mod, parts[0])
                if head is not None and len(parts) == 2 \
                        and parts[1] in _INSTANCE_ONLY:
                    self.err(node, self._instance_only_msg(
                        MODULE_ALIASES.get(mod, mod), parts[0], parts[1]))
                    self.want(head)
                    continue
                if head is not None and len(parts) == 2 \
                        and parts[1] not in self.filter_attrs:
                    alias = MODULE_ALIASES.get(mod, mod)
                    self.err(node, f"{alias}.{parts[0]} has no attribute "
                             f"{parts[1]!r}. A filter is run as "
                             f"{alias}.{parts[0]}.execute(data_structure=ds,"
                             f" <param>=<value>, ...)")
                    self.want(head)
                    continue
                key = ".".join([mod] + parts[:-1])
                if key in self.enums:
                    mem = _members(self.cat, key)
                    if parts[-1] not in mem:
                        self.err(node, f"{py_name(key)} has no member "
                                 f"{parts[-1]!r}. Its members: "
                                 f"{', '.join(mem)}")

    def _class_of_value(self, node, depth: int = 0) -> Optional[str]:
        if isinstance(node, ast.Name) and node.id in self.rebound:
            return self.class_of_name(node.id, depth)
        kind = self.infer(node, depth)
        if kind and kind[0] == "object" and kind[1] in self.classes:
            return kind[1]
        if kind and kind[0] == "datapath" and "simplnx.DataPath" in \
                self.classes:
            return "simplnx.DataPath"
        return None

    def _check_object_attr(self, node: ast.Attribute) -> None:
        """ds.add_filter, r.valid(), v.not_a_property: an attribute that the
        catalog's record of the object's class does not have."""
        if not isinstance(node.value, (ast.Name, ast.Subscript)):
            return
        # ds[path].x / geom = ds[path]; geom.x: which DataObject it is
        # depends on the data, but an attribute NO DataObject class has
        # (add_array, measured) is wrong whatever is stored there.
        do_attrs = self.cat.get("dataobject_attrs")
        kind = self.infer(node.value)
        if kind and kind[0] == "dataobject":
            if do_attrs and node.attr not in do_attrs \
                    and not node.attr.startswith("_"):
                what = (node.value.id if isinstance(node.value, ast.Name)
                        else "ds[...]")
                self.err(node, f"{what} is an object read out of the "
                         f"DataStructure, and no simplnx DataObject has an "
                         f"attribute {node.attr!r}."
                         f"{_and_nearest(node.attr, do_attrs)} Data is "
                         f"added by filters (nx.<Filter>.execute(...)), and "
                         f"an array's values through .npview()")
            return
        if not isinstance(node.value, ast.Name):
            return
        cls = self._class_of_value(node.value)
        if not cls:
            return
        rec = self.classes[cls]
        have = set(rec.get("props") or {}) | set(rec.get("methods") or [])
        if node.attr in have or node.attr.startswith("_"):
            return
        self.err(node, f"{node.value.id} is a {py_name(cls)}, which has no "
                 f"attribute {node.attr!r}. It has: "
                 f"{', '.join(sorted(have)) or 'nothing public'}."
                 + (" Filters are run with nx.<Filter>.execute(data_structure"
                    "=ds, ...), not through the DataStructure."
                    if cls == "simplnx.DataStructure" else ""))

    def _no_setitem(self, cls: str) -> bool:
        rec = self.classes.get(cls) or {}
        if "dunders" in rec:
            return "__setitem__" not in rec["dunders"]
        # Catalogs from before "dunders": measured on the installed build.
        return cls == "simplnx.DataStructure"

    def check_assignments(self) -> None:
        """v.prop = value on a compound parameter object: the property must
        exist, be settable, and take that type. Plus the two ways llama3.1:8b
        lost the DataStructure: ds[path] = ... (it has no item assignment)
        and ds = nx.X.execute(data_structure=ds, ...) (execute returns a
        result, so the next filter got that instead of ds)."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call):
                self._check_rebinding(node, node.targets[0].id)
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) \
                        and isinstance(tgt.value, ast.Name):
                    cls = self._class_of_value(tgt.value)
                    if cls and self._no_setitem(cls):
                        self.err(tgt, f"{tgt.value.id}[...] = ... does not "
                                 f"work: a {py_name(cls)} has no item "
                                 f"assignment. A filter writes into the "
                                 f"DataStructure itself — call nx.<Filter>."
                                 f"execute(data_structure={tgt.value.id}, "
                                 f"...) and keep its result in its own name")
                    continue
                if not (isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)):
                    continue
                r = self.ref(tgt.value)
                if r is not None and len(r[1]) == 1 \
                        and self.filter_entry(r[0], r[1][0]):
                    alias = MODULE_ALIASES.get(r[0], r[0])
                    self.err(tgt, f"{alias}.{r[1][0]} has no settable "
                             f"parameters; pass {tgt.attr}=... to "
                             f"{alias}.{r[1][0]}.execute(...) instead")
                    continue
                cls = self._class_of_value(tgt.value)
                if not cls:
                    continue
                rec = self.classes[cls]
                props = rec.get("props") or {}
                if tgt.attr not in props:
                    continue           # _check_object_attr reports it
                if tgt.attr in (rec.get("readonly") or []):
                    self.err(tgt, f"{py_name(cls)}.{tgt.attr} is read-only")
                    continue
                why = self.value_problem(self.infer(node.value),
                                         props[tgt.attr])
                if why:
                    self.err(tgt, f"{tgt.value.id}.{tgt.attr} {why}")

    def _check_rebinding(self, node: ast.Assign, name: str) -> None:
        r = self.ref(node.value.func)
        if r is None or len(r[1]) != 2 or r[1][1] != "execute" \
                or not self.filter_entry(r[0], r[1][0]):
            return
        call = node.value
        ds_arg = next((k.value for k in call.keywords
                       if k.arg == "data_structure"),
                      call.args[0] if call.args else None)
        if isinstance(ds_arg, ast.Name) and ds_arg.id == name:
            alias = MODULE_ALIASES.get(r[0], r[0])
            self.err(node, f"{name} = {alias}.{r[1][0]}.execute(data_structure="
                     f"{name}, ...) replaces the DataStructure with the "
                     f"filter's result (errors/warnings), so the next filter "
                     f"gets that instead. Keep the result in its own name: "
                     f"result = {alias}.{r[1][0]}.execute(data_structure="
                     f"{name}, ...)")

    def check_calls(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            r = self.ref(node.func)
            if r is None:
                # F.execute(...) with F bound any other way than one plain
                # assignment: fs[0], a loop variable, a helper's argument...
                if isinstance(node.func, ast.Attribute) \
                        and node.func.attr in _RUN_ATTRS:
                    self._check_run(node, None)
                continue
            mod, parts = r
            alias = MODULE_ALIASES.get(mod, mod)
            if len(parts) == 1 and self.filter_entry(mod, parts[0]):
                self.err(node, f"{alias}.{parts[0]}(...) builds a filter "
                         f"object, which this API does not use: call "
                         f"{alias}.{parts[0]}.execute(data_structure=ds, "
                         f"<param>=<value>, ...) directly, with every "
                         f"parameter as a keyword of execute()")
                self.want(self.filter_entry(mod, parts[0]))
                continue
            if len(parts) == 2 and parts[1] == "execute":
                entry = self.filter_entry(mod, parts[0])
                if entry is not None:
                    self._check_run(node, [((mod, entry), True)])
                continue
            cls = self.class_of_ref(mod, parts)
            if cls:
                self._check_ctor(node, cls)

    @staticmethod
    def _instance_only_msg(alias: str, name: str, attr: str) -> str:
        return (f"{alias}.{name}.{attr} is not how a script runs a filter: it "
                f"is a method of a filter OBJECT and takes no parameter list, "
                f"so called on {alias}.{name} it fails ('incompatible "
                f"function arguments'). Run it as {alias}.{name}.execute("
                f"data_structure=ds, <param>=<value>, ...)")

    def _filter_shaped(self, node: ast.Call) -> bool:
        """Does this .execute(...) look like a filter run (it passes the
        DataStructure), rather than, say, a database cursor's?"""
        if any(k.arg == "data_structure" for k in node.keywords):
            return True
        return bool(node.args) and not isinstance(node.args[0], ast.Starred) \
            and self._class_of_value(node.args[0]) == "simplnx.DataStructure"

    def _run_targets(self, expr, env):
        """([((module, filter entry), strict)], [why]) — every filter the
        receiver ``expr`` of an .execute(...) may be."""
        leaves, whys = self.flow.values(expr, env)
        out, why_out = [], list(whys)
        for leaf, strict, _e in leaves:
            r = self.ref(leaf)
            if r is not None:
                mod, parts = r
                entry = self.filter_entry(mod, parts[0]) \
                    if len(parts) == 1 else None
                if entry is not None:
                    out.append(((mod, entry), strict))
                # any other nx name: check_attributes reports it
                continue
            if isinstance(leaf, ast.Call) and self.ref(leaf.func) is not None:
                continue          # an nx object (nx.X() is reported elsewhere)
            if isinstance(leaf, (ast.Constant, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Lambda, ast.JoinedStr)):
                continue          # not a filter, and not hiding one
            why_out.append("it is computed when the script runs")
        return out, why_out

    def _keyword_options(self, node: ast.Call, env):
        """([(keywords, strict)], [(where, label, why)]): each set of
        (name, value node, via) the call may pass — its own keywords plus
        whatever each ** dict holds — and every ** / * that cannot be read
        off the source."""
        named = [(k.arg, k.value, None) for k in node.keywords if k.arg]
        opts = [(named, True)]
        unreadable = []
        for k in node.keywords:
            if k.arg is not None:
                continue
            label = "**" + _src(k.value, 40)
            sub, whys = self.flow.kw_options(k.value, env)
            if whys or not sub:
                unreadable.append((k.value, label,
                                   whys[0] if whys else "it is empty"))
                continue
            opts = [(a + [(key, v, label) for key, v in b], sa and sb)
                    for a, sa in opts for b, sb in sub][:_MAX_ENVS]
        for a in node.args:
            if isinstance(a, ast.Starred):
                unreadable.append((a, "*" + _src(a.value, 40),
                                   "positional arguments carry no parameter "
                                   "names"))
        return opts, unreadable

    def _check_run(self, node: ast.Call, direct) -> None:
        """An .execute(...) call: against every filter its receiver may be
        (``direct`` when it names one), with every keyword set it may pass,
        once per way the code around it is reached (loop iteration, call
        site of the enclosing function)."""
        attr = node.func.attr
        envs = self.flow.envs_for(node)
        if not envs:
            if direct is None:
                return            # in a function nobody calls: never runs
            envs = [{}]
        for env in envs:
            if direct is not None:
                cands = direct
            else:
                cands, whys = self._run_targets(node.func.value, env)
                if not cands:
                    if whys and self._filter_shaped(node):
                        self.err(node, f"{_src(node.func.value)}.{attr}(...) "
                                 f"runs a filter this check cannot identify "
                                 f"({whys[0]}), so neither the filter nor "
                                 f"its parameters can be checked. Name the "
                                 f"filter in the call: nx.<Filter>.execute("
                                 f"data_structure=ds, <param>=<value>, ...)")
                    continue
                if attr in _INSTANCE_ONLY:
                    for (mod, entry), _st in cands:
                        self.err(node, self._instance_only_msg(
                            MODULE_ALIASES.get(mod, mod), entry["py_attr"],
                            attr))
                        self.want(entry)
                    continue
            opts, unreadable = self._keyword_options(node, env)
            loose = []
            for (mod, entry), strict in cands:
                alias = MODULE_ALIASES.get(mod, mod)
                name = entry["py_attr"]
                for where, label, why in unreadable:
                    self.err(where, f"{alias}.{name}.execute({label}): {why}, "
                             f"so the parameters it passes cannot be checked "
                             f"against {name}'s parameter list. Pass each one "
                             f"as a keyword: {alias}.{name}.execute("
                             f"data_structure=ds, <param>=<value>, ...)")
                    self.want(entry)
                for kws, kw_strict in opts:
                    probs = self._execute_problems(node, alias, entry, kws,
                                                   not unreadable)
                    if strict and kw_strict:
                        for where, msg in probs:
                            self.err(where, msg)
                        if probs:
                            self.want(entry)
                    else:
                        loose.append((entry, probs))
            # A rebound name: the call has to fit ONE of the values it may
            # hold at that point; it is wrong only if it fits none.
            if loose and all(probs for _e, probs in loose):
                for entry, probs in loose:
                    for where, msg in probs:
                        self.err(where, msg)
                    self.want(entry)

    def _execute_problems(self, node: ast.Call, alias: str, entry: dict,
                          kws, readable: bool) -> List[Tuple[Any, str]]:
        """What is wrong with running ``entry`` with keywords ``kws``
        ([(name, value node, via)]); [(where, message)]."""
        name = entry["py_attr"]
        params = (entry.get("execute") or {}).get("params", [])
        types = {p["name"]: p.get("type") or "" for p in params}
        out: List[Tuple[Any, str]] = []
        if len(node.args) > 1:
            out.append((node, f"{alias}.{name}.execute() takes its parameters "
                              f"by keyword: execute(data_structure=ds, "
                              f"<param>=<value>, ...)"))
        given = {arg for arg, _v, _via in kws}
        if node.args:
            given.add("data_structure")
        for arg, value, via in kws:
            tail = f" (passed through {via})" if via else ""
            if arg not in types:
                real = [p for p in types if p != "data_structure"]
                near = _near(arg, real)
                out.append((value, f"{alias}.{name}.execute() has no "
                                   f"parameter {arg!r}."
                            + (f" Did you mean {', '.join(near)}?" if near
                               else "")
                            + f" Its parameters: {', '.join(real) or 'none'}"
                            + tail))
                continue
            why = self.value_problem(self.infer(value), types[arg])
            if why:
                out.append((value, f"{alias}.{name}.execute({arg}=...): "
                                   f"{arg} {why}{tail}"))
        if readable:
            for p in params:
                req = p.get("required") or p["name"] == "data_structure"
                if req and p["name"] not in given:
                    out.append((node, f"{alias}.{name}.execute() is missing "
                                      f"{p['name']}="
                                + ("ds (the DataStructure every filter works "
                                   "on)" if p["name"] == "data_structure"
                                   else f"<{p.get('type')}>")))
        return out

    def _check_ctor(self, node: ast.Call, cls: str) -> None:
        inits = (self.classes.get(cls) or {}).get("init") or []
        real = [s for s in inits
                if not any(p["name"].startswith("*") for p in s)]
        if not real or any(isinstance(a, ast.Starred) for a in node.args) \
                or any(k.arg is None for k in node.keywords):
            return
        kws = [k for k in node.keywords if k.arg]
        fits = []
        for sig in real:
            names = [p["name"] for p in sig]
            if len(node.args) <= len(sig) \
                    and all(k.arg in names[len(node.args):] for k in kws) \
                    and all(p.get("default") is not None
                            or i < len(node.args) or p["name"] in
                            {k.arg for k in kws}
                            for i, p in enumerate(sig)):
                fits.append(sig)
        if not fits:
            sigs = " | ".join(
                "(" + ", ".join(f"{p['name']}: {p.get('type')}" for p in s)
                + ")" for s in real)
            self.err(node, f"{py_name(cls)}(...) does not take those "
                     f"arguments. It takes: {sigs}")
            return
        # Overloads are tried in turn, as pybind11 does: DataPath('A/B') is
        # the str overload, DataPath(['A', 'B']) the list one. Report only
        # when no overload takes these values.
        first = None
        for sig in fits:
            probs = []
            for i, a in enumerate(node.args):
                why = self.value_problem(self.infer(a),
                                         sig[i].get("type") or "")
                if why:
                    probs.append((a, f"{py_name(cls)}(...) argument {i + 1} "
                                     f"({sig[i]['name']}) {why}"))
            types = {p["name"]: p.get("type") or "" for p in sig}
            for k in kws:
                why = self.value_problem(self.infer(k.value),
                                         types.get(k.arg, ""))
                if why:
                    probs.append((k.value, f"{py_name(cls)}({k.arg}=...): "
                                           f"{k.arg} {why}"))
            if not probs:
                return
            first = first or probs
        for where, msg in first or []:
            self.err(where, msg)


def check_script(code: str, catalog: dict) -> Dict[str, Any]:
    """{"errors": [...], "suggest": [filter entries]} for a model-written
    simplnx script, against the INSTALLED catalog. A script that does not
    parse returns no errors here: nx_policy.validate_script reports that."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {"errors": [], "suggest": []}
    c = _Checker(tree, catalog or {})
    c.collect()
    c.check_names()
    c.check_attributes()
    c.check_calls()
    c.check_assignments()
    return {"errors": c.errors, "suggest": c.suggest}
