"""The knowledge graph (council_core/knowledge_graph.py), scored against the
synthetic Ironbridge vault (council_core/kg_corpus.py) and its answer key.

Every test builds its own vault under tmp_path; nothing touches a real vault.
The xlsx and PDF documents need openpyxl and pypdf; tests that need every
document skip when those are missing (the council env lacks both), and one
test checks that the graph then SAYS those files were not read.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from council_core import kg_corpus
from council_core import knowledge_graph as kgm


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


needs_all_readers = pytest.mark.skipif(
    not (_has("openpyxl") and _has("pypdf")),
    reason="the corpus's xlsx/PDF need openpyxl and pypdf")


def _vault(tmp_path, name="vault"):
    v = tmp_path / name
    key = kg_corpus.build(v)
    return v, key


def _graph(v, *, confirm=True, poc=False):
    kg = kgm.KnowledgeGraph(v)
    if confirm:
        kg.confirm_all_rules()
    if poc:
        kg.add_label_synonym("POC", "Point of Contact")
    return kg


def _truth_key_of(key, ent):
    for k, e in key["entities"].items():
        if e["type"] == ent["type"] and kgm.entity_key(e["type"], e["name"]) == ent["key"]:
            return k
    return "?" + ent["name"]


def _found(kg, key):
    return {(_truth_key_of(key, r["subject"]), r["predicate"],
             _truth_key_of(key, r["object"])): r["status"]
            for r in kg.all_relations() if r["predicate"] != "DOCUMENTED_IN"}


def _truth(key, *, evidence):
    out = set()
    for r in key["relations"]:
        if r["evidence"] in evidence:
            out.add((r["subject"], r["predicate"], r["object"]))
    return out


def _tree_hashes(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ── names and ids ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, display, key", [
    ("Lee, Carol", "Carol Lee", "carol lee"),
    ("Carol Lee", "Carol Lee", "carol lee"),
    ("Bob Smith, Jr.", "Bob Smith, Jr.", "bob smith"),
    ("Tomás Echeverría", "Tomás Echeverría", "tomas echeverria"),
    ("Whitfield, D.", "D. Whitfield", "d whitfield"),
])
def test_person_names(raw, display, key):
    assert kgm.person_display(raw) == display
    assert kgm.person_key(raw) == key


def test_ids_are_stable_hashes_not_names():
    a = kgm.entity_id("PERSON", kgm.person_key("Lee, Carol"))
    assert a == kgm.entity_id("PERSON", kgm.person_key("Carol Lee"))
    assert "carol" not in a.lower()
    assert kgm.entity_key("PART", " pn-1234/a ") == "PN-1234/A"
    assert kgm.entity_key("PROJECT", "PRJ-0915") == "PRJ-0915"
    assert kgm.entity_key("PROJECT", "Helios Turbine Upgrade") == "helios turbine upgrade"


# ── the corpus ───────────────────────────────────────────────────────────
def test_corpus_refuses_to_overwrite(tmp_path):
    v, _key = _vault(tmp_path)
    with pytest.raises(FileExistsError):
        kg_corpus.build(v)


def test_corpus_docx_reads_without_python_docx(tmp_path):
    import vault_rag
    v, _ = _vault(tmp_path)
    text = vault_rag._docx_text_stdlib(
        v / "data_in/ironbridge/reports/northwind_design_review.docx")
    assert "Attendees: Carol Lee, Tomás Echeverría, Bob Smith, Jr." in text


# ── seeding, scored ──────────────────────────────────────────────────────
@needs_all_readers
def test_seed_finds_every_labelled_link_and_nothing_wrong(tmp_path):
    v, key = _vault(tmp_path)
    kg = _graph(v)
    stats = kg.seed()
    assert stats["unreadable"] == []
    found = _found(kg, key)
    labelled = _truth(key, evidence={"row", "record"})
    # The two links behind the drifted 'POC' label are not there until the
    # user confirms it.
    needs_poc = {("bob", "CONTACT_FOR", "northwind"), ("marcus", "CONTACT_FOR", "bands")}
    assert labelled - needs_poc <= set(found)
    assert not (needs_poc & set(found))
    wrong = {t for t in found if t not in _truth(key, evidence={"row", "record", "free_text"})}
    assert wrong == set()
    # No model: links that only prose states are not invented.
    free_only = _truth(key, evidence={"free_text"}) - labelled
    assert not (free_only & set(found))


@needs_all_readers
def test_confirmed_synonym_completes_the_graph(tmp_path):
    v, key = _vault(tmp_path)
    kg = _graph(v, poc=True)
    kg.seed()
    found = _found(kg, key)
    assert _truth(key, evidence={"row", "record"}) <= set(found)
    # Marcus LEADS the program in the tracker; only 'M. Oyelaran' in the POC
    # column makes him its contact, and that name was inferred, so the link
    # waits for the user.
    assert found[("marcus", "CONTACT_FOR", "bands")] == "suggested"
    rel = [r for r in kg.all_relations()
           if r["subject"]["name"] == "Bob Smith, Jr." and r["predicate"] == "CONTACT_FOR"]
    assert [r["status"] for r in rel] == ["seeded"]
    kinds = {(r["kind"], r["surface"]) for r in kg.reviews()}
    assert ("inferred_alias", "M. Oyelaran") in kinds


def test_inferred_name_link_stays_suggested(tmp_path):
    root = tmp_path / "v" / "data_in"
    root.mkdir(parents=True)
    (root / "a.csv").write_text("Project,Owner\nPRJ-1,Marcus Oyelaran\n", encoding="utf-8")
    (root / "b.csv").write_text("Project,Point of Contact\nPRJ-2,M. Oyelaran\n",
                                encoding="utf-8")
    kg = _graph(tmp_path / "v")
    kg.seed()
    st = {(r["object"]["name"], r["predicate"]): r["status"] for r in kg.all_relations()}
    assert st[("PRJ-1", "LEADS")] == "seeded"
    assert st[("PRJ-2", "CONTACT_FOR")] == "suggested"


def test_proposed_rules_are_not_used_until_confirmed(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v, confirm=False)
    assert {s for _r, s in kg.field_rules()} == {"proposed"}
    stats = kg.seed()
    assert stats["records"] == 0
    assert kg.all_relations() and {r["predicate"] for r in kg.all_relations()} \
        == {"DOCUMENTED_IN"}      # only the user's own Collections


def test_drifted_label_is_suggested_not_assumed(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    sug = {s["label"]: s for s in kg.suggest_label_synonyms()}
    assert sug["POC"]["means"] == "Point of Contact"
    assert sug["POC"]["precheck"] is False          # 0.80 < 0.85
    assert all(len(lbl.split()) <= 5 for lbl in sug)


@needs_all_readers
def test_evidence_has_sheet_row_page_and_line(tmp_path):
    v, key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    leads = [n for n in kg.neighbors(dana["id"]) if n["predicate"] == "LEADS"]
    where = {(e["path"].split("/")[-1], e["where"]) for n in leads for e in n["evidence"]}
    assert ("program_tracker.xlsx", "sheet Projects, row 2 (Program Lead, Project ID)") in where
    assert ("helios_status_2026-03.md", "line 4") in where
    shim_b = kg.search("PN-1234/B", "PART")[0]
    sup = [n for n in kg.neighbors(shim_b["id"]) if n["predicate"] == "SUPERSEDES"][0]
    ecn = [e for e in sup["evidence"] if e["path"].endswith(".pdf")][0]
    assert ecn["locator"] == {"page": 2, "line": 5}
    assert "Supersedes: PN-1234/A" in ecn["quote"]


@needs_all_readers
def test_ambiguous_initial_is_a_question_not_a_guess(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    amb = [r for r in kg.reviews() if r["kind"] == "ambiguous_name"]
    assert len(amb) == 1
    assert amb[0]["surface"] == "D. Whitfield"
    assert sorted(c["name"] for c in amb[0]["candidates"]) == ["Dan Whitfield",
                                                                "Dana Whitfield"]
    assert amb[0]["path"].endswith("helios_status_2026-03.md")
    assert amb[0]["where"] == "line 13"


def test_collections_link_projects_to_documents(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    docs = {(r["subject"]["name"], r["object"]["name"].split("/")[-1])
            for r in kg.all_relations() if r["predicate"] == "DOCUMENTED_IN"}
    if _has("openpyxl"):       # the tracker ties 'Helios' to PRJ-0915
        assert ("PRJ-0915", "helios_status_2026-03.md") in docs
        assert ("PRJ-0931", "northwind_design_review.docx") in docs
    else:
        # Without the tracker nothing ties 'Northwind' to PRJ-0931, so each
        # Collection stands as a project of its own rather than a guess.
        assert ("Northwind", "northwind_design_review.docx") in docs
        assert ("Helios", "helios_status_2026-03.md") in docs


# ── rebuilds, decisions, safety ──────────────────────────────────────────
def test_rebuild_is_stable_and_keeps_decisions(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    first = kg.seed()
    rels = {r["id"]: r for r in kg.all_relations()}
    target = next(r for r in rels.values() if r["predicate"] == "OWNS")
    kg.set_relation_status(target["id"], "rejected", note="wrong owner")
    second = kg.seed()
    assert first["entities"] == second["entities"]
    after = {r["id"]: r for r in kg.all_relations(include_rejected=True)}
    assert set(after) == set(rels)
    assert after[target["id"]]["status"] == "rejected"
    assert target["id"] not in {r["id"] for r in kg.all_relations()}
    assert [d["action"] for d in kg.decisions()][-1] == "relation_rejected"


def test_ids_match_across_two_vaults(tmp_path):
    a = _graph(_vault(tmp_path, "a")[0])
    b = _graph(_vault(tmp_path, "b")[0])
    a.seed()
    b.seed()
    ids = lambda kg: sorted(r[0] for r in kg.db.execute(   # noqa: E731
        "SELECT id FROM entities WHERE type != 'DOCUMENT'"))
    assert ids(a) == ids(b)
    assert sorted(r["id"] for r in a.all_relations() if r["predicate"] != "DOCUMENTED_IN") \
        == sorted(r["id"] for r in b.all_relations() if r["predicate"] != "DOCUMENTED_IN")


def test_changed_document_drops_its_old_facts(tmp_path):
    root = tmp_path / "v" / "data_in"
    root.mkdir(parents=True)
    f = root / "parts.csv"
    f.write_text("P/N,Owner\nPN-1,Ann Moss\n", encoding="utf-8")
    kg = _graph(tmp_path / "v")
    kg.seed()
    assert [(r["subject"]["name"], r["object"]["name"]) for r in kg.all_relations()] \
        == [("Ann Moss", "PN-1")]
    f.write_text("P/N,Owner\nPN-1,Raj Patel\n", encoding="utf-8")
    kg.seed()
    assert [(r["subject"]["name"], r["object"]["name"]) for r in kg.all_relations()] \
        == [("Raj Patel", "PN-1")]
    assert kg.search("Ann Moss") == []      # nothing supports her any more


def test_seeding_never_changes_the_vault(tmp_path):
    v, _key = _vault(tmp_path)
    before = _tree_hashes(v)
    kg = _graph(v, poc=True)
    kg.seed()
    kg.write_exports()
    after = {k: h for k, h in _tree_hashes(v).items()
             if not k.startswith(kgm.STORE_DIR + "/")}
    assert after == before
    assert (v / kgm.STORE_DIR / kgm.STORE_NAME).exists()


def test_store_is_hidden_from_vault_searches(tmp_path):
    import conversation_logger as cl
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    pj, pc = kg.write_exports()
    assert cl.is_protected_path(pj, v) and cl.is_protected_path(pc, v)
    assert json.loads(pj.read_text(encoding="utf-8"))["schema_version"] == kgm.SCHEMA_VERSION
    assert pc.read_text(encoding="utf-8").startswith("subject_type,subject,predicate")


def test_damaged_store_is_reported_and_left_alone(tmp_path):
    v = tmp_path / "v"
    p = kgm.store_path(v)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"this is not a database" * 100)
    before = p.read_bytes()
    with pytest.raises(kgm.KnowledgeGraphDamaged):
        kgm.KnowledgeGraph(v)
    assert p.read_bytes() == before


def test_newer_schema_is_refused(tmp_path):
    v = tmp_path / "v"
    kgm.KnowledgeGraph(v).close()
    db = sqlite3.connect(kgm.store_path(v))
    db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    db.commit()
    db.close()
    with pytest.raises(kgm.KnowledgeGraphDamaged, match="newer"):
        kgm.KnowledgeGraph(v)


def test_missing_reader_is_reported_not_silent(tmp_path, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name in ("openpyxl", "pypdf")
                        else real(name, *a))
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    stats = kg.seed()
    bad = sorted(u.split(":")[0].split("/")[-1] for u in stats["unreadable"])
    assert bad == ["ecn_0915_014.pdf", "program_tracker.xlsx"]
    assert len(kg.coverage()["unreadable"]) == 2


# ── reading the graph ────────────────────────────────────────────────────
def test_search_is_accent_and_order_insensitive(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    assert kg.search("tomas")[0]["name"] == "Tomás Echeverría"
    assert kg.search("Lee, Carol")[0]["name"] == "Carol Lee"
    assert kg.search("pn-1234", "PART")[0]["name"].startswith("PN-1234/")


def test_gazetteer_finds_unlabelled_mentions(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    brg = kg.search("BRG-7720", "PART")[0]
    hits = {(m["path"].split("/")[-1], m["where"], m["method"]) for m in kg.mentions(brg["id"])}
    # Only prose links the bearing to Helios: found as a mention, not a link.
    assert ("helios_status_2026-03.md", "line 11", "gazetteer") in hits
    helios_link = [n for n in kg.neighbors(brg["id"])
                   if (n["other"] or {}).get("name") == "PRJ-0915"]
    assert helios_link == []


def test_locator_text():
    assert kgm.locator_text({"sheet": "Projects", "row": 2, "column": "Owner"}) \
        == "sheet Projects, row 2 (Owner)"
    assert kgm.locator_text('{"page": 1, "line": 5}') == "page 1, line 5"
    assert kgm.locator_text({}) == "whole document"


def test_a_spot_is_cited_once(tmp_path):
    v, _key = _vault(tmp_path)
    kg = _graph(v)
    kg.seed()
    for eid, in kg.db.execute("SELECT id FROM entities WHERE type != 'DOCUMENT'"):
        spots = [(m["path"], m["where"]) for m in kg.mentions(eid)]
        assert len(spots) == len(set(spots))


# ── review fixes (merge of knowledge-graph into qt-migration) ─────────────
def _tiny(tmp_path, files, name="tiny"):
    """A vault whose data_in holds exactly ``files`` ({relative path: text or
    bytes}), every rule confirmed."""
    v = tmp_path / name
    for rel, body in files.items():
        f = v / "data_in" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            f.write_bytes(body)
        else:
            f.write_text(body, encoding="utf-8")
    return v


def _people(kg):
    return sorted(r[0] for r in kg.db.execute(
        "SELECT name FROM entities WHERE type='PERSON'"))


def _names(kg, etype):
    return sorted(r[0] for r in kg.db.execute(
        "SELECT name FROM entities WHERE type=?", (etype,)))


def test_a_list_of_people_and_a_pair_of_projects_stay_separate(tmp_path):
    v = _tiny(tmp_path, {"tracker.csv": 'Project,Point of Contact\n'
                                         'Northwind,"Alice, Bob and Carol"\n'
                                         '"Helios, Atlas","Lee, Carol"\n'})
    with _graph(v) as kg:
        kg.seed()
        assert _people(kg) == ["Alice", "Bob", "Carol", "Carol Lee"]
        assert _names(kg, "PROJECT") == ["Atlas", "Helios", "Northwind"]


def _links(kg):
    return sorted((r["subject"]["name"], r["predicate"], r["object"]["name"], r["status"])
                  for r in kg.all_relations() if r["predicate"] != "DOCUMENTED_IN")


def test_text_labels_must_be_the_rule_label_not_contain_it(tmp_path):
    # 'Project Manager' is not a 'Project', 'Part Qty' not a 'Part', 'Owner
    # Email' not an 'Owner' — the table reader already required the exact
    # header; text read any key up to 3 words longer than the label.
    v = _tiny(tmp_path, {"status.md": "Project: PRJ-1\nProject Manager: Ann Stone\n"
                                      "Project Status: Green\n"
                                      "Part Description: Bearing housing\nPart Qty: 4\n"
                                      "Program Lead: Carol Lee\n"
                                      "Owner Email: carol.lee@example.com\n"})
    with _graph(v) as kg:
        kg.seed()
        assert _names(kg, "PROJECT") == ["PRJ-1"]
        assert _names(kg, "PART") == []
        assert _people(kg) == ["Carol Lee"]
        assert _links(kg) == [("Carol Lee", "LEADS", "PRJ-1", "seeded")]


def test_a_json_value_is_cited_on_its_own_line(tmp_path):
    import field_search as fs
    body = json.dumps([{"Project": "PRJ-9", "Part": "PN-1234/A",
                        "Supersedes": "PN-1234"}], indent=2)
    v = _tiny(tmp_path, {"parts.json": body})
    lines = body.splitlines()
    want = next(i for i, ln in enumerate(lines, 1) if '"Supersedes"' in ln)
    assert [(l["value"], l["line"]) for l in
            fs.field_value_locations(v / "data_in" / "parts.json", "Supersedes")] \
        == [("PN-1234", want)]
    with _graph(v) as kg:
        kg.seed()
        sup = next(r for r in kg.all_relations() if r["predicate"] == "SUPERSEDES")
        ev = kg.evidence(sup["id"])
        assert [e["locator"].get("line") for e in ev] == [want]
