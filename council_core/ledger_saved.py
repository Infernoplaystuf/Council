"""
council_core.ledger_saved — Ledgers saved, named and grouped, so one can be
run again as its folders gain files (user, 2026-10-23: "I want to be able to
rerun from that stage ... ledger runs should be saved, categorized, and
named by the council"; grouped "both by template and or project/program").

A saved Ledger is a file beside its parent CSV, ``<csv>.ledger.json``: its
template, the folders and files it was run on as they were picked, the
settings of its last run (part, part-name pattern, operators, pictures),
a project / program name, the size and time of each file it read, and one
entry per run - its name, its review, its filled templates, its counts.
It travels with the CSV. An index in the app folder (``ledgers.json``)
lists the saved Ledgers for the Ledger tab; taking one off the list
deletes nothing.

``changed_files`` tells the files that are new or changed since they were
read, for the Ledger tab's "Update with new files". A run is named by the
rules (``auto_name``), or by Council's model when one was picked
(``model_name``); the user can rename it. Source files are never written.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SUFFIX = ".ledger.json"
INDEX_NAME = "ledgers.json"
#: how each kind of run is said in the Runs window
KINDS = {"extract": "Extract", "update": "Update with new files",
         "rerun": "Re-run with my answers"}
#: the Ledger tab's "Group by" choices -> how a saved Ledger is filed
GROUPS = {"template": "Template", "project": "Project / program",
          "project+template": "Project, then template"}
NO_PROJECT = "(no project)"


@dataclass
class Run:
    id: str                       # when it ran, to the second: unique per Ledger
    name: str
    kind: str                     # a KINDS key
    at: str                       # ISO time
    review: str = ""
    filled: str = ""              # the folder of its filled templates
    files: int = 0                # workbooks read
    new_files: int = 0            # of them new or changed (an update)
    values: int = 0
    pre_accepted: int = 0
    to_review: int = 0

    @property
    def how(self) -> str:
        return KINDS.get(self.kind, self.kind)


@dataclass
class SavedLedger:
    csv: str
    template: str = ""
    name: str = ""
    project: str = ""
    #: the folders and files as picked (a folder is scanned again each run)
    sources: List[str] = field(default_factory=list)
    #: the last run's part, pattern_mask, prefer, use_images
    settings: Dict[str, Any] = field(default_factory=dict)
    #: each file read: path -> [size, modified time in ns]
    files: Dict[str, List[int]] = field(default_factory=dict)
    runs: List[Run] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.name or Path(self.template or self.csv).stem

    @property
    def last(self) -> Optional[Run]:
        return self.runs[-1] if self.runs else None

    def run(self, run_id: str) -> Optional[Run]:
        return next((r for r in self.runs if r.id == run_id), None)


def path_for(csv) -> Path:
    return Path(csv).with_name(Path(csv).name + SUFFIX)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(csv) -> Optional[SavedLedger]:
    """The saved Ledger of this parent CSV, None when there is none (or it
    cannot be read)."""
    try:
        data = json.loads(path_for(csv).read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    runs = []
    for r in data.get("runs") or []:
        if isinstance(r, dict) and r.get("id"):
            runs.append(Run(**{k: v for k, v in r.items() if k in Run.__dataclass_fields__}))
    files = {str(k): list(v) for k, v in (data.get("files") or {}).items()
             if isinstance(v, (list, tuple)) and len(v) == 2}
    return SavedLedger(csv=str(csv), template=str(data.get("template") or ""),
                       name=str(data.get("name") or ""),
                       project=str(data.get("project") or ""),
                       sources=[str(s) for s in data.get("sources") or []],
                       settings=dict(data.get("settings") or {}), files=files, runs=runs)


def save(led: SavedLedger) -> None:
    """Beside its CSV (atomic), and on the app's list of saved Ledgers."""
    data = asdict(led)
    data.pop("csv")
    _write(path_for(led.csv), data)
    _index_add(led.csv)


# ── the list of saved Ledgers (the app folder) ──────────────────────────
def index_path() -> Path:
    from council_core import paths
    return paths.app_dir() / INDEX_NAME


def _index() -> List[str]:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return []
    return [str(c) for c in (data.get("ledgers") or [])] if isinstance(data, dict) else []


def _index_add(csv) -> None:
    have = _index()
    key = str(Path(csv).resolve())
    if key not in have:
        _write(index_path(), {"ledgers": have + [key]})


def forget(csv) -> None:
    """Off the list of saved Ledgers. Nothing is deleted: the CSV, its
    saved-Ledger file and its reviews stay where they are."""
    key = str(Path(csv).resolve())
    have = _index()
    if key in have:
        _write(index_path(), {"ledgers": [c for c in have if c != key]})


def listed() -> List[SavedLedger]:
    """The saved Ledgers on the list whose files are still there."""
    out = []
    for csv in _index():
        led = load(csv) if Path(csv).is_file() else None
        if led is not None:
            out.append(led)
    return out


def projects() -> List[str]:
    return sorted({led.project for led in listed() if led.project}, key=str.lower)


def group(ledgers: Sequence[SavedLedger], by: str = "template"
          ) -> List[Tuple[str, List[SavedLedger]]]:
    """The saved Ledgers filed under a heading each: by template, by project
    / program, or by project then template ('Project › Template')."""
    heads: Dict[str, List[SavedLedger]] = {}
    for led in ledgers:
        template = Path(led.template).stem if led.template else "(no template)"
        project = led.project or NO_PROJECT
        head = {"project": project,
                "project+template": f"{project} › {template}"}.get(by, template)
        heads.setdefault(head, []).append(led)
    return [(h, sorted(heads[h], key=lambda x: x.title.lower()))
            for h in sorted(heads, key=lambda h: (h.startswith("("), h.lower()))]


# ── files read, and what changed since ──────────────────────────────────
def file_key(path) -> str:
    """One spelling per file (a folder picked as D:\\x or d:/x/)."""
    try:
        p = Path(path).resolve()
    except OSError:
        p = Path(path)
    return os.path.normcase(str(p))


def stamp(path) -> Optional[List[int]]:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns)]


def changed_files(led: Optional[SavedLedger], files: Sequence
                  ) -> Tuple[List[str], List[str], List[str]]:
    """``files`` split into (new, changed, unchanged) against what the Ledger
    read before."""
    new, changed, same = [], [], []
    seen = (led.files if led is not None else {}) or {}
    for f in files:
        f = str(f)
        was = seen.get(file_key(f))
        if was is None:
            new.append(f)
        elif stamp(f) != list(was):
            changed.append(f)
        else:
            same.append(f)
    return new, changed, same


def files_read(review, given: Sequence = ()) -> List[str]:
    """The files a run read, by its review's hidden _run sheet (a file
    skipped as too slow is not one, so the next update tries it again);
    ``given`` when the review does not say."""
    from council_core import kg_excel
    try:
        meta = kg_excel._review_meta(Path(review)) if review else None
    except Exception:                                       # noqa: BLE001
        meta = None
    if not meta or not meta.get("sources"):
        return [str(f) for f in given]
    sources = [str(s) for s in meta["sources"]]
    read = meta.get("read")
    if isinstance(read, list):
        return [sources[i] for i in read if isinstance(i, int) and 0 <= i < len(sources)]
    return sources


# ── names ───────────────────────────────────────────────────────────────
def _source_words(sources: Sequence[str], most: int = 3) -> str:
    names = list(dict.fromkeys(Path(s).stem if Path(s).suffix else Path(s).name
                               for s in sources if s))
    if not names:
        return ""
    words = " + ".join(names[:most])
    return words + (f" + {len(names) - most} more" if len(names) > most else "")


def auto_name(template: str, sources: Sequence[str], when: Optional[datetime] = None,
              kind: str = "extract", new_files: int = 0) -> str:
    """'LMM Template · NDE results + Laser logs · 23 Oct 2026', and for an
    update '· 12 new files'."""
    when = when or datetime.now()
    bits = [Path(template).stem if template else "Ledger", _source_words(sources),
            f"{when.day} {when:%b %Y}"]
    if kind == "update":
        bits.append(f"{new_files} new file{'s' if new_files != 1 else ''}")
    elif kind == "rerun":
        bits.append("with your answers")
    return " · ".join(b for b in bits if b)


NAME_SCHEMA = {"type": "object", "properties": {"name": {"type": "string", "maxLength": 80}},
               "required": ["name"]}


def model_name(chat: Callable[[str, Dict[str, Any]], Tuple[str, str]], facts: str,
               fallback: str) -> str:
    """A run's name from Council's model (ledger_llm's Chat: local only),
    else ``fallback``. Facts are names and counts, never values."""
    prompt = ("Give a short name (at most 8 words) for this run of a data ledger, so a "
              "person can find it again in a list. Use the template, the folders and "
              "the date. Answer as JSON {\"name\": ...}.\n\n" + facts)
    try:
        text, _ = chat(prompt, NAME_SCHEMA)
        name = str(json.loads(text).get("name") or "").strip()
    except Exception:                                       # noqa: BLE001
        return fallback
    name = " ".join(name.split())[:80]
    return name or fallback


# ── recording a run ─────────────────────────────────────────────────────
def record_run(csv, summary, *, kind: str = "extract", sources: Sequence[str] = (),
               files: Sequence[str] = (), template: str = "",
               settings: Optional[Dict[str, Any]] = None, new_files: int = 0,
               namer: Optional[Callable[[str, str], str]] = None) -> Optional[Run]:
    """A finished run (an ledger_ingest.ExtractSummary) added to the saved
    Ledger of ``csv``, made when there is none. ``sources``: as picked
    (kept as the Ledger's; an update or re-run passes none and keeps
    them); ``files``: the files given to the run, used when its review does
    not say which it read. Each file read is stamped (size, time).
    ``namer(facts, fallback)`` names it (Council's model), else auto_name.
    None when the run wrote no review (nothing to keep)."""
    review = str(getattr(summary, "review", "") or "")
    if not review or not Path(review).is_file():
        return None
    led = load(csv) or SavedLedger(csv=str(csv))
    if template:
        led.template = str(template)
    if sources:
        led.sources = [str(s) for s in sources]
    if settings is not None:
        led.settings = dict(settings)
    read = files_read(review, files)
    if not led.sources:                    # an older review's Ledger: its files
        led.sources = [str(f) for f in read]
    for f in read:
        st = stamp(f)
        if st is not None:
            led.files[file_key(f)] = st
    now = datetime.now()
    rid = f"{now:%Y%m%d_%H%M%S}"
    while led.run(rid):
        rid += "_"
    fallback = auto_name(led.template, led.sources or read, now, kind, new_files)
    name = fallback
    if namer is not None:
        facts = (f"template: {Path(led.template).stem or '(none)'}\n"
                 f"folders: {_source_words(led.sources or read, most=6)}\n"
                 f"date: {now:%d %b %Y}\nkind: {KINDS.get(kind, kind)}\n"
                 f"files read: {len(read)}" + (f", {new_files} new or changed"
                                                if kind == "update" else ""))
        name = namer(facts, fallback)
    from council_core import ledger_review
    run = Run(id=rid, name=name, kind=kind, at=now.isoformat(timespec="seconds"),
              review=review, filled=str(ledger_review.filled_dir(review)), files=len(read),
              new_files=new_files, values=int(getattr(summary, "values", 0) or 0),
              pre_accepted=int(getattr(summary, "pre_accepted", 0) or 0),
              to_review=int(getattr(summary, "to_review", 0) or 0))
    led.runs.append(run)
    save(led)
    return run


def rename_run(csv, run_id: str, name: str) -> bool:
    led = load(csv)
    run = led.run(run_id) if led is not None else None
    if run is None or not name.strip():
        return False
    run.name = name.strip()
    save(led)
    return True


def set_details(csv, *, name: Optional[str] = None, project: Optional[str] = None) -> bool:
    """A saved Ledger's own name or project / program changed."""
    led = load(csv)
    if led is None:
        return False
    if name is not None:
        led.name = name.strip()
    if project is not None:
        led.project = project.strip()
    save(led)
    return True
