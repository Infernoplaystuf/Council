"""
A text note's encoding: an ANSI (Windows-1252, Notepad's default for years)
file is read as Windows-1252, not as UTF-8 with replacement characters.

The bug (review, 2026-10-19): field_search._read_text decoded every text file
as UTF-8 with errors='replace', so an ANSI note's 'Tomás Echeverría' read as
'Tom\\ufffds Echeverr\\ufffda' and the knowledge graph made a second, garbled
person of him beside the UTF-8 note's. The JSON reader did the same, and a
JSON file with a BOM gave no records at all.

Then the first fix read a file all one way or the other (review,
2026-10-19): a UTF-8 note or log with a line added by an ANSI tool kept
U+FFFD in that line, or turned its UTF-8 accents to mojibake. Now each byte
UTF-8 cannot read is read as cp1252 and the rest stays UTF-8.

Every vault is a tmp_path; no model, no network.
"""
from __future__ import annotations

import codecs
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import field_search as fs                                               # noqa: E402
from council_core import knowledge_graph as kgm                        # noqa: E402

NAME = "Tomás Echeverría"
NOTE = f"Project: Atlas\nAttendees: {NAME}\nSee [[{NAME}]].\n"


def _graph(v):
    kg = kgm.KnowledgeGraph(v, image_reader=None)
    kg.confirm_all_rules()
    return kg


def _vault(tmp_path, files):
    di = tmp_path / "data_in"
    di.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (di / name).write_bytes(raw)
    return tmp_path


# ─────────────────────────────── the decoder

def test_an_ansi_file_decodes_as_cp1252():
    assert fs.decode_text(NOTE.encode("cp1252")) == NOTE


def test_utf8_with_and_without_a_bom():
    assert fs.decode_text(NOTE.encode("utf-8")) == NOTE
    assert fs.decode_text(codecs.BOM_UTF8 + NOTE.encode("utf-8")) == NOTE


def test_utf16_by_its_bom():
    # Notepad's 'Unicode', Excel's 'Unicode Text'.
    assert fs.decode_text(NOTE.encode("utf-16")) == NOTE
    assert fs.decode_text(codecs.BOM_UTF16_BE + NOTE.encode("utf-16-be")) == NOTE


def test_a_utf8_file_with_one_damaged_byte_stays_utf8():
    raw = f"Signed: {NAME}, José García, Zoë Ångström\n".encode("utf-8") + b"\xff\n"
    text = fs.decode_text(raw)
    assert NAME in text and "José García" in text
    assert "Ã" not in text                          # not read as cp1252


def test_a_byte_cp1252_does_not_define_falls_back_to_latin1():
    text = fs.decode_text("Café ".encode("cp1252") + b"\x81")
    assert text.startswith("Café") and "\ufffd" not in text


def test_an_undefined_byte_leaves_the_rest_cp1252():
    # Only that byte is latin-1: '€' stays '€' (latin-1 would make it
    # '\x80'), in a plain ANSI file and in a mixed one.
    assert fs.decode_text("€ ".encode("cp1252") + b"\x81") == "€ \x81"
    assert fs.decode_text("é".encode("utf-8") + b" \x81 \x80") == "é \x81 €"


# A UTF-8 note or log with a line an ANSI tool added (review, 2026-10-19).
UTF8_HEAD = "Shift log – Café résumé “ok”\n"
ANSI_TAIL = f"Inspector: {NAME}\n"


def test_a_mixed_file_reads_both_halves():
    # More UTF-8 accents than ANSI bytes: the ANSI line had U+FFFD.
    raw = UTF8_HEAD.encode("utf-8") + ANSI_TAIL.encode("cp1252")
    assert fs.decode_text(raw) == UTF8_HEAD + ANSI_TAIL


def test_a_mixed_file_with_as_many_ansi_bytes_keeps_its_utf8():
    # As many ANSI bytes as UTF-8 accents: the UTF-8 line was mojibake.
    head = "Signed: José Ruíz\n"
    raw = head.encode("utf-8") + ANSI_TAIL.encode("cp1252")
    text = fs.decode_text(raw)
    assert text == head + ANSI_TAIL
    assert "Ã" not in text and "�" not in text


def test_a_mixed_file_keeps_a_real_replacement_character():
    # A U+FFFD already in the UTF-8 half is text, not a bad byte to count.
    head = "Name: Tom�s (garbled before) éé\n"
    raw = head.encode("utf-8") + "José\n".encode("cp1252")
    assert fs.decode_text(raw) == head + "José\n"


def test_a_cut_off_utf16_file_is_still_utf16():
    # An odd last byte: it was read as cp1252, a NUL after every letter.
    raw = NOTE.encode("utf-16")[:-1]
    text = fs.decode_text(raw)
    assert text.startswith(NOTE[:-1]) and "\x00" not in text


def test_read_text_and_document_lines_read_an_ansi_note(tmp_path):
    p = tmp_path / "ansi.txt"
    p.write_bytes(NOTE.encode("cp1252"))
    assert fs._read_text(p) == NOTE
    lines = kgm.document_lines(p)
    assert (None, 2, f"Attendees: {NAME}") in lines
    assert not any("\ufffd" in ln for _pg, _i, ln in lines)


# ─────────────────────────────── the graph: one person, not two

def test_an_ansi_note_and_a_utf8_note_name_one_person(tmp_path):
    v = _vault(tmp_path, {
        "utf8.md": NOTE.encode("utf-8"),
        "ansi.txt": NOTE.encode("cp1252"),
        "bom.md": codecs.BOM_UTF8 + NOTE.encode("utf-8"),
    })
    with _graph(v) as kg:
        kg.seed()
        people = [r["name"] for r in kg.db.execute(
            "SELECT name FROM entities WHERE type='PERSON' AND merged_into IS NULL")]
        names = [r["name"] for r in kg.db.execute("SELECT name FROM entities")]
        # Every document gave its evidence to the one person, the wiki-link
        # in the ANSI note included.
        docs = {r["path"] for r in kg.db.execute(
            "SELECT DISTINCT d.path FROM mentions m JOIN documents d"
            " ON d.id = m.document_id JOIN entities e ON e.id = m.entity_id"
            " WHERE e.type = 'PERSON' AND m.method = 'wikilink'")}
    assert people == [NAME]
    assert not any("\ufffd" in n for n in names), names
    assert docs == {"utf8.md", "ansi.txt", "bom.md"}


def test_an_ansi_csv_names_the_same_person(tmp_path):
    # The CSV reader already fell back to cp1252 (field_search._csv_frame);
    # pinned here beside the text reader so the two cannot drift apart.
    v = _vault(tmp_path, {
        "utf8.md": NOTE.encode("utf-8"),
        "team.csv": f"Project,Attendees\nAtlas,{NAME}\n".encode("cp1252"),
    })
    with _graph(v) as kg:
        kg.seed()
        people = [r["name"] for r in kg.db.execute(
            "SELECT name FROM entities WHERE type='PERSON' AND merged_into IS NULL")]
    assert people == [NAME]


def test_a_mixed_note_names_one_person(tmp_path):
    # A UTF-8 shift log with a record an ANSI tool appended: its accents
    # were U+FFFD, and 'Tom�s Echeverr�a' was a second person
    # beside the UTF-8 note's (review, 2026-10-19).
    v = _vault(tmp_path, {
        "a.md": NOTE.encode("utf-8"),
        "shift.log": (UTF8_HEAD.encode("utf-8")
                      + f"Project: Atlas\nAttendees: {NAME}\n".encode("cp1252")),
    })
    with _graph(v) as kg:
        kg.seed()
        people = [r["name"] for r in kg.db.execute(
            "SELECT name FROM entities WHERE type='PERSON' AND merged_into IS NULL")]
        names = [r["name"] for r in kg.db.execute("SELECT name FROM entities")]
        docs = {r["path"] for r in kg.db.execute(
            "SELECT DISTINCT d.path FROM mentions m JOIN documents d"
            " ON d.id = m.document_id JOIN entities e ON e.id = m.entity_id"
            " WHERE e.type = 'PERSON'")}
    assert people == [NAME]
    assert not any("�" in n for n in names), names
    assert docs == {"a.md", "shift.log"}


def test_a_mixed_csv_reads_both_halves(tmp_path):
    # The CSV reader read the whole file as cp1252 once one byte was not
    # UTF-8: the UTF-8 rows' 'José Ruíz' became a person 'JosÃ© RuÃ­z'
    # (review, 2026-10-19).
    v = _vault(tmp_path, {
        "team.csv": ("Project,Attendees\nAtlas,José Ruíz\n".encode("utf-8")
                     + f"Atlas,{NAME}\n".encode("cp1252")),
    })
    with _graph(v) as kg:
        kg.seed()
        people = sorted(r["name"] for r in kg.db.execute(
            "SELECT name FROM entities WHERE type='PERSON' AND merged_into IS NULL"))
    assert people == ["José Ruíz", NAME]


def test_ansi_and_bom_json_are_read(tmp_path):
    doc = json.dumps({"Project": "Atlas", "Attendees": NAME}, ensure_ascii=False)
    v = _vault(tmp_path, {
        "ansi.json": doc.encode("cp1252"),
        "bom.json": codecs.BOM_UTF8 + doc.replace("Atlas", "Nimbus").encode("utf-8"),
    })
    with _graph(v) as kg:
        kg.seed()
        got = sorted((r["type"], r["name"]) for r in kg.db.execute(
            "SELECT type, name FROM entities WHERE type != 'DOCUMENT'"
            " AND merged_into IS NULL"))
    assert got == [("PERSON", NAME), ("PROJECT", "Atlas"), ("PROJECT", "Nimbus")]
