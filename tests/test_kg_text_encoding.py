"""
A text note's encoding: an ANSI (Windows-1252, Notepad's default for years)
file is read as Windows-1252, not as UTF-8 with replacement characters.

The bug (review, 2026-10-19): field_search._read_text decoded every text file
as UTF-8 with errors='replace', so an ANSI note's 'Tomás Echeverría' read as
'Tom\\ufffds Echeverr\\ufffda' and the knowledge graph made a second, garbled
person of him beside the UTF-8 note's. The JSON reader did the same, and a
JSON file with a BOM gave no records at all.

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
    assert text.startswith("Café") and "�" not in text


def test_read_text_and_document_lines_read_an_ansi_note(tmp_path):
    p = tmp_path / "ansi.txt"
    p.write_bytes(NOTE.encode("cp1252"))
    assert fs._read_text(p) == NOTE
    lines = kgm.document_lines(p)
    assert (None, 2, f"Attendees: {NAME}") in lines
    assert not any("�" in ln for _pg, _i, ln in lines)


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
    assert not any("�" in n for n in names), names
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
