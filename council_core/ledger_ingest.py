"""
ledger_ingest.py — the spreadsheet-ingestion pipeline as three calls a front
end (the Ledger tab, council_qt/tabs/ledger.py) or an agent tool can make, each returning
a summary instead of printing one.

    create_parent(template, csv)          template -> parent CSV (+ schema)
    run_extract(csv, sources, review)     sources  -> review workbook
    run_apply(review, fill_dir)           review   -> parent CSV (+ filled templates)

Nothing here knows about a toolkit: no widget, no thread. The tab runs these
on a worker thread; the tests call them directly. The work itself is
sample_ledger.py and source_extract.py — this file only gathers their results
into the numbers the user is shown (values found / pre-accepted / to review /
images read / problems).

ledger_tools(policy) builds the same two steps as safe_agent Tools for a
HUMAN to register in a ToolRegistry before it is frozen. Nothing in this file
registers anything: the model can never add a tool.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from sample_ledger import LedgerError, ParentCSV, read_template
from source_extract import (ImageReader, apply_review, decide, default_image_reader,
                            extract, load_aliases, workbook_files, write_review)

#: listener(line, fraction or None) - called from the worker thread with every
#: progress line (also printed in the terminal); the caller hops to its UI.
Listener = Callable[[str, Optional[float]], None]


def default_csv_for(template) -> Path:
    """The parent CSV sample_ledger.py's command line would make."""
    t = Path(template)
    return t.with_name(t.stem.replace(" ", "_") + "_parent.csv")


def default_review_for(csv_path) -> Path:
    return Path(csv_path).with_name(f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx")


def collect_sources(paths: Sequence) -> List[Path]:
    """Files as given; a folder means every workbook UNDER it, recursively
    (.xlsx, .xlsm, .xls, .xlsb; not Excel's ~$ lock files, and hidden /
    ``~$`` folders at every depth are pruned). Duplicates dropped, order
    kept."""
    out: List[Path] = []
    for s in paths:
        p = Path(s)
        found = workbook_files(p) if p.is_dir() else [p]
        for x in found:
            if x not in out:
                out.append(x)
    return out


# ── step 1: template -> parent CSV ──────────────────────────────────────
@dataclass
class ParentSummary:
    csv: str
    created: bool
    fields: int
    items: int
    samples: int
    rows: int
    description: str

    def describe(self) -> str:
        verb = "created" if self.created else "extended"
        return (f"Parent CSV {verb}: {self.csv}\n{self.fields} fields, {self.items} items; "
                f"{self.samples} samples / {self.rows} rows so far.")


def create_parent(template, csv_path=None, id_field: Optional[str] = None) -> ParentSummary:
    schema = read_template(template, id_field)
    csv_path = Path(csv_path) if csv_path else default_csv_for(template)
    existed = csv_path.exists()
    ledger = ParentCSV.create(csv_path, schema)
    return ParentSummary(str(ledger.path), not existed, len(schema.fields),
                         len(schema.items), len(ledger.sample_ids()), len(ledger.rows),
                         schema.describe())


# ── step 2: sources -> review workbook ──────────────────────────────────
@dataclass
class ExtractSummary:
    review: str
    files: int
    values: int
    pre_accepted: int
    to_review: int
    unmatched: int
    images: int
    images_read: int
    problems: List[str] = field(default_factory=list)
    #: One ``(filename, kind)`` per workbook Excel refused to open, so the
    #: Ledger log can show a breakdown at the end of a batch.
    open_failures: List[Any] = field(default_factory=list)

    def describe(self) -> str:
        s = (f"{self.files} files: {self.values} values found, {self.pre_accepted} "
             f"pre-accepted, {self.to_review} to review, {self.unmatched} values under "
             f"labels that matched no field, {self.images_read} of {self.images} "
             f"images read")
        if self.open_failures:
            s += "\n" + summarize_open_failures(self.open_failures)
        return s + "".join(f"\n  ! {p}" for p in self.problems) + f"\nreview: {self.review}"


_OPEN_FAILURE_LABELS = {
    "password": "password-protected",
    "rights": "rights-protected (IRM)",
    "corrupt": "corrupt / wrong format / blocked",
    "timeout": "timed out (sign-in prompt?)",
    "access": "access denied",
    "unavailable": "Excel COM not available",
    "unknown": "unknown reason",
    "other": "other error",
}


def summarize_open_failures(open_failures) -> str:
    """A one-line roll-up for the Ledger log."""
    from collections import Counter
    counts = Counter(k for _n, k in open_failures)
    parts = [f"{counts[k]} {label}" for k, label in _OPEN_FAILURE_LABELS.items()
             if counts.get(k)]
    return f"{len(open_failures)} files failed to open — " + ", ".join(parts)


def _images_read(notes) -> int:
    return sum(1 for n in notes if n.reader not in ("", "none installed")
               and not n.text.startswith(("reader failed", "(no text found)")))


def run_extract(csv_path, sources: Sequence, review_path=None, part: str = "",
                use_images: bool = True, image_reader: Optional[ImageReader] = None,
                progress: Optional[Listener] = None) -> ExtractSummary:
    from progress_report import Progress
    with Progress("ledger extract", listener=progress) as prog:
        s = _extract(csv_path, sources, review_path, part, use_images, image_reader, prog)
        prog.finish(f"{s.files} workbooks: {s.values} values found, {s.to_review} to "
                    f"review, {len(s.problems)} problems")
        return s


def _extract(csv_path, sources, review_path, part, use_images, image_reader,
             prog) -> ExtractSummary:
    say = prog.update
    ledger = ParentCSV(csv_path)
    if ledger.schema is None:
        raise LedgerError(f"{Path(csv_path).name} has no schema beside it; "
                          "create it from a template first")
    files = collect_sources(sources)
    if not files:
        raise LedgerError("no source workbooks: add .xlsx files or a folder of them")
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise LedgerError(f"source workbooks not found: {missing}")
    out = Path(review_path) if review_path else default_review_for(csv_path)
    mapping, ignore = load_aliases(csv_path)
    if use_images and image_reader is None:
        say("starting the local image reader…")
        image_reader = default_image_reader()
    say(f"reading {len(files)} workbooks…")
    run = extract(ledger.schema, files, mapping, ignore,
                  image_dir=out.with_name(out.stem + "_images"),
                  image_reader=image_reader, part=part, use_images=use_images,
                  progress=prog)
    prog.stage("writing the review workbook")
    dec = decide(run, ledger.schema)
    write_review(out, run, dec, ledger.schema, str(csv_path))
    ok = sum(d.accept for d in dec)
    return ExtractSummary(str(out), len(files), len(dec), ok, len(dec) - ok,
                          len(run.unmatched), len(run.images), _images_read(run.images),
                          list(run.problems), list(run.open_failures))


# ── step 3: review -> parent CSV (+ filled templates) ───────────────────
@dataclass
class ApplySummary:
    accepted: int
    rows: int
    csv_result: str
    learned: Dict[str, str]
    ignored: List[str]
    filled: List[str]
    problems: List[str] = field(default_factory=list)

    def describe(self) -> str:
        s = ""
        if self.learned:
            s += f"learned {len(self.learned)} label mappings\n"
        s += f"{self.accepted} values accepted -> {self.rows} rows: {self.csv_result}"
        s += "".join(f"\n  filled {f}" for f in self.filled)
        return s + "".join(f"\n  ! {w}" for w in self.problems[:30])


def run_apply(review_path, fill_dir=None, use_images: bool = True,
              image_reader: Optional[ImageReader] = None,
              progress: Optional[Listener] = None) -> ApplySummary:
    from progress_report import Progress
    with Progress("ledger apply", listener=progress) as prog:
        if use_images and image_reader is None:
            prog.update("starting the local image reader…")
            image_reader = default_image_reader()
        r = apply_review(review_path, fill_dir or None, image_reader=image_reader,
                         use_images=use_images, progress=prog)
        prog.finish(f"{r.accepted} values accepted -> {r.rows} rows: {r.csv_result}")
        return ApplySummary(r.accepted, r.rows, r.csv_result, dict(r.learned),
                            list(r.ignored), list(r.filled), list(r.warnings))


def open_path(path) -> None:
    """Open a workbook or folder in its default app (Excel for a review)."""
    p = str(path)
    if sys.platform.startswith("win"):
        os.startfile(p)                                   # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


# ── agent tools (for a human to register) ───────────────────────────────
def _inside(root: Path, raw: Any, what: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"'{what}' must be a non-empty string")
    p = Path(raw.strip())
    full = (p if p.is_absolute() else root / p).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise PermissionError(f"{what}: {raw!r} is outside {root}") from None
    return full


def ledger_tools(policy) -> Dict[str, Any]:
    """`ingest_extract` and `ingest_apply` as safe_agent Tools.

    Sources are read only from policy.file_root; the review workbook, the
    parent CSV it applies to, and filled templates must all be inside
    policy.output_dir — the agent's one writeable area. Values reach the CSV
    only through a review a person saved, never on the model's say-so.
    Images are not read by these tools (no OCR inside an agent step's time
    limit); the review lists them for the person.

    Returned, not registered: process-start wiring calls
    ``registry.register(tool)`` for the ones it wants, then ``freeze()``.
    """
    from safe_agent import Tool

    def extract_tool(args: Dict[str, Any], pol) -> Dict[str, Any]:
        out_root, in_root = Path(pol.output_dir), Path(pol.file_root)
        csv = _inside(out_root, args.get("csv"), "csv")
        raw = args.get("sources")
        if not isinstance(raw, list) or not raw:
            return {"error": "'sources' must be a non-empty list of paths"}
        sources = [_inside(in_root, s, "sources") for s in raw]
        review = out_root / f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        part = args.get("part", "")
        s = run_extract(csv, sources, review, part=part if isinstance(part, str) else "",
                        use_images=False)
        return {"review": str(review), "summary": s.describe()}

    def apply_tool(args: Dict[str, Any], pol) -> Dict[str, Any]:
        import json
        import openpyxl
        out_root, in_root = Path(pol.output_dir), Path(pol.file_root)
        review = _inside(out_root, args.get("review"), "review")
        wb = openpyxl.load_workbook(review, read_only=True)
        try:
            meta = json.loads(wb["_run"]["A1"].value)
        except Exception:
            return {"error": f"{review.name} is not a review workbook"}
        finally:
            wb.close()
        _inside(out_root, meta.get("csv"), "the review's parent CSV")
        for s in meta.get("sources", []):
            _inside(in_root, s, "the review's sources")
        fill = args.get("fill_dir")
        fill_dir = _inside(out_root, fill, "fill_dir") if fill else None
        s = run_apply(review, fill_dir, use_images=False)
        return {"summary": s.describe()}

    return {
        "ingest_extract": Tool(
            name="ingest_extract", fn=extract_tool, timeout_s=300,
            description="Read source Excel workbooks and write a review workbook of the "
                        "values found, for a person to check. Writes nothing else.",
            schema={"type": "object", "required": ["csv", "sources"], "properties": {
                "csv": {"type": "string", "description": "parent CSV (in the output area)"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": ".xlsx files or folders (in the file root)"},
                "part": {"type": "string", "description": "part number, if not in them"}}}),
        "ingest_apply": Tool(
            name="ingest_apply", fn=apply_tool, timeout_s=300,
            description="Put the values a person accepted in a saved review workbook "
                        "into its parent CSV, and optionally write filled templates.",
            schema={"type": "object", "required": ["review"], "properties": {
                "review": {"type": "string"},
                "fill_dir": {"type": "string", "description": "optional, in the output area"}}}),
    }
