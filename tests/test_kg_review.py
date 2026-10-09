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


# ── review fixes (2026-10-07) ─────────────────────────────────────────────
def _caroline_with_a_rejected_link(kg):
    """A duplicate 'Caroline Lee' whose model link to PRJ-0915 the user
    rejected, while Carol Lee's own link to PRJ-0915 comes from labels."""
    kg._entity("PERSON", "Caroline Lee")
    kg.db.commit()
    caroline = person(kg, "Caroline Lee")
    proj = kg.search("PRJ-0915", "PROJECT")[0]["id"]
    doc = kg.db.execute("SELECT id, content_hash FROM documents WHERE path LIKE"
                        " '%helios_status_2026-03.md'").fetchone()
    kg._relate(caroline, "CONTACT_FOR", proj, "suggested", doc_id=doc[0],
               content_hash=doc[1], locator={"line": 4}, quote="q", method="model",
               run_id="r", model="m @ pc")
    kg.db.commit()
    rid = next(r["id"] for r in kg.all_relations() if r["subject"]["name"] == "Caroline Lee")
    kg.set_relation_status(rid, "rejected")
    return person(kg, "Carol Lee"), caroline


def _statuses(kg):
    return {(r["subject"]["name"], r["predicate"], r["object"]["name"]): r["status"]
            for r in kg.all_relations(include_rejected=True)}


def test_a_link_only_a_model_supports_is_a_suggestion(kg):
    # Carol Lee CONTACT_FOR PRJ-0915 is seeded from labels; a model says it
    # too; then the label rules are withdrawn. The desktop's seed() kept it
    # 'seeded' with only the model's evidence - firm, and never put to the
    # user. (Fixed by the laptop's re-derived statuses, kept in the merge.)
    import json as _json
    note = kg.root / "ironbridge" / "notes" / "helios_contact.txt"
    sent = "Carol Lee is the point of contact for the Helios Turbine Upgrade and PRJ-0915."
    note.write_text("Helios contacts\n" + sent + "\n", encoding="utf-8")
    kg.seed()

    def says_contact(messages, **kw):
        text = messages[-1]["content"].split('"""')[1]
        links = ([{"subject": "Carol Lee", "predicate": "CONTACT_FOR", "object": "PRJ-0915",
                   "quote": sent}] if sent in text else [])
        return _json.dumps({"links": links})
    kg.suggest_from_text({"pc": says_contact}, model="m")
    assert rel_status(kg, "Carol Lee", "CONTACT_FOR", "PRJ-0915") == "seeded"
    kg.set_rule_status("Point of Contact", "rejected")
    kg.set_rule_status("POC", "rejected")
    kg.seed()
    assert rel_status(kg, "Carol Lee", "CONTACT_FOR", "PRJ-0915") == "suggested"


def test_merge_never_overwrites_the_survivors_own_link_and_asks_instead(kg):
    carol, caroline = _caroline_with_a_rejected_link(kg)
    assert rel_status(kg, "Carol Lee", "CONTACT_FOR", "PRJ-0915") == "seeded"
    clashes = kg.merge(carol, caroline)
    # The user rejected CAROLINE's link, never Carol's: hers stays, and the
    # clash is handed back for the tab to ask about.
    assert rel_status(kg, "Carol Lee", "CONTACT_FOR", "PRJ-0915") == "seeded"
    assert [(c["predicate"], c["kept"], c["theirs"]) for c in clashes] == [
        ("CONTACT_FOR", "seeded", "rejected")]
    kg.unmerge(caroline)
    assert rel_status(kg, "Caroline Lee", "CONTACT_FOR", "PRJ-0915") == "rejected"
    kg.seed()
    assert rel_status(kg, "Carol Lee", "CONTACT_FOR", "PRJ-0915") == "seeded"
    assert rel_status(kg, "Caroline Lee", "CONTACT_FOR", "PRJ-0915") == "rejected"


def test_merge_then_unmerge_then_rebuild_restores_every_status(kg):
    carol, caroline = _caroline_with_a_rejected_link(kg)
    sup = next(r["id"] for r in kg.all_relations() if r["predicate"] == "SUPERSEDES"
               and r["subject"]["name"] == "PN-1234/B")
    kg.set_relation_status(sup, "accepted")
    kg.seed()
    before = _statuses(kg)
    assert before[("PN-1234/B", "SUPERSEDES", "PN-1234/A")] == "accepted"
    shim_a = kg.search("PN-1234/A", "PART")[0]["id"]
    shim_b = kg.search("PN-1234/B", "PART")[0]["id"]
    kg.merge(carol, caroline)
    kg.merge(shim_b, shim_a)          # their accepted SUPERSEDES becomes a loop
    kg.seed()                         # a rebuild while merged
    assert not any(r["subject"]["id"] == r["object"]["id"]
                   for r in kg.all_relations(include_rejected=True))
    kg.unmerge(caroline)
    kg.unmerge(shim_a)
    kg.seed()
    assert _statuses(kg) == before


def test_unmerge_keeps_an_earlier_merge_into_the_split_entry(kg):
    for n in ("Alpha Person", "Beta Person", "Gamma Person"):
        kg._entity("PERSON", n)
    kg.db.commit()
    a, b, c = (person(kg, n) for n in ("Alpha Person", "Beta Person", "Gamma Person"))
    kg.merge(b, a)
    kg.merge(c, b)
    assert kg.resolve(a) == c
    kg.unmerge(b)
    assert kg.resolve(a) == b and kg.resolve(b) == b


def test_unmerge_takes_back_the_names_merge_copied(kg):
    kg._entity("PERSON", "Caroline Lee")
    kg.db.commit()
    caroline, carol = person(kg, "Caroline Lee"), person(kg, "Carol Lee")
    kg._alias(caroline, "Caz", "user")
    kg.db.commit()
    kg.merge(carol, caroline)
    assert "Caz" in kg.entity(carol)["aliases"]
    kg.unmerge(caroline)
    kg.seed()
    owners = [kg.entity(r[0])["name"] for r in
              kg.db.execute("SELECT entity_id FROM aliases WHERE alias='Caz'")]
    assert owners == ["Caroline Lee"]


def test_an_answer_about_a_person_who_is_gone_is_asked_again(tmp_path):
    # 'D. Whitfield' = Dana everywhere; Dana then leaves the documents and a
    # rebuild deletes her; a new note names 'D. Whitfield' again. The answer
    # pointed at the deleted id: links and mentions with no entity, and the
    # CSV export crashed.
    v = tmp_path / "v"
    root = v / "data_in"
    root.mkdir(parents=True)
    (root / "projects.csv").write_text(
        "Project ID,Program Lead\nPRJ-0001,Dana Whitfield\nPRJ-0002,Dan Whitfield\n",
        encoding="utf-8")
    (root / "note.txt").write_text("Project: PRJ-0003\nProgram Lead: D. Whitfield\n",
                                   encoding="utf-8")
    g = kgm.KnowledgeGraph(v)
    try:
        g.confirm_all_rules()
        g.seed()
        q = review(g, "D. Whitfield")
        dana = next(c["id"] for c in q["candidates"] if c["name"] == "Dana Whitfield")
        g.answer_review(q["id"], dana, everywhere=True)
        g.seed()
        (root / "projects.csv").write_text("Project ID,Program Lead\nPRJ-0002,Dan Whitfield\n",
                                           encoding="utf-8")
        (root / "note.txt").unlink()
        g.seed()
        assert g.entity(dana) is None
        (root / "note2.txt").write_text("Project: PRJ-0004\nProgram Lead: D. Whitfield\n",
                                        encoding="utf-8")
        g.seed()
        ids = {r[0] for r in g.db.execute("SELECT id FROM entities")}
        assert all(r["subject_id"] in ids and r["object_id"] in ids
                   for r in g.db.execute("SELECT * FROM relations"))
        assert all(r[0] in ids for r in g.db.execute("SELECT entity_id FROM mentions"))
        assert any(r["surface"] == "D. Whitfield" for r in g.reviews())
        g.export_relations_csv()
        g.write_exports()
    finally:
        g.close()


def test_an_answer_can_be_forgotten(kg):
    q = review(kg, "D. Whitfield")
    kg.answer_review(q["id"], person(kg, "Dana Whitfield"))
    kg.seed()
    assert not [r for r in kg.reviews() if r["surface"] == "D. Whitfield"]
    ans = kg.answers()
    assert [(a["surface"], a["name"]) for a in ans] == [("D. Whitfield", "Dana Whitfield")]
    assert ans[0]["path"].endswith("helios_status_2026-03.md")
    kg.forget_answer(ans[0]["key"], ans[0]["document_id"])
    assert kg.answers() == [] and kg.decisions()[-1]["action"] == "name_undo"
    kg.seed()
    assert [r for r in kg.reviews() if r["surface"] == "D. Whitfield"]


def _small(tmp_path, files):
    v = tmp_path / "small"
    for rel, body in files.items():
        f = v / "data_in" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    g = kgm.KnowledgeGraph(v)
    g.confirm_all_rules()
    g.seed()
    return g


def test_two_people_merged_are_one_candidate_for_their_initial(tmp_path):
    # Merged, 'Dana J Whitfield' still counted as a second person for the
    # gazetteer's 'D. Whitfield', so every rebuild asked "which one?" naming
    # the survivor and her own merged spelling (and the labelled line got an
    # inferred alias too).
    g = _small(tmp_path, {
        "t.csv": "Project,Program Lead\nPRJ-1,Dana Whitfield\nPRJ-2,Dana J Whitfield\n",
        "memo.txt": "Notes\nSpoke with D. Whitfield about the schedule.\n",
        "status.md": "Project: PRJ-3\nOwner: D. Whitfield\n"})
    try:
        assert len([r for r in g.reviews() if r["kind"] == "ambiguous_name"]) == 2
        g.merge(person(g, "Dana Whitfield"), person(g, "Dana J Whitfield"))
        g.seed()
        assert [r for r in g.reviews() if r["kind"] == "ambiguous_name"] == []
        dana = person(g, "Dana Whitfield")
        spots = {(m["path"], m["surface"]) for m in g.mentions(dana)}
        assert {("memo.txt", "D. Whitfield"), ("status.md", "D. Whitfield")} <= spots
        assert g.initial_candidates("d whitfield") == [dana]
    finally:
        g.close()


def test_forgetting_an_older_councils_answer_for_a_surname_first_name_asks_again(tmp_path):
    # An answer 92699e0 stored under fold('Whitfield, D.') = 'whitfield d'
    # was listed under that raw key, and forgetting it matched no question
    # (person_key gives 'd whitfield'): no question came back, and the name
    # stayed unlinked.
    import time
    g = _small(tmp_path, {
        "t.csv": "Project,Program Lead\nPRJ-1,Dana Whitfield\nPRJ-2,Dan Whitfield\n",
        "s.md": "Project: PRJ-3\nOwner: Whitfield, D.\n"})
    try:
        q = review(g, "Whitfield, D.")
        dana = person(g, "Dana Whitfield")
        with g.db:                                  # exactly what 92699e0 wrote
            g.db.execute("INSERT OR REPLACE INTO name_decisions VALUES (?,?,?,?)",
                         (kgm.fold(q["surface"]), "", dana, time.time()))
            g.db.execute("UPDATE review SET status='resolved' WHERE id=?", (q["id"],))
            g._decide("name", {"surface": q["surface"], "entity_id": dana,
                               "document_id": None, "review_id": q["id"]})
        g.seed()
        assert any((r["subject"]["name"], r["object"]["name"]) == ("Dana Whitfield", "PRJ-3")
                   for r in g.all_relations())
        ans = g.answers()
        assert [(a["key"], a["surface"]) for a in ans] == [("whitfield d", "Whitfield, D.")]
        g.forget_answer(ans[0]["key"], ans[0]["document_id"])
        g.seed()
        assert [r["surface"] for r in g.reviews()] == ["Whitfield, D."]
        assert g.reviews("resolved") == []
    finally:
        g.close()


def test_an_everywhere_answer_covers_the_last_first_spelling(kg):
    q = review(kg, "D. Whitfield")
    kg.answer_review(q["id"], person(kg, "Dana Whitfield"), everywhere=True)
    csvp = kg.root / "ironbridge" / "trackers" / "reviewers.csv"
    csvp.write_text('Project,Owner\nPRJ-0944,"Whitfield, D."\n', encoding="utf-8")
    kg.seed()
    assert not [r for r in kg.reviews() if "hitfield" in r["surface"]]
    assert rel_status(kg, "Dana Whitfield", "LEADS", "PRJ-0944") == "seeded"
