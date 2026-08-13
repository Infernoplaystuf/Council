"""
api_catalogue.py — the vault's own callables, as a searchable catalogue.

Built from code_chunks.extract_signatures over the .py files in the vault, so a
customer who drops a vendor's documentation folder and example scripts in gets
an index of every public function, class and method with its REAL parameter
list.

WHY THIS IS A LOOKUP SURFACE AND NOT AN EXECUTION SURFACE
---------------------------------------------------------
The obvious reading of "turn methods into tools" is to let the agent CALL them.
That would mean importing arbitrary Python out of the vault, and importing a
module runs its top level — a vendor example script can open files, spawn
processes or phone home before a single function is called. This app is
read-only on user data and offline by design, and there is no sandbox that makes
`import some_customer_module` safe.

So the catalogue answers questions instead: what callables exist, what does this
one take, what does a correct call look like. The agent writes code grounded in
the real signature and the USER runs it — exactly the shape the DREAM3D path
already uses, where nx_generate emits a script, nx_policy gates it, and
execution is a separate deliberate act.

That is not a lesser version of the idea. The failure it prevents is the one the
idea exists to solve: a model recalling `mesh(part, size=...)` from training
when the real parameter is `element_size`. The signature comes from the source
either way; only the running does not happen here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import code_chunks

# A vault can hold a lot of Python; this bounds a cold build. Raised freely —
# the cost is one ast.parse per file, roughly a millisecond each.
MAX_FILES = 4000
# Directories that are never API surface: the app's own output, caches, venvs.
SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", "node_modules", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", ".backups", ".trash",
})

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(s: str) -> List[str]:
    """Lower-case tokens, with snake_case and camelCase split.

    `element_size` must match a query for "size", and `elementSize` must match
    the same way — a user asking about a parameter does not know which
    convention the vendor used."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(s or ""))
    out: List[str] = []
    for t in _TOKEN_RE.findall(s.lower()):
        out.append(t)
        out.extend(p for p in t.split("_") if p and p != t)
    return out


@dataclass
class Entry:
    """One callable, plus the text it can be matched on."""
    spec: Dict[str, Any]
    haystack: str = ""
    name_tokens: frozenset = frozenset()

    @property
    def name(self) -> str:
        return str(self.spec.get("name", ""))

    @property
    def source(self) -> str:
        return str(self.spec.get("source", ""))


@dataclass
class Catalogue:
    entries: List[Entry] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.entries)

    # -- lookup ------------------------------------------------------

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Exact match on the tool name, then on the bare callable name.

        Two passes because a model will write `mesh_and_export` for something
        catalogued as `Mesher_run` as often as not, and refusing an
        almost-right name helps nobody."""
        want = str(name or "").strip().replace(".", "_")
        for e in self.entries:
            if e.name == want:
                return e.spec
        tail = want.rsplit("_", 1)[-1].lower()
        for e in self.entries:
            if e.name.lower().endswith(tail) or e.name.lower() == want.lower():
                return e.spec
        return None

    def search(self, query: str, k: int = 8) -> List[Dict[str, Any]]:
        """The k callables most likely to serve ``query``.

        Lexical and deterministic — no model call, no embedding, no warm-up.
        The catalogue is small and a single in-process GGUF serialises all
        inference, so paying a model call to pick a function the agent is about
        to be told about anyway would be backwards."""
        q = set(_tokens(query))
        if not q:
            return []
        scored: List[tuple] = []
        for e in self.entries:
            hits = q & set(_tokens(e.haystack))
            if not hits:
                continue
            # A hit in the NAME is worth more than one in the docstring: a user
            # asking about "export" wants export_stl, not a function whose doc
            # happens to mention exporting.
            score = len(hits) + 2.0 * len(q & e.name_tokens)
            scored.append((score, e.name, e.spec))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [spec for _s, _n, spec in scored[:k]]


def _render_params(spec: Dict[str, Any]) -> str:
    bits = []
    for p in spec.get("parameters", []):
        if p.get("required"):
            bits.append(f"{p['name']}: {p.get('type', 'any')}")
        else:
            bits.append(f"{p['name']}: {p.get('type', 'any')} = "
                        f"{p.get('default', 'None')}")
    return ", ".join(bits)


def describe(spec: Dict[str, Any]) -> str:
    """One catalogue entry, as the model should see it.

    The template line is the point: it shows the call SHAPE with the blanks
    marked, so the model supplies values instead of inventing the structure."""
    req = [p["name"] for p in spec.get("parameters", []) if p.get("required")]
    tmpl = ", ".join(f"{n}=<{n}>" for n in req)
    return (f"{spec['name']}({_render_params(spec)}) -> "
            f"{spec.get('returns') or 'unknown'}\n"
            f"    {spec.get('description') or '(no docstring)'}\n"
            f"    defined at {spec.get('source', '?')}\n"
            f"    template: {spec['name']}({tmpl})")


# ============================================================
# Build
# ============================================================

def _iter_py(root: Path, max_files: int) -> tuple:
    files: List[Path] = []
    truncated = False
    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None
    try:
        for p in sorted(root.rglob("*.py")):
            if any(part in SKIP_DIRS or part.startswith(".")
                   for part in p.relative_to(root).parts[:-1]):
                continue
            if _cl is not None:
                try:
                    if _cl.is_protected_path(p, root):
                        continue
                except Exception:
                    pass
            files.append(p)
            if len(files) >= max_files:
                truncated = True
                break
    except Exception:
        pass
    return files, truncated


_CACHE: Dict[str, tuple] = {}


def build(vault_dir: Any, *, max_files: int = MAX_FILES,
          use_cache: bool = True) -> Catalogue:
    """Catalogue every public callable under ``vault_dir``.

    Cached on a (count, newest-mtime) signature: a corpus that has not changed
    is not re-parsed, and one that has is rebuilt whole. Cheap enough that a
    finer-grained cache would cost more complexity than it saves."""
    root = Path(vault_dir)
    files, truncated = _iter_py(root, max_files)
    sig = ""
    if use_cache:
        try:
            newest = max((f.stat().st_mtime for f in files), default=0.0)
            sig = f"{len(files)}:{newest:.3f}"
            hit = _CACHE.get(str(root))
            if hit and hit[0] == sig:
                return hit[1]
        except OSError:
            sig = ""

    cat = Catalogue(files_scanned=len(files), truncated=truncated)
    for spec in code_chunks.build_catalogue(files):
        hay = " ".join([
            spec.get("name", ""), spec.get("description", ""),
            " ".join(p.get("name", "") for p in spec.get("parameters", [])),
            str(spec.get("returns", "")),
        ])
        cat.entries.append(Entry(
            spec=spec, haystack=hay,
            name_tokens=frozenset(_tokens(spec.get("name", "")))))
    if use_cache and sig:
        _CACHE[str(root)] = (sig, cat)
    return cat


def clear_cache() -> None:
    _CACHE.clear()
