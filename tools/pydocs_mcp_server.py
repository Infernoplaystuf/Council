#!/usr/bin/env python
"""
pydocs_mcp_server — documentation for installed Python packages, over MCP.

WHAT IT IS
A Model Context Protocol server (stdio transport) that answers "what does this
package offer and how is it called" for any Python package installed in an
interpreter — by READING THE SOURCE, not by importing it. The Council's Docs
tab starts it by default, and the docs benchmark serves its synthetic package
through it.

    python tools/pydocs_mcp_server.py [--python EXE] [--path DIR]...
                                      [--package NAME]... [--cache-dir DIR]

  --python EXE   document the packages installed for ANOTHER interpreter (the
                 nxpython env's simplnx, say). EXE is run once with -c to print
                 its sys.path; no package code runs.
  --path DIR     an extra folder to find packages in (searched first).
  --package N    restrict search to these packages; also what resources/list
                 lists. Without it the package comes from the tool call, or
                 from a package name mentioned in the query.
  --cache-dir D  keep each package's index as JSON (never pickle) keyed by its
                 files' sizes and times, so the second start is instant.

TOOLS
  search_docs(query, package?, limit?)  ranked functions/classes/methods/
                                        modules with signatures and summaries
  get_doc(name, max_chars?)             one object's full page
  list_packages(prefix?)                what can be documented
RESOURCES
  pydoc://<dotted.name>                 a module's (or any object's) page

WHY STATIC (ast), NOT IMPORT
Importing a package runs its code: numpy's import takes a second and loads
DLLs; a hardware SDK's import may open a device; a broken package raises.
Reading the source with `ast` runs nothing, works for a package installed for
a different Python version (simplnx is cp312; the Council is 3.11), and sees
the same names and docstrings `help()` would. Compiled modules are covered by
their .pyi stubs — simplnx ships a 159 KB stub with every filter's `execute`
signature, which is exactly the grounding Dream3D script writing lacks — and
numpy's C functions by its `add_newdoc(...)` calls, which are read statically
too. A compiled module with no stub at all is listed but has no page.

PUBLIC NAMES
A function lives in `numpy/_core/fromnumeric.py` but is used as `numpy.sum`.
Re-exports (`from ._core.fromnumeric import *`, `__all__`) are followed, and
each object is shown under its shortest public name; private module paths are
kept as aliases so either spelling finds the page.

PROTOCOL
JSON-RPC 2.0, one message per line on stdin/stdout. stdout carries protocol
only: anything else that prints is redirected to stderr at start-up, because a
stray print on stdout is the classic way a stdio server breaks its client.
Stdlib only, so it runs under any Python 3.8+ the user points it at.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SERVER_NAME = "council-pydocs"
SERVER_VERSION = "1.0"
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: structuredContent and outputSchema arrived in this version.
STRUCTURED_SINCE = "2025-06-18"
INDEX_FORMAT = 3

MAX_MODULES = 4000          # per package — a safety net, not a target
MAX_FILE_BYTES = 4_000_000
PAGE_SIZE = 100             # resources/list
SKIP_DIRS = {"__pycache__", "tests", "test", "_tests", "testing_data",
             "site-packages", "node_modules", ".git"}

STOPWORDS = set("""
a an and are as at be by can do does for from get got have how i if in into is
it its me my of on or the that this to use used using what when where which
who why will with you your want need make should would could please python
package module function functions method methods class classes call called
example code write show give tell return returns value values way does do
""".split())


def log(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)


# ======================================================================
# Text helpers
# ======================================================================

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def stem(word: str) -> str:
    """A deliberately crude stemmer: plural and -ing/-ed endings only.

    Good enough to make 'arrays' find 'array' and 'sorting' find 'sort';
    anything cleverer mangles identifiers."""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def terms(text: str) -> List[str]:
    """Search terms: identifiers split on _ and camelCase, lowercased,
    stemmed, stopwords dropped. The whole identifier is kept too, so
    `encode_frame` matches as one term as well as two."""
    out: List[str] = []
    for word in _IDENT.findall(text or ""):
        lower = word.lower()
        parts = [p.lower() for chunk in word.split("_") if chunk
                 for p in _CAMEL.findall(chunk)]
        if len(parts) > 1 and lower.strip("_") not in STOPWORDS:
            out.append(lower.strip("_"))
        for p in parts:
            if p not in STOPWORDS and len(p) > 1:
                out.append(stem(p))
    return out


def first_paragraph(doc: str) -> str:
    para = (doc or "").strip().split("\n\n", 1)[0]
    return " ".join(para.split())


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ======================================================================
# Finding packages
# ======================================================================

def interpreter_path(python: str) -> List[str]:
    """sys.path of another interpreter. Runs it with -c; no package code."""
    out = subprocess.run(
        [python, "-c", "import sys, json; print(json.dumps(sys.path))"],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError(f"{python} could not report its sys.path: "
                           f"{out.stderr.strip()[-300:]}")
    return [p for p in json.loads(out.stdout.strip().splitlines()[-1]) if p]


_EXT_SUFFIXES = (".pyd", ".so", ".dll", ".dylib")


def _top_level(entry: str, full: str) -> Optional[Tuple[str, str]]:
    """(name, kind) when a directory entry is an importable top-level module."""
    if os.path.isdir(full):
        if entry in SKIP_DIRS or "." in entry or "-" in entry:
            return None
        for init in ("__init__.py", "__init__.pyi"):
            if os.path.isfile(os.path.join(full, init)):
                return entry, "package"
        return None
    if entry.endswith((".py", ".pyi")):
        name = entry.rsplit(".", 1)[0]
        return (name, "module") if name.isidentifier() else None
    if entry.endswith(_EXT_SUFFIXES):
        name = entry.split(".", 1)[0]
        return (name, "extension") if name.isidentifier() else None
    return None


class Finder:
    """Which top-level packages exist, and where — first root wins, like
    import does."""

    def __init__(self, roots: Sequence[str]):
        self.roots = [r for r in roots if r and os.path.isdir(r)]
        self._found: Optional[Dict[str, Tuple[str, str]]] = None

    def packages(self) -> Dict[str, Tuple[str, str]]:
        """name -> (root, kind). Cached: one listdir per root."""
        if self._found is None:
            found: Dict[str, Tuple[str, str]] = {}
            for root in self.roots:
                try:
                    entries = sorted(os.listdir(root))
                except OSError:
                    continue
                for entry in entries:
                    hit = _top_level(entry, os.path.join(root, entry))
                    if hit and hit[0] not in found:
                        found[hit[0]] = (root, hit[1])
            self._found = found
        return self._found

    def files(self, name: str) -> List[Tuple[str, str, str]]:
        """(module name, .py path or "", .pyi path or "") for every module of
        a package, the package's __init__ first."""
        hit = self.packages().get(name)
        if hit is None:
            return []
        root, kind = hit
        if kind != "package":
            py = os.path.join(root, name + ".py")
            pyi = os.path.join(root, name + ".pyi")
            return [(name, py if os.path.isfile(py) else "",
                     pyi if os.path.isfile(pyi) else "")]
        out: List[Tuple[str, str, str]] = []
        base = os.path.join(root, name)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and "." not in d
                and "-" not in d and not d.startswith("."))
            rel = os.path.relpath(dirpath, root).replace(os.sep, ".")
            stems: Dict[str, Dict[str, str]] = {}
            for f in filenames:
                if f.endswith((".py", ".pyi")):
                    s, ext = f.rsplit(".", 1)
                    if s.isidentifier():
                        stems.setdefault(s, {})[ext] = os.path.join(dirpath, f)
            if "__init__" not in stems and dirpath != base:
                dirnames[:] = []           # not a package: no submodules
                continue
            for s in sorted(stems, key=lambda x: (x != "__init__", x)):
                mod = rel if s == "__init__" else f"{rel}.{s}"
                out.append((mod, stems[s].get("py", ""),
                            stems[s].get("pyi", "")))
                if len(out) >= MAX_MODULES:
                    return out
        return out

    def fingerprint(self, name: str) -> str:
        """Changes when any of the package's source files change."""
        h = hashlib.sha1()
        for mod, py, pyi in self.files(name):
            for p in (py, pyi):
                if p:
                    try:
                        st = os.stat(p)
                        h.update(f"{p}|{st.st_size}|{int(st.st_mtime)}"
                                 .encode("utf-8", "replace"))
                    except OSError:
                        pass
        return h.hexdigest()


# ======================================================================
# Reading one module
# ======================================================================

def _unparse(node: Optional[ast.AST], limit: int = 60) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:                                     # noqa: BLE001
        return "…"
    return clip(" ".join(text.split()), limit)


def format_args(args: ast.arguments, drop_first: bool = False) -> str:
    """A signature's parameter list from the AST, as `help()` would show it."""
    parts: List[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults)
    for i, (arg, default) in enumerate(zip(positional, defaults)):
        if drop_first and i == 0:
            continue
        text = arg.arg
        if arg.annotation is not None:
            text += f": {_unparse(arg.annotation, 40)}"
        if default is not None:
            text += (" = " if arg.annotation is not None else "=") + _unparse(
                default, 40)
        parts.append(text)
        if args.posonlyargs and i == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        text = arg.arg
        if arg.annotation is not None:
            text += f": {_unparse(arg.annotation, 40)}"
        if default is not None:
            text += (" = " if arg.annotation is not None else "=") + _unparse(
                default, 40)
        parts.append(text)
    if args.kwarg is not None:
        parts.append("**" + args.kwarg.arg)
    if parts and parts[0] == "/":
        parts.pop(0)
    return ", ".join(parts)


def _decorators(node) -> Set[str]:
    names = set()
    for d in getattr(node, "decorator_list", []):
        target = d.func if isinstance(d, ast.Call) else d
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _string(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def parse_tolerant(source: str, path: str = "<source>",
                   attempts: int = 25) -> Optional[ast.Module]:
    """ast.parse, blanking out lines that do not parse — up to `attempts`.

    Generated stubs are not always valid Python. simplnx.pyi (pybind11
    stubgen) declares a field named `2d: bool`, which made the WHOLE 159 KB
    stub unreadable: every filter's signature lost to one line. Replacing the
    offending line with `pass` (same indentation, so a block never ends up
    empty) loses that one declaration and keeps the rest."""
    lines = source.splitlines()
    for _ in range(attempts):
        try:
            return ast.parse("\n".join(lines), filename=path)
        except SyntaxError as exc:
            n = (exc.lineno or 0) - 1
            if not 0 <= n < len(lines):
                return None
            text = lines[n]
            indent = text[:len(text) - len(text.lstrip())]
            replacement = indent + "pass"
            if lines[n] == replacement:
                return None
            lines[n] = replacement
        except (ValueError, RecursionError):
            return None
    return None


def _clean_doc(doc: Optional[str]) -> str:
    import inspect
    return inspect.cleandoc(doc) if doc else ""


def _entry(qual: str, kind: str, module: str, signature: str = "",
           doc: str = "", file: str = "", line: int = 0, **extra) -> dict:
    e = {"qual": qual, "kind": kind, "module": module, "sig": signature,
         "doc": doc, "file": file, "line": line}
    e.update(extra)
    return e


class ModuleReader:
    """One module's definitions, imports, __all__ and add_newdoc calls."""

    def __init__(self, module: str, is_package: bool):
        self.module = module
        self.is_package = is_package
        self.defs: Dict[str, dict] = {}         # local name -> entry
        self.members: Dict[str, List[dict]] = {}  # class qual -> entries
        self.imports: Dict[str, str] = {}       # local name -> dotted target
        self.stars: List[str] = []
        self.all: Optional[List[str]] = None
        self.newdocs: List[Tuple[str, str, str]] = []
        self.doc = ""
        self.error = ""

    # -- relative import resolution -----------------------------------
    def _absolute(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.module.split(".")
        if not self.is_package:
            base = base[:-1]
        if node.level > 1:
            base = base[:len(base) - (node.level - 1)]
        return ".".join(base + ([node.module] if node.module else []))

    def read(self, path: str, stub: bool = False) -> None:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                self.error = "too large"
                return
            with open(path, "rb") as fh:
                source = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return
        tree = parse_tolerant(source, path)
        if tree is None:
            self.error = "SyntaxError: could not be read"
            return
        if not self.doc:
            self.doc = _clean_doc(ast.get_docstring(tree))
        self._walk(tree.body, path, stub)

    def _walk(self, body: Sequence[ast.stmt], path: str, stub: bool) -> None:
        previous: Optional[str] = None
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(node, path, stub)
            elif isinstance(node, ast.ClassDef):
                self._class(node, path, stub)
            elif isinstance(node, ast.ImportFrom):
                target = self._absolute(node)
                for alias in node.names:
                    if alias.name == "*":
                        self.stars.append(target)
                    else:
                        self.imports[alias.asname or alias.name] = (
                            f"{target}.{alias.name}" if target else alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        self.imports.setdefault(top, top)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                previous = self._assign(node, path, stub)
                continue
            elif isinstance(node, ast.Expr):
                text = _string(node.value)
                if text is not None and previous and previous in self.defs \
                        and not self.defs[previous]["doc"]:
                    # Attribute docstring: a string right after `NAME = ...`.
                    self.defs[previous]["doc"] = _clean_doc(text)
                self._maybe_newdoc(node.value)
                if isinstance(node.value, ast.Call):
                    self._all_extend(node.value)
            elif isinstance(node, ast.If):
                self._walk(node.body, path, stub)
                self._walk(node.orelse, path, stub)
            elif isinstance(node, ast.Try):
                self._walk(node.body, path, stub)
                for h in node.handlers:
                    self._walk(h.body, path, stub)
                self._walk(node.orelse, path, stub)
                self._walk(node.finalbody, path, stub)
            previous = None

    def _function(self, node, path: str, stub: bool) -> None:
        name = node.name
        qual = f"{self.module}.{name}"
        existing = self.defs.get(name)
        sig = format_args(node.args)
        doc = _clean_doc(ast.get_docstring(node))
        returns = _unparse(node.returns, 40)
        if existing is not None and existing["kind"] == "function":
            # A second definition: an @overload stub or a py+pyi pair. Keep the
            # docstring wherever it is, and prefer a real signature over
            # `*args, **kwargs`.
            if not existing["doc"] and doc:
                existing["doc"] = doc
            if existing["sig"] in ("", "*args, **kwargs") and sig:
                existing["sig"] = sig
            existing["overloads"] = existing.get("overloads", 1) + 1
            return
        self.defs[name] = _entry(qual, "function", self.module, sig, doc,
                                 path, node.lineno, returns=returns)

    def _class(self, node: ast.ClassDef, path: str, stub: bool,
               prefix: str = "") -> None:
        name = prefix + node.name
        qual = f"{self.module}.{name}"
        doc = _clean_doc(ast.get_docstring(node))
        bases = [_unparse(b, 40) for b in node.bases]
        existing = self.defs.get(name) if not prefix else None
        if existing is not None and existing["kind"] == "class":
            if not existing["doc"] and doc:
                existing["doc"] = doc
            entry = existing
        else:
            entry = _entry(qual, "class", self.module, "", doc, path,
                           node.lineno, bases=bases)
            if not prefix:
                self.defs[name] = entry
        members = self.members.setdefault(qual, [])
        have = {m["name"] for m in members}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deco = _decorators(item)
                if item.name == "__init__":
                    sig = format_args(item.args, drop_first=True)
                    if not entry["sig"] or entry["sig"] == "*args, **kwargs":
                        entry["sig"] = sig
                    init_doc = _clean_doc(ast.get_docstring(item))
                    if init_doc and init_doc not in entry["doc"]:
                        entry["doc"] = (entry["doc"] + "\n\n" + init_doc
                                        ).strip()
                    continue
                if item.name.startswith("_") and item.name != "__call__":
                    continue
                if item.name in have:
                    continue
                kind = "property" if "property" in deco else "method"
                drop = "staticmethod" not in deco
                members.append(_entry(
                    f"{qual}.{item.name}", kind, self.module,
                    "" if kind == "property" else format_args(
                        item.args, drop_first=drop),
                    _clean_doc(ast.get_docstring(item)), path, item.lineno,
                    name=item.name, returns=_unparse(item.returns, 40),
                    static="staticmethod" in deco))
                have.add(item.name)
            elif isinstance(item, ast.ClassDef) and not prefix:
                self._class(item, path, stub, prefix=node.name + ".")
                members.append(_entry(f"{qual}.{item.name}", "class",
                                      self.module, "", _clean_doc(
                                          ast.get_docstring(item)),
                                      path, item.lineno, name=item.name))
            elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = (item.targets if isinstance(item, ast.Assign)
                           else [item.target])
                for t in targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_") \
                            and t.id not in have:
                        value = getattr(item, "value", None)
                        ann = getattr(item, "annotation", None)
                        members.append(_entry(
                            f"{qual}.{t.id}", "attribute", self.module,
                            "", "", path, item.lineno, name=t.id,
                            value=_unparse(value, 60) if value is not None
                            else "", annotation=_unparse(ann, 40)))
                        have.add(t.id)

    def _assign(self, node, path: str, stub: bool) -> Optional[str]:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target])
        value = getattr(node, "value", None)
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if t.id == "__all__":
                self._all_from(node)
                return None
            if t.id.startswith("__"):
                return None
            if isinstance(node, ast.AugAssign):
                return None
            kind = "constant" if t.id.isupper() else "data"
            ann = getattr(node, "annotation", None)
            if t.id not in self.defs:
                self.defs[t.id] = _entry(
                    f"{self.module}.{t.id}", kind, self.module, "", "", path,
                    node.lineno, value=_unparse(value, 80) if value is not None
                    else "", annotation=_unparse(ann, 40))
            return t.id
        return None

    def _literal_names(self, node) -> List[str]:
        if isinstance(node, (ast.List, ast.Tuple)):
            return [s for s in (_string(e) for e in node.elts) if s]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._literal_names(node.left) + self._literal_names(
                node.right)
        return []

    def _all_from(self, node) -> None:
        names = self._literal_names(getattr(node, "value", None))
        if isinstance(node, ast.AugAssign):
            self.all = (self.all or []) + names
        elif names or isinstance(getattr(node, "value", None),
                                 (ast.List, ast.Tuple)):
            self.all = names

    def _all_extend(self, call: ast.Call) -> None:
        f = call.func
        if (isinstance(f, ast.Attribute) and f.attr in ("extend", "append")
                and isinstance(f.value, ast.Name) and f.value.id == "__all__"
                and call.args):
            names = (self._literal_names(call.args[0]) if f.attr == "extend"
                     else [s for s in [_string(call.args[0])] if s])
            self.all = (self.all or []) + names

    def _maybe_newdoc(self, value) -> None:
        """numpy documents its C functions with add_newdoc(module, name, doc)
        calls. Read them as data: (module, 'name' or 'Class.method', doc)."""
        if not isinstance(value, ast.Call):
            return
        f = value.func
        fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if not fname.startswith("add_newdoc") or len(value.args) < 3:
            return
        mod, name, doc = (value.args[0], value.args[1], value.args[2])
        mod_s, name_s = _string(mod), _string(name)
        if not mod_s or not name_s:
            return
        if isinstance(doc, ast.Tuple) and len(doc.elts) == 2:
            attr, text = _string(doc.elts[0]), _string(doc.elts[1])
            if attr and text:
                self.newdocs.append((mod_s, f"{name_s}.{attr}", text))
            return
        text = _string(doc)
        if text:
            self.newdocs.append((mod_s, name_s, text))


# ======================================================================
# A package's index
# ======================================================================

class PackageIndex:
    """Every documented object of one package, its public names, and an
    inverted index for search. Built once, then served from memory."""

    FIELDS = (("name", 4.0), ("sig", 1.5), ("summary", 2.0), ("doc", 1.0),
              ("module", 0.6))

    def __init__(self, package: str, entries: Dict[str, dict],
                 aliases: Dict[str, str], modules: List[str],
                 errors: Dict[str, str], seconds: float = 0.0):
        self.package = package
        self.entries = entries          # qual -> entry (with "public")
        self.aliases = aliases          # any name -> qual
        self.modules = modules
        self.errors = errors
        self.seconds = seconds
        self._build_search()

    # -- persistence (JSON, never pickle) ------------------------------
    def to_json(self) -> dict:
        return {"format": INDEX_FORMAT, "package": self.package,
                "entries": self.entries, "aliases": self.aliases,
                "modules": self.modules, "errors": self.errors}

    @classmethod
    def from_json(cls, data: dict) -> "PackageIndex":
        return cls(data["package"], data["entries"], data["aliases"],
                   data["modules"], data.get("errors") or {})

    # -- search ------------------------------------------------------------
    def _fields(self, e: dict) -> Dict[str, str]:
        public = e.get("public") or e["qual"]
        last = public.rsplit(".", 1)[-1]
        owner = public.rsplit(".", 2)[-2] if e["kind"] in (
            "method", "property", "attribute") and public.count(".") >= 2 \
            else ""
        # The owning class goes with the module path, at low weight: as a NAME
        # term it made every `ReadCSVFileFilter.uuid` rank level with the
        # class itself (measured on simplnx, whose 300 filters each carry
        # name/uuid/human_name statics).
        return {"name": f"{last} {last}",
                "sig": e.get("sig", ""),
                "summary": first_paragraph(e.get("doc", "")),
                "doc": (e.get("doc") or "")[:2500],
                "module": f"{e.get('module', '')} {owner}"}

    def _build_search(self) -> None:
        self._ids = list(self.entries)
        postings: Dict[str, Dict[int, float]] = {}
        lengths: List[float] = []
        for i, qual in enumerate(self._ids):
            e = self.entries[qual]
            length = 0.0
            for fname, weight in self.FIELDS:
                for t in terms(self._fields(e)[fname]):
                    bucket = postings.setdefault(t, {})
                    bucket[i] = bucket.get(i, 0.0) + weight
                    length += weight
            lengths.append(length)
        self._postings = postings
        self._lengths = lengths
        self._avg = (sum(lengths) / len(lengths)) if lengths else 1.0
        self._pos = {q: i for i, q in enumerate(self._ids)}
        self._by_last: Dict[str, List[str]] = {}
        for qual, e in self.entries.items():
            last = (e.get("public") or qual).rsplit(".", 1)[-1].lower()
            self._by_last.setdefault(last, []).append(qual)

    def search(self, query: str, limit: int = 8) -> List[Tuple[float, dict]]:
        n = max(1, len(self._ids))
        q_terms = [t for t in terms(query) if t != self.package.lower()]
        scores: Dict[int, float] = {}
        k1, b = 1.2, 0.6
        for t in set(q_terms):
            bucket = self._postings.get(t)
            if not bucket:
                continue
            idf = math.log(1 + (n - len(bucket) + 0.5) / (len(bucket) + 0.5))
            for i, tf in bucket.items():
                norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * self._lengths[i]
                                                   / self._avg))
                scores[i] = scores.get(i, 0.0) + idf * norm
        # An identifier typed exactly is the strongest signal there is.
        named: Set[int] = set()
        for word in set(re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", query or "")):
            for qual in self._by_last.get(word.rsplit(".", 1)[-1].lower(), []):
                i = self._pos[qual]
                named.add(i)
                # Shallower public names win the tie: `array` means
                # numpy.array before numpy.ma.array before a helper's field.
                dots = max(1, (self.entries[qual].get("public") or qual)
                           .count("."))
                scores[i] = scores.get(i, 0.0) + 6.0 * (1.0 + 1.0 / dots)
        ranked = []
        for i, score in scores.items():
            e = self.entries[self._ids[i]]
            public = e.get("public") or e["qual"]
            if any(part.startswith("_") and not part.startswith("__")
                   for part in public.split(".")):
                # Private: only when asked for by name. A `_TO_MV` table
                # handed to a model as "documentation" invites it to use
                # an internal the package never promised.
                if i not in named:
                    continue
                score *= 0.5
            if e["kind"] in ("module", "data"):
                score *= 0.85
            ranked.append((score, e))
        ranked.sort(key=lambda se: (-se[0], len(se[1].get("public") or "")))
        return ranked[:max(1, limit)]

    # -- lookup ------------------------------------------------------------
    def lookup(self, name: str) -> Optional[dict]:
        name = (name or "").strip().strip("`").rstrip("()")
        if not name:
            return None
        qual = self.aliases.get(name) or (name if name in self.entries
                                          else None)
        if qual:
            return self.entries[qual]
        lowered = name.lower()
        for alias, qual in self.aliases.items():
            if alias.lower() == lowered:
                return self.entries[qual]
        # "Ledger.append" or "append": a unique dotted suffix.
        suffix = "." + lowered
        hits = sorted({q for alias, q in self.aliases.items()
                       if alias.lower().endswith(suffix)},
                      key=lambda q: len(self.entries[q].get("public") or q))
        return self.entries[hits[0]] if hits else None

    def suggestions(self, name: str, n: int = 5) -> List[str]:
        publics = sorted({e.get("public") or q for q, e in
                          self.entries.items()})
        last = {p.rsplit(".", 1)[-1]: p for p in publics}
        close = difflib.get_close_matches(name, publics, n=n, cutoff=0.6)
        close += [last[m] for m in difflib.get_close_matches(
            name.rsplit(".", 1)[-1], list(last), n=n, cutoff=0.7)]
        out: List[str] = []
        for c in close:
            if c not in out:
                out.append(c)
        return out[:n]

    def members_of(self, qual: str) -> List[dict]:
        prefix = qual + "."
        return [e for q, e in self.entries.items()
                if q.startswith(prefix) and "." not in q[len(prefix):]]


def _public(name: str) -> bool:
    return not any(p.startswith("_") and not (p.startswith("__")
                                              and p.endswith("__"))
                   for p in name.split("."))


def build_index(finder: Finder, package: str) -> PackageIndex:
    t0 = time.perf_counter()
    readers: Dict[str, ModuleReader] = {}
    errors: Dict[str, str] = {}
    for module, py, pyi in finder.files(package):
        is_pkg = (py or pyi).replace("\\", "/").rsplit("/", 1)[-1].startswith(
            "__init__.") if (py or pyi) else False
        reader = ModuleReader(module, is_pkg)
        if py:
            reader.read(py)
        if pyi:
            reader.read(pyi, stub=True)
        if reader.error and not reader.defs:
            errors[module] = reader.error
        readers[module] = reader

    entries: Dict[str, dict] = {}
    for module, r in readers.items():
        entries[module] = _entry(module, "module", module, "", r.doc,
                                 "", 0)
        for name, e in r.defs.items():
            entries[e["qual"]] = e
        for owner, members in r.members.items():
            for m in members:
                entries.setdefault(m["qual"], m)

    # add_newdoc docstrings (numpy's C functions and methods). Those whose
    # target is not defined where the call says — `ndarray` is declared in
    # numpy/__init__.pyi but documented as numpy._core.multiarray.ndarray —
    # are retried below, once public names exist.
    leftover: List[Tuple[str, str, str]] = []
    for r in readers.values():
        for mod, name, text in r.newdocs:
            qual = f"{mod}.{name}"
            target = entries.get(qual)
            if target is None and "." not in name and mod in entries \
                    and mod in readers and name not in readers[mod].imports \
                    and not readers[mod].stars:
                target = entries[qual] = _entry(qual, "function", mod,
                                                "", "", "", 0)
            if target is None:
                leftover.append((mod, name, text))
            elif not target.get("doc"):
                target["doc"] = _clean_doc(text)

    # -- re-exports ----------------------------------------------------
    resolving: Set[Tuple[str, str]] = set()

    def resolve(module: str, name: str, depth: int = 0) -> Optional[str]:
        r = readers.get(module)
        if r is None or depth > 12 or (module, name) in resolving:
            return None
        resolving.add((module, name))
        try:
            if name in r.defs:
                return r.defs[name]["qual"]
            target = r.imports.get(name)
            if target is not None:
                if target in readers:
                    return target
                tmod, _, tname = target.rpartition(".")
                if tmod in readers:
                    return resolve(tmod, tname, depth + 1)
                return None
            sub = f"{module}.{name}"
            if sub in readers:
                return sub
            for star in r.stars:
                sr = readers.get(star)
                if sr is None or name.startswith("_"):
                    continue
                if sr.all is not None and name not in sr.all:
                    continue
                hit = resolve(star, name, depth + 1)
                if hit:
                    return hit
            return None
        finally:
            resolving.discard((module, name))

    export_cache: Dict[str, Set[str]] = {}

    def exports(module: str, depth: int = 0) -> Set[str]:
        if module in export_cache:
            return export_cache[module]
        r = readers.get(module)
        if r is None or depth > 8:
            return set()
        export_cache[module] = set()            # recursion guard
        if r.all is not None:
            names = set(r.all)
        else:
            names = {n for n in r.defs if not n.startswith("_")}
            names |= {n for n, t in r.imports.items()
                      if not n.startswith("_")
                      and t.split(".")[0] == package}
            for star in r.stars:
                names |= {n for n in exports(star, depth + 1)
                          if not n.startswith("_")}
        export_cache[module] = names
        return names

    aliases: Dict[str, str] = {}
    for qual in entries:
        aliases[qual] = qual
    for module in readers:
        if not _public(module):
            continue
        for name in exports(module):
            qual = resolve(module, name)
            if qual and qual in entries:
                aliases.setdefault(f"{module}.{name}", qual)

    # The public name: shortest public alias, else the defining path.
    best: Dict[str, str] = {}
    for alias, qual in aliases.items():
        if not _public(alias):
            continue
        current = best.get(qual)
        if current is None or (alias.count("."), len(alias)) < (
                current.count("."), len(current)):
            best[qual] = alias
    for qual, e in entries.items():
        e["public"] = best.get(qual, qual)
    # Members of a re-exported class get the class's public prefix too:
    # numpy.ndarray.reshape, not numpy._core.multiarray.ndarray.reshape.
    for qual, e in entries.items():
        if e["kind"] in ("method", "property", "attribute") or (
                e["kind"] == "class" and qual.count(".") > e["module"].count(
                    ".") + 1):
            owner, _, member = qual.rpartition(".")
            owner_e = entries.get(owner)
            if owner_e is not None and owner_e.get("public") != owner:
                alias = f"{owner_e['public']}.{member}"
                e["public"] = alias
                aliases.setdefault(alias, qual)
    for qual, e in entries.items():
        aliases.setdefault(e["public"], qual)

    for mod, name, text in leftover:
        qual = (aliases.get(f"{mod}.{name}")
                or aliases.get(f"{package}.{name}"))
        if qual is None and "." in name:
            owner, _, member = name.rpartition(".")
            owner_qual = (aliases.get(f"{mod}.{owner}")
                          or aliases.get(f"{package}.{owner}"))
            if owner_qual is not None and not member.startswith("_"):
                owner_e = entries[owner_qual]
                qual = f"{owner_qual}.{member}"
                entries[qual] = _entry(qual, "method", owner_e["module"], "",
                                       "", "", 0, name=member)
                entries[qual]["public"] = f"{owner_e['public']}.{member}"
                aliases[qual] = qual
                aliases.setdefault(entries[qual]["public"], qual)
        if qual is None and "." not in name and mod in entries:
            qual = f"{mod}.{name}"
            entries[qual] = _entry(qual, "function", mod, "", "", "", 0)
            entries[qual]["public"] = qual
            aliases[qual] = qual
        if qual is not None and not entries[qual].get("doc"):
            entries[qual]["doc"] = _clean_doc(text)

    # "Defined in numpy/linalg/_linalg.py", not the absolute path: the page
    # goes into a model's prompt, where a 100-character site-packages path is
    # 40 tokens of nothing.
    root = finder.packages().get(package, ("", ""))[0]
    for e in entries.values():
        if e.get("file") and root:
            try:
                e["file"] = os.path.relpath(e["file"], root).replace(
                    os.sep, "/")
            except ValueError:
                pass

    modules = sorted(readers)
    return PackageIndex(package, entries, aliases, modules, errors,
                        seconds=time.perf_counter() - t0)


# ======================================================================
# Pages
# ======================================================================

def signature_line(e: dict) -> str:
    public = e.get("public") or e["qual"]
    kind = e["kind"]
    if kind in ("function", "method"):
        ret = f" -> {e['returns']}" if e.get("returns") else ""
        return f"{public}({e.get('sig', '')}){ret}"
    if kind == "class":
        return f"class {public}({e.get('sig', '')})"
    if kind in ("constant", "data", "attribute"):
        ann = f": {e['annotation']}" if e.get("annotation") else ""
        val = f" = {e['value']}" if e.get("value") else ""
        return f"{public}{ann}{val}"
    if kind == "property":
        return f"{public}  (property)"
    return public


def render_page(index: PackageIndex, e: dict, max_chars: int = 12000) -> str:
    public = e.get("public") or e["qual"]
    lines = [f"# {public}  ({e['kind']})", ""]
    if e["kind"] != "module":
        lines += ["```python", signature_line(e), "```", ""]
    if e.get("bases"):
        lines += ["Bases: " + ", ".join(e["bases"]), ""]
    doc = e.get("doc") or ""
    lines += [doc if doc else "(No docstring.)", ""]
    members = index.members_of(e["qual"])
    if e["kind"] == "module":
        mod = e["qual"]
        members = [index.entries[q] for q in index.entries
                   if index.entries[q].get("module") == mod
                   and q.count(".") == mod.count(".") + 1
                   and index.entries[q]["kind"] != "module"
                   and not q.rsplit(".", 1)[-1].startswith("_")]
        subs = [m for m in index.modules if m.startswith(mod + ".")
                and m.count(".") == mod.count(".") + 1 and _public(m)]
        exported = sorted(a for a, q in index.aliases.items()
                          if a.startswith(mod + ".") and "." not in a[
                              len(mod) + 1:] and index.entries[q]["module"]
                          != mod and index.entries[q]["kind"] != "module")
        if subs:
            lines += ["Submodules: " + ", ".join(subs[:60]), ""]
        if exported:
            lines += ["Also available here: " + ", ".join(
                a.rsplit(".", 1)[-1] for a in exported[:120]), ""]
    if members:
        lines.append("Members:")
        for m in members[:80]:
            summary = clip(first_paragraph(m.get("doc", "")), 120)
            lines.append(f"- {signature_line(m)}"
                         + (f" — {summary}" if summary else ""))
        if len(members) > 80:
            lines.append(f"- … and {len(members) - 80} more")
        lines.append("")
    where = e.get("file") or ""
    if where:
        lines.append(f"Defined in {where}"
                     + (f", line {e['line']}" if e.get("line") else ""))
    text = "\n".join(lines).rstrip() + "\n"
    if len(text) > max_chars:
        text = text[:max_chars - 40].rstrip() + "\n\n[… page truncated]\n"
    return text


# ======================================================================
# The library: packages on demand
# ======================================================================

class Library:
    def __init__(self, finder: Finder, packages: Sequence[str] = (),
                 cache_dir: str = ""):
        self.finder = finder
        self.packages = [p for p in packages if p]
        self.cache_dir = cache_dir
        self._indexes: Dict[str, PackageIndex] = {}

    def installed(self) -> List[str]:
        return sorted(n for n in self.finder.packages()
                      if not n.startswith("_"))

    def index(self, package: str) -> Optional[PackageIndex]:
        package = package.strip().split(".")[0]
        if package in self._indexes:
            return self._indexes[package]
        if package not in self.finder.packages():
            return None
        idx = self._from_cache(package)
        if idx is None:
            idx = build_index(self.finder, package)
            log(f"[pydocs] indexed {package}: {len(idx.entries)} objects in "
                f"{idx.seconds:.2f} s")
            self._to_cache(package, idx)
        self._indexes[package] = idx
        return idx

    def _cache_file(self, package: str) -> str:
        key = hashlib.sha1("|".join(
            [package, self.finder.packages()[package][0]]).encode(
                "utf-8", "replace")).hexdigest()[:16]
        return os.path.join(self.cache_dir, f"{package}-{key}.json")

    def _from_cache(self, package: str) -> Optional[PackageIndex]:
        if not self.cache_dir:
            return None
        path = self._cache_file(package)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("format") != INDEX_FORMAT or data.get(
                    "fingerprint") != self.finder.fingerprint(package):
                return None
            return PackageIndex.from_json(data)
        except (OSError, ValueError, KeyError):
            return None

    def _to_cache(self, package: str, idx: PackageIndex) -> None:
        if not self.cache_dir:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            data = idx.to_json()
            data["fingerprint"] = self.finder.fingerprint(package)
            path = self._cache_file(package)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError as exc:
            log(f"[pydocs] cache not written: {exc}")

    def packages_for(self, query: str, package: str = "") -> List[str]:
        """Which packages a search covers: the argument, else --package,
        else a package named in the query, else every one indexed so far."""
        if package:
            return [p.strip().split(".")[0] for p in package.split(",")
                    if p.strip()]
        if self.packages:
            return list(self.packages)
        installed = self.finder.packages()
        named = []
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query or ""):
            if word in installed and word not in named and \
                    not word.startswith("_"):
                named.append(word)
        if named:
            return named
        return list(self._indexes)

    def resolve(self, name: str) -> Tuple[Optional[PackageIndex],
                                          Optional[dict]]:
        name = (name or "").strip().strip("`")
        if name.startswith("pydoc://"):
            name = name[len("pydoc://"):]
        top = name.split(".")[0]
        order = []
        if top in self.finder.packages():
            order.append(top)
        order += [p for p in self.packages + list(self._indexes)
                  if p not in order]
        for package in order:
            idx = self.index(package)
            if idx is None:
                continue
            e = idx.lookup(name)
            if e is not None:
                return idx, e
        return None, None

    def suggestions(self, name: str) -> List[str]:
        out: List[str] = []
        for package in self.packages + [p for p in self._indexes
                                         if p not in self.packages]:
            idx = self.index(package)
            if idx is not None:
                out += idx.suggestions(name)
        return out[:6]


# ======================================================================
# MCP
# ======================================================================

SEARCH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "kind": {"type": "string"},
                           "signature": {"type": "string"},
                           "summary": {"type": "string"},
                           "score": {"type": "number"},
                           "uri": {"type": "string"}},
            "required": ["name", "kind"]}},
        "packages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["results"],
}

TOOLS = [
    {"name": "search_docs",
     "title": "Search Python package documentation",
     "description": (
         "Search the documentation of an installed Python package. Returns "
         "the best-matching functions, classes, methods, constants and "
         "modules with their signatures and one-line summaries. Read one in "
         "full with get_doc."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "What to look for, in words or "
                                      "identifiers."},
             "package": {"type": "string",
                         "description": "Top-level package to search, e.g. "
                                        "numpy. Optional when the server was "
                                        "started for one package."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 25,
                       "default": 8}},
         "required": ["query"]},
     "outputSchema": SEARCH_RESULT_SCHEMA},
    {"name": "get_doc",
     "title": "Read one documentation page",
     "description": (
         "The full documentation page for one Python object: its signature, "
         "docstring and members. Pass a dotted name from search_docs, e.g. "
         "numpy.linalg.solve."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "name": {"type": "string",
                      "description": "Dotted name of a module, class, "
                                     "function, method or constant."},
             "max_chars": {"type": "integer", "minimum": 500,
                           "maximum": 60000, "default": 12000}},
         "required": ["name"]}},
    {"name": "list_packages",
     "title": "List documentable packages",
     "description": "Installed top-level Python packages this server can "
                    "document.",
     "inputSchema": {
         "type": "object",
         "properties": {"prefix": {"type": "string"}}}},
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Server:
    def __init__(self, library: Library):
        self.library = library
        self.version = SUPPORTED_PROTOCOLS[0]

    # -- tools -------------------------------------------------------------
    def _text(self, text: str, structured: Optional[dict] = None,
              error: bool = False) -> dict:
        result: Dict[str, Any] = {"content": [{"type": "text", "text": text}],
                                  "isError": bool(error)}
        if structured is not None and self.version >= STRUCTURED_SINCE:
            result["structuredContent"] = structured
        return result

    def tool_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return self._text("search_docs needs a query.", error=True)
        try:
            limit = max(1, min(25, int(args.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        packages = self.library.packages_for(query, str(args.get("package")
                                                        or ""))
        if not packages:
            some = ", ".join(self.library.installed()[:40])
            return self._text(
                "Say which package to search: pass package='<name>'. "
                f"Installed packages include: {some}", error=True)
        missing = [p for p in packages
                   if p not in self.library.finder.packages()]
        if missing and len(missing) == len(packages):
            return self._text(
                f"{', '.join(missing)} is not installed for this "
                f"interpreter.", error=True)
        ranked: List[Tuple[float, dict]] = []
        for p in packages:
            idx = self.library.index(p)
            if idx is not None:
                ranked += idx.search(query, limit)
        ranked.sort(key=lambda se: -se[0])
        ranked = ranked[:limit]
        results = [{"name": e.get("public") or e["qual"], "kind": e["kind"],
                    "signature": signature_line(e),
                    "summary": clip(first_paragraph(e.get("doc", "")), 200),
                    "score": round(score, 3),
                    "uri": "pydoc://" + (e.get("public") or e["qual"])}
                   for score, e in ranked]
        if not results:
            text = (f"No documentation in {', '.join(packages)} matches "
                    f"{query!r}.")
        else:
            text = "\n".join(
                f"{i}. {r['name']} [{r['kind']}] — {r['signature']}"
                + (f"\n   {r['summary']}" if r["summary"] else "")
                for i, r in enumerate(results, 1))
        return self._text(text, {"results": results, "packages": packages})

    def tool_get_doc(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return self._text("get_doc needs a name.", error=True)
        try:
            max_chars = max(500, min(60000, int(args.get("max_chars")
                                                or 12000)))
        except (TypeError, ValueError):
            max_chars = 12000
        idx, e = self.library.resolve(name)
        if e is None or idx is None:
            close = self.library.suggestions(name)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return self._text(f"No documentation for {name!r}.{hint}",
                              error=True)
        return self._text(render_page(idx, e, max_chars))

    def tool_list_packages(self, args: dict) -> dict:
        prefix = str(args.get("prefix") or "").lower()
        names = [n for n in self.library.installed()
                 if n.lower().startswith(prefix)]
        return self._text("\n".join(names) if names else "(none)",
                          {"packages": names})

    # -- resources -----------------------------------------------------------
    def _resource_names(self) -> List[str]:
        if self.library.packages:
            out: List[str] = []
            for p in self.library.packages:
                idx = self.library.index(p)
                if idx is not None:
                    out += [m for m in idx.modules if _public(m)]
            return out
        return self.library.installed()

    def resources_list(self, params: dict) -> dict:
        names = self._resource_names()
        try:
            start = int(params.get("cursor") or 0)
        except (TypeError, ValueError):
            raise RpcError(-32602, "Invalid cursor")
        page = names[start:start + PAGE_SIZE]
        result: Dict[str, Any] = {"resources": [
            {"uri": f"pydoc://{n}", "name": n,
             "title": f"{n} (module documentation)",
             "mimeType": "text/markdown"} for n in page]}
        if start + PAGE_SIZE < len(names):
            result["nextCursor"] = str(start + PAGE_SIZE)
        return result

    def resources_read(self, params: dict) -> dict:
        uri = str(params.get("uri") or "")
        if not uri.startswith("pydoc://"):
            raise RpcError(-32002, f"Resource not found: {uri}")
        idx, e = self.library.resolve(uri)
        if e is None or idx is None:
            raise RpcError(-32002, f"Resource not found: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                              "text": render_page(idx, e, 60000)}]}

    # -- dispatch ------------------------------------------------------------
    def handle(self, message: dict) -> Optional[dict]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        method = message.get("method")
        if "id" not in message:
            return None                      # notifications need no answer
        rid = message["id"]
        if method is None:
            return None                      # a response to us; we ask none
        params = message.get("params") or {}
        try:
            result = self._dispatch(str(method), params)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:                          # noqa: BLE001
            log(f"[pydocs] {method} failed: {exc!r}")
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603,
                              "message": f"Internal error: {exc}"}}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            self.version = asked if asked in SUPPORTED_PROTOCOLS else \
                SUPPORTED_PROTOCOLS[0]
            return {"protocolVersion": self.version,
                    "capabilities": {"tools": {"listChanged": False},
                                     "resources": {"listChanged": False,
                                                   "subscribe": False}},
                    "serverInfo": {"name": SERVER_NAME,
                                   "version": SERVER_VERSION},
                    "instructions": (
                        "Documentation of installed Python packages, read "
                        "from their source. search_docs finds objects; "
                        "get_doc reads one page.")}
        if method == "ping":
            return {}
        if method == "tools/list":
            tools = TOOLS
            if self.version < STRUCTURED_SINCE:
                tools = [{k: v for k, v in t.items() if k not in (
                    "outputSchema", "title")} for t in TOOLS]
            return {"tools": tools}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise RpcError(-32602, "arguments must be an object")
            handler = {"search_docs": self.tool_search,
                       "get_doc": self.tool_get_doc,
                       "list_packages": self.tool_list_packages}.get(
                           str(name))
            if handler is None:
                raise RpcError(-32602, f"Unknown tool: {name}")
            return handler(args)
        if method == "resources/list":
            return self.resources_list(params)
        if method == "resources/read":
            return self.resources_read(params)
        if method == "resources/templates/list":
            return {"resourceTemplates": [
                {"uriTemplate": "pydoc://{name}", "name": "Python object",
                 "mimeType": "text/markdown"}]}
        raise RpcError(-32601, f"Method not found: {method}")


def serve(server: Server, stdin, stdout) -> None:
    def write(obj: Any) -> None:
        stdout.write((json.dumps(obj, ensure_ascii=False,
                                 separators=(",", ":")) + "\n").encode(
                                     "utf-8"))
        stdout.flush()

    for raw in iter(stdin.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError as exc:
            write({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"Parse error: {exc}"}})
            continue
        if isinstance(message, list):
            replies = [r for r in (server.handle(m) for m in message) if r]
            if replies:
                write(replies)
        else:
            reply = server.handle(message)
            if reply is not None:
                write(reply)


def make_library(argv: Optional[Sequence[str]] = None) -> Library:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", default="")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--cache-dir", default="")
    args = parser.parse_args(argv)
    if args.python:
        roots = interpreter_path(args.python)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots = [p for p in sys.path if p and os.path.abspath(p) != here]
    roots = [os.path.abspath(p) for p in args.path] + roots
    packages = [p.strip() for chunk in args.package for p in chunk.split(",")
                if p.strip()]
    return Library(Finder(roots), packages, args.cache_dir)


def main(argv: Optional[Sequence[str]] = None) -> int:
    real_out = sys.stdout.buffer
    sys.stdout = sys.stderr                  # stdout is for protocol only
    try:
        library = make_library(argv)
    except Exception as exc:                              # noqa: BLE001
        log(f"[pydocs] cannot start: {exc}")
        return 2
    serve(Server(library), sys.stdin.buffer, real_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
