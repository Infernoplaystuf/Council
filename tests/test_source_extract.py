"""
source_extract — source workbooks of other layouts -> review -> parent CSV and
filled templates.

The sources (tests/ledger_fixtures.py) are laid out the ways real ones differ
from the template: a part number written inline, flaws down the side in mm, a
sheet named for the part with codes written 1-A, results transposed, a table
that exists only as a picture, several parts in rows, and a value two sources
disagree on. No OCR engine is needed: images go through a stand-in reader.

Run:  python -m pytest tests/test_source_extract.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from ledger_fixtures import (CONCEALMENT_IMAGE_ROWS, ITEMS, fake_reader,  # noqa: E402
                             make_laser_log, make_nde_book, make_summary, make_template)

from sample_ledger import ParentCSV, read_template  # noqa: E402
from source_extract import (ItemCodes, Matcher, apply_review, boxes_to_rows,  # noqa: E402
                            decide, extract, load_aliases, parse_vision_text,
                            split_unit, tokens, write_review)


@pytest.fixture
def setup(tmp_path):
    schema = read_template(make_template(tmp_path / "tmpl.xlsx"))
    csv = tmp_path / "parent.csv"
    ParentCSV.create(csv, schema)
    sources = [make_laser_log(tmp_path / "laser_log.xlsx"),
               make_nde_book(tmp_path / "NDE results.xlsx"),
               make_summary(tmp_path / "summary.xlsx")]
    return schema, csv, sources, tmp_path


def _run(schema, sources, tmp_path, **kw):
    return extract(schema, sources, image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS),
                   image_dir=tmp_path / "imgs", **kw)


def _by(decisions):
    return {(d.obs.part, d.obs.item, d.obs.field, d.obs.value): d for d in decisions}


# ── pieces ──────────────────────────────────────────────────────────────
def test_labels_normalise():
    assert tokens("Part #") == tokens("P/N") == tokens("Part Number") == ["part", "number"]
    assert tokens("EC Pre") == ["eddy", "current", "pre"]
    assert tokens("Laser Team FOD A (um)") == ["laser", "team", "fod"]
    assert split_unit("Flaw Length (mm)") == ("Flaw Length", "mm")
    assert split_unit("FOD um") == ("FOD", "um")


def test_item_codes():
    codes = ItemCodes(ITEMS)
    for raw in ("1A", "1a", "Flaw 1A", "1-A", "A1", "#1A", "flaw no. 1A"):
        assert codes.canon(raw) == "1A", raw
    assert codes.canon("4A") is None and codes.canon("Power") is None


def test_matcher_keeps_numbered_rows_apart(setup):
    schema = setup[0]
    m = Matcher(schema)
    assert m.match("Inspector 2", per_item=True).target.name == "Visual Inspection: Inspector 2"
    assert m.match("EC Post", ["NDE"], per_item=True).target.name == \
        "NDE Inspection: Eddy Current Response Post-Conceal"
    date = m.match("Date", per_item=False)
    assert date.target.name == "Date" and not date.sure        # generic: review it
    assert m.match("Operator").target is None


def test_ocr_boxes_become_aligned_rows():
    boxes = [(10, 10, 60, 20, "Media"), (110, 11, 180, 21, "Amplitude"),
             (10, 40, 60, 50, "ceramic"), (110, 41, 130, 51, "40"),
             (110, 71, 130, 81, "35")]                     # a blank first cell
    assert boxes_to_rows(boxes) == [["Media", "Amplitude"], ["ceramic", "40"], ["", "35"]]


def test_vision_text_parsing():
    md = "| Media | Amplitude |\n|---|---|\n| ceramic | 40 |"
    assert parse_vision_text(md) == [["Media", "Amplitude"], ["ceramic", "40"]]
    assert parse_vision_text("Power: 285 W\nOperator: JB") == [["Power", "285 W"],
                                                                ["Operator", "JB"]]
    assert parse_vision_text("NONE") is None


# ── a whole extraction ──────────────────────────────────────────────────
def test_every_layout_is_read(setup):
    schema, _, sources, tmp = setup
    dec = _by(decide(_run(schema, sources, tmp), schema))
    # inline "P/N: 101" names the part; a value two cells right of its label
    assert dec[("101", "", "Frequency (kHz)", "50")].accept
    assert ("101", "", "Power (W)", "285") in dec
    # flaws down the side, in mm: converted, with a note
    fod = dec[("101", "2B", "Flaw Attributes: Laser Team FOD (um)", "404")]
    assert fod.obs.note == "converted mm -> um"
    assert dec[("101", "3C", "Flaw Attributes: Flaw Length (in)", "0.0787402")].accept
    # grid with codes written 1-A, on a sheet named "Part 101"
    pre = dec[("101", "2B", "NDE Inspection: Eddy Current Response Pre-Conceal", "64")]
    assert pre.accept and pre.obs.where.endswith("Part 101!G4")
    # transposed visual results
    assert dec[("101", "3C", "Visual Inspection: Inspector 2", "not seen")].accept
    # a table of parts: each row its own part
    assert dec[("102", "", "Power (W)", "300")].accept


def test_disagreeing_sources_are_never_pre_accepted(setup):
    schema, _, sources, tmp = setup
    dec = _by(decide(_run(schema, sources, tmp), schema))
    a, b = dec[("101", "", "Power (W)", "285")], dec[("101", "", "Power (W)", "290")]
    assert not a.accept and not b.accept
    assert a.conflict == "290" and b.conflict == "285"


def test_image_values_are_found_but_left_for_review(setup):
    schema, _, sources, tmp = setup
    run = _run(schema, sources, tmp)
    dec = _by(decide(run, schema))
    media = dec[("101", "", "Concealment 2: Media", "steel")]
    assert media.obs.via == "image" and not media.accept
    assert len(run.images) == 1 and run.images[0].values == 4
    assert Path(run.images[0].saved_as).exists()


def test_no_reader_means_images_are_saved_not_read(setup):
    schema, _, sources, tmp = setup
    run = extract(schema, sources, image_dir=tmp / "imgs", use_images=False)
    assert run.images[0].reader == "none installed"
    assert not any(o.from_image for o in run.obs)


def test_unknown_labels_and_problems(setup):
    schema, _, sources, tmp = setup
    run = _run(schema, sources, tmp)
    assert {o.label for o in run.unmatched} == {"Operator"}
    assert run.problems == []


def test_missing_part_number_is_reported(setup):
    schema, _, _, tmp = setup
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["B1"] = "Power (W)", 280
    wb.save(tmp / "nameless.xlsx")
    run = extract(schema, [tmp / "nameless.xlsx"], use_images=False)
    assert run.obs[0].part == "" and "no Part Number" in run.problems[0]
    run = extract(schema, [tmp / "nameless.xlsx"], use_images=False, part="105")
    assert run.obs[0].part == "105"


# ── review -> apply ─────────────────────────────────────────────────────
def _edit_review(path, accept=(), map_labels=None, ignore=()):
    wb = openpyxl.load_workbook(path)
    ws = wb["Values"]
    for row in ws.iter_rows(min_row=2):
        key = (str(row[1].value or ""), str(row[2].value or ""), row[3].value,
               str(row[4].value))
        for want in accept:
            if all(w is None or w == k for w, k in zip(want, key)):
                row[0].value = "Y"
    lab = wb["Labels to map"]
    for row in lab.iter_rows(min_row=2):
        if map_labels and row[2].value in map_labels:
            row[0].value = map_labels[row[2].value]
        if row[2].value in ignore:
            row[1].value = "Y"
    wb.save(path)


def test_review_then_apply_fills_csv_and_template(setup):
    schema, csv, sources, tmp = setup
    run = _run(schema, sources, tmp)
    review = tmp / "review.xlsx"
    write_review(review, run, decide(run, schema), schema, str(csv))
    ws = openpyxl.load_workbook(review)["Values"]
    assert [c.value for c in ws[1]][:5] == ["Accept", "Part", "Item", "Field", "Value"]

    _edit_review(review,
                 accept=[("101", "", "Power (W)", "285"),            # settle the clash
                         ("101", None, "Flaw Attributes: Laser Team FOD (um)", None),
                         ("101", "", "Concealment 1: Media", None),  # checked the image
                         ("101", "", "Date", None), ("102", "", "Date", None)],
                 ignore=["Operator"])
    res = apply_review(review, fill_dir=tmp / "filled",
                       image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS))

    ledger = ParentCSV(csv)
    assert ledger.sample_ids() == ["101", "102"]
    assert [r["Flaw #"] for r in ledger.rows_for("101")] == ITEMS
    r = ledger.get("101", "2B")
    assert r["Power (W)"] == "285" and r["Frequency (kHz)"] == "50"
    assert r["Flaw Attributes: Laser Team FOD (um)"] == "404"
    assert r["NDE Inspection: Eddy Current Response Pre-Conceal"] == "64"
    assert r["Concealment 1: Media"] == "ceramic"
    assert r["Concealment 2: Media"] == ""                       # not accepted
    assert ledger.get("102", "1A")["Power (W)"] == "300"

    # accepted unsure matches are remembered; the ignored label too
    mapping, ignore = load_aliases(csv)
    assert mapping["fod"] == "Flaw Attributes: Laser Team FOD (um)"
    assert "orientation" not in mapping          # it was a sure match: nothing to learn
    assert "date" not in mapping and "operator" in ignore

    # the filled template has the values in the template's own cells
    assert sorted(Path(p).name for p in res.filled) == ["tmpl_101.xlsx", "tmpl_102.xlsx"]
    out = openpyxl.load_workbook(tmp / "filled" / "tmpl_101.xlsx").active
    fod = schema.field("Flaw Attributes: Laser Team FOD (um)")
    assert out[fod.cells["2B"]].value == 404
    assert out[schema.field("Power (W)").cells[""]].value == 285
    assert str(out["L7"].value).startswith("=")                  # formulas kept


def test_second_file_uses_what_was_learned(setup):
    schema, csv, sources, tmp = setup
    run = _run(schema, sources[:1], tmp)
    review = tmp / "r1.xlsx"
    write_review(review, run, decide(run, schema), schema, str(csv))
    _edit_review(review, accept=[("101", None, "Flaw Attributes: Laser Team FOD (um)", None)])
    apply_review(review, use_images=False)
    # a new part, same layout: FOD is now pre-accepted
    wb = openpyxl.load_workbook(sources[0])
    wb["Build"]["A3"] = "P/N: 103"
    wb.save(tmp / "laser_log_103.xlsx")
    mapping, ignore = load_aliases(csv)
    dec = decide(extract(schema, [tmp / "laser_log_103.xlsx"], mapping, ignore,
                         use_images=False), schema)
    fod = [d for d in dec if d.obs.field == "Flaw Attributes: Laser Team FOD (um)"]
    assert len(fod) == 9 and all(d.accept and d.obs.via == "alias" for d in fod)


def test_reapplying_a_review_changes_nothing(setup):
    schema, csv, sources, tmp = setup
    run = _run(schema, sources, tmp)
    review = tmp / "review.xlsx"
    write_review(review, run, decide(run, schema), schema, str(csv))
    apply_review(review, image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS))
    before = csv.read_text(encoding="utf-8-sig").splitlines()
    res = apply_review(review, image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS))
    assert "0 rows added, 0 updated" in res.csv_result
    after = csv.read_text(encoding="utf-8-sig").splitlines()
    strip = lambda rows: [",".join(r.split(",")[:-1]) for r in rows]   # updated_at
    assert strip(before) == strip(after)


def test_cli_extract_and_apply(setup, capsys):
    from source_extract import main
    schema, csv, sources, tmp = setup
    out = tmp / "cli_review.xlsx"
    assert main(["extract", str(csv), str(sources[0]), "-o", str(out), "--no-images"]) == 0
    assert out.exists() and "pre-accepted" in capsys.readouterr().out
    assert main(["apply", str(out), "--no-images"]) == 0
    assert "rows" in capsys.readouterr().out
    meta = json.loads(openpyxl.load_workbook(out)["_run"]["A1"].value)
    assert meta["csv"] == str(csv)


REAL = Path.home() / "Downloads" / "LMM Template.xlsx"


@pytest.mark.skipif(not REAL.exists(), reason="the LMM template is not on this machine")
def test_against_the_real_template(tmp_path):
    """The synthetic laser log and NDE book, read against the real 30-flaw
    template: only the 9 flaws they hold are filled, in the right fields."""
    schema = read_template(REAL)
    run = extract(schema, [make_laser_log(tmp_path / "l.xlsx"),
                           make_nde_book(tmp_path / "n.xlsx")],
                  image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS))
    dec = _by(decide(run, schema))
    assert dec[("101", "2B", "NDE Inspection: Eddy Current Response Pre-Conceal", "64")].accept
    assert dec[("101", "3C", "Visual Inspection: Inspector 2", "not seen")].accept
    assert ("101", "1A", "Flaw Attributes: Laser Team FOD (um)", "400") in dec


# ── OCR line repair (what RapidOCR really returned on a "Label: value" image) ─
def test_ocr_split_line_is_joined_and_overlap_deduped():
    boxes = [(16, 20, 167, 38, "PartNumber:2041"),
             (16, 54, 129, 73, "LaserPower("), (120, 54, 191, 74, "(W):285"),
             (16, 88, 185, 107, "Spot Size (mm): 0.08")]
    assert boxes_to_rows(boxes) == [["PartNumber:2041"], ["LaserPower(W):285"],
                                    ["Spot Size (mm): 0.08"]]
    assert tokens("PartNumber") == ["part", "number"]


def test_inline_label_without_space():
    from source_extract import _split_inline
    assert _split_inline("PartNumber:2041") == ("PartNumber", "2041")
    assert _split_inline("10:30") == ("10:30", "")
    assert _split_inline(r"C:\data\run.xlsx") == (r"C:\data\run.xlsx", "")


# ── the real OCR engine, when installed ─────────────────────────────────
def _table_png(path, rows, widths, lines=True):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        pytest.skip("no Arial font to draw test images with")
    im = Image.new("RGB", (sum(widths) + 20, 34 * len(rows) + 20), "white")
    d = ImageDraw.Draw(im)
    for r, row in enumerate(rows):
        x = 10
        for c, v in enumerate(row):
            if lines:
                d.rectangle([x, 10 + 34 * r, x + widths[c], 44 + 34 * r], outline=(160,) * 3)
            d.text((x + 6, 17 + 34 * r), v, font=font, fill="black")
            x += widths[c]
    im.save(path)
    return path


def test_real_ocr_reads_pictures_only_workbook(setup):
    """Part number, settings, a run table and flaw values exist only as
    pictures. Read by RapidOCR, every value lands in the right field, a blank
    cell stays blank, "0.030" keeps its zero, and nothing is pre-accepted."""
    pytest.importorskip("rapidocr_onnxruntime")
    from openpyxl.drawing.image import Image as XLImage
    from source_extract import rapidocr_reader
    schema, _, _, tmp = setup
    pics = {
        "A3": _table_png(tmp / "form.png", [["Part Number: 2041"], ["Laser Power (W): 285"]],
                         [300], lines=False),
        "A10": _table_png(tmp / "conceal.png", [["Media", "Amplitude", "Notes"],
                                                ["ceramic", "40", ""], ["steel", "35", "re-run"]],
                          [110, 110, 100]),
        "H3": _table_png(tmp / "flaws.png", [["Flaw", "FOD (um)", "Length (in)"],
                                             ["1A", "412", "0.031"], ["3A", "", "0.030"]],
                         [80, 110, 120]),
    }
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    for anchor, p in pics.items():
        ws.add_image(XLImage(str(p)), anchor)
    wb.save(tmp / "pictures.xlsx")

    run = extract(schema, [tmp / "pictures.xlsx"], image_reader=rapidocr_reader(),
                  image_dir=tmp / "imgs")
    got = {(d.obs.part, d.obs.item, d.obs.field): d.obs.value for d in decide(run, schema)}
    assert got == {
        ("2041", "", "Power (W)"): "285",
        ("2041", "", "Concealment 1: Media"): "ceramic",
        ("2041", "", "Concealment 2: Media"): "steel",
        ("2041", "", "Concealment 1: Amplitude"): "40",
        ("2041", "", "Concealment 2: Amplitude"): "35",
        ("2041", "", "Concealment 2: Notes"): "re-run",
        ("2041", "1A", "Flaw Attributes: Laser Team FOD (um)"): "412",
        ("2041", "1A", "Flaw Attributes: Flaw Length (in)"): "0.031",
        ("2041", "3A", "Flaw Attributes: Flaw Length (in)"): "0.030",
    }
    assert not any(d.accept for d in decide(run, schema))
    assert [n.values for n in run.images] == [1, 5, 3]
