"""A synthetic vault for building and scoring the knowledge graph.

The desktop's real vault holds code, logs and git clones, not the people /
parts / projects documents the graph is for, so the graph is developed against
this made-up company ("Ironbridge") instead. Every case that broke naive
extraction in the laptop's research is here on purpose:

  * 'Last, First' names ('Whitfield, Dana') and accented names (Tomás);
  * a 'D. Whitfield' who could be Dana or Dan (two real people);
  * part revisions where B supersedes A ('PN-1234/A', 'PN-1234/B');
  * a part code with leading zeros (PN-0088) and a description with a comma;
  * a drifted label ('POC' for 'Point of Contact') that must be confirmed;
  * links that exist ONLY in free text (part→project — no model found one);
  * a multi-sheet workbook whose people are on the second sheet, a 3-page PDF
    whose facts sit on pages 1 and 2, a Word file, JSON, and Collections.

``build(vault)`` writes the documents under ``<vault>/data_in/ironbridge/``
plus a ``collections.json``, and RETURNS the answer key (it is not written
into the vault, where it would be read as a document). It refuses to write
into an ironbridge folder that already has files, so it can never overwrite
anything. The file writers (xlsx / docx / pdf) are dependency-free: the
council env lacks openpyxl, python-docx and reportlab, and a test corpus that
needs them would silently not build there.

Run ``python -m council_core.kg_corpus <empty-folder>`` to make a demo vault
(the answer key lands next to it as ``<folder>_answer_key.json``).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Sequence
from xml.sax.saxutils import escape

CORPUS_DIR = "ironbridge"

# ── the world ────────────────────────────────────────────────────────────
# key -> (type, canonical name, aliases as they appear in the documents)
ENTITIES: Dict[str, tuple] = {
    "dana":   ("PERSON", "Dana Whitfield", ["Whitfield, Dana", "D. Whitfield"]),
    "dan":    ("PERSON", "Dan Whitfield", ["Dan"]),
    "carol":  ("PERSON", "Carol Lee", ["Lee, Carol"]),
    "marcus": ("PERSON", "Marcus Oyelaran", ["Oyelaran, Marcus", "M. Oyelaran"]),
    "priya":  ("PERSON", "Priya Raman", []),
    "tomas":  ("PERSON", "Tomás Echeverría", ["Tomas Echeverria"]),
    "hana":   ("PERSON", "Hana Kowalski", ["Kowalski, Hana", "Hana"]),
    "bob":    ("PERSON", "Bob Smith, Jr.", ["Bob Smith"]),
    "helios":    ("PROJECT", "PRJ-0915", ["Helios Turbine Upgrade", "Helios"]),
    "bands":     ("PROJECT", "PRJ-0920", ["Bearings & Seals Program"]),
    "northwind": ("PROJECT", "PRJ-0931", ["Northwind Retrofit", "Northwind"]),
    "atlas":     ("PROJECT", "PRJ-0944", ["Atlas Test Rig", "Atlas"]),
    "shim_a": ("PART", "PN-1234/A", []),
    "shim_b": ("PART", "PN-1234/B", []),
    "brg":    ("PART", "BRG-7720", []),
    "seal":   ("PART", "SL-0450", []),
    "hx":     ("PART", "HX-3301", []),
    "ctl":    ("PART", "CTL-5005", []),
    "gasket": ("PART", "PN-0088", []),
}

PREDICATES = ("LEADS", "CONTACT_FOR", "WORKS_ON", "OWNS", "USES_PART",
              "SUPERSEDES")

# The labels a user of this vault would confirm, by entity type. 'POC' is the
# drifted form: it should be SUGGESTED for 'Point of Contact', not assumed.
FIELD_LABELS = {
    "PROJECT": ["Project ID", "Project"],
    "PART": ["P/N", "Part", "Supersedes"],
    "PERSON": ["Program Lead", "Point of Contact", "Owner", "Originator",
               "Approved by", "Attendees", "pointOfContact"],
}
DRIFTED_LABELS = {"POC": "Point of Contact"}

TRACKER_ROWS = [
    # Project ID, Project Name, Program Lead, Point of Contact, Status
    ("PRJ-0915", "Helios Turbine Upgrade", "Whitfield, Dana", "Carol Lee", "Active"),
    ("PRJ-0920", "Bearings & Seals Program", "Oyelaran, Marcus", "Priya Raman", "Active"),
    ("PRJ-0931", "Northwind Retrofit", "Lee, Carol", "Tomás Echeverría", "Planning"),
    ("PRJ-0944", "Atlas Test Rig", "Kowalski, Hana", "Dan Whitfield", "On Hold"),
]
PARTS_ROWS = [
    # P/N, Description, Rev, Project, Owner, Supersedes
    ("PN-1234/A", "Turbine Blade Root Shim", "A", "PRJ-0915", "Dana Whitfield", ""),
    ("PN-1234/B", "Turbine Blade Root Shim", "B", "PRJ-0915", "Dana Whitfield", "PN-1234/A"),
    ("BRG-7720", "Bearing Pack (sealed)", "C", "PRJ-0920", "Marcus Oyelaran", ""),
    ("SL-0450", "Lip Seal, Viton", "A", "PRJ-0920", "Priya Raman", ""),
    ("HX-3301", "Heat Exchanger Core", "D", "PRJ-0931", "Tomás Echeverría", ""),
    ("CTL-5005", "Controller PCB", "B", "PRJ-0944", "Hana Kowalski", ""),
    ("PN-0088", "Gasket, Inlet", "A", "PRJ-0931", "Carol Lee", ""),
]
ACTION_ROWS = [
    # Item, POC, Project, Due
    ("AI-101", "Priya Raman", "PRJ-0920", "2026-05-01"),
    ("AI-102", "Lee, Carol", "PRJ-0915", "2026-05-08"),
    ("AI-103", "M. Oyelaran", "PRJ-0920", "2026-05-15"),
    ("AI-104", "Bob Smith", "PRJ-0931", "2026-05-22"),
]

HELIOS_STATUS_MD = """\
# Helios Turbine Upgrade — March status

Project: PRJ-0915
Owner: Whitfield, Dana
Point of Contact (Primary): Carol Lee

## Summary

Carol Lee confirmed that PN-1234/B replaces PN-1234/A on all Helios builds
from April onward.
The BRG-7720 bearing pack from the Bearings & Seals Program will also be
fitted to the Helios rig for the endurance run.
D. Whitfield will review the shim drawings with Tomás Echeverría next week.

## Risks

Supplier lead time on the shim is six weeks.
"""

ECN_PAGES = [
    ["ENGINEERING CHANGE NOTICE ECN-0915-014",
     "Project: PRJ-0915",
     "Title: Blade root shim revision",
     "Originator: Dana Whitfield",
     "Approved by: Lee, Carol; Kowalski, Hana",
     "Date: 2026-03-18"],
    ["Description of change",
     "PN-1234/B supersedes PN-1234/A. The B revision adds a 0.2 mm chamfer",
     "to stop fretting at the blade root.",
     "Part: PN-1234/B",
     "Supersedes: PN-1234/A"],
    ["Verification",
     "The revised shim PN-1234/B will be proven on the Atlas Test Rig",
     "before release to the Helios line.",
     "Hana Kowalski owns the rig schedule."],
]

NORTHWIND_DOCX = [
    "Northwind Retrofit — Design Review 2",
    "Project: PRJ-0931",
    "Attendees: Carol Lee, Tomás Echeverría, Bob Smith, Jr.",
    "Decision: the HX-3301 core moves to revision D.",
    "Bob Smith will source the PN-0088 inlet gaskets for Northwind.",
]

MEETING_TXT = """\
Atlas sync — 2026-04-02

Dan Whitfield reported a firmware fault on the CTL-5005 controller board.
Hana asked Dan to loop in Marcus Oyelaran about bearing noise.
Next sync in two weeks.
"""

UNRELATED_TXT = """\
Shopping list for the break room: coffee, filters, oat milk.
Reminder: the parking lot is being resurfaced on Friday.
"""

SUPPLIER_JSON = [
    {"supplier": "Sealtek", "pointOfContact": "Priya Raman", "part": "SL-0450"},
    {"supplier": "Rotary Bearings Ltd", "pointOfContact": "Marcus Oyelaran",
     "part": "BRG-7720"},
]

COLLECTIONS = {
    "Helios": ["ironbridge/reports/helios_status_2026-03.md",
               "ironbridge/reports/ecn_0915_014.pdf"],
    "Northwind": ["ironbridge/reports/northwind_design_review.docx"],
}

# ── the answer key ───────────────────────────────────────────────────────
# (subject key, predicate, object key, evidence, file)
# evidence: 'row' = two labelled cells in one table row; 'record' = labelled
# fields in one JSON record / one document header; 'free_text' = only prose
# says it (needs a model, stays suggested); 'collection' = project<->document.
RELATIONS: List[tuple] = [
    ("dana", "LEADS", "helios", "row", "trackers/program_tracker.xlsx"),
    ("carol", "CONTACT_FOR", "helios", "row", "trackers/program_tracker.xlsx"),
    ("marcus", "LEADS", "bands", "row", "trackers/program_tracker.xlsx"),
    ("priya", "CONTACT_FOR", "bands", "row", "trackers/program_tracker.xlsx"),
    ("carol", "LEADS", "northwind", "row", "trackers/program_tracker.xlsx"),
    ("tomas", "CONTACT_FOR", "northwind", "row", "trackers/program_tracker.xlsx"),
    ("hana", "LEADS", "atlas", "row", "trackers/program_tracker.xlsx"),
    ("dan", "CONTACT_FOR", "atlas", "row", "trackers/program_tracker.xlsx"),
    ("helios", "USES_PART", "shim_a", "row", "trackers/parts_list.csv"),
    ("helios", "USES_PART", "shim_b", "row", "trackers/parts_list.csv"),
    ("bands", "USES_PART", "brg", "row", "trackers/parts_list.csv"),
    ("bands", "USES_PART", "seal", "row", "trackers/parts_list.csv"),
    ("northwind", "USES_PART", "hx", "row", "trackers/parts_list.csv"),
    ("atlas", "USES_PART", "ctl", "row", "trackers/parts_list.csv"),
    ("northwind", "USES_PART", "gasket", "row", "trackers/parts_list.csv"),
    ("dana", "OWNS", "shim_a", "row", "trackers/parts_list.csv"),
    ("dana", "OWNS", "shim_b", "row", "trackers/parts_list.csv"),
    ("marcus", "OWNS", "brg", "row", "trackers/parts_list.csv"),
    ("priya", "OWNS", "seal", "row", "trackers/parts_list.csv"),
    ("tomas", "OWNS", "hx", "row", "trackers/parts_list.csv"),
    ("hana", "OWNS", "ctl", "row", "trackers/parts_list.csv"),
    ("carol", "OWNS", "gasket", "row", "trackers/parts_list.csv"),
    ("shim_b", "SUPERSEDES", "shim_a", "row", "trackers/parts_list.csv"),
    # Only reachable once the user confirms POC = Point of Contact.
    ("bob", "CONTACT_FOR", "northwind", "row", "trackers/action_items_q2.csv"),
    # ...and this one also needs 'M. Oyelaran' resolved to Marcus (the only
    # M. Oyelaran), which the graph infers and asks the user to confirm.
    ("marcus", "CONTACT_FOR", "bands", "row", "trackers/action_items_q2.csv"),
    ("priya", "CONTACT_FOR", "seal", "record", "trackers/supplier_contacts.json"),
    ("marcus", "CONTACT_FOR", "brg", "record", "trackers/supplier_contacts.json"),
    ("dana", "LEADS", "helios", "record", "reports/helios_status_2026-03.md"),
    ("carol", "CONTACT_FOR", "helios", "record", "reports/helios_status_2026-03.md"),
    ("dana", "WORKS_ON", "helios", "record", "reports/ecn_0915_014.pdf"),
    ("carol", "WORKS_ON", "helios", "record", "reports/ecn_0915_014.pdf"),
    ("hana", "WORKS_ON", "helios", "record", "reports/ecn_0915_014.pdf"),
    ("helios", "USES_PART", "shim_b", "record", "reports/ecn_0915_014.pdf"),
    ("shim_b", "SUPERSEDES", "shim_a", "record", "reports/ecn_0915_014.pdf"),
    ("tomas", "WORKS_ON", "northwind", "record", "reports/northwind_design_review.docx"),
    ("bob", "WORKS_ON", "northwind", "record", "reports/northwind_design_review.docx"),
    ("carol", "WORKS_ON", "northwind", "record", "reports/northwind_design_review.docx"),
    # Free text only — the links the laptop's models could not find.
    ("helios", "USES_PART", "brg", "free_text", "reports/helios_status_2026-03.md"),
    ("tomas", "WORKS_ON", "helios", "free_text", "reports/helios_status_2026-03.md"),
    ("atlas", "USES_PART", "shim_b", "free_text", "reports/ecn_0915_014.pdf"),
    ("dan", "WORKS_ON", "atlas", "free_text", "notes/meeting_2026-04-02.txt"),
]

# How a labelled field relates to the others in the same record (a table row,
# a JSON object, or one document's header fields). This is the default the
# graph seeds with; it is written down here so the answer key and the seeder
# agree. role -> (predicate, target type, fallback predicate, fallback type).
LABEL_ROLES = {
    "Program Lead": ("LEADS", "PROJECT", None, None),
    "Point of Contact": ("CONTACT_FOR", "PART", "CONTACT_FOR", "PROJECT"),
    "pointOfContact": ("CONTACT_FOR", "PART", "CONTACT_FOR", "PROJECT"),
    "Owner": ("OWNS", "PART", "LEADS", "PROJECT"),
    "Originator": ("WORKS_ON", "PROJECT", None, None),
    "Approved by": ("WORKS_ON", "PROJECT", None, None),
    "Attendees": ("WORKS_ON", "PROJECT", None, None),
}

# Where some facts are, so locators can be checked exactly.
LOCATIONS = [
    {"file": "trackers/program_tracker.xlsx", "value": "Whitfield, Dana",
     "sheet": "Projects", "row": 2, "column": "Program Lead"},
    {"file": "trackers/parts_list.csv", "value": "PN-0088", "row": 8,
     "column": "P/N"},
    {"file": "reports/ecn_0915_014.pdf", "value": "Lee, Carol", "page": 1,
     "line": 5},
    {"file": "reports/ecn_0915_014.pdf", "value": "PN-1234/A", "page": 2,
     "line": 5},
    {"file": "reports/helios_status_2026-03.md", "value": "Whitfield, Dana",
     "line": 4},
]

# 'D. Whitfield' in the status report is Dana (she owns the shim); a merge
# review must ask, not guess, because Dan Whitfield exists too.
AMBIGUOUS = [{"alias": "D. Whitfield", "candidates": ["dana", "dan"],
              "truth": "dana", "file": "reports/helios_status_2026-03.md"}]


def answer_key() -> Dict[str, Any]:
    """The ground truth, JSON-serialisable."""
    return {
        "corpus": CORPUS_DIR,
        "entities": {k: {"type": t, "name": n, "aliases": list(a)}
                     for k, (t, n, a) in ENTITIES.items()},
        "relations": [{"subject": s, "predicate": p, "object": o,
                       "evidence": ev, "file": f}
                      for s, p, o, ev, f in RELATIONS],
        "field_labels": FIELD_LABELS,
        "drifted_labels": DRIFTED_LABELS,
        "locations": LOCATIONS,
        "ambiguous": AMBIGUOUS,
        "collections": COLLECTIONS,
        "noise_files": ["notes/break_room.txt"],
    }


# ── dependency-free writers ──────────────────────────────────────────────
def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path: Path, sheets: Sequence[tuple]) -> None:
    """A minimal valid .xlsx: ``sheets`` is ``[(name, [[cell, ...], ...])]``,
    every cell written as an inline string (part codes keep leading zeros)."""
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    wb_sheets, wb_rels = [], []
    for n, (name, _rows) in enumerate(sheets, start=1):
        ct.append(f'<Override PartName="/xl/worksheets/sheet{n}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        wb_sheets.append(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>')
        wb_rels.append(f'<Relationship Id="rId{n}" '
                       'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                       f'Target="worksheets/sheet{n}.xml"/>')
    ct.append("</Types>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets>{"".join(wb_sheets)}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'{"".join(wb_rels)}</Relationships>')
        for n, (_name, rows) in enumerate(sheets, start=1):
            xml_rows = []
            for r, row in enumerate(rows, start=1):
                cells = "".join(
                    f'<c r="{_col_letter(c)}{r}" t="inlineStr"><is><t xml:space="preserve">'
                    f'{escape(str(v))}</t></is></c>'
                    for c, v in enumerate(row) if str(v) != "")
                xml_rows.append(f'<row r="{r}">{cells}</row>')
            z.writestr(f"xl/worksheets/sheet{n}.xml",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')


def write_docx(path: Path, paragraphs: Sequence[str]) -> None:
    """A minimal valid .docx, one paragraph per string."""
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>'
                   for p in paragraphs)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f'<w:body>{body}</w:body></w:document>')


def _pdf_str(s: str) -> bytes:
    b = s.encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def write_pdf(path: Path, pages: Sequence[Sequence[str]]) -> None:
    """A minimal valid PDF with a real text layer, one list of lines per page
    (Helvetica, WinAnsi, so 'Tomás' extracts as written)."""
    objs: List[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>")
    for i, lines in enumerate(pages):
        stream = [b"BT /F1 11 Tf 14 TL 56 740 Td"]
        for ln in lines:
            stream.append(b"(" + _pdf_str(ln) + b") Tj T*")
        stream.append(b"ET")
        data = b"\n".join(stream)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n"
                    + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    import csv
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── build ────────────────────────────────────────────────────────────────
def build(vault: Any) -> Dict[str, Any]:
    """Write the Ironbridge documents into ``<vault>/data_in/ironbridge/``
    and its Collections into ``<vault>/collections.json``; return the answer
    key. Refuses (FileExistsError) when the corpus folder already has files
    or collections.json already exists, so it never overwrites anything."""
    vault = Path(vault)
    base = vault / "data_in" / CORPUS_DIR
    coll_path = vault / "collections.json"
    if base.exists() and any(base.iterdir()):
        raise FileExistsError(f"{base} already has files; use an empty vault")
    if coll_path.exists():
        raise FileExistsError(f"{coll_path} exists; use an empty vault")
    for sub in ("trackers", "reports", "notes"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    write_xlsx(base / "trackers" / "program_tracker.xlsx", [
        ("Read Me", [["Ironbridge program tracker — owned by the PMO."],
                     ["Projects are on the next sheet."]]),
        ("Projects", [["Project ID", "Project Name", "Program Lead",
                       "Point of Contact", "Status"], *TRACKER_ROWS]),
    ])
    _write_csv(base / "trackers" / "parts_list.csv",
               ["P/N", "Description", "Rev", "Project", "Owner", "Supersedes"],
               PARTS_ROWS)
    _write_csv(base / "trackers" / "action_items_q2.csv",
               ["Item", "POC", "Project", "Due"], ACTION_ROWS)
    (base / "trackers" / "supplier_contacts.json").write_text(
        json.dumps(SUPPLIER_JSON, indent=2, ensure_ascii=False), encoding="utf-8")
    (base / "reports" / "helios_status_2026-03.md").write_text(
        HELIOS_STATUS_MD, encoding="utf-8")
    write_pdf(base / "reports" / "ecn_0915_014.pdf", ECN_PAGES)
    write_docx(base / "reports" / "northwind_design_review.docx", NORTHWIND_DOCX)
    (base / "notes" / "meeting_2026-04-02.txt").write_text(MEETING_TXT, encoding="utf-8")
    (base / "notes" / "break_room.txt").write_text(UNRELATED_TXT, encoding="utf-8")

    now = 1775000000.0
    coll_path.write_text(json.dumps(
        [{"name": n, "files": fs, "note": "synthetic", "created_ts": now,
          "updated_ts": now} for n, fs in COLLECTIONS.items()],
        indent=2), encoding="utf-8")
    return answer_key()


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m council_core.kg_corpus <empty-folder>")
        return 2
    vault = Path(argv[0]).resolve()
    key = build(vault)
    key_path = vault.parent / f"{vault.name}_answer_key.json"
    if key_path.exists():
        raise FileExistsError(f"{key_path} exists")
    key_path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"corpus: {vault / 'data_in' / CORPUS_DIR}\nanswer key: {key_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
