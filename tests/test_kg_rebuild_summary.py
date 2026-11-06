"""
Tests for the Connections / knowledge-graph rebuild summary helpers.

The user reported "243 files read, 2 workbooks, no connections, no errors"
— the UI line was `"Rebuilt from N document(s) in X s."` which told them
nothing about why 241 files produced no entities. These helpers turn the
``kg.seed()`` stats dict into a human line that includes the per-kind
breakdown and the categorized unreadable list.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core.knowledge_graph import (_categorize_unreadable_reason,  # noqa: E402
                                          rebuild_summary_line,
                                          unreadable_breakdown)


# ------------------------------------------------------------- categorization

def test_each_known_reason_maps_to_a_short_label():
    cases = {
        "needs the pypdf package, which is not installed": "needs pypdf",
        "needs openpyxl, which is not installed": "needs openpyxl",
        "has a password to open; open it in Excel …": "password-protected",
        "is rights-protected (IRM / sensitivity label); open it …": "rights-protected",
        "Excel refused to open the file; it may be corrupt …": "corrupt / wrong format",
        "access denied by the operating system; the file may be locked": "access denied",
        "Excel automation is not available here (needs Windows, Excel …)": "no Excel COM",
        "Excel did not open file.xlsx within 90 s and was stopped": "Excel timeout",
        "could not be found by Excel when it tried to open it": "not found",
    }
    for reason, label in cases.items():
        assert _categorize_unreadable_reason(reason) == label, reason


def test_an_uncategorized_reason_falls_back_to_other():
    assert _categorize_unreadable_reason("something strange") == "other"
    assert _categorize_unreadable_reason("") == "other"


def test_the_excel_readers_newer_reasons_have_labels_of_their_own(tmp_path):
    """A path too long for Excel or for Windows, an empty file, an Excel
    refusing every call (or never starting) and an Excel stopping in the
    middle of a read were all rolled up as 'other' - and a valid .xls 256-259
    characters deep had at least been 'corrupt / wrong format' (review,
    2026-10-20). The reasons as the readers word them."""
    import excel_open

    def said(path):
        with pytest.raises(excel_open.ExcelOpenError) as e:
            excel_open.read_with_excel(path, want_pictures=False,
                                       app_factory=lambda: pytest.fail("Excel started"))
        return str(e.value)
    base = tmp_path.resolve()
    deep = base / ("p" * (258 - len(str(base)) - 1 - 4) + ".xls")
    empty = base / "e.xls"
    empty.write_bytes(b"")
    cases = {
        said(deep): "path too long",
        ("long.xlsx: path too long: 270 characters — enable Windows long paths or move "
         "the folder"): "path too long",                             # source_extract
        said(empty): "empty file",
        str(excel_open._excel_down("Call was rejected by callee", True)): "no Excel COM",
        str(excel_open._excel_down("Server execution failed", False)): "no Excel COM",
        ("a.xls: Excel stopped while the workbook was being read (The RPC server is "
         "unavailable) — it may have closed or crashed on it"): "Excel stopped",
        ("Excel stopped while it was being read (AttributeError: "
         "<unknown>.Shapes)"): "Excel stopped",                       # kg_excel's own
    }
    assert "path too long for Excel: 258 characters" in said(deep)
    for reason, label in cases.items():
        assert _categorize_unreadable_reason(reason) == label, reason
        assert unreadable_breakdown([f"data_in/x.xls: {reason}"]) == {label: 1}


# ------------------------------------------------------------- breakdown

def test_the_breakdown_counts_by_category():
    entries = [
        "data_in/a.pdf: needs the pypdf package, which is not installed",
        "data_in/b.pdf: needs the pypdf package, which is not installed",
        "data_in/c.xlsx: Excel refused to open the file; corrupt",
        "data_in/d.xlsx: is rights-protected (IRM); open it first",
        "e.xlsx: something strange",
    ]
    counts = unreadable_breakdown(entries)
    assert counts["needs pypdf"] == 2
    assert counts["corrupt / wrong format"] == 1
    assert counts["rights-protected"] == 1
    assert counts["other"] == 1


def test_an_empty_list_breaks_down_to_an_empty_dict():
    assert unreadable_breakdown([]) == {}


def test_an_entry_without_a_colon_is_still_categorized_by_its_text():
    # Some fallbacks produce a bare reason without a leading rel path.
    assert unreadable_breakdown(
        ["needs the pypdf package, which is not installed"]
    ) == {"needs pypdf": 1}


# ------------------------------------------------------------- summary line

def _stats(**over):
    s = {"documents": 0, "documents_read": 0, "documents_unchanged": 0,
         "ledgers": 0, "workbooks": 0, "documents_other": 0,
         "unreadable": [], "unreadable_reasons": {},
         "relations": {"seeded": 0, "accepted": 0, "suggested": 0},
         "seconds": 0.0}
    s.update(over)
    return s


def test_the_summary_lists_each_non_zero_kind():
    stats = _stats(documents=200, workbooks=2, ledgers=0,
                   documents_other=198, seconds=12.3)
    line = rebuild_summary_line(stats)
    assert "Rebuilt from 200 document(s) in 12.3s" in line
    assert "2 workbooks" in line
    assert "198 other docs" in line
    assert "ledger" not in line                      # zero-count kinds hidden


def test_the_summary_includes_unreadable_breakdown_sorted_by_count():
    stats = _stats(
        documents=243, workbooks=2,
        unreadable=["x.pdf: needs the pypdf package"] * 240,
        unreadable_reasons={"needs pypdf": 240},
    )
    line = rebuild_summary_line(stats)
    assert "240 unreadable" in line
    assert "240 needs pypdf" in line


def test_the_summary_says_zero_links_when_nothing_was_seeded():
    stats = _stats(documents=10, workbooks=10)
    line = rebuild_summary_line(stats)
    assert "0 links found" in line


def test_the_summary_includes_seeded_and_suggested_counts():
    # Updated on purpose (review, 2026-10-19): the suggested links are added
    # on ('5 links (7 suggested)' read as 7 of the 5).
    stats = _stats(documents=10, workbooks=10,
                   relations={"seeded": 3, "accepted": 2, "suggested": 7})
    line = rebuild_summary_line(stats)
    assert "; 5 links + 7 suggested" in line


def test_the_suggested_links_are_added_on_not_part_of_the_links():
    """20 seeded + 5 suggested said '20 links (5 suggested)': it read as 5
    of the 20. 'Links' are the seeded and accepted ones, as the coverage
    line counts them ('20 links, 5 waiting for you') - a total with the
    suggested ones in it put a second link count on screen (review,
    2026-10-19)."""
    line = rebuild_summary_line(_stats(documents=3, relations={"seeded": 20,
                                                                "suggested": 5}))
    assert "; 20 links + 5 suggested" in line
    assert "(5 suggested)" not in line and "25 links" not in line
    line = rebuild_summary_line(_stats(documents=3, relations={"seeded": 20}))
    assert line.endswith("; 20 links.")
    line = rebuild_summary_line(_stats(documents=3, relations={"suggested": 4}))
    assert "; 0 links + 4 suggested" in line
    line = rebuild_summary_line(_stats(documents=3, relations={"seeded": 1,
                                                                "suggested": 1}))
    assert "; 1 link + 1 suggested" in line


def test_an_unchanged_rebuild_says_so_and_claims_no_new_entities():
    """An unchanged rebuild carries the last run's wiki-link counters: it
    said '(23 matched existing, 18 new entities)' and nothing said the
    graph was the same (review, 2026-10-19)."""
    stats = _stats(documents=21, documents_other=21, graph_unchanged=True,
                   wikilink_mentions=41, wikilinks_matched=23, wikilinks_created=18,
                   relations={"seeded": 20, "suggested": 5})
    line = rebuild_summary_line(stats)
    assert "nothing changed since the last rebuild" in line
    assert "new entit" not in line and "matched existing" not in line
    assert "; 20 links + 5 suggested" in line               # the graph as it stands
    # a rebuild that worked the graph out again still says what it found
    stats["graph_unchanged"] = False
    line = rebuild_summary_line(stats)
    assert "nothing changed" not in line
    assert "(23 matched existing, 18 new entities)" in line


def test_a_rebuild_with_only_other_docs_does_not_claim_workbooks():
    stats = _stats(documents=5, documents_other=5)
    line = rebuild_summary_line(stats)
    assert "5 other docs" in line
    assert "workbook" not in line


def test_the_two_from_two_hundred_fourty_three_shape_prints_cleanly():
    """The exact shape of the user's bug report (243 / 2 / 0 connections)."""
    stats = _stats(
        documents=243, workbooks=2, documents_other=1,
        unreadable=["x: needs the pypdf package"] * 240,
        unreadable_reasons={"needs pypdf": 240},
    )
    line = rebuild_summary_line(stats)
    assert "Rebuilt from 243 document(s)" in line
    assert "2 workbooks" in line
    assert "240 unreadable" in line
    assert "needs pypdf" in line
    assert "0 links found" in line


def test_the_readers_own_reasons_land_in_their_categories():
    """Real strings from kg_excel (review, 2026-10-19)."""
    from council_core.knowledge_graph import unreadable_breakdown
    got = unreadable_breakdown([
        "a.xlsb: a .xlsb workbook opens only through Microsoft Excel (with the "
        "pywin32 package), which is not available here",
        "b.xlsx: is not a workbook openpyxl can read (and Excel, which might, is "
        "not available here)",
        "c.xlsx: needs the openpyxl package, which is not installed"])
    assert got == {"no Excel COM": 1, "corrupt / wrong format": 1, "needs openpyxl": 1}
