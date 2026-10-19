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
    # Two Capitalized words that are not a name (e.g. "Night Line") still
    # infer as PERSON because the regex cannot tell them apart from a
    # person's name without extra context; this is a documented limit
    # (user can relabel), and the test fixes it so future refactors do not
    # silently change the behavior.
    assert kgm._infer_wikilink_type("Night Line") == "PERSON"


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
