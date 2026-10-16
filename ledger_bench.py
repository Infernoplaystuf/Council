"""
ledger_bench.py — sample source workbooks with known answers, and a score of
how well source_extract reads and merges them.

    python ledger_bench.py make  "LMM Template.xlsx"  OUT_DIR
        -> OUT_DIR/*.xlsx  eleven source workbooks for parts 2041-2048, laid
           out the ways real ones differ, and OUT_DIR/truth.json: every value
           written, with its part, flaw, template field and (for EC
           readings) the inspector who logged it
    python ledger_bench.py score OUT_DIR
        -> per workbook and for all of them together: values found in the
           right field, found under the wrong field, missed, values that are
           not in the workbook at all, and - the one that matters most -
           wrong values that were PRE-ACCEPTED

Nothing here is real data: every number comes from a seeded random generator,
so `make` writes the same workbooks every time.

WHAT THE WORKBOOKS EXERCISE
  2041_build_record      settings as a label/value column under a merged
                         title; flaws down the side with FOD in mm (template:
                         um); abbreviated labels
  2041_EC_stacked        THREE inspectors' EC tables stacked on one sheet,
                         each in the template's A/B/C grid shape, each with
                         its own initials and date, different amounts (all
                         30 flaws / the A block only / Pre-Conceal only)
  2042_EC_side_by_side   two inspectors' tables side by side on one sheet,
                         flaws down the side
  2043_EC_by_sheet       one sheet per inspector, plus an empty summary sheet
  EC_log_2044-2045       a long log: one row per reading (date, inspector,
                         part, flaw, stage, response), two parts, three people
  concealment_runs       runs as rows for four parts
  2045_visual            visual results with flaws as rows, initials above
  2046_filled_template   a filled copy of the template itself (the baseline)
  parts_summary          part settings for every part as rows; 2041's power
                         disagrees with its build record
  2047_pictures          settings and an EC table only as pasted pictures
  2048_messy             starts at G15, numbers typed with units as text,
                         a blank row inside the table, half the flaws, the
                         part number only in the file name, a notes sheet
                         full of numbers that are not values
"""
from __future__ import annotations

import io
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import openpyxl
from openpyxl.styles import Font

from sample_ledger import fill_template, read_template

PARTS = ["2041", "2042", "2043", "2044", "2045", "2046", "2047", "2048"]
BLOCKS = "ABC"
ITEMS = [f"{i}{b}" for b in BLOCKS for i in range(1, 11)]
NDE = "NDE Inspection: "
FA = "Flaw Attributes: "
EC = {"Pre": NDE + "Eddy Current Response Pre-Conceal",
      "Post": NDE + "Eddy Current Response Post-Conceal",
      "Mid": NDE + "Eddy Current Response Mid-Conceal"}
INSPECTORS = ["JB", "MK", "RT"]
BIG = Font(bold=True, size=14)
BOLD = Font(bold=True)


# ── the made-up truth ───────────────────────────────────────────────────
@dataclass
class Truth:
    """Every value written to a workbook: (file, part, item, field, value,
    inspector). item is '' for a part-level value."""
    rows: List[dict] = field(default_factory=list)

    def add(self, file, part, item, fname, value, inspector=""):
        self.rows.append({"file": file, "part": part, "item": item, "field": fname,
                          "value": value, "inspector": inspector})


class Parts:
    """One consistent set of made-up values per part; each workbook writes a
    slice of it, so two workbooks about one part agree unless a test says not."""

    def __init__(self, seed: int = 7):
        r = self.r = random.Random(seed)
        self.p: Dict[str, dict] = {}
        for part in PARTS:
            d: dict = {
                "Frequency (kHz)": r.choice([20, 30, 40, 50, 60]),
                "%pec": r.choice([60, 70, 80, 90]),
                "Power (W)": r.randrange(200, 400, 5),
                "Scan Speed (mm/s)": r.randrange(800, 2000, 50),
                "Jump Speed (mm/s)": r.randrange(2000, 5000, 100),
                "Beam Expander": r.choice(["2x", "3x", "4x"]),
                "Spot Size (mm)": r.choice([0.05, 0.06, 0.08, 0.1]),
                "Date": f"2026-09-{r.randrange(1, 28):02d}",
                "Roughness": round(r.uniform(1.5, 6.0), 1),
            }
            flaws = {}
            for it in ITEMS:
                base = r.randrange(40, 95)
                flaws[it] = {
                    FA + "Laser Team FOD (um)": r.randrange(150, 600, 5),
                    FA + "Flaw Orientation (deg)": r.choice([0, 45, 90, 315]),
                    FA + "Flaw Length (in)": round(r.uniform(0.020, 0.060), 3),
                    NDE + "FPI": r.choice(["Y", "N", "Y"]),
                    "_ec": {ins: {"Pre": base + r.randint(-4, 4),
                                  "Post": max(5, base // 3 + r.randint(-3, 3)),
                                  "Mid": max(5, base // 2 + r.randint(-3, 3))}
                            for ins in INSPECTORS},
                    "_vis": [r.choice(["seen", "seen", "not seen"]) for _ in range(3)],
                }
            d["flaws"] = flaws
            d["runs"] = [{
                "Media": r.choice(["ceramic", "steel", "plastic"]),
                "Media Run Time": r.choice([15, 30, 45, 60]),
                "Media Size": r.choice(["#4", "#6", "#8"]),
                "Tub Status": r.choice(["new", "used"]),
                "Amplitude": r.randrange(25, 60),
                "Amount": r.choice([5, 10, 15]),
                "Notes": r.choice(["", "", "re-run", "checked"]),
            } for _ in range(r.randint(2, 5))]
            self.p[part] = d

    def ec(self, part, item, ins, stage):
        return self.p[part]["flaws"][item]["_ec"][ins][stage]


# ── small writing helpers ───────────────────────────────────────────────
def _title(ws, cell, text, merge_to=None):
    ws[cell] = text
    ws[cell].font = BIG
    if merge_to:
        ws.merge_cells(f"{cell}:{merge_to}")


def _grid(ws, top, left, part, rows, P: Parts, T: Truth, fname, items=ITEMS,
          initials="", date=""):
    """The template's own shape: per block a 'Flaw #' row of codes across,
    then one row per measurement. rows = [(label, field, getter)]."""
    r = top
    for b in BLOCKS:
        codes = [it for it in items if it.endswith(b)]
        if not codes:
            continue
        ws.cell(r, left, "Flaw #").font = BOLD
        for j, it in enumerate(codes):
            ws.cell(r, left + 1 + j, it)
        if initials:
            ws.cell(r, left + 12, "Inspector Initials")
            ws.cell(r, left + 13, "Date")
        for k, (label, fld, get) in enumerate(rows, start=1):
            ws.cell(r + k, left, label)
            for j, it in enumerate(codes):
                v = get(it)
                if v is None:
                    continue
                ws.cell(r + k, left + 1 + j, v)
                T.add(fname, part, it, fld, v, initials)
            if initials:
                ws.cell(r + k, left + 12, initials)
                ws.cell(r + k, left + 13, date)
        r += len(rows) + 2
    return r


def _png(rows, widths, lines=True) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("arial.ttf", 18)
    im = Image.new("RGB", (sum(widths) + 20, 34 * len(rows) + 20), "white")
    d = ImageDraw.Draw(im)
    for i, row in enumerate(rows):
        x = 10
        for c, v in enumerate(row):
            if lines:
                d.rectangle([x, 10 + 34 * i, x + widths[c], 44 + 34 * i], outline=(150,) * 3)
            d.text((x + 6, 17 + 34 * i), str(v), font=font, fill="black")
            x += widths[c]
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ── the workbooks ───────────────────────────────────────────────────────
def build_record(out: Path, P: Parts, T: Truth, part="2041"):
    f = f"{part}_build_record.xlsx"
    d = P.p[part]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    _title(ws, "B1", f"Build record — Part {part}", "H1")
    labels = [("Freq (kHz)", "Frequency (kHz)"), ("% PEC", "%pec"),
              ("Laser Power [W]", "Power (W)"), ("Scan Spd (mm/s)", "Scan Speed (mm/s)"),
              ("Jump Spd (mm/s)", "Jump Speed (mm/s)"), ("Beam Exp.", "Beam Expander"),
              ("Spot (mm)", "Spot Size (mm)"), ("Build Date", "Date")]
    for i, (lab, fld) in enumerate(labels, start=4):
        ws.cell(i, 2, lab)
        ws.cell(i, 3, d[fld])
        T.add(f, part, "", fld, d[fld])
    ws.cell(3, 5, "Flaws").font = BOLD
    heads = ["Flaw ID", "FOD (mm)", "Orient. (deg)", "Length (in)", "# Passes"]
    for j, h in enumerate(heads):
        ws.cell(4, 5 + j, h).font = BOLD
    for i, it in enumerate(ITEMS, start=5):
        fl = d["flaws"][it]
        fod_um = fl[FA + "Laser Team FOD (um)"]
        vals = [it, fod_um / 1000, fl[FA + "Flaw Orientation (deg)"],
                fl[FA + "Flaw Length (in)"], 300]
        for j, v in enumerate(vals):
            ws.cell(i, 5 + j, v)
        T.add(f, part, it, FA + "Laser Team FOD (um)", fod_um)
        T.add(f, part, it, FA + "Flaw Orientation (deg)", fl[FA + "Flaw Orientation (deg)"])
        T.add(f, part, it, FA + "Flaw Length (in)", fl[FA + "Flaw Length (in)"])
        T.add(f, part, it, FA + "Passes", 300)
    wb.save(out / f)


def ec_stacked(out: Path, P: Parts, T: Truth, part="2041"):
    """Three people's tables on one sheet, each in the template's grid shape,
    each a different amount."""
    f = f"{part}_EC_stacked.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC"
    _title(ws, "A1", f"Eddy current — Part {part}")
    plan = [("JB", "2026-09-12", ITEMS, ["Pre", "Post"]),
            ("MK", "2026-09-13", [it for it in ITEMS if it.endswith("A")], ["Pre", "Post"]),
            ("RT", "2026-09-15", ITEMS, ["Pre"])]
    r = 3
    for ins, date, items, stages in plan:
        ws.cell(r, 1, f"Inspector: {ins}").font = BOLD
        ws.cell(r, 4, f"Date: {date}")
        rows = [(f"EC Response {s}-Conceal", EC[s],
                 (lambda it, s=s, ins=ins: P.ec(part, it, ins, s))) for s in stages]
        r = _grid(ws, r + 1, 2, part, rows, P, T, f, items, ins, date) + 2
    wb.save(out / f)


def ec_side_by_side(out: Path, P: Parts, T: Truth, part="2042"):
    f = f"{part}_EC_side_by_side.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Part {part} EC"
    for k, ins in enumerate(["JB", "MK"]):
        left = 2 + k * 6
        ws.cell(2, left, f"{ins} — {('2026-09-18', '2026-09-19')[k]}").font = BOLD
        for j, h in enumerate(["Flaw", "EC Pre", "EC Post", "EC Mid"]):
            ws.cell(3, left + j, h).font = BOLD
        for i, it in enumerate(ITEMS, start=4):
            ws.cell(i, left, it)
            for j, s in enumerate(["Pre", "Post", "Mid"], start=1):
                v = P.ec(part, it, ins, s)
                ws.cell(i, left + j, v)
                T.add(f, part, it, EC[s], v, ins)
    wb.save(out / f)


def ec_by_sheet(out: Path, P: Parts, T: Truth, part="2043"):
    f = f"{part}_EC_by_sheet.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Summary"
    wb.active["A1"] = f"Part {part} — eddy current, see each inspector's sheet"
    for ins, date in [("JB", "9-20"), ("MK", "9-21"), ("RT", "9-21")]:
        ws = wb.create_sheet(f"{ins} {date}")
        ws["A1"], ws["B1"] = "Part Number", int(part)
        ws["A2"], ws["B2"] = "Inspector", ins
        ws.append([])
        ws.append(["Flaw #", "Eddy Current Pre-Conceal (%FSH)", "Eddy Current Post-Conceal (%FSH)"])
        for it in ITEMS:
            pre, post = P.ec(part, it, ins, "Pre"), P.ec(part, it, ins, "Post")
            ws.append([it, pre, post])
            T.add(f, part, it, EC["Pre"], pre, ins)
            T.add(f, part, it, EC["Post"], post, ins)
    wb.save(out / f)


def ec_long_log(out: Path, P: Parts, T: Truth, parts=("2044", "2045")):
    f = "EC_log_2044-2045.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Log"
    ws.append(["Date", "Inspector", "Part No.", "Flaw", "Stage", "Response"])
    for part in parts:
        for ins in INSPECTORS[:2] if part == "2044" else INSPECTORS:
            for it in ITEMS[:20]:
                for s in ["Pre", "Post"]:
                    v = P.ec(part, it, ins, s)
                    ws.append(["2026-09-25", ins, int(part), it, f"{s}-Conceal", v])
                    T.add(f, part, it, EC[s], v, ins)
    wb.save(out / f)


def concealment(out: Path, P: Parts, T: Truth, parts=("2041", "2042", "2043", "2044")):
    f = "concealment_runs.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Runs"
    cols = [("Part", None), ("Run #", None), ("Media", "Media"),
            ("Run Time (min)", "Media Run Time"), ("Media Size", "Media Size"),
            ("Tub", "Tub Status"), ("Amplitude", "Amplitude"), ("Amount", "Amount"),
            ("Notes", "Notes")]
    ws.append([c for c, _ in cols])
    for part in parts:
        for n, run in enumerate(P.p[part]["runs"], start=1):
            row = [int(part), n]
            for _c, key in cols[2:]:
                row.append(run[key])
                if run[key] != "":
                    T.add(f, part, "", f"Concealment {n}: {key}", run[key])
            ws.append(row)
    wb.save(out / f)


def visual(out: Path, P: Parts, T: Truth, part="2045"):
    f = f"{part}_visual.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Visual"
    ws["A1"] = f"Visual inspection, part {part}"
    ws["A3"] = "Initials"
    for j, ini in enumerate(["AB", "CD", "EF"]):
        ws.cell(3, 2 + j, ini)
    ws.append(["Flaw", "Inspector 1", "Inspector 2", "Inspector 3"])
    for it in ITEMS:
        vis = P.p[part]["flaws"][it]["_vis"]
        ws.append([it] + vis)
        for k, v in enumerate(vis, start=1):
            T.add(f, part, it, f"Visual Inspection: Inspector {k}", v)
    wb.save(out / f)


def filled_template(out: Path, P: Parts, T: Truth, schema, part="2046"):
    f = f"{part}_filled_template.xlsx"
    d = P.p[part]
    base = {schema.id_field: part}
    for fld in ["Frequency (kHz)", "%pec", "Power (W)", "Scan Speed (mm/s)",
                "Jump Speed (mm/s)", "Beam Expander", "Spot Size (mm)", "Date", "Roughness"]:
        base[fld] = d[fld]
        T.add(f, part, "", fld, d[fld])
    for n, run in enumerate(d["runs"], start=1):
        for key in ("Media", "Media Run Time", "Amplitude"):
            base[f"Concealment {n}: {key}"] = run[key]
            T.add(f, part, "", f"Concealment {n}: {key}", run[key])
    rows = []
    for it in ITEMS:
        fl = d["flaws"][it]
        row = dict(base)
        row["Flaw #"] = it
        for fld in (FA + "Laser Team FOD (um)", FA + "Flaw Orientation (deg)",
                    FA + "Flaw Length (in)", NDE + "FPI"):
            row[fld] = fl[fld]
            T.add(f, part, it, fld, fl[fld])
        for s in ("Pre", "Post"):
            row[EC[s]] = P.ec(part, it, "JB", s)
            row[EC[s] + " — Inspector Initials"] = "JB"
            T.add(f, part, it, EC[s], row[EC[s]], "JB")
        rows.append(row)
    fill_template(schema, rows, out / f)
    # the template's own pre-filled values stay where nothing was written
    written = set(rows[0])
    for fld in schema.fields:
        if fld.name in written:
            continue
        for it, v in (fld.defaults or {}).items():
            if v not in (None, ""):
                T.add(f, part, it, fld.name, v)


def summary(out: Path, P: Parts, T: Truth):
    f = "parts_summary.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Part Number", "Frequency (kHz)", "Power (W)", "Scan Speed (mm/s)", "Date"])
    for part in PARTS:
        d = P.p[part]
        power = d["Power (W)"] + (5 if part == "2041" else 0)      # the clash
        ws.append([int(part), d["Frequency (kHz)"], power, d["Scan Speed (mm/s)"], d["Date"]])
        T.add(f, part, "", "Frequency (kHz)", d["Frequency (kHz)"])
        T.add(f, part, "", "Power (W)", power)
        T.add(f, part, "", "Scan Speed (mm/s)", d["Scan Speed (mm/s)"])
        T.add(f, part, "", "Date", d["Date"])
    wb.save(out / f)


def pictures(out: Path, P: Parts, T: Truth, part="2047"):
    from openpyxl.drawing.image import Image as XLImage
    f = f"{part}_pictures.xlsx"
    d = P.p[part]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["A1"] = "Inspection report (screenshots)"
    form = [[f"Part Number: {part}"], [f"Laser Power (W): {d['Power (W)']}"],
            [f"Spot Size (mm): {d['Spot Size (mm)']}"]]
    T.add(f, part, "", "Power (W)", d["Power (W)"])
    T.add(f, part, "", "Spot Size (mm)", d["Spot Size (mm)"])
    items = ITEMS[:6]
    table = [["Flaw", "EC Pre", "EC Post"]]
    for it in items:
        pre, post = P.ec(part, it, "MK", "Pre"), P.ec(part, it, "MK", "Post")
        table.append([it, pre, post])
        T.add(f, part, it, EC["Pre"], pre)          # no initials in the picture
        T.add(f, part, it, EC["Post"], post)
    ws.add_image(XLImage(io.BytesIO(_png(form, [320], lines=False))), "A3")
    ws.add_image(XLImage(io.BytesIO(_png(table, [80, 100, 100]))), "A12")
    wb.save(out / f)


def messy(out: Path, P: Parts, T: Truth, part="2048"):
    f = f"line3_{part}.xlsx"                 # the part number only in the name
    d = P.p[part]
    items = ITEMS[:5] + ITEMS[10:13]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["G13"] = "flaw data (typed by hand)"
    ws.merge_cells("G13:K13")
    heads = ["Flaw", "FOD", "Orientation", "Length", "EC pre"]
    for j, h in enumerate(heads):
        ws.cell(15, 7 + j, h).font = BOLD
    r = 16
    for k, it in enumerate(items):
        if k == 4:
            r += 1                          # a blank row inside the table
        fl = d["flaws"][it]
        fod = fl[FA + "Laser Team FOD (um)"]
        pre = P.ec(part, it, "RT", "Pre")
        ws.cell(r, 7, it)
        ws.cell(r, 8, f"{fod / 1000:.3f} mm")
        ws.cell(r, 9, f"{fl[FA + 'Flaw Orientation (deg)']}°")
        ws.cell(r, 10, f'{fl[FA + "Flaw Length (in)"]}"')
        ws.cell(r, 11, str(pre))
        T.add(f, part, it, FA + "Laser Team FOD (um)", fod)
        T.add(f, part, it, FA + "Flaw Orientation (deg)", fl[FA + "Flaw Orientation (deg)"])
        T.add(f, part, it, FA + "Flaw Length (in)", fl[FA + "Flaw Length (in)"])
        T.add(f, part, it, EC["Pre"], pre)          # nobody's initials on the sheet
        r += 1
    notes = wb.create_sheet("Notes")
    for line in ["Re-ran 3 on 9/14 because amplitude read 45 not 40",
                 "Ask JB about flaw 2B - looked like 0.6 mm",
                 "Tub 2 out of service until 10/1",
                 "Power was dropped to 250 W for the last 10 flaws? check"]:
        notes.append([line])
    wb.save(out / f)


BUILDERS = [build_record, ec_stacked, ec_side_by_side, ec_by_sheet, ec_long_log,
            concealment, visual, summary, pictures, messy]


def make(template, out_dir) -> Truth:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    schema = read_template(template)
    P, T = Parts(), Truth()
    for b in BUILDERS:
        b(out, P, T)
    filled_template(out, P, T, schema)
    for t in T.rows:
        t["value"] = _norm(t["value"])
    (out / "truth.json").write_text(json.dumps({"template": str(Path(template).resolve()),
                                                "rows": T.rows}, indent=1), encoding="utf-8")
    return T


# ── scoring ─────────────────────────────────────────────────────────────
def _norm(v) -> str:
    s = str(v).strip()
    try:
        x = float(s)
    except ValueError:
        return s.casefold()
    return f"{x:.6g}"


@dataclass
class Score:
    name: str
    truth: int = 0
    right: int = 0
    wrong_field: int = 0
    missed: int = 0
    extra: int = 0
    pre_ok: int = 0
    pre_wrong: int = 0
    multi_cells: int = 0          # cells several inspectors logged
    multi_all_found: int = 0      # ... where every inspector's value was found
    disagree: int = 0             # ... where they logged different values
    disagree_pre: int = 0         # ... and one of them was pre-accepted anyway
    signed: int = 0               # readings with initials: each in its own column
    signed_found: int = 0         # ... found there, under the right initials
    examples: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))

    def line(self) -> str:
        pct = 100 * self.right / self.truth if self.truth else 0
        s = (f"{self.name:30} {self.truth:5} {self.right:5} {pct:5.0f}% {self.wrong_field:5} "
             f"{self.missed:6} {self.extra:5} {self.pre_ok:6} {self.pre_wrong:7}")
        if self.signed:
            s += f"   by initials {self.signed_found}/{self.signed}"
        if self.multi_cells:
            s += (f"   inspectors: {self.multi_all_found}/{self.multi_cells} cells all found;"
                  f" {self.disagree_pre} of {self.disagree} disagreeing pre-accepted")
        return s


HEAD = (f"{'workbook':30} {'truth':>5} {'right':>5} {'':>6} {'wrong':>5} {'missed':>6} "
        f"{'extra':>5} {'pre-ok':>6} {'PRE-BAD':>7}")


def score(out_dir, files: Optional[Sequence[str]] = None, image_reader=None,
          use_images=True) -> List[Score]:
    from sample_ledger import TemplateSchema  # noqa: F401  (schema via read_template)
    from source_extract import decide, extract
    out = Path(out_dir)
    data = json.loads((out / "truth.json").read_text(encoding="utf-8"))
    schema = read_template(data["template"])
    truth = data["rows"]
    names = list(files) if files else sorted({t["file"] for t in truth})
    scores = []
    for name in names + ["ALL (merged)"]:
        group = names if name.startswith("ALL") else [name]
        rows = [t for t in truth if t["file"] in group]
        run = extract(schema, [out / g for g in group], image_reader=image_reader,
                      use_images=use_images)
        decisions = decide(run, schema)
        scores.append(_score(name, rows, decisions))
    return scores


def _score(name, rows, decisions) -> Score:
    sc = Score(name)
    from sample_ledger import inspector_column
    want: Dict[Tuple[str, str, str], Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    own = set()
    for t in rows:
        want[(t["part"], t["item"], t["field"])][t["value"]].add(t["inspector"])
        if t["inspector"]:
            key = (t["part"], t["item"], inspector_column(t["field"], t["inspector"]))
            want[key][t["value"]].add(t["inspector"])
            own.add(key)
    got = defaultdict(set)
    for d in decisions:
        o = d.obs
        key = (o.part, o.item, o.field)
        v = _norm(o.value)
        got[key].add(v)
        if v in want.get(key, {}):
            if d.accept:
                sc.pre_ok += 1
        else:
            sc.extra += 1
            if d.accept:
                sc.pre_wrong += 1
                sc.examples["PRE-ACCEPTED WRONG"].append(f"{key} = {o.value!r} ({o.where})")
            elif len(sc.examples["extra"]) < 400:
                sc.examples["extra"].append(f"{key} = {o.value!r} ({o.where}, {o.label!r})")
    by_part_item = defaultdict(set)
    for (p, it, _f), vals in got.items():
        by_part_item[(p, it)] |= vals
    for key, values in want.items():
        for v in values:
            sc.truth += 1
            if v in got.get(key, ()):
                sc.right += 1
            elif v in by_part_item.get(key[:2], ()):
                sc.wrong_field += 1
                sc.examples["wrong field"].append(f"{key} = {v}")
            else:
                sc.missed += 1
                sc.examples["missed"].append(f"{key} = {v}")
        if key in own:
            sc.signed += len(values)
            sc.signed_found += sum(v in got.get(key, ()) for v in values)
            continue
        inspectors = set().union(*values.values()) - {""}
        if len(inspectors) > 1:
            sc.multi_cells += 1
            sc.multi_all_found += all(v in got.get(key, ()) for v in values)
            if len(values) > 1:
                sc.disagree += 1
                pre = [d for d in decisions if d.accept and (d.obs.part, d.obs.item,
                                                             d.obs.field) == key]
                if pre:
                    sc.disagree_pre += 1
                    sc.examples["inspectors disagree, one pre-accepted"].append(
                        f"{key}: {dict((v, sorted(i)) for v, i in values.items())} "
                        f"-> {pre[0].obs.value!r}")
    return sc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "make":
        T = make(argv[1], argv[2])
        print(f"{len(T.rows)} values in {len({t['file'] for t in T.rows})} workbooks -> {argv[2]}")
        return 0
    if len(argv) >= 2 and argv[0] == "score":
        scores = score(argv[1], use_images="--no-images" not in argv)
        print(HEAD)
        for s in scores:
            print(s.line())
        detail = Path(argv[1]) / "score_detail.txt"
        with detail.open("w", encoding="utf-8") as fh:
            for s in scores:
                fh.write(f"\n=== {s.name}\n")
                for kind, ex in s.examples.items():
                    fh.write(f"-- {kind} ({len(ex)})\n" + "".join(f"   {e}\n" for e in ex[:60]))
        print(f"\ndetails: {detail}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
