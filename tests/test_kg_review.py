"""KG3 — answering the graph's questions and merging duplicates
(KnowledgeGraph.answer_review / merge / unmerge), on the synthetic vault."""
from __future__ import annotations

import pytest

from council_core import kg_corpus
from council_core import knowledge_graph as kgm


def has(mod):
    import importlib.util
    return importlib.util.find_spec(mod) is not None


@pytest.fixture
def kg(tmp_path):
    v = tmp_path / "vault"
    kg_corpus.build(v)
    g = kgm.KnowledgeGraph(v)
    g.confirm_all_rules()
    g.add_label_synonym("POC", "Point of Contact")
    g.seed()
    yield g
    g.close()


def person(kg, name):
    return next(h["id"] for h in kg.search(name, "PERSON") if kg.entity(h["id"])["name"] == name)


def review(kg, surface):
    return next(r for r in kg.reviews() if r["surface"] == surface)


def rel_status(kg, subj, pred, obj):
    for r in kg.all_relations(include_rejected=True):
        if (r["subject"]["name"], r["predicate"], r["object"]["name"]) == (subj, pred, obj):
            return r["status"]
    return None


def test_ambiguous_initial_answered_for_this_document(kg):
    q = review(kg, "D. Whitfield")
    dana = person(kg, "Dana Whitfield")
    kg.answer_review(q["id"], dana)
    kg.seed()
    assert all(r["surface"] != "D. Whitfield" for r in kg.reviews())
    spots = {(m["path"].split("/")[-1], m["where"]) for m in kg.mentions(dana)}
    assert ("helios_status_2026-03.md", "line 13") in spots
    assert kg.decisions()[-1]["action"] == "name"


def test_an_answer_for_one_document_does_not_decide_another(kg, tmp_path):
    q = review(kg, "D. Whitfield")
    kg.answer_review(q["id"], person(kg, "Dana Whitfield"))
    other = kg.root / "ironbridge" / "notes" / "atlas_followup.txt"
    other.write_text("D. Whitfield will rerun the CTL-5005 firmware test on Atlas.\n",
                     encoding="utf-8")
    kg.seed()
    still = [r for r in kg.reviews() if r["surface"] == "D. Whitfield"]
    assert [r["path"].split("/")[-1] for r in still] == ["atlas_followup.txt"]


def test_everywhere_answer_settles_all_documents(kg):
    other = kg.root / "ironbridge" / "notes" / "atlas_followup.txt"
    other.write_text("D. Whitfield will rerun the CTL-5005 firmware test on Atlas.\n",
                     encoding="utf-8")
    kg.seed()
    q = review(kg, "D. Whitfield")
    kg.answer_review(q["id"], person(kg, "Dan Whitfield"), everywhere=True)
    kg.seed()
    assert not [r for r in kg.reviews() if r["surface"] == "D. Whitfield"]


def test_neither_makes_a_separate_person(kg):
    q = review(kg, "D. Whitfield")
    kg.answer_review(q["id"], None)
    kg.seed()
    assert not [r for r in kg.reviews() if r["surface"] == "D. Whitfield"]


@pytest.mark.skipif(not has("openpyxl"), reason="needs the tracker workbook")
def test_confirming_an_inferred_name_firms_its_links(kg):
    assert rel_status(kg, "Marcus Oyelaran", "CONTACT_FOR", "PRJ-0920") == "suggested"
    q = review(kg, "M. Oyelaran")
    kg.answer_review(q["id"], person(kg, "Marcus Oyelaran"), everywhere=True)
    kg.seed()
    assert rel_status(kg, "Marcus Oyelaran", "CONTACT_FOR", "PRJ-0920") == "seeded"


def test_rejecting_an_inferred_name_unlinks_it(kg):
    q = review(kg, "M. Oyelaran")
    kg.answer_review(q["id"], None, everywhere=True)
    kg.seed()
    assert rel_status(kg, "Marcus Oyelaran", "CONTACT_FOR", "PRJ-0920") != "suggested"
    assert any(h["name"] == "M. Oyelaran" for h in kg.search("Oyelaran", "PERSON"))


def test_merge_moves_names_links_and_survives_a_rebuild(kg):
    # Pretend 'Bob Smith, Jr.' and a second spelling were two people.
    bob = person(kg, "Bob Smith, Jr.")
    kg._entity("PERSON", "Robert Smith")
    kg.db.commit()
    rob = person(kg, "Robert Smith")
    kg.merge(bob, rob)
    assert kg.resolve(rob) == bob
    hits = {h["id"] for h in kg.search("Robert Smith", "PERSON")}
    assert hits == {bob}
    kg.seed()
    assert {h["id"] for h in kg.search("Robert Smith", "PERSON")} <= {bob}
    assert "merge" in [d["action"] for d in kg.decisions()]


def test_merge_keeps_the_users_decisions(kg):
    carol = person(kg, "Carol Lee")
    kg._entity("PERSON", "Caroline Lee")
    kg.db.commit()
    caroline = person(kg, "Caroline Lee")
    # a user-accepted link on the duplicate
    kg._relate(caroline, "WORKS_ON", kg.search("PRJ-0944", "PROJECT")[0]["id"], "suggested",
               doc_id=kg.db.execute("SELECT id FROM documents LIMIT 1").fetchone()[0],
               content_hash="x", locator={"line": 1}, quote="q", method="model", run_id="r")
    kg.db.commit()
    rid = next(r["id"] for r in kg.all_relations() if r["subject"]["name"] == "Caroline Lee")
    kg.set_relation_status(rid, "accepted")
    kg.merge(carol, caroline)
    assert rel_status(kg, "Carol Lee", "WORKS_ON", "PRJ-0944") == "accepted"
    assert rel_status(kg, "Caroline Lee", "WORKS_ON", "PRJ-0944") is None


def test_unmerge_restores_the_entity(kg):
    carol = person(kg, "Carol Lee")
    kg._entity("PERSON", "Caroline Lee")
    kg.db.commit()
    caroline = person(kg, "Caroline Lee")
    kg.merge(carol, caroline)
    kg.unmerge(caroline)
    assert kg.resolve(caroline) == caroline
    assert [d["action"] for d in kg.decisions()][-2:] == ["merge", "unmerge"]


@pytest.mark.parametrize("bad", ["same", "type"])
def test_merge_refuses_nonsense(kg, bad):
    carol = person(kg, "Carol Lee")
    other = carol if bad == "same" else kg.search("PN-0088", "PART")[0]["id"]
    with pytest.raises(ValueError):
        kg.merge(carol, other)
