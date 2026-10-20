"""
Tests for the [[wiki-link]] parser.

The parser is pure and does not touch the DB; this PR only wires it to the
rebuild stats counters. A follow-up PR will connect each ref to a mention
on the entity it names.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                                            # noqa: E402

from council_core.knowledge_graph import (WIKILINK_RE, WikiLinkRef,       # noqa: E402
                                          field_link_value, scan_wikilinks,
                                          rebuild_summary_line)


def _lines(*entries):
    """Make a document_lines-shaped sequence from (page, line, text)."""
    return [(e[0], e[1], e[2]) for e in entries]


# ------------------------------------------------------------- the regex

def test_the_regex_matches_a_simple_name():
    assert WIKILINK_RE.search("talked to [[Jordan Blake]] yesterday")


def test_the_regex_is_non_greedy_across_two_refs_on_one_line():
    m = WIKILINK_RE.findall("[[A]] and [[B]]")
    assert m == ["A", "B"]


def test_the_regex_does_not_match_a_single_bracket():
    assert not WIKILINK_RE.search("[Not a wikilink]")
    assert not WIKILINK_RE.search("[[unclosed")


# ------------------------------------------------------------- scan_wikilinks

def test_a_bare_name_becomes_one_ref():
    refs = scan_wikilinks(_lines((None, 1, "see [[Jordan Blake]] for details")))
    assert refs == [WikiLinkRef(page=None, line=1,
                                surface="Jordan Blake",
                                display="Jordan Blake")]


def test_a_pipe_alias_splits_surface_and_display():
    refs = scan_wikilinks(_lines(
        (None, 2, "ask [[Jordan Blake|JB]] about this")))
    assert refs == [WikiLinkRef(page=None, line=2,
                                surface="Jordan Blake", display="JB")]


def test_a_display_only_with_empty_alias_falls_back_to_surface():
    refs = scan_wikilinks(_lines((None, 3, "see [[Jordan Blake|]] today")))
    assert refs[0].display == "Jordan Blake"


def test_multiple_refs_on_one_line_are_all_captured():
    refs = scan_wikilinks(_lines(
        (None, 1, "[[A]] saw [[B]] working with [[C|c]]")))
    assert [r.surface for r in refs] == ["A", "B", "C"]
    assert refs[-1].display == "c"


def test_empty_brackets_are_ignored():
    refs = scan_wikilinks(_lines((None, 1, "[[]] empty"),
                                 (None, 2, "[[   ]] whitespace")))
    assert refs == []


def test_wikilinks_in_a_pdf_carry_the_page_number():
    refs = scan_wikilinks(_lines((3, 7, "cross-reference [[Part 101]]")))
    assert refs == [WikiLinkRef(page=3, line=7,
                                surface="Part 101",
                                display="Part 101")]


def test_lines_without_a_double_bracket_fast_path_do_not_run_regex():
    # Mix of hits and misses to exercise the "if '[[' not in text" early-out.
    refs = scan_wikilinks(_lines(
        (None, 1, "nothing interesting here"),
        (None, 2, "[[Part A1]] followed by prose"),
        (None, 3, "also nothing"),
        (None, 4, "and [[Part B2]]"),
    ))
    assert [r.surface for r in refs] == ["Part A1", "Part B2"]


def test_a_tables_escaped_pipe_is_not_part_of_the_name():
    # Obsidian writes the alias pipe as '\|' inside a markdown table, or the
    # table would read it as a cell break (review, 2026-10-19).
    refs = scan_wikilinks(_lines((None, 1, r"| Owner | [[Priya Raman\|Priya]] |")))
    assert refs == [WikiLinkRef(page=None, line=1,
                                surface="Priya Raman", display="Priya")]


# ------------------------------------------------------- a field's value
# (review, 2026-10-19): 'Part Number: [[PN-1002]]' made a part '[[PN-1002]]'
# beside PN-1002, and 'Owner: [[Jordan Blake]]. Tracked as [[PRJ-0915]].' a
# person of the whole sentence. A field's value is its first link's target.

@pytest.mark.parametrize("value, line, want", [
    ("[[PN-1002]]", "", "PN-1002"),
    ("[[Priya Raman]]", "", "Priya Raman"),
    ('"[[Priya Raman]]"', "", "Priya Raman"),             # YAML front matter
    ("'[[Priya Raman]]'", "", "Priya Raman"),
    ("[[Priya Raman|Priya]]", "", "Priya Raman"),          # not the alias
    ("[[Priya Raman#Contact]]", "", "Priya Raman"),        # not the anchor
    ("[[Priya Raman#^b1|PR]]", "", "Priya Raman"),
    ("[[Jordan Blake]]. Tracked as [[PRJ-0915]]", "", "Jordan Blake"),
    ("Dr. [[Priya Raman]]", "", "Priya Raman"),
    ("![[diagram.png]] and [[Priya Raman]]", "", "Priya Raman"),
    ("Priya Raman", "", "Priya Raman"),                    # no link: as it is
    ("PN-1234/A", "", "PN-1234/A"),
    ("![[diagram.png]]", "", ""),                          # an embed names nothing
    ("[[#Summary]]", "", ""),                              # nor a heading here
    # A link the value splitter cut in two is found whole in its line.
    ("[[Lee", "Attendees: [[Lee, Carol]]", "Lee, Carol"),
    ("Carol]]", "Attendees: [[Lee, Carol]]", "Lee, Carol"),
    ("[[Lee", "Attendees: [[Leeds Upgrade]]; [[Lee, Carol]]", "Lee, Carol"),
    ("Carol]]", "Attendees: [[Ann McCarol]]; [[Lee, Carol]]", "Lee, Carol"),
    ("[[Priya Raman\\", r"| Owner | [[Priya Raman\|Priya]] |", "Priya Raman"),
    ("[[Lee", "", "Lee"),                                  # the line lost: unbracketed
])
def test_a_fields_value_is_its_first_links_target(value, line, want):
    assert field_link_value(value, line) == want


# ------------------------------------------------------- summary line integration

def test_the_summary_line_reports_the_wikilink_count_when_set():
    line = rebuild_summary_line({
        "documents": 10, "workbooks": 3,
        "wikilink_mentions": 7, "wikilink_unique": 4,
    })
    assert "7 [[wiki-link]] mentions found" in line


def test_a_single_wikilink_is_rendered_singular():
    line = rebuild_summary_line({"documents": 1, "wikilink_mentions": 1})
    assert "1 [[wiki-link]] mention found" in line
    assert "1 [[wiki-link]] mentions" not in line


def test_zero_wikilinks_do_not_pollute_the_line():
    line = rebuild_summary_line({"documents": 10, "workbooks": 10,
                                 "wikilink_mentions": 0})
    assert "wiki-link" not in line
