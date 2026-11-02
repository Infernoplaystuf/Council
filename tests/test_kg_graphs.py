"""Named knowledge graphs (council_core/kg_graphs.py): several graphs, each over
the folders chosen for it, as Morphik's create_graph(name, filters) - user,
2026-10-21: "one graph per vault" was a wrong assumption."""
from __future__ import annotations

import json

import pytest

from council_core import kg_graphs as kgg
from council_core import knowledge_graph as kgm


def _vault(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in" / "falcon").mkdir(parents=True)
    (v / "data_in" / "osprey").mkdir(parents=True)
    (v / "data_in" / "falcon" / "parts.csv").write_text(
        "Project,Part Number,Owner\nKA-101,WG-2201,Ngozi Okafor\n", encoding="utf-8")
    (v / "data_in" / "falcon" / "notes.md").write_text("Owner: Sam Whitaker\nProject: KA-101\n",
                                                        encoding="utf-8")
    (v / "data_in" / "osprey" / "parts.csv").write_text(
        "Project,Part Number,Owner\nKA-102,LG-0310,Jens Halvorsen\n", encoding="utf-8")
    return v


def _people(kg):
    return sorted(r[0] for r in kg.db.execute(
        "SELECT name FROM entities WHERE type='PERSON' AND merged_into IS NULL"))


def test_the_main_graph_is_always_there_and_cannot_be_deleted(tmp_path):
    gl = kgg.GraphList(_vault(tmp_path))
    assert [(g.id, g.name) for g in gl.all()] == [("main", "All documents")]
    with pytest.raises(ValueError):
        gl.delete("main")
    with gl.open("main") as kg:
        assert kg.path == kgm.store_path(gl.vault)


def test_a_named_graph_reads_only_its_folders(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    info = gl.create("Falcon", [v / "data_in" / "falcon"], focus="wing parts")
    assert gl.get(info.id).name == "Falcon"
    with gl.open(info.id) as kg:
        assert kg.path.parent.parent == v / ".knowledge_graph" / "graphs"
        assert kg.focus() == "wing parts"
        kg.seed()
        assert _people(kg) == ["Ngozi Okafor", "Sam Whitaker"]
    with gl.open("main") as kg:
        kg.seed()
        assert _people(kg) == ["Jens Halvorsen", "Ngozi Okafor", "Sam Whitaker"]
    # only some file types
    md = gl.create("Falcon notes", [v / "data_in" / "falcon"], suffixes=["md"])
    with gl.open(md.id) as kg:
        kg.seed()
        assert _people(kg) == ["Sam Whitaker"]


def test_a_folder_outside_the_vault_is_read_only_and_added_to_that_graph(tmp_path):
    v = _vault(tmp_path)
    outside = tmp_path / "share"
    outside.mkdir()
    (outside / "team.csv").write_text("Project,Owner\nKA-9,Ann Stone\n", encoding="utf-8")
    before = (outside / "team.csv").read_bytes()
    gl = kgg.GraphList(v)
    info = gl.create("Shared", [outside])
    with gl.open(info.id) as kg:
        kg.seed()
        assert _people(kg) == ["Ann Stone"]
    with gl.open(info.id) as kg:                         # added once, not twice
        assert len(kg.extra_roots()) == 1
    with gl.open("main") as kg:
        assert kg.extra_roots() == []                    # not the main graph's
    assert (outside / "team.csv").read_bytes() == before
    assert sorted(p.name for p in outside.iterdir()) == ["team.csv"]


def test_bad_choices_are_refused(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    with pytest.raises(ValueError):
        gl.create("", [v / "data_in"])
    with pytest.raises(ValueError):
        gl.create("Nothing", [])
    with pytest.raises(ValueError):
        gl.create("Store", [v / ".knowledge_graph"])        # inside the vault, not data_in
    gl.create("Falcon", [v / "data_in" / "falcon"])
    with pytest.raises(ValueError):
        gl.create("falcon", [v / "data_in"])                # the name is taken
    with pytest.raises(ValueError):
        gl.store_dir("../../x")


def test_deleting_a_graph_removes_only_its_own_store(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    info = gl.create("Falcon", [v / "data_in" / "falcon"])
    with gl.open(info.id) as kg:
        kg.seed()
    with gl.open("main") as kg:
        kg.seed()
    store = gl.store_dir(info.id)
    assert (store / "graph.sqlite").exists()
    gl.delete(info.id)
    assert not store.exists()
    assert [g.id for g in gl.all()] == ["main"]
    assert kgm.store_path(v).exists()
    assert (v / "data_in" / "falcon" / "parts.csv").exists()


def test_use_in_answers_is_kept_per_graph(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    info = gl.create("Falcon", [v / "data_in" / "falcon"], use_in_answers=False)
    assert [g.id for g in gl.for_answers()] == ["main"]
    gl.update("main", use_in_answers=False)
    gl.update(info.id, use_in_answers=True, name="Falcon wing")
    assert [(g.id, g.name) for g in gl.for_answers()] == [(info.id, "Falcon wing")]


def test_a_damaged_list_is_reported_and_left_alone(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    gl.path.parent.mkdir(parents=True)
    gl.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(kgg.GraphListDamaged):
        gl.all()
    with pytest.raises(kgg.GraphListDamaged):
        gl.create("Falcon", [v / "data_in" / "falcon"])
    assert gl.path.read_text(encoding="utf-8") == "{ not json"
    assert [g.id for g in gl.for_answers()] == ["main"]      # answers still work


def test_the_list_is_plain_json(tmp_path):
    v = _vault(tmp_path)
    gl = kgg.GraphList(v)
    gl.create("Falcon", [v / "data_in" / "falcon"], suffixes=[".CSV"])
    data = json.loads(gl.path.read_text(encoding="utf-8"))
    assert data["graphs"][0]["suffixes"] == [".csv"]
    assert data["graphs"][0]["folders"] == [str((v / "data_in" / "falcon").resolve())]
