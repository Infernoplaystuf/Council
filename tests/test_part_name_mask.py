"""
Tests for the user-supplied part-name pattern mask in the Ledger pipeline.

Grammar: ``@`` matches a letter (case-insensitive), ``#`` matches a digit,
anything else matches literally. The mask is tried first in filename and
sheet-name part detection; the existing keyword / lone-number heuristics
remain as the fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_ledger import LedgerError                                 # noqa: E402
from source_extract import (_part_from_filename, _part_from_name,     # noqa: E402
                            mask_to_regex)


# ------------------------------------------------------------- mask_to_regex

def test_a_mask_of_letters_and_digits_matches_that_shape():
    pat = mask_to_regex("@@@#@@###")
    assert pat.search("ABC1DE234") is not None
    assert pat.search("abc1de234") is not None                       # case-insensitive
    assert pat.search("AB1DE234") is None                            # one letter short
    assert pat.search("ABCX1DE234") is None                          # extra letter breaks the shape


def test_a_mask_with_literal_characters_requires_them():
    pat = mask_to_regex("P/N-###")
    assert pat.search("inspection_P/N-101_run3") is not None
    assert pat.search("inspection_PN-101_run3") is None              # missing slash
    assert pat.search("inspection_101_run3") is None                 # missing prefix


def test_the_match_is_bounded_by_non_alphanumeric_characters():
    pat = mask_to_regex("@@@###")
    assert pat.search("file_ABC123.xlsx") is not None
    # Preceded or followed by an extra letter/digit → no match (would widen the shape).
    assert pat.search("fileABC123X.xlsx") is None
    assert pat.search("4ABC123extra") is None


def test_a_mask_run_collapses_to_a_quantified_class():
    # Just a sanity check of the compiled form.
    assert mask_to_regex("@@@").pattern.startswith("(?<![A-Za-z0-9])[A-Za-z]{3}")


# ------------------------------------------------------------- invalid masks

def test_an_empty_mask_raises_a_ledger_error():
    with pytest.raises(LedgerError):
        mask_to_regex("")


def test_a_whitespace_only_mask_raises_a_ledger_error():
    with pytest.raises(LedgerError):
        mask_to_regex("   ")


# ------------------------------------------------------------- _part_from_filename

def test_a_filename_match_with_a_mask_returns_the_matched_text():
    pat = mask_to_regex("@@@#@@###")
    assert _part_from_filename("inspection_ABC1DE234_run3", pat) == "ABC1DE234"


def test_without_a_mask_the_existing_fallbacks_still_apply():
    # Two 2+-digit numbers, no mask → the lone-number fallback refuses to guess.
    assert _part_from_filename("inspection_2041_run30") == ""
    # The keyword fallback still works.
    assert _part_from_filename("Sample_2041") == "2041"


def test_a_digit_only_mask_pulls_the_matching_number():
    pat = mask_to_regex("####")
    # The lone-number fallback would skip this (two 2+-digit numbers); the mask wins.
    assert _part_from_filename("inspection_2041_run30", pat) == "2041"


def test_the_mask_match_wins_over_the_fallbacks():
    # The keyword fallback would pick "123" (Sample_123); the mask grabs
    # the three-letter / three-digit shape instead.
    pat = mask_to_regex("@@@###")
    assert _part_from_filename("Sample_123_ABC456_run", pat) == "ABC456"


# ------------------------------------------------------------- _part_from_name

def test_a_sheet_name_matched_by_the_mask_returns_the_shape():
    pat = mask_to_regex("@@@###")
    assert _part_from_name("Part ABC234", pat) == "ABC234"


def test_a_sheet_named_sheet_1_is_still_ignored_even_with_a_mask():
    pat = mask_to_regex("###")
    assert _part_from_name("Sheet 1", pat) == ""                     # the Sheet-N guard runs first


def test_a_plain_part_number_sheet_name_still_matches_without_a_mask():
    assert _part_from_name("Part 101") == "101"
    assert _part_from_name("101") == "101"


# ------------------------------------------------------------- extract integration

def test_a_truthy_but_whitespace_only_mask_still_raises(tmp_path):
    """mask_to_regex() rejects a whitespace-only string so a user who typed
    spaces does not get silently ignored."""
    with pytest.raises(LedgerError):
        mask_to_regex(" \t ")
