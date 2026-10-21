"""Asking the knowledge graph (council_core/kg_query.py): the entities a
question names, their links a few hops out, the chain between two of them,
cited - for "Ask the graph" in Connections and for the Council's answers
(user, 2026-10-21)."""
from __future__ import annotations

import pytest

from council_core import kg_graphs as kgg
from council_core import kg_query as kq
from council_core import knowledge_graph as kgm


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in" / "falcon").mkdir(parents=True)
    (v / "data_in" / "tracker.csv").write_text(
        "Project ID,Project Name,Program Lead\n"
        "KA-101,Falcon Wing Retrofit,\"Okafor, Ngozi\"\n"
        "KA-102,Osprey Landing Gear,Jens Halvorsen\n", encoding="utf-8")
    (v / "data_in" / "parts.csv").write_text(
        "Project,Part Number,Owner\nKA-101,WG-2201,Sam Whitaker\n"
        "KA-102,LG-0310,Jens Halvorsen\n", encoding="utf-8")
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
    return v


def test_the_names_in_a_question_are_found_longest_first(vault):
    with kgm.KnowledgeGraph(vault) as kg:
        got = kq.entities_in_text(kg, "Who leads the falcon wing retrofit, and does "
                                      "okafor ngozi know sam whitaker?")
        assert [e["name"] for e in got] == ["KA-101", "Ngozi Okafor", "Sam Whitaker"]
        assert kq.entities_in_text(kg, "who works on the project?") == []
        assert [e["name"] for e in kq.entities_in_text(kg, "status of KA-102")] == ["KA-102"]


def test_links_are_followed_hops_deep_and_rejected_ones_never(vault):
    with kgm.KnowledgeGraph(vault) as kg:
        sam = kg.search("Sam Whitaker")[0]["id"]
        one = kq.neighbourhood(kg, [sam], hops=1)
        assert [(f.subject["name"], f.predicate, f.object["name"]) for f in one] == [
            ("Sam Whitaker", "OWNS", "WG-2201")]
        two = kq.neighbourhood(kg, [sam], hops=2)
        assert ("KA-101", "USES_PART", "WG-2201") in [
            (f.subject["name"], f.predicate, f.object["name"]) for f in two]
        assert [f.n for f in two] == list(range(1, len(two) + 1))
        kg.set_relation_status(one[0].relation_id, "rejected")
        assert kq.neighbourhood(kg, [sam], hops=2) == []


def test_two_named_things_get_the_chain_between_them(vault):
    with kgm.KnowledgeGraph(vault) as kg:
        ctx = kq.context(kg, "How is Sam Whitaker connected to Ngozi Okafor?")
        assert len(ctx["paths"]) == 1
        chain = ctx["paths"][0]
        assert [(f.subject["name"], f.predicate, f.object["name"]) for f in chain] == [
            ("Sam Whitaker", "OWNS", "WG-2201"), ("KA-101", "USES_PART", "WG-2201"),
            ("Ngozi Okafor", "LEADS", "KA-101")]
        assert "How they connect: " in ctx["text"]
        assert 'KA-101 "Falcon Wing Retrofit" (project)' in ctx["text"]
        assert "tracker.csv, row 2" in ctx["text"]
        assert str(vault / "data_in" / "tracker.csv") in ctx["sources"]


def test_ask_answers_with_a_model_and_lists_the_facts_without_one(vault):
    seen = []

    def chat(messages, **kw):
        seen.append((messages, kw))
        return "Ngozi Okafor leads KA-101 [1]."
    got = kq.ask(vault, "What does Ngozi Okafor lead?", chat)
    assert got["answer"] == "Ngozi Okafor leads KA-101 [1]." and got["error"] == ""
    msgs, kw = seen[0]
    assert kw == {"num_predict": 900, "schema": None}
    assert "Use ONLY the numbered facts" in msgs[0]["content"]
    assert "[1] Ngozi Okafor (person) — leads → KA-101" in msgs[1]["content"]
    plain = kq.ask(vault, "What does Ngozi Okafor lead?")
    assert plain["answer"] is None and "[1] Ngozi Okafor" in plain["text"]
    none = kq.ask(vault, "What is the weather?", chat)
    assert none["answer"] is None and "names no person" in none["error"]
    assert len(seen) == 1                                   # no model call for nothing

    def broken(messages, **kw):
        raise ConnectionError("Ollama is not running")
    bad = kq.ask(vault, "What does Ngozi Okafor lead?", broken)
    assert bad["answer"] is None and "Ollama is not running" in bad["error"]
    assert "[1]" in bad["text"]


def test_only_the_graphs_marked_for_answers_are_read(vault):
    gl = kgg.GraphList(vault)
    (vault / "data_in" / "falcon" / "notes.csv").write_text(
        "Project,Owner\nKA-900,Ann Stone\n", encoding="utf-8")
    info = gl.create("Falcon", [vault / "data_in" / "falcon"], use_in_answers=False)
    with gl.open(info.id) as kg:
        kg.seed()
    assert kq.block(vault, "what does Ann Stone own?") is None
    gl.update(info.id, use_in_answers=True)
    text, sources = kq.block(vault, "what does Ann Stone own?")
    assert 'Knowledge graph "Falcon"' in text and "Ann Stone (person)" in text
    assert sources == [str(vault / "data_in" / "falcon" / "notes.csv")]
    # asked by name, whatever the flag
    gl.update(info.id, use_in_answers=False)
    assert kq.gather(vault, "what does Ann Stone own?", graphs=[info.id])


def test_a_graph_never_built_is_not_created_by_asking(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    assert kq.block(v, "who is Ngozi Okafor?") is None
    assert not (v / ".knowledge_graph").exists()


def test_a_busy_store_is_skipped(vault, monkeypatch):
    def busy(*a, **k):
        raise kgm.KnowledgeGraphBusy("being rebuilt")
    monkeypatch.setattr(kgg.GraphList, "open", busy)
    assert kq.block(vault, "who is Ngozi Okafor?") is None


def test_a_big_neighbourhood_is_capped(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    rows = "".join(f"KA-1,PN-{i:04d},Ann Stone\n" for i in range(200))
    (v / "data_in" / "parts.csv").write_text("Project,Part Number,Owner\n" + rows,
                                             encoding="utf-8")
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        facts = kq.neighbourhood(kg, [kg.search("Ann Stone")[0]["id"]], hops=2,
                                 max_facts=40)
        # 15 from Ann Stone, 15 from KA-1: no one entity fills the block
        assert [f.hop for f in facts].count(1) == 15 and len(facts) == 30
        assert len(kq.neighbourhood(kg, [kg.search("Ann Stone")[0]["id"]], hops=2,
                                    max_facts=20)) == 20
    text, _ = kq.block(v, "what does Ann Stone own?", max_chars=1500)
    assert len(text) < 1600 and text.endswith("(… more links in the graph)")


@pytest.mark.parametrize("pred, way, want", [
    ("LEADS", "out", "leads"), ("LEADS", "in", "led by"),
    ("SUPPLIES", "out", "supplies"), ("SUPPLIES", "in", "← supplies"),
    ("LOCATED_IN", "out", "located in"),
])
def test_a_link_reads_from_each_end(pred, way, want):
    assert kq.phrase(pred, way) == want
