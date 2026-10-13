"""
council_core.project — a code project the council works on, and what it
knows about it.

A coding agent that works from memory invents names and breaks rules it
was never told. Every coding job here starts from three things the project
keeps, so a small model sees the right files and the house rules instead:

  THE BRIEF      what the project is, its rules, how to run its tests and
                 how to check its GUI. The project's own CLAUDE.md /
                 COUNCIL.md / README is read (never written); the Council's
                 additions live in its own brief.md. The Council may PROPOSE
                 a change (brief_proposed.md); only the user accepts it.
  THE CODE MAP   every code file with its classes and functions and their
                 signatures (council_core.code_map), cached per file.
  REFERENCES     documents and scripts the user attaches — specs, notes,
                 example code — chunked and keyword-searched
                 (ProjectReferences), so a question pulls the few passages
                 it needs instead of pasting whole files into a small
                 context window.

Where it lives: <vault>/.council_projects/<slug>/

    project.json        root, test command, GUI checks, references
    brief.md            the Council's part of the brief (user-approved)
    brief_proposed.md   a proposed change, waiting for the user
    notes/              one handoff note per job (council_core.code_agent)
    worktrees/          one git worktree per job (council_core.worktree)
    code_map.json       the map's cache

The project folder itself is only read here. Nothing leaves the PC.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DIR_NAME = ".council_projects"
#: Files in the project root read as its own brief, in this order.
BRIEF_FILES = ("COUNCIL.md", "CLAUDE.md", "AGENTS.md", "README.md")
BRIEF_CHARS = 8000


def slug(name: str) -> str:
    s = re.sub(r"[^\w-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "project"


@dataclass
class GuiCheck:
    """A widget the GUI check builds: `module` and the class in it, with
    keyword arguments (JSON values) for its constructor."""
    module: str
    cls: str
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    name: str
    root: str
    #: Run in the project's root (a worktree during a job), split like a
    #: shell would but run without one. "" = no tests.
    test_command: str = ""
    gui_checks: List[GuiCheck] = field(default_factory=list)
    references: List[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def slug(self) -> str:
        return slug(self.name)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["gui_checks"] = [asdict(g) for g in self.gui_checks]
        return d

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Project":
        checks = [GuiCheck(**{k: v for k, v in g.items()
                              if k in ("module", "cls", "kwargs")})
                  for g in d.get("gui_checks") or [] if isinstance(g, dict)]
        return Project(name=str(d.get("name") or "project"),
                       root=str(d.get("root") or ""),
                       test_command=str(d.get("test_command") or ""),
                       gui_checks=checks,
                       references=[str(r) for r in d.get("references") or []],
                       created=float(d.get("created") or time.time()))


def projects_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME


def project_dir(vault_dir: Path, project: Project) -> Path:
    return projects_dir(vault_dir) / project.slug


def save(vault_dir: Path, project: Project) -> Path:
    """Write project.json (atomically)."""
    d = project_dir(vault_dir, project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "project.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(project.to_json(), indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load(vault_dir: Path, name: str) -> Optional[Project]:
    path = projects_dir(vault_dir) / slug(name) / "project.json"
    try:
        return Project.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def list_projects(vault_dir: Path) -> List[Project]:
    out = []
    root = projects_dir(vault_dir)
    for d in sorted(root.glob("*/project.json")) if root.is_dir() else []:
        try:
            out.append(Project.from_json(json.loads(d.read_text(
                encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


# ============================================================
# The brief
# ============================================================

def repo_brief(root: Path) -> Tuple[str, str]:
    """(file name, text) of the project's own brief, or ("", "")."""
    for name in BRIEF_FILES:
        p = Path(root) / name
        try:
            if p.is_file():
                return name, p.read_text(encoding="utf-8",
                                         errors="replace")[:BRIEF_CHARS]
        except OSError:
            continue
    return "", ""


def council_brief(vault_dir: Path, project: Project) -> str:
    try:
        return (project_dir(vault_dir, project) / "brief.md").read_text(
            encoding="utf-8")
    except OSError:
        return ""


def brief(vault_dir: Path, project: Project,
          root: Optional[Path] = None) -> str:
    """The whole brief a coding job starts from."""
    name, own = repo_brief(Path(root or project.root))
    parts = [f"PROJECT: {project.name}"]
    if own:
        parts.append(f"FROM THE PROJECT'S {name}:\n{own}")
    extra = council_brief(vault_dir, project)
    if extra.strip():
        parts.append(f"THE COUNCIL'S NOTES ON THIS PROJECT:\n{extra}")
    if project.test_command:
        parts.append(f"TESTS: {project.test_command}")
    if project.gui_checks:
        parts.append("GUI CHECKS: " + ", ".join(
            f"{g.module}.{g.cls}" for g in project.gui_checks))
    return "\n\n".join(parts)


def propose_brief(vault_dir: Path, project: Project, text: str) -> Path:
    """The Council proposes a new brief.md; nothing changes until
    accept_brief."""
    d = project_dir(vault_dir, project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "brief_proposed.md"
    path.write_text(text, encoding="utf-8")
    return path


def proposed_brief(vault_dir: Path, project: Project) -> str:
    try:
        return (project_dir(vault_dir, project) / "brief_proposed.md") \
            .read_text(encoding="utf-8")
    except OSError:
        return ""


def accept_brief(vault_dir: Path, project: Project,
                 text: Optional[str] = None) -> bool:
    """The user's yes: the proposal (or `text`) becomes brief.md. The old
    brief is kept as brief-<time>.md, never overwritten in place."""
    d = project_dir(vault_dir, project)
    new = text if text is not None else proposed_brief(vault_dir, project)
    if not new.strip():
        return False
    d.mkdir(parents=True, exist_ok=True)
    cur = d / "brief.md"
    if cur.exists():
        cur.replace(d / time.strftime("brief-%Y%m%d-%H%M%S.md"))
    tmp = d / "brief.md.tmp"
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(cur)
    prop = d / "brief_proposed.md"
    if prop.exists() and text is None:
        prop.unlink()
    return True


# ============================================================
# References
# ============================================================

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d{2,}")
REF_SUFFIXES = {".py", ".md", ".txt", ".rst", ".json", ".yaml", ".yml",
                ".toml", ".ini", ".cfg", ".csv", ".log", ".xml", ".html",
                ".js", ".ts", ".c", ".h", ".cpp", ".cs", ".java", ".go",
                ".rs", ".pdf", ".docx", ".xlsx", ".sql", ".ui", ".qss"}
MAX_REF_FILES = 2000
MAX_REF_BYTES = 5_000_000


def _tokens(text: str) -> List[str]:
    out = []
    for t in _TOKEN.findall(text or ""):
        low = t.lower()
        out.append(low)
        if "_" in low:                  # snake_case: its parts count too
            out.extend(p for p in low.split("_") if len(p) > 2)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", t)
        if len(parts) > 1:              # CamelCase too
            out.extend(p.lower() for p in parts if len(p) > 2)
    return out


def _read(path: Path) -> str:
    if path.suffix.lower() in (".pdf", ".docx", ".xlsx"):
        try:
            import vault_rag
            return vault_rag._extract_text(path)
        except Exception:                                 # noqa: BLE001
            return ""
    try:
        if path.stat().st_size > MAX_REF_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _chunks(text: str, source: str, suffix: str) -> List[Dict[str, Any]]:
    try:
        import vault_rag
        return vault_rag._chunk_for(text, source, suffix)
    except Exception:                                     # noqa: BLE001
        size = 1200
        return [{"text": text[i:i + size], "source": source}
                for i in range(0, len(text), size)]


class ProjectReferences:
    """Keyword (BM25) search over the project's reference files. Rebuilt
    only when a file was added, removed or changed."""

    def __init__(self, paths: Sequence[str]):
        self.paths = [Path(p) for p in paths]
        self._sig: Tuple = ()
        self._chunks: List[Dict[str, Any]] = []
        self._tf: List[Counter] = []
        self._df: Counter = Counter()
        self._avg = 1.0

    def files(self) -> List[Path]:
        out: List[Path] = []
        for p in self.paths:
            if p.is_file():
                out.append(p)
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if len(out) >= MAX_REF_FILES:
                        break
                    rel = f.relative_to(p).parts
                    if f.is_file() and f.suffix.lower() in REF_SUFFIXES \
                            and not any(x.startswith(".") or x in (
                                "__pycache__", "node_modules", "venv")
                                for x in rel):
                        out.append(f)
        return out

    def _signature(self, files: List[Path]) -> Tuple:
        sig = []
        for f in files:
            try:
                st = f.stat()
                sig.append((str(f), st.st_size, int(st.st_mtime)))
            except OSError:
                continue
        return tuple(sig)

    def refresh(self) -> int:
        files = self.files()
        sig = self._signature(files)
        if sig == self._sig:
            return len(self._chunks)
        chunks: List[Dict[str, Any]] = []
        for f in files:
            text = _read(f)
            if text.strip():
                chunks.extend(_chunks(text, str(f), f.suffix.lower()))
        self._chunks = chunks
        self._tf = [Counter(_tokens(c.get("text", ""))) for c in chunks]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._avg = (sum(sum(tf.values()) for tf in self._tf)
                     / max(1, len(self._tf)))
        self._sig = sig
        return len(chunks)

    def search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """The k best chunks for `query`: {"text", "source", "score"}."""
        self.refresh()
        q = set(_tokens(query))
        if not q or not self._chunks:
            return []
        n = len(self._chunks)
        scored = []
        for i, tf in enumerate(self._tf):
            length = sum(tf.values()) or 1
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) /
                               (self._df[t] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length /
                                                 self._avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [dict(self._chunks[i], score=round(s, 3))
                for s, i in scored[:k]]

    def block(self, query: str, k: int = 4, max_chars: int = 5000) -> str:
        """A REFERENCES block for a prompt, or ""."""
        hits = self.search(query, k)
        if not hits:
            return ""
        parts, used = ["REFERENCES (from the documents attached to this "
                       "project):"], 0
        for i, h in enumerate(hits, 1):
            piece = f"[{i}] {Path(h['source']).name}\n{h['text'].strip()[:1600]}"
            if used + len(piece) > max_chars:
                break
            parts.append(piece)
            used += len(piece)
        return "\n\n".join(parts)


_REFS: Dict[Tuple[str, ...], ProjectReferences] = {}


def references(project: Project) -> ProjectReferences:
    """The (cached) reference index for `project`."""
    key = tuple(project.references)
    if key not in _REFS:
        _REFS[key] = ProjectReferences(project.references)
    return _REFS[key]


__all__ = ["Project", "GuiCheck", "save", "load", "list_projects",
           "project_dir", "projects_dir", "brief", "repo_brief",
           "propose_brief", "proposed_brief", "accept_brief",
           "ProjectReferences", "references", "slug"]
