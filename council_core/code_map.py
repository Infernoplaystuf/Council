"""
council_core.code_map — every code file in a project with its classes,
functions and their signatures, for a coding agent to find its way.

The fan-out planner already read an index (file, size, top-level names).
A coding agent needs more: what a function takes, which class a method is
on, and where — so it can call the real `render(self, title)` instead of
inventing `render(title, width)`, and open the right lines. Python files
get full signatures (code_chunks.extract_signatures); other code files get
their function/class names (code_chunks' brace splitter).

Each file's outline is cached in <project dir>/code_map.json by size and
modification time, so a second job maps a large project in milliseconds.

`render(focus=…)` fits the map into a budget: files whose path or names
share words with the task come first, in full; the rest as one line each,
then just their paths.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

CODE_SUFFIXES = {".py", ".js", ".ts", ".java", ".cs", ".cpp", ".cc", ".c",
                 ".h", ".hpp", ".go", ".rs", ".m", ".qss", ".ui"}
SKIP_DIRS = {"__pycache__", "node_modules", "venv", ".venv", "build",
             "dist", ".git", ".mypy_cache", ".pytest_cache"}
MAX_FILE_BYTES = 1_500_000


@dataclass
class FileMap:
    path: str
    size: int
    mtime: int
    lines: int = 0
    items: List[str] = field(default_factory=list)   # "12 def f(a, b)"
    error: str = ""


def code_files(root: Path) -> List[str]:
    out = []
    root = Path(root)
    for p in sorted(root.rglob("*")):
        try:
            rel = PurePosixPath(p.relative_to(root).as_posix())
        except ValueError:
            continue
        if any(x in SKIP_DIRS or x.startswith(".") for x in rel.parts[:-1]):
            continue
        if not p.is_file() or p.suffix.lower() not in CODE_SUFFIXES:
            continue
        out.append(str(rel))
    return out


def _outline(path: Path) -> FileMap:
    st = path.stat()
    fm = FileMap(str(path), st.st_size, int(st.st_mtime))
    if st.st_size > MAX_FILE_BYTES:
        fm.error = "too large to map"
        return fm
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fm.error = str(exc)
        return fm
    fm.lines = text.count("\n") + 1
    import code_chunks
    if path.suffix.lower() == ".py":
        sigs = code_chunks.extract_signatures(text, path.name)
        if not sigs and text.strip() and "def " in text:
            fm.error = "does not parse"
        for s in sigs:
            params = ", ".join(
                p.name + (f": {p.annotation}" if p.annotation else "")
                + (f"={p.default}" if p.default else "") for p in s.params)
            ret = f" -> {s.returns}" if s.returns else ""
            fm.items.append(f"{s.lineno} {s.kind} {s.qualname}({params}){ret}")
    elif path.suffix.lower() not in (".qss", ".ui"):
        for c in code_chunks.split_braced(text, path.name) or []:
            if c.name:
                fm.items.append(f"{c.lineno} {c.kind} {c.name}")
    return fm


class CodeMap:
    def __init__(self, root: Path, cache: Optional[Path] = None):
        self.root = Path(root)
        self.cache = Path(cache) if cache else None
        self.files: Dict[str, FileMap] = {}

    def build(self) -> "CodeMap":
        old: Dict[str, Dict[str, Any]] = {}
        if self.cache and self.cache.exists():
            try:
                old = json.loads(self.cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
        files: Dict[str, FileMap] = {}
        for rel in code_files(self.root):
            p = self.root / rel
            try:
                st = p.stat()
            except OSError:
                continue
            o = old.get(rel)
            if o and o.get("size") == st.st_size and \
                    o.get("mtime") == int(st.st_mtime):
                files[rel] = FileMap(rel, o["size"], o["mtime"],
                                     o.get("lines", 0), o.get("items", []),
                                     o.get("error", ""))
                continue
            try:
                fm = _outline(p)
            except Exception as exc:                      # noqa: BLE001
                fm = FileMap(rel, st.st_size, int(st.st_mtime),
                             error=str(exc))
            fm.path = rel
            files[rel] = fm
        self.files = files
        if self.cache:
            try:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache.with_suffix(".tmp")
                tmp.write_text(json.dumps({k: vars(v) for k, v in
                                           files.items()}), encoding="utf-8")
                tmp.replace(self.cache)
            except OSError:
                pass
        return self

    def find(self, name: str) -> List[str]:
        """"path:line kind qualname(...)" for every item whose name
        contains `name` (case-insensitive)."""
        want = (name or "").lower()
        out = []
        for rel, fm in self.files.items():
            for it in fm.items:
                head = it.split("(", 1)[0]
                if want and want in head.lower():
                    line, rest = it.split(" ", 1)
                    out.append(f"{rel}:{line} {rest}")
        return out

    def render(self, focus: str = "", max_chars: int = 6000) -> str:
        """The map for a prompt, files relevant to `focus` first."""
        words = {w.lower() for w in re.findall(r"[A-Za-z_]{3,}", focus or "")}

        def score(rel: str, fm: FileMap) -> int:
            hay = (rel + " " + " ".join(fm.items)).lower()
            return sum(1 for w in words if w in hay)

        ranked = sorted(self.files.items(),
                        key=lambda kv: (-score(*kv), kv[0]))
        out, used = [f"CODE MAP of {self.root.name} ({len(self.files)} "
                     "files; line, kind, name(signature)):"], 0
        brief_rest: List[str] = []
        for rel, fm in ranked:
            full = score(rel, fm) > 0 or len(self.files) <= 12
            if full:
                block = f"{rel} ({fm.lines} lines)" + (
                    f" [{fm.error}]" if fm.error else "")
                block += "".join(f"\n    {it}" for it in fm.items[:60])
                if len(fm.items) > 60:
                    block += f"\n    … {len(fm.items) - 60} more"
            else:
                names = ", ".join(i.split(" ", 2)[-1].split("(")[0]
                                  for i in fm.items[:8])
                block = f"{rel}: {names}" if names else rel
            if used + len(block) > max_chars:
                brief_rest.append(rel)
                continue
            out.append(block)
            used += len(block)
        if brief_rest:
            listing = ", ".join(brief_rest)
            room = max(0, max_chars + 1500 - used)
            out.append("Other files: " + (listing if len(listing) <= room
                                          else listing[:room] + " …"))
        return "\n".join(out)


def for_project(root: Path, cache: Optional[Path] = None) -> CodeMap:
    return CodeMap(root, cache).build()


__all__ = ["CodeMap", "FileMap", "for_project", "code_files"]
