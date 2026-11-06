"""
ledger_ingest — the pipeline as calls that return summaries, and the two
agent tools a person may register.

Synthetic workbooks only (tests/ledger_fixtures.py); images go through a
stand-in reader, so no OCR engine is needed.

Run:  python -m pytest tests/test_ledger_ingest.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from ledger_fixtures import (CONCEALMENT_IMAGE_ROWS, fake_reader, make_laser_log,  # noqa: E402
                             make_nde_book, make_summary, make_template)

from council_core import ledger_ingest  # noqa: E402
from sample_ledger import LedgerError, ParentCSV  # noqa: E402
from tool_registry import RegistryFrozen, ToolRegistry  # noqa: E402


@pytest.fixture
def books(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    make_summary(src / "summary.xlsx")
    (src / "~$laser_log.xlsx").write_bytes(b"lock")       # Excel's lock file
    return make_template(tmp_path / "LMM Template.xlsx"), src, tmp_path


def accept(review, wanted):
    """Mark rows Y in the review, as a person would in Excel."""
    wb = openpyxl.load_workbook(review)
    for row in wb["Values"].iter_rows(min_row=2):
        key = (str(row[1].value or ""), str(row[2].value or ""), row[3].value,
               str(row[4].value))
        if any(all(w is None or w == k for w, k in zip(want, key)) for want in wanted):
            row[0].value = "Y"
    wb.save(review)


def test_parent_is_made_beside_the_template_then_extended(books):
    template, _src, tmp = books
    s = ledger_ingest.create_parent(template)
    assert Path(s.csv) == tmp / "LMM_Template_parent.csv" and s.created
    assert s.items == 9 and s.fields > 10 and s.samples == 0
    again = ledger_ingest.create_parent(template, s.csv)
    assert not again.created and again.csv == s.csv


def test_a_folder_means_its_workbooks_without_lock_files(books):
    _t, src, _tmp = books
    kept, ignored = ledger_ingest.collect_sources([src, src / "summary.xlsx"])
    assert sorted(p.name.lower() for p in kept) == ["laser_log.xlsx", "nde results.xlsx",
                                                   "summary.xlsx"]
    assert ignored == []


def test_extract_then_apply_summaries(books):
    template, src, tmp = books
    csv = ledger_ingest.create_parent(template).csv
    seen = []
    s = ledger_ingest.run_extract(csv, [src], tmp / "review.xlsx",
                                  image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS),
                                  progress=lambda line, frac: seen.append(line))
    assert s.files == 3 and s.images == 1 and s.images_read == 1
    assert s.values == s.pre_accepted + s.to_review and s.pre_accepted > 0 and s.to_review > 0
    assert any(line.startswith("reading workbooks: 3/3 workbooks (100%)") for line in seen)
    assert (tmp / "review.xlsx").exists() and (tmp / "review_images").is_dir()
    assert "values found" in s.describe()

    accept(tmp / "review.xlsx", [("101", "", "Power (W)", "285"),
                                 ("101", None, "Flaw Attributes: Laser Team FOD (um)", None)])
    a = ledger_ingest.run_apply(tmp / "review.xlsx", tmp / "filled",
                                image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS))
    assert a.accepted > 0 and "rows added" in a.csv_result
    assert ParentCSV(csv).get("101", "1A")["Power (W)"] == "285"
    assert sorted(Path(f).name for f in a.filled) == ["LMM_Template_101.xlsx",
                                                      "LMM_Template_102.xlsx"]


def test_no_reader_counts_images_as_not_read(books):
    template, src, tmp = books
    csv = ledger_ingest.create_parent(template).csv
    s = ledger_ingest.run_extract(csv, [src], tmp / "r.xlsx", use_images=False)
    assert s.images == 1 and s.images_read == 0


def test_plain_errors(books):
    template, src, tmp = books
    csv = ledger_ingest.create_parent(template).csv
    with pytest.raises(LedgerError, match="no source workbooks"):
        ledger_ingest.run_extract(csv, [], use_images=False)
    with pytest.raises(LedgerError, match="not found"):
        ledger_ingest.run_extract(csv, [tmp / "nope.xlsx"], use_images=False)
    with pytest.raises(LedgerError, match="parent CSV not found"):
        ledger_ingest.run_extract(tmp / "missing.csv", [src], use_images=False)


# ── agent tools ─────────────────────────────────────────────────────────
@pytest.fixture
def policy(books):
    from safe_agent import AgentPolicy
    template, src, tmp = books
    out = tmp / "agent_out"
    out.mkdir()
    ledger_ingest.create_parent(template, out / "parent.csv")
    return AgentPolicy(allowed_tools=("ingest_extract", "ingest_apply"),
                       file_root=src, output_dir=out)


def test_tools_are_returned_never_registered(policy):
    reg = ToolRegistry()
    tools = ledger_ingest.ledger_tools(policy)
    assert reg.names() == ()                      # building them registered nothing
    for t in tools.values():
        reg.register(t)
    reg.freeze()
    assert set(reg.names()) == {"ingest_extract", "ingest_apply"}
    with pytest.raises(RegistryFrozen):
        reg.register(ledger_ingest.ledger_tools(policy)["ingest_apply"])


def test_tools_extract_and_apply_inside_their_roots(policy):
    tools = ledger_ingest.ledger_tools(policy)
    out = Path(policy.output_dir)
    r = tools["ingest_extract"].fn({"csv": "parent.csv", "sources": ["."]}, policy)
    review = Path(r["review"])
    assert review.parent == out and "values found" in r["summary"]
    accept(review, [("102", "", "Power (W)", "300")])
    r = tools["ingest_apply"].fn({"review": review.name, "fill_dir": "filled"}, policy)
    assert "rows added" in r["summary"]
    assert ParentCSV(out / "parent.csv").get("102", "1A")["Power (W)"] == "300"
    assert (out / "filled").is_dir()


def test_tools_refuse_paths_outside_their_roots(policy, books):
    _t, src, tmp = books
    tools = ledger_ingest.ledger_tools(policy)
    ex, ap = tools["ingest_extract"].fn, tools["ingest_apply"].fn
    with pytest.raises(PermissionError):           # sources only from file_root
        ex({"csv": "parent.csv", "sources": [str(tmp / "LMM Template.xlsx")]}, policy)
    with pytest.raises(PermissionError):
        ex({"csv": "parent.csv", "sources": ["../LMM Template.xlsx"]}, policy)
    with pytest.raises(PermissionError):           # the CSV only in output_dir
        ex({"csv": str(tmp / "elsewhere.csv"), "sources": ["."]}, policy)
    with pytest.raises(PermissionError):
        ap({"review": str(tmp / "review.xlsx")}, policy)

    # a review made by a person for a CSV outside the agent's area
    csv = ledger_ingest.create_parent(tmp / "LMM Template.xlsx", tmp / "people.csv").csv
    out = Path(policy.output_dir)
    ledger_ingest.run_extract(csv, [src], out / "theirs.xlsx", use_images=False)
    with pytest.raises(PermissionError, match="parent CSV"):
        ap({"review": "theirs.xlsx"}, policy)
    r = ex({"csv": "parent.csv", "sources": ["."]}, policy)
    with pytest.raises(PermissionError):
        ap({"review": Path(r["review"]).name, "fill_dir": str(tmp)}, policy)


def test_extract_and_apply_report_progress_in_the_terminal(books, capsys):
    template, src, tmp = books
    csv = ledger_ingest.create_parent(template).csv
    heard = []
    ledger_ingest.run_extract(csv, [src], tmp / "review.xlsx", use_images=False,
                              progress=lambda line, frac: heard.append((line, frac)))
    err = capsys.readouterr().err
    assert "[ledger extract] reading workbooks: 0/3 workbooks (0%)" in err
    assert "[ledger extract] reading workbooks: 3/3 workbooks (100%)" in err
    assert "[ledger extract] done in" in err
    assert any(f == 1.0 for _, f in heard)
    ledger_ingest.run_apply(tmp / "review.xlsx", tmp / "filled", use_images=False)
    err = capsys.readouterr().err
    assert "[ledger apply] reading workbooks: 3/3 workbooks (100%)" in err
    assert "[ledger apply] filling templates: 2/2 parts (100%)" in err
    assert "[ledger apply] done in" in err


def test_a_new_review_never_takes_an_existing_name(tmp_path, monkeypatch):
    """Two runs in one second (Re-run with my answers) wrote the same name."""
    from datetime import datetime as real

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 10, 23, 12, 0, 0)
    monkeypatch.setattr(ledger_ingest, "datetime", Frozen)
    csv = tmp_path / "l.csv"
    first = ledger_ingest.default_review_for(csv)
    first.write_bytes(b"")
    second = ledger_ingest.default_review_for(csv)
    assert (first.name, second.name) == ("review_20261023_120000.xlsx",
                                         "review_20261023_120000_2.xlsx")
