"""
Tests for the Excel-open error classifier. The classifier translates
Workbooks.Open exceptions into named ExcelOpenError kinds so a batch of
extracts can summarize what went wrong instead of surfacing opaque
"An exception was thrown" / xlmain11.chm references.

pywintypes.com_error is not available cross-platform; the tests pass a
plain Exception whose args match com_error's shape:
(hresult, description, excepinfo, argerr) with excepinfo =
(code, source, description, help_file, help_context, scode).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from excel_open import (ExcelOpenError, _com_error_description,       # noqa: E402
                        _com_error_hresult, classify_open_failure)
from council_core.ledger_ingest import summarize_open_failures        # noqa: E402


def _fake_com_error(hresult, description, excepinfo_desc=None,
                    excepinfo_scode=None):
    """Build an exception shaped like pywintypes.com_error."""
    excepinfo = (0, "Microsoft Excel",
                 excepinfo_desc or description,
                 "xlmain11.chm", 0,
                 excepinfo_scode if excepinfo_scode is not None else hresult)
    exc = Exception(hresult, description, excepinfo, 0)
    return exc


# ------------------------------------------------------------- hresult extraction

def test_extracts_hresult_from_the_excepinfo_scode_first(tmp_path):
    """pywintypes.com_error puts the Excel-side code in excepinfo[5]."""
    exc = _fake_com_error(-2146828218, "Call was rejected by callee",
                          excepinfo_desc="file could not be opened",
                          excepinfo_scode=-2146827284)
    assert _com_error_hresult(exc) == -2146827284


def test_falls_back_to_the_outer_hresult_when_no_excepinfo():
    exc = Exception(-2147024891, "Access denied", None, 0)
    assert _com_error_hresult(exc) == -2147024891


def test_description_prefers_excepinfo_text():
    exc = _fake_com_error(-2146827284, "outer text",
                          excepinfo_desc="the file could not be opened")
    assert _com_error_description(exc) == "the file could not be opened"


# ------------------------------------------------------------- the common cases

def test_the_cannot_open_hresult_becomes_the_corrupt_kind(tmp_path):
    p = tmp_path / "weird.xls"
    p.write_bytes(b"<html>...</html>")
    exc = _fake_com_error(-2146827284, "the file could not be opened")
    err = classify_open_failure(exc, p)
    assert isinstance(err, ExcelOpenError)
    assert err.kind == "corrupt"
    msg = str(err)
    assert "weird.xls" in msg
    assert "corrupt" in msg.lower() or "Trust Center" in msg
    # Diagnostic bytes present so a bug report has something to look at.
    assert "size" in msg
    assert "first bytes" in msg or "<unreadable>" in msg


def test_access_denied_becomes_the_access_kind(tmp_path):
    p = tmp_path / "locked.xlsx"
    p.write_bytes(b"PK\x03\x04")
    exc = _fake_com_error(-2147024891, "Access denied")
    err = classify_open_failure(exc, p)
    assert err.kind == "access"
    assert "access denied" in str(err).lower()


def test_a_description_mentioning_password_becomes_the_password_kind(tmp_path):
    p = tmp_path / "p.xlsx"
    p.write_bytes(b"PK\x03\x04")
    exc = Exception(-2146788248, "The password you supplied is not correct.")
    err = classify_open_failure(exc, p)
    assert err.kind == "password"


def test_a_description_mentioning_rights_becomes_the_rights_kind(tmp_path):
    p = tmp_path / "irm.xlsx"
    p.write_bytes(b"PK\x03\x04")
    exc = Exception(-2147024671, "This file is rights-protected and cannot be opened.")
    err = classify_open_failure(exc, p)
    assert err.kind == "rights"


def test_a_timeout_wins_over_everything(tmp_path):
    p = tmp_path / "slow.xlsx"
    p.write_bytes(b"PK\x03\x04")
    exc = _fake_com_error(-2146827284, "the file could not be opened")
    err = classify_open_failure(exc, p, timeout=90, stopped=[True])
    assert err.kind == "timeout"
    assert "90 s" in str(err)


def test_an_unknown_error_falls_back_with_diagnostic_bytes(tmp_path):
    p = tmp_path / "mystery.xlsx"
    p.write_bytes(b"PK\x03\x04\x14\x00\x00\x00")
    exc = _fake_com_error(-123456789, "something strange happened")
    err = classify_open_failure(exc, p)
    assert err.kind == "unknown"
    msg = str(err)
    assert "hresult=-123456789" in msg
    assert "504b030414000000" in msg       # first bytes rendered as hex
    assert "size=" in msg


# ------------------------------------------------------------- summary line

def test_the_summary_counts_each_kind_once():
    text = summarize_open_failures([
        ("a.xlsx", "corrupt"),
        ("b.xlsx", "rights"),
        ("c.xlsx", "corrupt"),
        ("d.xlsx", "corrupt"),
        ("e.xlsx", "password"),
    ])
    assert text.startswith("5 files failed to open")
    assert "3 corrupt / wrong format / blocked" in text
    assert "1 rights-protected (IRM)" in text
    assert "1 password-protected" in text


def test_the_summary_omits_zero_count_kinds():
    text = summarize_open_failures([("a.xlsx", "timeout")])
    assert "timed out" in text
    assert "password" not in text
    assert "rights" not in text


def test_an_empty_batch_summary_is_the_header_only():
    text = summarize_open_failures([])
    assert "0 files failed to open" in text


# ------------------------------------------------------------- Excel's own words (review, 2026-10-19)

def test_a_file_excel_cannot_find_is_not_called_corrupt(tmp_path):
    p = tmp_path / "moved.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    exc = _fake_com_error(-2146827284, "Sorry, we couldn't find moved.xls. Is it "
                                       "possible it was moved, renamed or deleted?")
    err = classify_open_failure(exc, p)
    assert err.kind == "unknown" and "could not find" in str(err)
    assert "couldn't find moved.xls" in str(err)            # Excel's words kept


def test_a_file_in_use_is_the_access_kind(tmp_path):
    p = tmp_path / "busy.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    exc = _fake_com_error(-2146827284, "Microsoft Excel cannot access the file "
                                       "'busy.xls'. The document may be read-only")
    assert classify_open_failure(exc, p).kind == "access"


def test_corrupt_keeps_excels_description(tmp_path):
    p = tmp_path / "weird.xls"
    p.write_bytes(b"<html>")
    exc = _fake_com_error(-2146827284, "The file format and extension don't match")
    err = classify_open_failure(exc, p)
    assert err.kind == "corrupt" and "don't match" in str(err)


# ------------------------------------------------------------- (review, 2026-10-20)

def test_a_file_excel_cannot_find_names_the_path_limit_this_excel_has(tmp_path):
    """The message said Excel accepts 218 characters; Excel 365 here opens
    255 (measured: 256-259 fail)."""
    p = tmp_path / "moved.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    exc = _fake_com_error(-2146827284, "Sorry, we couldn't find moved.xls.")
    msg = str(classify_open_failure(exc, p))
    assert "(255 characters: " in msg and "218" not in msg


def test_only_excel_saying_the_format_is_not_valid_is_for_good(tmp_path):
    """A file Excel says is no workbook ('the file format or file extension
    is not valid') is refused again for the same bytes: for good, so the
    knowledge graph stops starting Excel for it on every rebuild. Its
    catch-all 'Open method of Workbooks class failed' (a path too long gave
    it too) and a Trust Center block (a setting) are not."""
    p = tmp_path / "notes.xlsx"
    p.write_bytes(b"this is not a workbook")
    said = {
        "Excel cannot open the file 'notes.xlsx' because the file format or file extension "
        "is not valid. Verify that the file has not been corrupted and that the file "
        "extension matches the format of the file.": True,
        "Open method of Workbooks class failed": False,
        "You are attempting to open a file type (Excel 4.0 Worksheets) that has been "
        "blocked by your File Block settings in the Trust Center.": False,
    }
    for words, for_good in said.items():
        err = classify_open_failure(_fake_com_error(-2147352567, "Exception occurred.",
                                                    excepinfo_desc=words,
                                                    excepinfo_scode=-2146827284), p)
        assert err.kind == "corrupt" and err.for_good is for_good, words
    assert summarize_open_failures([("a.xls", "path"), ("b.xls", "empty")]) == (
        "2 files failed to open — 1 path too long, 1 empty file (0 bytes)")
