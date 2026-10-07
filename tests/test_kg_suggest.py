"""KG2 — free-text extraction into the graph (KnowledgeGraph.suggest_from_text),
on the synthetic Ironbridge vault, with scripted "models" that answer from the
corpus's answer key. Nothing calls a real model."""
from __future__ import annotations

import json
import threading

import pytest

from council_core import kg_corpus
from council_core import knowledge_graph as kgm

needs_all = pytest.mark.skipif(not (kgm.missing_reader.__module__ and
                                    __import__("importlib").util.find_spec("pypdf")),
                               reason="the corpus PDF needs pypdf")

FREE = [(s, p, o, f) for s, p, o, ev, f in kg_corpus.RELATIONS if ev == "free_text"]
NAMES = {k: n for k, (_t, n, _a) in kg_corpus.ENTITIES.items()}


def oracle(calls=None):
    """Answers each chunk with the corpus's free-text links whose sentence is in it."""
    sentences = {
        ("helios", "USES_PART", "brg"): "The BRG-7720 bearing pack from the Bearings & Seals Program will also be fitted to the Helios rig",
        ("tomas", "WORKS_ON", "helios"): "D. Whitfield will review the shim drawings with Tomás Echeverría next week.",
        ("atlas", "USES_PART", "shim_b"): "The revised shim PN-1234/B will be proven on the Atlas Test Rig",
        ("dan", "WORKS_ON", "atlas"): "Dan Whitfield reported a firmware fault on the CTL-5005 controller board.",
        ("bob", "OWNS", "gasket"): "Bob Smith will source the PN-0088 inlet gaskets for Northwind.",
    }

    def chat(messages, **kw):
        user = messages[-1]["content"]
        text = user.split('"""')[1]
        flat = " ".join(text.split())
        links = []
        for (s, p, o), sent in sentences.items():
            if " ".join(sent.split()) in flat:
                links.append({"subject": NAMES[s], "predicate": p, "object": NAMES[o],
                              "quote": sent})
        if calls is not None:
            calls.append(threading.current_thread().name)
        return json.dumps({"links": links})
    return chat


@pytest.fixture
def kg(tmp_path):
    v = tmp_path / "vault"
    kg_corpus.build(v)
    g = kgm.KnowledgeGraph(v)
    g.confirm_all_rules()
    g.seed()
    yield g
    g.close()


def _model_links(kg):
    out = {}
    for r in kg.all_relations():
        for ev in kg.evidence(r["id"]):
            if ev["method"] == "model":
                out[(r["subject"]["name"], r["predicate"], r["object"]["name"])] = (r["status"], ev)
    return out


def test_only_chunks_with_two_known_entities_go_to_a_model(kg):
    paths = {c["path"].split("/")[-1] for c in kg.text_chunks()}
    assert "break_room.txt" not in paths
    assert {"helios_status_2026-03.md", "meeting_2026-04-02.txt",
            "northwind_design_review.docx"} <= paths
    assert not any(p.endswith((".csv", ".xlsx", ".json")) for p in paths)


def test_the_documents_project_is_offered_even_by_a_one_word_name(kg):
    chunk = next(c for c in kg.text_chunks() if c["path"].endswith("helios_status_2026-03.md"))
    names = {k.name for k in kg.known_entities(chunk["entities"])}
    assert {"PRJ-0915", "BRG-7720"} <= names


@needs_all
def test_free_text_links_arrive_as_suggestions_with_their_line(kg):
    stats = kg.suggest_from_text({"this PC": oracle()}, model="oracle")
    assert stats["errors"] == 0 and stats["links"] >= 4
    got = _model_links(kg)
    for s, p, o, _f in FREE:
        key = (NAMES[s], p, NAMES[o])
        assert key in got, key
    status, ev = got[("PRJ-0915", "USES_PART", "BRG-7720")]
    assert status == "suggested"
    assert ev["path"].endswith("helios_status_2026-03.md") and ev["locator"] == {"line": 11}
    assert ev["model"] == "oracle @ this PC"
    _st, pdf_ev = got[("PRJ-0944", "USES_PART", "PN-1234/B")]
    assert pdf_ev["locator"]["page"] == 3


def test_a_link_already_known_from_labels_stays_seeded(kg):
    # 'Bob WORKS_ON Northwind' comes from the docx Attendees field; a model
    # saying it too adds evidence, it does not demote it.
    def chat(messages, **kw):
        return json.dumps({"links": [{"subject": "Bob Smith", "predicate": "WORKS_ON",
                                      "object": "PRJ-0931",
                                      "quote": "Bob Smith will source the PN-0088 inlet gaskets for Northwind."}]})
    kg.suggest_from_text({"pc": chat}, model="m")
    rel = next(r for r in kg.all_relations() if r["subject"]["name"] == "Bob Smith, Jr."
               and r["predicate"] == "WORKS_ON")
    assert rel["status"] == "seeded"
    assert {e["method"] for e in kg.evidence(rel["id"])} >= {"document", "model"}


def test_resumes_and_skips_what_is_done(kg):
    first = kg.suggest_from_text({"pc": oracle()}, model="oracle")
    again = kg.suggest_from_text({"pc": oracle()}, model="oracle")
    assert first["chunks"] > 0 and again["chunks"] == 0
    other = kg.suggest_from_text({"pc": oracle()}, model="another-model")
    assert other["chunks"] == first["chunks"]


def test_stop_then_continue(kg):
    total = len(kg.text_chunks())
    done = []

    def stop_after_one():
        return len(done) >= 1
    s1 = kg.suggest_from_text({"pc": oracle(done)}, model="o", should_stop=stop_after_one)
    assert s1["stopped"] and s1["done"] == 1
    s2 = kg.suggest_from_text({"pc": oracle()}, model="o")
    assert s2["chunks"] == total - 1 and not s2["stopped"]


def test_work_is_shared_between_workers(kg):
    calls = []
    stats = kg.suggest_from_text({"this PC": oracle(calls), "NodePrimus": oracle(calls)},
                                 model="o")
    assert sum(stats["by_worker"].values()) == stats["done"] == stats["chunks"]
    assert set(stats["by_worker"]) <= {"this PC", "NodePrimus"}


def test_an_edited_document_drops_its_stale_suggestions(kg):
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    assert ("Dan Whitfield", "WORKS_ON", "PRJ-0944") in _model_links(kg)
    meeting = kg.root / "ironbridge" / "notes" / "meeting_2026-04-02.txt"
    meeting.write_text("Atlas sync cancelled.\n", encoding="utf-8")
    kg.seed()
    assert ("Dan Whitfield", "WORKS_ON", "PRJ-0944") not in _model_links(kg)
    # ...and the changed file is read again next time.
    assert any(c["path"].endswith("helios_status_2026-03.md") for c in kg.text_chunks())


def test_rejected_model_link_stays_rejected(kg):
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    rel = next(r for r in kg.all_relations() if r["subject"]["name"] == "Dan Whitfield"
               and r["predicate"] == "WORKS_ON")
    kg.set_relation_status(rel["id"], "rejected")
    kg.suggest_from_text({"pc": oracle()}, model="oracle-2")
    kg.seed()
    st = kg.db.execute("SELECT status FROM relations WHERE id=?", (rel["id"],)).fetchone()[0]
    assert st == "rejected"


def test_a_failing_model_is_counted_not_fatal(kg):
    stats = kg.suggest_from_text({"pc": lambda m, **k: "not json"}, model="broken")
    assert stats["errors"] == stats["done"] > 0 and stats["links"] == 0
