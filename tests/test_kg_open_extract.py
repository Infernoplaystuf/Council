"""The open, Morphik-like build (user, 2026-10-21: "I would like the connections
tab to function very similar to morphik"): a model reads every passage and
names the entities in it, of any type, and the relationships between them;
the Council checks each against the text and keeps them, cited, across
rebuilds. Scripted "models" only - nothing calls a real one."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from council_core import kg_extract as kx
from council_core import knowledge_graph as kgm

KICKOFF = """# Falcon kickoff

Ngozi Okafor will lead the Falcon Wing Retrofit (KA-101). Sam Whitaker owns the WG-2201 spar.
The spar WG-2201 is used on Falcon Wing Retrofit.
Nordic Forge, based in Oslo, supplies the WG-2201 forgings.
"""

ANSWER = {
    "entities": [
        {"name": "Ngozi Okafor", "type": "Engineer", "description": "leads the retrofit"},
        {"name": "Falcon Wing Retrofit", "type": "Program"},
        {"name": "KA-101", "type": "project"},
        {"name": "Sam Whitaker", "type": "person"},
        {"name": "WG-2201", "type": "component", "description": "wing spar"},
        {"name": "Nordic Forge", "type": "supplier"},
        {"name": "Oslo", "type": "city"},
        {"name": "Dana Whitfield", "type": "person"},          # not in the text
        {"name": "they", "type": "person"},                    # names nothing
    ],
    "relationships": [
        {"source": "Ngozi Okafor", "relation": "leads", "target": "Falcon Wing Retrofit",
         "quote": "Ngozi Okafor will lead the Falcon Wing Retrofit"},
        {"source": "Sam Whitaker", "relation": "owns", "target": "WG-2201",
         "quote": "Sam Whitaker owns the WG-2201 spar."},
        {"source": "WG-2201", "relation": "used on", "target": "Falcon Wing Retrofit",
         "quote": "The spar WG-2201 is used on Falcon Wing Retrofit."},
        {"source": "Nordic Forge", "relation": "supplies", "target": "WG-2201",
         "quote": "Nordic Forge, based in Oslo, supplies the WG-2201 forgings."},
        {"source": "Nordic Forge", "relation": "located in", "target": "Oslo",
         "quote": "Nordic Forge, based in Oslo"},
        {"source": "Ngozi Okafor", "relation": "leads", "target": "Oslo",
         "quote": "a sentence that is not there"},
        {"source": "Dana Whitfield", "relation": "works on", "target": "KA-101",
         "quote": "Ngozi Okafor will lead the Falcon Wing Retrofit"},
    ],
}


def scripted(answer=ANSWER, calls=None):
    def chat(messages, **kw):
        if calls is not None:
            calls.append(kw)
        return json.dumps(answer)
    return chat


def _vault(tmp_path, text=KICKOFF, name="kickoff.md"):
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    (v / "data_in" / name).write_text(text, encoding="utf-8")
    return v


def _rels(kg):
    return {(r["subject"]["name"], r["predicate"], r["object"]["name"]): r["status"]
            for r in kg.all_relations() if r["predicate"] != "DOCUMENTED_IN"}


def _types(kg):
    return {r["name"]: r["type"] for r in kg.db.execute(
        "SELECT name, type FROM entities WHERE type != 'DOCUMENT' AND merged_into IS NULL")}


# ── the words a model uses ───────────────────────────────────────────────
@pytest.mark.parametrize("raw, want", [
    ("Engineer", "PERSON"), ("design engineer", "PERSON"), ("Supplier", "ORGANIZATION"),
    ("companies", "ORGANIZATION"), ("Program", "PROJECT"), ("component", "PART"),
    ("city", "LOCATION"), ("Heat Treatment", "HEAT_TREATMENT"),
    # DOCUMENT is the graph's own type for the files it read
    ("document", "REPORT"), ("DOCUMENT", "REPORT"), ("", "CONCEPT"),
])
def test_a_models_type_is_mapped_onto_the_graphs(raw, want):
    assert kx.canonical_type(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("leads", ("LEADS", False)), ("replaces", ("SUPERSEDES", False)),
    ("replaced by", ("SUPERSEDES", True)), ("used on", ("USES_PART", True)),
    ("supplies parts to", ("SUPPLIES_PARTS_TO", False)), ("", ("RELATED_TO", False)),
])
def test_a_models_relationship_is_mapped_and_turned_round(raw, want):
    assert kx.relation_direction(raw) == want


# ── what the Council keeps of an answer ──────────────────────────────────
def test_only_what_the_passage_says_is_kept():
    ents, links, rejected = kx.check_open(ANSWER, KICKOFF, [])
    names = {e.name: e.type for e in ents}
    # 'Falcon Wing Retrofit (KA-101)': one project, the code its name
    assert names == {"Ngozi Okafor": "PERSON", "KA-101": "PROJECT", "Sam Whitaker": "PERSON",
                     "WG-2201": "PART", "Nordic Forge": "ORGANIZATION", "Oslo": "LOCATION"}
    proj = next(e for e in ents if e.name == "KA-101")
    assert proj.aliases == ["Falcon Wing Retrofit"]
    got = {(l.subject.name, l.predicate, l.object.name) for l in links}
    assert got == {("Ngozi Okafor", "LEADS", "KA-101"), ("Sam Whitaker", "OWNS", "WG-2201"),
                   ("KA-101", "USES_PART", "WG-2201"),
                   ("Nordic Forge", "SUPPLIES", "WG-2201"),
                   ("Nordic Forge", "LOCATED_IN", "Oslo")}
    why = " | ".join(r.why for r in rejected)
    assert "the text never names Dana Whitfield" in why
    assert "not a particular thing" in why
    assert "its quote is not in the text" in why


def test_reversed_core_types_are_turned_round_and_impossible_ones_refused():
    text = "PN-77 is owned by Ann Stone. Ann Stone and PN-78 met at the review."
    raw = {"entities": [{"name": "Ann Stone", "type": "person"},
                        {"name": "PN-77", "type": "part"}, {"name": "PN-78", "type": "part"}],
           "relationships": [
               {"source": "PN-77", "relation": "OWNS", "target": "Ann Stone",
                "quote": "PN-77 is owned by Ann Stone."},
               {"source": "PN-78", "relation": "LEADS", "target": "PN-77",
                "quote": "Ann Stone and PN-78 met at the review."}]}
    _e, links, rejected = kx.check_open(raw, text, [])
    assert [(l.subject.name, l.predicate, l.object.name, l.fixed) for l in links] == [
        ("Ann Stone", "OWNS", "PN-77", "turned round (the types were reversed)")]
    assert "LEADS cannot link a PART to a PART" in rejected[-1].why


def test_a_person_and_a_part_of_one_name_are_never_one():
    """Morphik merged by label and ignored the type (a PERSON and a PART of
    one name became one, docs/morphik_assessment.md §2)."""
    known = [kx.Known("p1", "PART", "Atlas", [])]
    text = "Atlas signed off the build."
    raw = {"entities": [{"name": "Atlas", "type": "person"}], "relationships": []}
    ents, _l, _r = kx.check_open(raw, text, known)
    assert ents[0].type == "PERSON" and ents[0].known is None
    # an organisation of a known part's name is that part (one thing, two words)
    raw = {"entities": [{"name": "Atlas", "type": "organization"}], "relationships": []}
    ents, _l, _r = kx.check_open(raw, text, known)
    assert ents[0].known is not None and ents[0].type == "PART"


def test_initials_that_fit_two_known_people_are_not_guessed():
    known = [kx.Known("a", "PERSON", "Dana Whitfield", []),
             kx.Known("b", "PERSON", "Dan Whitfield", [])]
    raw = {"entities": [{"name": "D. Whitfield", "type": "person"}], "relationships": []}
    ents, _l, rejected = kx.check_open(raw, "D. Whitfield will review it.", known)
    assert ents == [] and "more than one known person" in rejected[0].why


def test_an_answer_with_no_json_is_an_error_not_a_crash():
    res = kx.extract_open("text", [], lambda m, **k: "sorry, no")
    assert res.error.startswith("ValueError") and res.entities == []


def test_the_prompt_carries_the_graphs_focus_and_known_names():
    msgs = kx.build_open_prompt("Some text.", [kx.Known("x", "PROJECT", "KA-101", [])],
                                focus="heat treatment furnaces and their operators")
    user = msgs[-1]["content"]
    assert "What to look for in these documents: heat treatment furnaces" in user
    assert "- PROJECT: KA-101" in user and '"""\nSome text.\n"""' in user


# ── the store ────────────────────────────────────────────────────────────
def test_a_prose_document_gives_a_graph(tmp_path):
    """The verification of 2026-10-21: a notes file naming a lead, a part
    and a supplier gave 0 entities and 0 links, and no model was asked."""
    v = _vault(tmp_path)
    calls = []
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        assert kg.why_no_links()["reason"]["code"] == "text_not_read"
        st = kg.extract_from_text({"pc": scripted(calls=calls)}, model="m")
        assert st["chunks"] == st["done"] == 1 and st["errors"] == 0
        assert calls[0]["schema"] is kx.OPEN_SCHEMA
        assert calls[0]["num_predict"] == kx.OPEN_NUM_PREDICT
        assert _types(kg) == {"Ngozi Okafor": "PERSON", "KA-101": "PROJECT",
                              "Sam Whitaker": "PERSON", "WG-2201": "PART",
                              "Nordic Forge": "ORGANIZATION", "Oslo": "LOCATION"}
        rels = _rels(kg)
        assert set(rels.values()) == {"seeded"}              # in the graph at once
        assert ("Nordic Forge", "SUPPLIES", "WG-2201") in rels
        part = kg.search("WG-2201")[0]
        assert kg.entity(part["id"])["description"] == "wing spar"
        sup = next(n for n in kg.neighbors(part["id"]) if n["predicate"] == "SUPPLIES")
        (ev,) = sup["evidence"]
        assert ev["method"] == "model" and ev["where"] == "line 5" and ev["model"] == "m @ pc"
        assert kg.search("falcon")[0]["name"] == "KA-101"
        assert kg.why_no_links()["reason"] is None


def test_what_a_model_read_stays_across_rebuilds_and_goes_when_the_text_changes(tmp_path):
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        kg.extract_from_text({"pc": scripted()}, model="m")
        before = (_types(kg), _rels(kg))
        kg.seed()
        assert (_types(kg), _rels(kg)) == before
        assert kg.extract_from_text({"pc": scripted()}, model="m")["chunks"] == 0
        (v / "data_in" / "kickoff.md").write_text("# Nothing here any more\n",
                                                  encoding="utf-8")
        kg.seed()
        assert _types(kg) == {} and _rels(kg) == {}
        assert kg.db.execute("SELECT COUNT(*) FROM entity_props").fetchone()[0] == 0


def test_a_stopped_build_continues_where_it_stopped(tmp_path):
    text = "\n".join(f"Line {i}: Ngozi Okafor checked WG-2201." for i in range(120))
    v = _vault(tmp_path, text)
    raw = {"entities": [{"name": "Ngozi Okafor", "type": "person"},
                        {"name": "WG-2201", "type": "part"}], "relationships": []}
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        calls = []
        st = kg.extract_from_text({"pc": scripted(raw, calls)}, model="m",
                                  should_stop=lambda: len(calls) >= 1)
        assert st["stopped"] and 1 <= st["done"] < st["chunks"]
        rest = kg.extract_from_text({"pc": scripted(raw)}, model="m")
        assert rest["chunks"] == st["chunks"] - st["done"] and not rest["stopped"]


def test_a_model_name_meets_the_labelled_one(tmp_path):
    """'Okafor, Ngozi' from a table and 'Ngozi Okafor' from a sentence are
    one person; the model's link lands on the labelled project."""
    v = _vault(tmp_path)
    (v / "data_in" / "register.csv").write_text(
        "Project ID,Project Name,Program Lead\nKA-101,Falcon Wing Retrofit,\"Okafor, Ngozi\"\n",
        encoding="utf-8")
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        assert _rels(kg) == {("Ngozi Okafor", "LEADS", "KA-101"): "seeded"}
        kg.extract_from_text({"pc": scripted()}, model="m")
        people = [n for n, t in _types(kg).items() if t == "PERSON"]
        assert sorted(people) == ["Ngozi Okafor", "Sam Whitaker"]
        lead = [n for n in kg.neighbors(kg.search("Ngozi Okafor")[0]["id"])
                if n["predicate"] == "LEADS"]
        assert len(lead) == 1
        assert {e["method"] for e in lead[0]["evidence"]} == {"row", "model"}


def test_a_graph_set_to_review_keeps_a_models_links_suggested(tmp_path):
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v) as kg:
        kg.set_model_links("review")
        kg.seed()
        kg.extract_from_text({"pc": scripted()}, model="m")
        assert set(_rels(kg).values()) == {"suggested"}


def test_building_never_changes_the_documents(tmp_path):
    v = _vault(tmp_path)

    def tree():
        return {p.relative_to(v / "data_in").as_posix():
                hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((v / "data_in").rglob("*")) if p.is_file()}
    before = tree()
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        kg.extract_from_text({"pc": scripted()}, model="m")
        kg.write_exports()
    assert tree() == before
    assert not any(Path(v / "data_in").rglob(".knowledge_graph"))


def test_one_thing_named_twice_is_one_entity():
    text = "Okafor, Ngozi signed it. Later Ngozi Okafor checked it again."
    raw = {"entities": [{"name": "Okafor, Ngozi", "type": "person"},
                        {"name": "Ngozi Okafor", "type": "person", "description": "checker"}],
           "relationships": []}
    ents, _l, _r = kx.check_open(raw, text, [])
    assert [(e.name, e.description) for e in ents] == [("Okafor, Ngozi", "checker")]
