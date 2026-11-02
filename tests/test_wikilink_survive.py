"""
``[[wiki-link]]`` mentions survive a rebuild that re-reads OTHER documents.

The bug (review, 2026-10-19): wiki-links were queued only while a document
was being (re-)read, but the graph phase wiped every mention and every
non-user alias before replaying that queue. So when one document changed,
every UNCHANGED document's wiki-link mentions — and the entities only they
supported — were deleted. These tests pin the fix (the graph phase scans
every usable document's cached lines), the counters, the workbook locator
(one mention per reference, not a wiki-link one and a gazetteer one), the
Obsidian ``#heading`` / ``^block`` / ``![[embed]]`` forms, and the person
inference for initials, accents and 'Last, First' - and that a Title Case
topic ('Root Cause') is not a person without positive evidence of one.

Every vault is a tmp_path; no model, no network, no Tesseract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import knowledge_graph as kgm                        # noqa: E402
from council_core.knowledge_graph import WikiLinkRef, scan_wikilinks    # noqa: E402

_COUNTERS = ("wikilink_mentions", "wikilink_unique", "wikilinks_matched",
             "wikilinks_created")


def _vault_with(tmp_path, files):
    data_in = tmp_path / "data_in"
    data_in.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (data_in / name).write_text(text, encoding="utf-8")
    return tmp_path


def _graph(v):
    kg = kgm.KnowledgeGraph(v, image_reader=None)
    kg.confirm_all_rules()
    return kg


def _seed(v):
    with _graph(v) as kg:
        return kg.seed()


def _entity(kg, name):
    """The live entity whose alias folds like ``name`` (or None)."""
    row = kg.db.execute(
        "SELECT e.id, e.type, e.name FROM entities e JOIN aliases a"
        " ON a.entity_id = e.id WHERE a.alias_norm = ? AND e.merged_into IS NULL",
        (kgm.fold(name),)).fetchone()
    return dict(row) if row is not None else None


def _wikilink_rows(kg):
    return [dict(r) for r in kg.db.execute(
        "SELECT m.surface, m.locator, d.path, e.name, e.type FROM mentions m"
        " JOIN documents d ON d.id = m.document_id"
        " JOIN entities e ON e.id = m.entity_id"
        " WHERE m.method = 'wikilink' ORDER BY d.path, m.locator")]


def _write(v, name, text):
    (v / "data_in" / name).write_text(text, encoding="utf-8")


# ─────────────────────────────── surviving a rebuild of another document

def test_an_unchanged_documents_wikilink_survives_another_documents_edit(tmp_path):
    v = _vault_with(tmp_path, {
        "a.md": "Spoke to [[Casey Morgan]] about the rig.\n",
        "b.md": "Nothing of note.\n",
    })
    first = _seed(v)
    assert first["wikilink_mentions"] == 1
    assert first["wikilinks_created"] == 1
    # Edit ONLY b.md: a.md is unchanged, so it is not read again.
    _write(v, "b.md", "Still nothing of note, but different now.\n")
    second = _seed(v)
    assert second["documents_read"] == 1
    assert second["documents_unchanged"] == 1
    assert second["graph_unchanged"] is False
    # The counters still report a.md's reference ...
    assert second["wikilink_mentions"] == 1
    assert second["wikilink_unique"] == 1
    assert second["wikilinks_matched"] + second["wikilinks_created"] == 1
    # ... and the mention and the entity it created are still there.
    with _graph(v) as kg:
        casey = _entity(kg, "Casey Morgan")
        assert casey is not None, "the wiki-link's entity was deleted"
        assert casey["type"] == "PERSON" and casey["name"] == "Casey Morgan"
        rows = _wikilink_rows(kg)
        assert [(r["path"], r["name"]) for r in rows] == [("a.md", "Casey Morgan")]
        wl = [m for m in kg.mentions(casey["id"]) if m["method"] == "wikilink"]
        assert len(wl) == 1 and wl[0]["where"] == "line 1"


def test_every_documents_wikilinks_are_counted_not_only_the_re_read_ones(tmp_path):
    v = _vault_with(tmp_path, {
        "a.md": "Met [[Casey Morgan]].\nLater [[Casey Morgan|CM]] called back.\n",
        "b.md": "Handed over to [[Riley Chen]].\n",
    })
    first = _seed(v)
    assert first["wikilink_mentions"] == 3
    assert first["wikilink_unique"] == 2
    _write(v, "b.md", "Handed over to [[Riley Chen]] on Friday.\n")
    second = _seed(v)
    assert second["documents_read"] == 1
    assert second["wikilink_mentions"] == 3
    assert second["wikilink_unique"] == 2
    with _graph(v) as kg:
        names = sorted({(r["path"], r["name"]) for r in _wikilink_rows(kg)})
    assert names == [("a.md", "Casey Morgan"), ("b.md", "Riley Chen")]


def test_a_re_read_document_is_not_counted_twice(tmp_path):
    v = _vault_with(tmp_path, {"a.md": "Spoke to [[Casey Morgan]].\n"})
    _seed(v)
    _write(v, "a.md", "Spoke to [[Casey Morgan]] again this morning.\n")
    second = _seed(v)
    assert second["documents_read"] == 1
    assert second["wikilink_mentions"] == 1
    assert second["wikilinks_matched"] + second["wikilinks_created"] == 1
    with _graph(v) as kg:
        assert len(_wikilink_rows(kg)) == 1


def test_a_withdrawn_wikilink_still_goes_while_the_others_stay(tmp_path):
    v = _vault_with(tmp_path, {
        "a.md": "Spoke to [[Casey Morgan]].\n",
        "b.md": "Asked [[Riley Chen]] to sign.\n",
    })
    _seed(v)
    _write(v, "b.md", "Nobody to ask now.\n")
    stats = _seed(v)
    assert stats["wikilink_mentions"] == 1
    with _graph(v) as kg:
        assert _entity(kg, "Casey Morgan") is not None
        assert _entity(kg, "Riley Chen") is None
        assert [r["name"] for r in _wikilink_rows(kg)] == ["Casey Morgan"]


# ─────────────────────────────── counters: created vs matched, unchanged graph

def test_created_counts_only_entities_that_did_not_exist_yet(tmp_path):
    v = _vault_with(tmp_path, {
        "a.md": "Spoke to [[Casey Morgan]].\n",
        "b.md": "Nothing of note.\n",
    })
    first = _seed(v)
    assert (first["wikilinks_created"], first["wikilinks_matched"]) == (1, 0)
    _write(v, "b.md", "Different now.\n")
    second = _seed(v)
    # Casey Morgan's entity was already in the graph: the rebuild found it,
    # it did not create it again.
    assert (second["wikilinks_created"], second["wikilinks_matched"]) == (0, 1)


def test_an_unchanged_graph_reports_the_last_rebuilds_wikilink_counters(tmp_path):
    v = _vault_with(tmp_path, {
        "a.md": "Spoke to [[Casey Morgan]].\nThen [[Riley Chen]].\n",
        "b.md": "Copied [[Casey Morgan]].\n",
    })
    first = _seed(v)
    assert first["graph_unchanged"] is False
    again = _seed(v)
    assert again["graph_unchanged"] is True
    assert {k: again[k] for k in _COUNTERS} == {k: first[k] for k in _COUNTERS}
    assert again["wikilink_mentions"] == 3
    # And after a rebuild that did work the graph out, the next unchanged
    # one reports THAT rebuild's numbers.
    _write(v, "b.md", "Copied [[Casey Morgan]] twice.\n")
    worked = _seed(v)
    assert worked["graph_unchanged"] is False
    unchanged = _seed(v)
    assert unchanged["graph_unchanged"] is True
    assert {k: unchanged[k] for k in _COUNTERS} == {k: worked[k] for k in _COUNTERS}


# ─────────────────────────────── Obsidian anchors and embeds

def test_a_heading_anchor_is_stripped_from_the_target():
    refs = scan_wikilinks([(None, 1, "call [[Jordan Blake#Contact]] today")])
    assert refs == [WikiLinkRef(page=None, line=1, surface="Jordan Blake",
                                display="Jordan Blake")]


def test_a_block_anchor_is_stripped_from_the_target():
    refs = scan_wikilinks([(None, 1, "see [[Jordan Blake#^a1b2c3]] and"
                                     " [[Riley Chen^d4e5]]")])
    assert [r.surface for r in refs] == ["Jordan Blake", "Riley Chen"]


def test_an_anchor_with_a_pipe_keeps_the_display_text():
    refs = scan_wikilinks([(None, 2, "ask [[Jordan Blake#Contact|JB]]")])
    assert refs == [WikiLinkRef(page=None, line=2, surface="Jordan Blake",
                                display="JB")]


def test_a_link_to_a_heading_in_the_same_note_names_nothing():
    assert scan_wikilinks([(None, 1, "see [[#Summary]] and [[#^blockid]]")]) == []


def test_an_embed_is_not_an_entity_reference():
    refs = scan_wikilinks([(None, 1, "![[diagram.png]] shows what"
                                     " [[Casey Morgan]] found"),
                           (None, 2, "![[Jordan Blake]]")])
    assert [r.surface for r in refs] == ["Casey Morgan"]


def test_anchors_and_embeds_end_to_end(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": ("Call [[Casey Morgan#Contact]] about it.\n"
                    "![[diagram.png]]\n"
                    "Block: [[Casey Morgan#^abc123]].\n"),
    })
    stats = _seed(v)
    assert stats["wikilink_mentions"] == 2
    assert stats["wikilink_unique"] == 1
    with _graph(v) as kg:
        names = {r["name"] for r in _wikilink_rows(kg)}
        assert names == {"Casey Morgan"}
        assert kg.db.execute("SELECT COUNT(*) FROM entities WHERE name LIKE"
                             " '%diagram%' OR name LIKE '%#%'").fetchone()[0] == 0


# ─────────────────────────────── a workbook's text cell: one mention

def test_a_wikilink_in_a_workbook_text_cell_is_one_mention_at_its_sheet_row(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    v = _vault_with(tmp_path, {})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Log"
    ws["A1"] = "Site log"
    ws["A3"] = "Nothing to report today at the cell."
    ws["A9"] = "Spoke with [[Casey Morgan]] about the rig on Tuesday."
    wb.save(v / "data_in" / "notes.xlsx")
    stats = _seed(v)
    assert stats["unreadable"] == []
    assert stats["wikilink_mentions"] == 1
    with _graph(v) as kg:
        casey = _entity(kg, "Casey Morgan")
        assert casey is not None
        got = kg.mentions(casey["id"])
    # The wiki-link's mention and the gazetteer's search of the same line
    # are the same reference: one mention, cited where the gazetteer would.
    assert [(m["method"], m["where"]) for m in got] == [("wikilink", "sheet Log, row 9")]
    assert got[0]["locator"] == {"line": 9, "page": "Log"}


# ─────────────────────────────── person inference

@pytest.mark.parametrize("surface", [
    "Jordan Blake",
    "Maria O'Brien",
    "Jean-Luc Picard",
    "Jordan R. Blake",
    "J. Blake",
    "José García",
    "Zoë Ångström",
    "Blake, Jordan",
    "Blake, Jordan R.",
    "Jordan Blake, Jr.",
    "Ludwig van Beethoven",
])
def test_names_infer_as_person(surface):
    assert kgm._infer_wikilink_type(surface) == "PERSON", surface


@pytest.mark.parametrize("surface", [
    "Blade Inspection Program",
    "Engine Upgrade Plan",
    "Quarterly Review",
    "Night Line",
    "Project Atlas",
    "Jordan",
    "J. R.",
    "project with lower",
    "Jordan Blake.",
])
def test_titles_and_fragments_do_not_infer_as_person(surface):
    assert kgm._infer_wikilink_type(surface) == "PROJECT", surface


def test_codes_still_infer_as_parts():
    assert kgm._infer_wikilink_type("ABC-123") == "PART"
    assert kgm._infer_wikilink_type("P 101") == "PART"


# ─────────────────────────────── a PERSON needs positive evidence
# (review, 2026-10-19): the shape alone - two Capitalised words - made every
# topic a person, so the People filter listed 'Root Cause' and 'Eddy Current'.

@pytest.mark.parametrize("surface", [
    "Root Cause",
    "Eddy Current",
    "First Article",
    "Turbine Blade",
    "Atlas Pump Retrofit",
    "Corrective Action",
    "Heat Treatment",
    "Night Owl",
    "ABC DEF",
    "SAM Registration",          # an acronym, though 'Sam' is a given name
])
def test_an_unknown_title_case_topic_is_a_project(surface):
    assert kgm._infer_wikilink_type(surface) == "PROJECT", surface


@pytest.mark.parametrize("surface", [
    "Dana Whitfield",
    "Mara Kim",
    "José García",
    "Tomás Echeverría",
    "Casey Morgan",
    "Riley Chen",
    "Priya Raman",
    "MARA KIM",
    # Common US names a short list left out, so a person the graph did not
    # know became a PROJECT (review, 2026-10-19).
    "Ashley Brooks",
    "Martin Lopez",
    "Raymond Hughes",
    "Frank Miller",
    "Debra Lane",
    "Cheryl Wu",
    "Kristen Ward",
    "Louis Hart",
])
def test_a_known_given_name_is_a_person(surface):
    assert kgm._infer_wikilink_type(surface) == "PERSON", surface


def test_the_graphs_own_people_are_evidence():
    # 'Xochitl' is in no built-in list: a person only when the graph
    # already knows someone of that first name, or that very person.
    assert kgm._infer_wikilink_type("Xochitl Mendoza") == "PROJECT"
    assert kgm._infer_wikilink_type("Xochitl Mendoza",
                                    given_names={"xochitl"}) == "PERSON"
    # 'Last, First' is no evidence on its own (a parts list writes 'Valve,
    # Check'): the flipped name needs its given name like any other.
    assert kgm._infer_wikilink_type("Mendoza, Xochitl") == "PROJECT"
    assert kgm._infer_wikilink_type("Mendoza, Xochitl",
                                    given_names={"xochitl"}) == "PERSON"
    assert kgm._infer_wikilink_type("Xochitl Mendoza Jr.") == "PROJECT"
    assert kgm._infer_wikilink_type(
        "Xochitl Mendoza Jr.", known_people={"xochitl mendoza"}) == "PERSON"


# 'Last, First' and a particle were evidence on their own (review,
# 2026-10-19): a parts list's 'Noun, Modifier' and a Spanish company, plant
# or place name became people.

@pytest.mark.parametrize("surface", [
    "Valve, Check",
    "Bearing, Ball",
    "Pump, Hydraulic",
    "Seal, Shaft",
    "Aceros de Monterrey",
    "Industrias de Mexico",
    "Planta de Juarez",
    "Rio de Janeiro",
    "Casa de Campo",
    "Dana Corporation",
    "Ben Franklin Award",
])
def test_noun_modifier_and_company_names_are_projects(surface):
    assert kgm._infer_wikilink_type(surface) == "PROJECT", surface


@pytest.mark.parametrize("surface", [
    "Blake, J.",                 # 'Last, First' with an initial
    "Pieter van Dijk",           # 'van' / 'von' say "a person" on their own
    "Ursula von der Leyen",
    "Maria de la Cruz",          # 'de' does not, but 'Maria' does
    "Leonardo da Vinci",
])
def test_an_initial_or_a_persons_particle_is_still_evidence(surface):
    assert kgm._infer_wikilink_type(surface) == "PERSON", surface


def test_a_team_in_a_person_column_vouches_for_no_one(tmp_path):
    # An Owner or Approved-by column often holds a team. As a PERSON its
    # first word counted as a given name, so [[Quality Escape]] and
    # [[First Article]] became people again (review, 2026-10-19).
    assert kgm._name_shape("Quality Team") is None
    assert kgm._name_shape("First Shift") is None
    v = _vault_with(tmp_path, {
        "plan.md": ("Project: Atlas\nOwner: Quality Team\n"
                    "Approved by: Engineering Department\n"),
        "plan2.md": "Project: Nimbus\nOwner: First Shift\n",
        "topics.md": ("See [[Quality Escape]], [[Engineering Change]] and"
                      " [[First Article]].\n"),
    })
    _seed(v)
    with _graph(v) as kg:
        types = {r["name"]: r["type"] for r in kg.db.execute(
            "SELECT name, type FROM entities WHERE type != 'DOCUMENT'"
            " AND merged_into IS NULL")}
        people = [h["name"] for h in kg.search("Quality", "PERSON")]
    for topic in ("Quality Escape", "Engineering Change", "First Article"):
        assert types.get(topic) == "PROJECT", (topic, types)
    assert "Quality Escape" not in people


def test_topics_stay_out_of_the_people_end_to_end(tmp_path):
    v = _vault_with(tmp_path, {
        "status.md": "Project: Atlas\nProgram Lead: Xochitl Ramírez\n",
        "topics.md": ("Review: [[Atlas Pump Retrofit]], [[Root Cause]], [[Eddy Current]],"
                      " [[First Article]], [[Turbine Blade]].\n"
                      "Owner: [[Jordan Blake]]. Ask [[Xochitl Mendoza]] too.\n"),
    })
    _seed(v)
    with _graph(v) as kg:
        types = {r["name"]: r["type"] for r in kg.db.execute(
            "SELECT name, type FROM entities WHERE type != 'DOCUMENT'"
            " AND merged_into IS NULL")}
        people = [h["name"] for h in kg.search("Root", "PERSON")]
    for topic in ("Atlas Pump Retrofit", "Root Cause", "Eddy Current",
                  "First Article", "Turbine Blade"):
        assert types.get(topic) == "PROJECT", (topic, types)
    assert types.get("Jordan Blake") == "PERSON"
    # A record named a Xochitl: her first name is evidence for the next one.
    assert types.get("Xochitl Ramírez") == "PERSON"
    assert types.get("Xochitl Mendoza") == "PERSON"
    assert people == []


def test_a_stale_person_does_not_vouch_for_itself(tmp_path):
    # A graph built by the old rule holds PERSON 'Root Cause'. Its first
    # word must not count as a given name on the next rebuild, or the old
    # mistake would keep itself alive.
    v = _vault_with(tmp_path, {"topics.md": "See [[Root Cause]].\n"})
    _seed(v)
    with _graph(v) as kg:
        with kg.db:
            eid = kgm.entity_id("PERSON", kgm.entity_key("PERSON", "Root Cause"))
            # OR IGNORE: under the old rule the first rebuild made it.
            kg.db.execute("INSERT OR IGNORE INTO entities (id, type, key, name,"
                          " created_ts) VALUES (?, 'PERSON', ?, 'Root Cause', 0)",
                          (eid, kgm.entity_key("PERSON", "Root Cause")))
            kg.db.execute("INSERT OR IGNORE INTO aliases VALUES"
                          " (?, 'Root Cause', ?, 'inferred')",
                          (eid, kgm.fold("Root Cause")))
    _write(v, "topics.md", "See [[Root Cause]] again.\n")
    _seed(v)
    with _graph(v) as kg:
        got = sorted((r["type"], r["name"]) for r in kg.db.execute(
            "SELECT type, name FROM entities WHERE type != 'DOCUMENT'"))
    assert got == [("PROJECT", "Root Cause")]


def test_an_accented_name_lands_as_one_person_end_to_end(tmp_path):
    v = _vault_with(tmp_path, {
        "note.md": "Signed by [[José García]].\nCopied [[Blake, Jordan R.]].\n",
    })
    stats = _seed(v)
    assert stats["wikilinks_created"] == 2
    with _graph(v) as kg:
        jose = _entity(kg, "José García")
        jb = _entity(kg, "Blake, Jordan R.")
    assert jose is not None and jose["type"] == "PERSON"
    assert jb is not None and jb["type"] == "PERSON"
    assert jb["name"] == "Jordan R. Blake"
