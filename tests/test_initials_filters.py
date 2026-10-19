"""
Expected-vs-actual tests for the initials detector. Each one encodes
real-world column-header text the user has hit and asserts that the KG
does NOT mistake it for an inspector's initials.

A failing test here means a column word is being asked about as a person
in the Connections tab's review queue — the kind of "stupid question"
the 2026-10-19 bug report called out ("Pre" and "Post" being asked).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core.kg_excel import is_initials                        # noqa: E402


# ------------------------------------------------- things that ARE initials

@pytest.mark.parametrize("raw", ["JB", "J.B.", "J B", "JMB", "j.b.",
                                 "AK", "D.J.", "ABW"])
def test_the_known_shapes_do_count_as_initials(raw):
    assert is_initials(raw), raw


def test_a_loose_lowercase_pair_counts_only_in_loose_mode():
    assert is_initials("jb", loose=True)
    assert not is_initials("jb", loose=False)


# -------------------------------------------- stage / phase stop-list

@pytest.mark.parametrize("raw", ["PRE", "POST", "pre", "post",
                                 "Pre", "Post", "MID", "END", "START"])
def test_inspection_stage_words_are_not_initials(raw):
    """User bug 2026-10-19: PRE / POST were being asked as inspector
    initials. These are common inspection-sheet column headers and must
    never reach the initials review path."""
    assert not is_initials(raw), raw


# -------------------------------------------- NDE method stop-list

@pytest.mark.parametrize("raw", ["NDE", "FPI"])
def test_nde_method_short_codes_are_not_initials(raw):
    """Only the method codes that are unambiguously not names. 'EC',
    'VT', 'UT', 'PT', 'MT', 'RT' are deliberately NOT filtered because
    the kg_excel comment notes they can be real inspector initials
    (e.g. Eva Chen signs as EC). The user can add to the stop-list by
    schema in a follow-up if their vault needs it."""
    assert not is_initials(raw), raw


def test_ec_remains_a_valid_initial_pair():
    """Regression guard: Eva Chen signs as EC per the kg_excel
    docstring. The stage-labels fix must not sweep EC away."""
    assert is_initials("EC")


# -------------------------------------------- unit labels

@pytest.mark.parametrize("raw", ["PSI", "KW", "MW", "HZ", "RPM", "IPM",
                                 "DEG", "SEC", "MIN", "MAX", "AVG",
                                 "STD", "PPM"])
def test_unit_labels_are_not_initials(raw):
    """Only units that are overwhelmingly units — 'MM', 'KG', 'LB' are
    excluded because they are two-letter and could be real inspector
    initials. The user who turns up a vault with unit-only two-letter
    column headers can add them to the stop-list in a follow-up."""
    assert not is_initials(raw), raw


# -------------------------------------------- outcome words still rejected

@pytest.mark.parametrize("raw", ["OK", "PASS", "FAIL", "N/A", "TBD",
                                 "YES", "NO", "SEEN", "DONE"])
def test_outcome_cells_remain_filtered(raw):
    """Guards against a regression if the stop-list is reorganized."""
    assert not is_initials(raw), raw


# -------------------------------------------- short capitalized words are names

@pytest.mark.parametrize("raw", ["Bob", "Ann", "Dana", "Kim"])
def test_a_capitalized_short_word_is_still_a_name_not_initials(raw):
    """A short capitalized word like 'Bob' is a name, not initials — see
    is_initials docstring. Guards against a regex refactor eating these."""
    assert not is_initials(raw), raw
