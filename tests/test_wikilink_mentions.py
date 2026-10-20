"""
End-to-end tests for the ``[[wiki-link]] → KG mention`` wiring (Piece B
tail of ``LEDGER_KG_DESIGN.md``).

Each test builds a minimal vault under ``tmp_path`` and runs ``kg.seed()``
against text documents that carry wiki-links. The parser-only tests live
in ``test_wikilink_scan.py`` — this file is for the DB-side wiring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import knowledge_graph as kgm                        # noqa: E402


def _vault_with(tmp_path, files):
    data_in = tmp_path / "data_in"
    data_in.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (data_in / name).write_text(text, encoding="utf-8")
    return tmp_path


def _graph(v):
    kg = kgm.KnowledgeGraph(v)
    kg.confirm_all_rules()
    return kg


def _mentions_for(kg, surface):
    """Every (surface, method) mention the KG has for an entity whose name
    folds like ``surface``."""
    fn = kgm.fold(surface)
    rows = kg.db.execute(
        "SELECT m.method, m.surface, m.locator, e.type, e.name FROM mentions m"
        " JOIN entities e ON e.id = m.entity_id"
        " JOIN aliases a ON a.entity_id = e.id"
        " WHERE a.alias_norm = ?", (fn,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------- _infer_wikilink_type

def test_a_code_shape_infers_as_part():
    assert kgm._infer_wikilink_type("ABC-123") == "PART"
    assert kgm._infer_wikilink_type("P 101") == "PART"


def test_two_capitalized_words_infer_as_person():
    assert kgm._infer_wikilink_type("Jordan Blake") == "PERSON"
    assert kgm._infer_wikilink_type("Maria O'Brien") == "PERSON"


def test_other_shapes_fall_back_to_project():
    assert kgm._infer_wikilink_type("project with lower") == "PROJECT"
    assert kgm._infer_wikilink_type("Jordan") == "PROJECT"           # single Capitalized word → not a confident person
    # Two Capitalized words that are not a name used to infer as PERSON
    # ("Night Line"). Since review, 2026-10-19 a word that names a thing,
    # not a person (Line, Program, Inspection, ... - _NOT_NAME_WORDS),
    # makes it a title. Two Capitalized words with no such word were still
    # a PERSON ("Night Owl" was pinned as one); since the second review,
    # 2026-10-19, a PERSON needs positive evidence (a known given name, an
    # initial, 'Last, First', a particle), so "Night Owl" is a PROJECT.
    assert kgm._infer_wikilink_type("Night Line") == "PROJECT"
    assert kgm._infer_wikilink_type("Night Owl") == "PROJECT"


# ---------------------------------------------- end-to-end seed + mentions

def test_a_new_person_wikilink_creates_the_entity_and_a_mention(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Spoke to [[Jordan Blake]] today about calibration.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilink_mentions"] == 1
    assert stats["wikilinks_created"] == 1
    assert stats["wikilinks_matched"] == 0
    with _graph(v) as kg:
        hits = _mentions_for(kg, "Jordan Blake")
    assert len(hits) == 1
    assert hits[0]["method"] == "wikilink"
    assert hits[0]["type"] == "PERSON"
    assert hits[0]["name"] == "Jordan Blake"


def test_a_wikilink_to_an_existing_entity_resolves_rather_than_duplicates(tmp_path):
    # Two docs: one establishes Jordan Blake as a PERSON via wiki-link, the
    # other wiki-links the same name. Second rebuild run → matched, not new.
    v = _vault_with(tmp_path, {
        "first.md": "First mention: [[Jordan Blake]].\n",
        "second.md": "Later: [[Jordan Blake]] signed off on the report.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    # Both wiki-links appear in the same rebuild — the first creates, the
    # second resolves against the alias the first just added.
    assert stats["wikilink_mentions"] == 2
    assert stats["wikilinks_created"] == 1
    assert stats["wikilinks_matched"] == 1


def test_a_code_shape_lands_as_a_part(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Flagged [[ABC-123]] for retest.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilinks_created"] == 1
    with _graph(v) as kg:
        hits = _mentions_for(kg, "ABC-123")
    assert len(hits) == 1
    assert hits[0]["type"] == "PART"


def test_a_pipe_alias_uses_the_surface_as_the_entity_name(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Call [[Jordan Blake|JB]] about the matter.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilink_mentions"] == 1
    with _graph(v) as kg:
        hits = _mentions_for(kg, "Jordan Blake")
    assert len(hits) == 1
    assert hits[0]["name"] == "Jordan Blake"


def test_zero_wikilinks_add_no_mentions_or_entities(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Nothing of interest here.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilink_mentions"] == 0
    assert stats["wikilinks_created"] == 0
    assert stats["wikilinks_matched"] == 0
    with _graph(v) as kg:
        row = kg.db.execute(
            "SELECT COUNT(*) FROM mentions WHERE method='wikilink'").fetchone()
    assert row[0] == 0


def test_withdrawing_a_wikilink_removes_its_mention_on_the_next_rebuild(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Spoke to [[Jordan Blake]] today.\n",
    })
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilink_mentions"] == 1
    # Rewrite the doc with no wikilink; the rebuild should wipe the mention.
    (v / "data_in" / "note.md").write_text("No references now.\n",
                                           encoding="utf-8")
    with _graph(v) as kg:
        stats = kg.seed()
    assert stats["wikilink_mentions"] == 0
    with _graph(v) as kg:
        row = kg.db.execute(
            "SELECT COUNT(*) FROM mentions WHERE method='wikilink'").fetchone()
    assert row[0] == 0


# ---------------------------------------------- a labelled field written as a link
# (review, 2026-10-19): a field took its value as written, brackets and all -
# a PART '[[PN-1002]]' beside PN-1002, Priya Raman renamed '[[Priya Raman]]'
# (a person keeps the fullest form of the name), and a PERSON called
# '[[Jordan Blake]]. Tracked as [[PRJ-0915]]'.

_PLAIN = "Project: Atlas Pump Retrofit\nOwner: Priya Raman\nPart Number: PN-1002\n"


def _shape(kg):
    """(live entities, relations) by name."""
    names = {r["id"]: r["name"] for r in kg.db.execute("SELECT id, name FROM entities")}
    ents = sorted((r["type"], r["name"]) for r in kg.db.execute(
        "SELECT type, name FROM entities WHERE type != 'DOCUMENT'"
        " AND merged_into IS NULL"))
    rels = sorted((names[r["subject_id"]], r["predicate"], names[r["object_id"]],
                   r["status"]) for r in kg.db.execute(
                       "SELECT subject_id, predicate, object_id, status FROM relations"))
    return ents, rels


@pytest.mark.parametrize("linked", [
    "Project: [[Atlas Pump Retrofit]]\nOwner: [[Priya Raman]]\n"
    "Part Number: [[PN-1002]]\n",
    # YAML front matter: Obsidian needs the quotes round a link there.
    '---\nproject: "[[Atlas Pump Retrofit]]"\nowner: "[[Priya Raman]]"\n---\n'
    "Part Number: [[PN-1002|the pump part]]\n",
    # A table: an anchor, and the alias pipe a table escapes.
    "| Project | [[Atlas Pump Retrofit#Scope]] |\n| Owner | [[Priya Raman\\|Priya]] |\n"
    "| Part Number | [[PN-1002]] |\n",
], ids=["fields", "front-matter", "table"])
def test_a_field_written_as_a_wikilink_reads_as_the_plain_field(tmp_path, linked):
    plain = _vault_with(tmp_path / "plain", {"a.md": _PLAIN})
    both = _vault_with(tmp_path / "both", {"a.md": _PLAIN, "b.md": linked})
    with _graph(plain) as kg:
        kg.seed()
        want = _shape(kg)
    with _graph(both) as kg:
        stats = kg.seed()
        got = _shape(kg)
        people = [h["name"] for h in kg.search("Priya", "PERSON")]
    assert want == ([("PART", "PN-1002"), ("PERSON", "Priya Raman"),
                     ("PROJECT", "Atlas Pump Retrofit")],
                    [("Atlas Pump Retrofit", "USES_PART", "PN-1002", "seeded"),
                     ("Priya Raman", "OWNS", "PN-1002", "seeded")])
    # One PART PN-1002, one person named 'Priya Raman', the same links.
    assert got == want
    assert people == ["Priya Raman"]
    # Each link still resolves to the field's entity: none is made anew.
    assert stats["wikilink_mentions"] == 3 and stats["wikilinks_created"] == 0


def test_a_field_holding_two_links_takes_the_first(tmp_path):
    v = _vault_with(tmp_path, {
        "d.md": "Owner: [[Jordan Blake]]. Tracked as [[PRJ-0915]].\n"
                "Part Number: PN-1002\n"})
    with _graph(v) as kg:
        kg.seed()
        ents, rels = _shape(kg)
        tracked = _mentions_for(kg, "PRJ-0915")
    assert ("PERSON", "Jordan Blake") in ents
    assert not [n for _t, n in ents if "[[" in n or "Tracked" in n], ents
    assert rels == [("Jordan Blake", "OWNS", "PN-1002", "seeded")]
    # The second link is the wiki-link pass's, as any link in the text is.
    assert [m["method"] for m in tracked] == ["wikilink"]


def test_a_link_the_value_splitter_cut_is_read_whole(tmp_path):
    # 'Last, First' inside a link: the comma cut it into '[[Lee' and 'Carol]]'.
    v = _vault_with(tmp_path, {
        "m.md": "Project: Atlas\nAttendees: [[Lee, Carol]], [[Tomas Echeverria]]\n"})
    with _graph(v) as kg:
        kg.seed()
        ents, rels = _shape(kg)
    assert [n for t, n in ents if t == "PERSON"] == ["Carol Lee", "Tomas Echeverria"]
    assert rels == [("Carol Lee", "WORKS_ON", "Atlas", "seeded"),
                    ("Tomas Echeverria", "WORKS_ON", "Atlas", "seeded")]


# ---------------------------------------------- initials: a wiki-link's person
# (review, 2026-10-19): the initials questions' candidates were worked out
# before the wiki-link pass, so 'Who is RT?' did not offer a Rita Tan only a
# [[Rita Tan]] names.

def test_the_initials_question_offers_a_person_only_a_wikilink_names(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    v = _vault_with(tmp_path, {"rita.md": "[[Rita Tan]] runs the lift tests.\n"})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Part Number", "Inspector"])
    ws.append(["SN-5501", "RT"])
    wb.save(v / "data_in" / "lift.xlsx")

    def question():
        with kgm.KnowledgeGraph(v, image_reader=None) as kg:
            kg.confirm_all_rules()
            stats = kg.seed()
            (q,) = [r for r in kg.reviews() if r["kind"] == "initials"]
            inspected = [k for k in _shape(kg)[1] if k[1] == "INSPECTED"]
        return stats, q, inspected

    stats, q, inspected = question()
    assert q["surface"] == "RT"
    assert [c["name"] for c in q["candidates"]] == ["Rita Tan"]
    assert inspected == []                  # offered, never picked
    # A rebuild that works the graph out again offers her too.
    (v / "data_in" / "rita.md").write_text("[[Rita Tan]] runs the lift tests again.\n",
                                           encoding="utf-8")
    stats, q, inspected = question()
    assert stats["graph_unchanged"] is False
    assert [c["name"] for c in q["candidates"]] == ["Rita Tan"]
    assert inspected == []


# ---------------------------------------------- summary line matched/created

def test_the_summary_line_breaks_down_matched_and_created():
    line = kgm.rebuild_summary_line({
        "documents": 2, "wikilink_mentions": 3,
        "wikilinks_matched": 1, "wikilinks_created": 2,
    })
    assert "3 [[wiki-link]] mentions found" in line
    assert "1 matched existing" in line
    assert "2 new entities" in line


def test_the_summary_singular_new_entity_reads_right():
    line = kgm.rebuild_summary_line({
        "documents": 1, "wikilink_mentions": 1,
        "wikilinks_matched": 0, "wikilinks_created": 1,
    })
    assert "1 new entity" in line
    assert "entities" not in line
