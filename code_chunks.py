"""
code_chunks.py — split source code on FUNCTION AND CLASS boundaries, and
extract the callable signatures at the same time.

Two outputs, one AST walk, because they are the same question asked twice:
"where does this unit of code begin and end?" is chunking, and "what does it
take and return?" is a tool definition.

WHY A CHARACTER WINDOW IS WRONG FOR CODE
----------------------------------------
vault_rag chunks prose at 800 characters with 150 of overlap, which is right for
documentation and wrong for source. An 800-char window lands mid-function: the
retrieved chunk holds a loop body with no `def` line, no arguments, and no
imports. A user asking "how do I call mesh()" gets the middle of an example
script and no signature — technically a relevant chunk, useless as an answer.
Splitting on the boundaries the language already defines means a retrieved chunk
is a whole callable thing.

WHY EVERY CHUNK CARRIES THE IMPORTS
-----------------------------------
A function retrieved on its own does not say that `Session` came from
`import acmecad as acme`. For a documentation assistant that IS the answer half
the time — the user needs the import line as much as the call. So each code
chunk is prefixed with the module's import block. It costs a few dozen tokens
and turns a fragment into something runnable.

WHY SIGNATURES ARE EXTRACTED HERE TOO
-------------------------------------
A signature is a fill-in-the-blanks template: the call structure is fixed and
only the argument VALUES vary. That is exactly how nx_transpile already renders
DREAM3D filters from nx_introspect's parsed signatures — the model never writes
the call, it supplies arguments that are checked against the real parameter
list. Generalising that to arbitrary Python needs the function's parameters,
their annotations and their defaults, which this walk already has in hand.

Python is parsed properly with ast. Other languages fall back to a brace/blank
line heuristic, and anything unparseable returns None so the caller can use its
existing window chunking rather than lose the file.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# A single callable longer than this is split further, on statement boundaries.
# Generous, because a whole function is the point; this only catches the
# 500-line monsters that would otherwise blow the context window on their own.
MAX_CHUNK_CHARS = 2400

# Import blocks longer than this are truncated in the per-chunk header — a
# module with 90 imports would otherwise cost more context than the code.
MAX_HEADER_CHARS = 600

_PY_SUFFIXES = frozenset({".py", ".pyw", ".pyi"})
# Languages where a blank line before a brace-or-keyword line is a decent
# boundary. Deliberately crude: better than a character window, honest about
# not being a parser.
_BRACE_SUFFIXES = frozenset({".js", ".ts", ".java", ".cs", ".cpp", ".cc",
                             ".c", ".h", ".hpp", ".go", ".rs", ".m"})


@dataclass
class Param:
    name: str
    annotation: str = ""
    default: str = ""          # rendered source, "" when required
    kind: str = "positional"   # positional | vararg | kwonly | kwarg

    @property
    def required(self) -> bool:
        return self.default == "" and self.kind in ("positional", "kwonly")


@dataclass
class Signature:
    """One callable, as a fill-in-the-blanks template.

    This is the mad-libs record: `name` and `params` are fixed by the source,
    and a caller supplies only the argument values — which can then be checked
    back against `params` before anything is emitted, the same way
    nx_generate.validate checks every emitted arg key against the installed
    filter catalogue."""
    name: str
    qualname: str
    kind: str                  # function | method | class
    params: List[Param] = field(default_factory=list)
    returns: str = ""
    doc: str = ""
    decorators: List[str] = field(default_factory=list)
    source_file: str = ""
    lineno: int = 0
    is_public: bool = True

    def render_call(self, args: Optional[Dict[str, Any]] = None,
                    receiver: str = "") -> str:
        """The call with the blanks filled in.

        Unsupplied REQUIRED parameters are rendered as <name> placeholders
        rather than omitted, so an incomplete call is visibly incomplete
        instead of a TypeError at run time."""
        vals = dict(args or {})
        parts: List[str] = []
        for p in self.params:
            if p.name in ("self", "cls"):
                continue
            if p.name in vals:
                parts.append(f"{p.name}={vals[p.name]!r}"
                             if not isinstance(vals[p.name], str)
                             or not vals[p.name].startswith(("nx.", "acme."))
                             else f"{p.name}={vals[p.name]}")
            elif p.required:
                parts.append(f"{p.name}=<{p.annotation or 'value'}>")
        head = f"{receiver}." if receiver else ""
        return f"{head}{self.name}({', '.join(parts)})"

    def to_tool_spec(self) -> Dict[str, Any]:
        """The shape a tool-calling layer wants: name, description, parameters.

        Emitted here so the catalogue can be handed to a model as a closed set
        of callables — the model chooses one and supplies arguments, it never
        writes the call itself."""
        return {
            "name": self.qualname.replace(".", "_"),
            "description": self.doc or f"{self.kind} {self.qualname}",
            "source": f"{self.source_file}:{self.lineno}",
            "parameters": [
                {"name": p.name, "type": p.annotation or "any",
                 "required": p.required,
                 **({"default": p.default} if p.default else {})}
                for p in self.params if p.name not in ("self", "cls")
            ],
            "returns": self.returns or "unknown",
        }


@dataclass
class CodeChunk:
    text: str
    source: str
    chunk_id: str
    chunk_index: int
    kind: str = "code"         # module | function | class | method | code
    name: str = ""
    lineno: int = 0

    def as_dict(self) -> Dict[str, Any]:
        """The dict shape vault_rag's index already stores."""
        return {"text": self.text, "source": self.source,
                "chunk_id": self.chunk_id, "chunk_index": self.chunk_index,
                "kind": self.kind, "name": self.name, "lineno": self.lineno}


# ============================================================
# Rendering helpers
# ============================================================

def _seg(lines: Sequence[str], node: ast.AST) -> str:
    """Exact source for a node, INCLUDING its decorators.

    ast sets a decorated function's lineno to the `def`, not to the first
    decorator, so slicing from node.lineno silently drops @property and
    friends — and a retrieved method that has lost its decorator reads as a
    plain method, which is a different thing."""
    start = getattr(node, "lineno", 1)
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, getattr(d, "lineno", start))
    end = getattr(node, "end_lineno", start)
    return "\n".join(lines[start - 1:end])


def _unparse(node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _params_of(node: ast.AST) -> List[Param]:
    """Every parameter, with annotation and default, in declaration order."""
    a = getattr(node, "args", None)
    if a is None:
        return []
    out: List[Param] = []
    pos = list(getattr(a, "posonlyargs", []) or []) + list(a.args)
    # Defaults bind to the TAIL of the positional list.
    pad = len(pos) - len(a.defaults)
    for i, arg in enumerate(pos):
        d = a.defaults[i - pad] if i >= pad else None
        out.append(Param(arg.arg, _unparse(arg.annotation), _unparse(d)))
    if a.vararg:
        out.append(Param(a.vararg.arg, _unparse(a.vararg.annotation),
                         kind="vararg"))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append(Param(arg.arg, _unparse(arg.annotation), _unparse(d),
                         kind="kwonly"))
    if a.kwarg:
        out.append(Param(a.kwarg.arg, _unparse(a.kwarg.annotation),
                         kind="kwarg"))
    return out


def _doc_summary(node: ast.AST) -> str:
    try:
        d = ast.get_docstring(node) or ""
    except Exception:
        d = ""
    return d.strip().split("\n\n")[0].replace("\n", " ").strip()[:300]


def _import_header(tree: ast.Module, lines: Sequence[str]) -> str:
    """The module's import block, for prefixing every code chunk."""
    got: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            got.append(_seg(lines, node))
    text = "\n".join(got)
    if len(text) > MAX_HEADER_CHARS:
        text = text[:MAX_HEADER_CHARS].rsplit("\n", 1)[0] + "\n# ... (more imports)"
    return text


def _cid(source: str, idx: int, text: str) -> str:
    return hashlib.md5(f"{source}:{idx}:{text[:50]}".encode()).hexdigest()[:16]


# ============================================================
# Signatures
# ============================================================

def extract_signatures(source_text: str, source_name: str = "") -> List[Signature]:
    """Every module-level function, class and method as a Signature.

    Returns [] rather than raising for source that does not parse — a corpus
    with one broken example script must still index the other ninety."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []
    out: List[Signature] = []

    def _fn(node, qual: str, kind: str) -> Signature:
        return Signature(
            name=node.name, qualname=qual, kind=kind,
            params=_params_of(node), returns=_unparse(node.returns),
            doc=_doc_summary(node),
            decorators=[_unparse(d) for d in (node.decorator_list or [])],
            source_file=source_name, lineno=getattr(node, "lineno", 0),
            is_public=not node.name.startswith("_"))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_fn(node, node.name, "function"))
        elif isinstance(node, ast.ClassDef):
            init = next((b for b in node.body
                         if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and b.name == "__init__"), None)
            # A class's constructor signature IS its call signature — that is
            # what a user needs to instantiate it, so the class carries
            # __init__'s parameters rather than an empty list.
            out.append(Signature(
                name=node.name, qualname=node.name, kind="class",
                params=_params_of(init) if init is not None else [],
                doc=_doc_summary(node),
                decorators=[_unparse(d) for d in (node.decorator_list or [])],
                source_file=source_name, lineno=getattr(node, "lineno", 0),
                is_public=not node.name.startswith("_")))
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(_fn(b, f"{node.name}.{b.name}", "method"))
    return out


# ============================================================
# Chunking
# ============================================================

def split_python(source_text: str, source_name: str = "") -> Optional[List[CodeChunk]]:
    """Chunk Python on def/class boundaries. None if it does not parse."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return None
    lines = source_text.splitlines()
    header = _import_header(tree, lines)
    chunks: List[CodeChunk] = []
    idx = 0

    def _add(text: str, kind: str, name: str, lineno: int) -> None:
        nonlocal idx
        body = text.strip()
        if not body:
            return
        # Prefix the imports so a retrieved function says where its names came
        # from. Skipped for the module chunk, which already contains them.
        full = (f"{header}\n\n{body}" if header and kind != "module"
                and header not in body else body)
        for part in _split_oversized(full, name):
            chunks.append(CodeChunk(text=part, source=source_name,
                                    chunk_id=_cid(source_name, idx, part),
                                    chunk_index=idx, kind=kind, name=name,
                                    lineno=lineno))
            idx += 1

    # Module preamble: docstring, imports, module-level constants. Skipping it
    # would lose "pip install acmecad" and every module-level default.
    top_bits: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        top_bits.append(_seg(lines, node))
    _add("\n".join(top_bits), "module", "<module>", 1)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add(_seg(lines, node), "function", node.name,
                 getattr(node, "lineno", 0))
        elif isinstance(node, ast.ClassDef):
            methods = [b for b in node.body
                       if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
            whole = _seg(lines, node)
            if len(whole) <= MAX_CHUNK_CHARS or not methods:
                _add(whole, "class", node.name, getattr(node, "lineno", 0))
                continue
            # Too big to keep whole: one chunk per method, each carrying the
            # class line so a retrieved method still says what it belongs to.
            class_line = lines[node.lineno - 1].strip()
            head_end = methods[0].lineno - 1
            _add("\n".join(lines[node.lineno - 1:head_end]), "class",
                 node.name, getattr(node, "lineno", 0))
            for m in methods:
                _add(f"{class_line}\n    ...\n\n{_seg(lines, m)}",
                     "method", f"{node.name}.{m.name}",
                     getattr(m, "lineno", 0))
    return chunks


def _split_oversized(text: str, name: str) -> List[str]:
    """Break a single huge callable on LINE boundaries, never mid-line.

    Each part after the first repeats a marker naming what it continues, so a
    retrieved tail is not an anonymous block of statements."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    out: List[str] = []
    buf: List[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > MAX_CHUNK_CHARS and buf:
            out.append("\n".join(buf))
            buf, size = [f"# ... continued from {name}"], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def split_braced(source_text: str, source_name: str = "") -> List[CodeChunk]:
    """Heuristic boundaries for non-Python source.

    Deliberately crude and labelled as such: a real parser per language is out
    of scope, but a blank line followed by a signature-looking line is a far
    better boundary than a character offset."""
    lines = source_text.splitlines()
    starts = [0]
    pat = re.compile(r"^[A-Za-z_$@#].*[({]\s*$|^\s*(?:public|private|protected|"
                     r"static|func|fn|def|class|struct|impl)\b")
    for i in range(1, len(lines)):
        if pat.match(lines[i]) and (not lines[i - 1].strip()):
            starts.append(i)
    starts.append(len(lines))
    out: List[CodeChunk] = []
    idx = 0
    for a, b in zip(starts, starts[1:]):
        body = "\n".join(lines[a:b]).strip()
        if not body:
            continue
        for part in _split_oversized(body, source_name):
            out.append(CodeChunk(text=part, source=source_name,
                                 chunk_id=_cid(source_name, idx, part),
                                 chunk_index=idx, kind="code",
                                 name="", lineno=a + 1))
            idx += 1
    return out


def chunk_source(text: str, source_name: str, suffix: str
                 ) -> Optional[List[Dict[str, Any]]]:
    """Code-aware chunks for ``text``, or None to use the caller's fallback.

    None (rather than an empty list or a guess) is how a caller tells the
    difference between "this is not code" and "this is code with no content" —
    the first must fall back to window chunking, the second must not."""
    suf = (suffix or "").lower()
    if suf in _PY_SUFFIXES:
        got = split_python(text, source_name)
        return [c.as_dict() for c in got] if got else None
    if suf in _BRACE_SUFFIXES:
        got = split_braced(text, source_name)
        return [c.as_dict() for c in got] if got else None
    return None


def build_catalogue(files: Sequence[Any]) -> List[Dict[str, Any]]:
    """Every public callable across ``files``, as tool specs.

    The mad-libs catalogue: hand this to a model as a CLOSED set of callables
    and it selects one and supplies arguments, exactly as nx_generate hands it
    the installed DREAM3D filter list. Private names (leading underscore) are
    excluded — they are not the API a user is meant to call."""
    from pathlib import Path
    out: List[Dict[str, Any]] = []
    for f in files:
        p = Path(f)
        if p.suffix.lower() not in _PY_SUFFIXES:
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sig in extract_signatures(src, p.name):
            if sig.is_public and sig.kind in ("function", "class", "method"):
                if "." in sig.qualname and sig.name.startswith("_"):
                    continue
                out.append(sig.to_tool_spec())
    return out
