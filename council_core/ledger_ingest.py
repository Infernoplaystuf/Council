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

#: progress(message) — called from the worker thread; the caller hops to its UI.
Progress = Callable[[str], None]


def default_csv_for(template) -> Path:
    """The parent CSV sample_ledger.py's command line would make."""
    t = Path(template)
    return t.with_name(t.stem.replace(" ", "_") + "_parent.csv")


def default_review_for(csv_path) -> Path:
    return Path(csv_path).with_name(f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx")


def collect_sources(paths: Sequence) -> List[Path]:
    """Files as given; a folder means every workbook in it (.xlsx, .xlsm,
    .xls, .xlsb; not Excel's ~$ lock files). Duplicates dropped, order kept."""
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

    def describe(self) -> str:
        s = (f"{self.files} files: {self.values} values found, {self.pre_accepted} "
             f"pre-accepted, {self.to_review} to review, {self.unmatched} values under "
             f"labels that matched no field, {self.images_read} of {self.images} "
             f"images read")
        return s + "".join(f"\n  ! {p}" for p in self.problems) + f"\nreview: {self.review}"


def _counting(reader: Optional[ImageReader], progress: Optional[Progress]):
    """Wrap the image reader so each image is reported as it is read."""
    if reader is None or progress is None:
        return reader
    n = [0]

    def read(data: bytes):
        n[0] += 1
        progress(f"reading image {n[0]}…")
        return reader(data)
    read.name = getattr(reader, "name", "image reader")
    return read


def _images_read(notes) -> int:
    return sum(1 for n in notes if n.reader not in ("", "none installed")
               and not n.text.startswith(("reader failed", "(no text found)")))


def run_extract(csv_path, sources: Sequence, review_path=None, part: str = "",
                use_images: bool = True, image_reader: Optional[ImageReader] = None,
                progress: Optional[Progress] = None) -> ExtractSummary:
    say = progress or (lambda _m: None)
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
                  image_reader=_counting(image_reader, progress), part=part,
                  use_images=use_images)
    dec = decide(run, ledger.schema)
    say("writing the review workbook…")
    write_review(out, run, dec, ledger.schema, str(csv_path))
    ok = sum(d.accept for d in dec)
    return ExtractSummary(str(out), len(files), len(dec), ok, len(dec) - ok,
                          len(run.unmatched), len(run.images), _images_read(run.images),
                          list(run.problems))


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
              progress: Optional[Progress] = None) -> ApplySummary:
    say = progress or (lambda _m: None)
    if use_images and image_reader is None:
        say("starting the local image reader…")
        image_reader = default_image_reader()
    say("applying the review…")
    r = apply_review(review_path, fill_dir or None,
                     image_reader=_counting(image_reader, progress), use_images=use_images)
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
