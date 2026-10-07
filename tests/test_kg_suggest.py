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


# ── review fixes (2026-10-07) ─────────────────────────────────────────────
def test_passages_that_failed_are_read_again(kg):
    # Ollama not running: every passage errored, and was recorded as done, so
    # a later run with Ollama up read nothing until the documents changed.
    def down(messages, **kw):
        raise ConnectionError("Ollama is not running")
    first = kg.suggest_from_text({"pc": down}, model="gemma3:12b")
    assert first["chunks"] > 0 and first["errors"] == first["chunks"]
    again = kg.suggest_from_text({"pc": oracle()}, model="gemma3:12b")
    assert again["chunks"] == first["chunks"] and again["errors"] == 0


def test_each_worker_records_the_model_that_read_the_passage(kg):
    # A Pi worker reads with its OWN model (llama3.2:3b); its passages were
    # recorded as read by the PC's model, so gemma3:12b never read them.
    total = len(kg.text_chunks())
    s1 = kg.suggest_from_text({"NodePrimus": (oracle(), "llama3.2:3b")}, model="gemma3:12b")
    assert s1["done"] == total
    models = {r[0] for r in kg.db.execute("SELECT model FROM extraction_done")}
    assert models == {"llama3.2:3b"}
    ev_models = {e["model"] for r in kg.all_relations() for e in kg.evidence(r["id"])
                 if e["method"] == "model"}
    assert ev_models == {"llama3.2:3b @ NodePrimus"}
    kinds = [r[0] for r in kg.db.execute("SELECT kind FROM runs WHERE kind LIKE 'extract%'")]
    assert kinds == ["extract:gemma3:12b"]
    # The PC's stronger model has not read them: it does now ...
    s2 = kg.suggest_from_text({"this PC": (oracle(), "gemma3:12b")}, model="gemma3:12b")
    assert s2["chunks"] == total
    # ... and a run with both has nothing left to do.
    s3 = kg.suggest_from_text({"this PC": (oracle(), "gemma3:12b"),
                               "NodePrimus": (oracle(), "llama3.2:3b")}, model="gemma3:12b")
    assert s3["chunks"] == 0


def test_a_resumed_run_does_not_read_finished_documents_again(kg):
    # MEASURED (review, 3,000 documents): a second run with every passage
    # done took 119 s to return chunks=0 - every document was read and cut
    # into passages before extraction_done was looked at.
    first = kg.suggest_from_text({"pc": oracle()}, model="o")
    assert first["chunks"] > 0 and first["documents_read"] >= 3
    again = kg.suggest_from_text({"pc": oracle()}, model="o")
    assert again["chunks"] == 0 and again["documents_read"] == 0
    assert again["documents_skipped"] == first["documents_read"]
    # A document whose passages change (a new name in it) is read again.
    meeting = kg.root / "ironbridge" / "notes" / "meeting_2026-04-02.txt"
    meeting.write_text(meeting.read_text(encoding="utf-8")
                       + "\nPriya Raman will join the Atlas Test Rig review.\n",
                       encoding="utf-8")
    kg.seed()
    third = kg.suggest_from_text({"pc": oracle()}, model="o")
    assert third["chunks"] > 0 and third["documents_read"] == 1


def test_mentions_are_found_by_document_through_an_index(kg):
    plan = " ".join(str(r[-1]) for r in kg.db.execute(
        "EXPLAIN QUERY PLAN SELECT entity_id, locator FROM mentions WHERE document_id=?",
        ("x",)))
    assert "USING INDEX" in plan and "mentions_document" in plan


def test_a_shorter_project_name_inside_a_longer_one_is_still_offered(kg):
    # The project terms are matched through an index of first words; a
    # project named 'Atlas' must still be offered where the text says
    # 'Atlas Test Rig' (another project's name could start the same way).
    pid = kg._entity("PROJECT", "PRJ-0999")
    kg._alias(pid, "Atlas Test", "user")
    kg.db.commit()
    terms = kg._project_index()
    text = "Hana Kowalski will run the Atlas Test Rig on Monday with Dan Whitfield."
    hits = kg._projects_named(text, terms)
    atlas = kg.search("PRJ-0944", "PROJECT")[0]["id"]
    assert {atlas, pid} <= hits


def test_model_evidence_from_a_deleted_file_goes(kg):
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    assert ("Dan Whitfield", "WORKS_ON", "PRJ-0944") in _model_links(kg)
    (kg.root / "ironbridge" / "notes" / "meeting_2026-04-02.txt").unlink()
    kg.seed()
    assert ("Dan Whitfield", "WORKS_ON", "PRJ-0944") not in _model_links(kg)


# ── review of 2026-10-07: a model's links are never lost for good ─────────
DAN = ("Dan Whitfield", "WORKS_ON", "PRJ-0944")


def _meeting(kg):
    return kg.root / "ironbridge" / "notes" / "meeting_2026-04-02.txt"


def test_a_file_moved_out_and_back_is_read_again_and_its_links_come_back(kg):
    # Its model evidence went while it was gone, but its passages still
    # counted as read: 'chunks=0, documents_skipped=1', the link never came
    # back, and the tab only ever asks for new passages.
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    away = kg.vault / "away.txt"
    _meeting(kg).replace(away)
    kg.seed()
    assert DAN not in _model_links(kg)
    away.replace(_meeting(kg))
    kg.seed()
    st = kg.suggest_from_text({"pc": oracle()}, model="oracle")
    assert st["chunks"] >= 1 and DAN in _model_links(kg)


def test_an_edit_undone_is_read_again(kg):
    p = _meeting(kg)
    original = p.read_bytes()
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    p.write_text("Nothing was decided.\n", encoding="utf-8")
    kg.seed()
    assert DAN not in _model_links(kg)
    p.write_bytes(original)
    kg.seed()
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    assert DAN in _model_links(kg)


@pytest.mark.skipif(__import__("sys").platform != "win32",
                    reason="an exclusive lock is a Windows share mode")
def test_a_file_locked_for_one_rebuild_keeps_its_model_links(kg):
    # Held open by another program (no sharing), the file could not be
    # hashed, was marked 'missing', and its model links were deleted.
    import ctypes
    from ctypes import wintypes
    kg.suggest_from_text({"pc": oracle()}, model="oracle")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(_meeting(kg)), 0x80000000, 0, None, 3, 0x80, None)
    assert h not in (None, wintypes.HANDLE(-1).value)
    try:
        st = kg.seed()
    finally:
        k32.CloseHandle(h)
    rel = _meeting(kg).relative_to(kg.root).as_posix()
    assert any(u.startswith(rel) for u in st["unreadable"])
    assert kg.db.execute("SELECT status FROM documents WHERE path=?",
                         (rel,)).fetchone()[0] == "unreadable"
    assert DAN in _model_links(kg)
    kg.seed()
    assert DAN in _model_links(kg)
    assert kg.suggest_from_text({"pc": oracle()}, model="oracle")["chunks"] == 0
