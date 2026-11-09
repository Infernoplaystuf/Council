"""
The Study plugin (council_plugins/study, id council.study) and the plugin
services it is the first to use (council_core.plugin_services):

  * it is built in and OFF until ticked; it names the "sources" and
    "models" services, which Council now offers;
  * a plugin's context gives only the services its manifest names;
  * the models service lists and runs US-made models only;
  * a notebook is a named Connections graph over chosen folders, left out
    of the Council's own answers; building it reads its files, and its
    passages are searchable with citations (file, page, lines) and read
    back from the file;
  * the tab builds offscreen from the plugin folder and searches.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import app_plugins as ap  # noqa: E402
from council_core import plugin_services as ps  # noqa: E402

PHOTO = """# Photosynthesis

Photosynthesis converts light energy into chemical energy stored in glucose.
It takes place in the chloroplasts of plant cells.
The light-dependent reactions occur in the thylakoid membranes and make ATP and NADPH.
The Calvin cycle occurs in the stroma and fixes carbon dioxide into sugars.
Chlorophyll a is the main pigment; it absorbs red and blue light.
"""
RESP = """Cellular respiration releases energy from glucose.
Glycolysis happens in the cytoplasm and yields two ATP per glucose.
The Krebs cycle occurs in the mitochondrial matrix.
The electron transport chain on the inner mitochondrial membrane makes most of the ATP.
"""


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    home = tmp_path / "app"
    monkeypatch.setenv("COUNCIL_APP_DIR", str(home))
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    return home


@pytest.fixture
def notes(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "photosynthesis.md").write_text(PHOTO, encoding="utf-8")
    (folder / "respiration.txt").write_text(RESP, encoding="utf-8")
    return folder


@pytest.fixture
def built(tmp_path, notes):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    src = ps.Sources(vault=vault)
    nb = src.create_notebook("Biology", [notes])
    src.build(nb.id)
    return src, nb


def _study():
    (found,) = [f for f in ap.discover() if f.id == "council.study"]
    return found


# ------------------------------------------------------------- the plugin
def test_study_is_built_in_and_starts_off():
    found = _study()
    assert found.builtin and found.can_turn_on, found.problem
    assert set(found.manifest.needs) == {"sources", "models"}
    assert found.manifest.network == "none"
    assert ap.switched_on([found]) == []


def test_council_offers_the_services_the_plugin_names():
    assert ap.SERVICES == ps.NAMES == {"sources", "models"}


def test_a_context_gives_only_the_services_its_manifest_names():
    found = _study()
    ctx = ap.PluginContext(manifest=found.manifest)
    assert isinstance(ctx.service("models"), ps.Models)
    assert ctx.service("models") is ctx.service("models")
    finance = [f for f in ap.discover() if f.id == "council.finance"][0]
    with pytest.raises(PermissionError):
        ap.PluginContext(manifest=finance.manifest).service("sources")


# ------------------------------------------------------------- the models
def test_the_models_service_refuses_a_model_not_made_in_the_us():
    with pytest.raises(ValueError, match="US-made"):
        ps.Models().chat("qwen2.5:7b")
    with pytest.raises(ValueError):
        ps.Models().chat("")


def test_the_models_service_lists_us_models_only(monkeypatch):
    import council_engine as ce
    from council_core import local_models as lm
    monkeypatch.setattr(ce, "_ollama_tags", lambda _h: {"models": [
        {"name": "granite3.3:8b", "details": {"family": "granite"}},
        {"name": "qwen2.5:7b", "details": {"family": "qwen2"}}]})
    monkeypatch.setattr(lm, "list_local_models", lambda **_k: [
        {"name": "Phi", "path": "/m/phi-4.gguf", "origin": "US"},
        {"name": "Mystery", "path": "/m/x.gguf", "origin": "unknown"}])
    got = ps.Models().available()
    assert ("granite3.3:8b", "granite3.3:8b") in got
    assert all("qwen" not in mid for _l, mid in got)
    assert ("GGUF: Phi", "/m/phi-4.gguf") in got
    assert all(mid != "/m/x.gguf" for _l, mid in got)


# ------------------------------------------------------------- the sources
def test_a_notebook_is_a_graph_left_out_of_the_councils_answers(built):
    src, nb = built
    assert nb.name == "Biology" and not nb.use_in_answers
    from council_core import kg_graphs as kgg
    gl = kgg.GraphList(src.vault)
    assert nb.id in {g.id for g in gl.all()}
    assert nb.id not in {g.id for g in gl.for_answers()}


def test_building_reads_the_files_and_says_it_is_built(tmp_path, notes):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    src = ps.Sources(vault=vault)
    nb = src.create_notebook("Bio", [notes])
    assert not src.built(nb.id)
    assert src.passages(nb.id) == []
    src.build(nb.id)
    assert src.built(nb.id)
    docs = src.documents(nb.id)
    assert sorted(d["display"] for d in docs) == ["notes/photosynthesis.md",
                                                  "notes/respiration.txt"]
    assert all(d["status"] == "ok" for d in docs)


def test_search_ranks_the_passage_that_answers_and_cites_it(built):
    src, nb = built
    hits = src.search(nb.id, "where does the Calvin cycle happen", 3)
    assert hits[0].display == "notes/photosynthesis.md"
    assert "Calvin cycle occurs in the stroma" in hits[0].text
    assert hits[0].cite() == "notes/photosynthesis.md, L1-L7"
    hits = src.search(nb.id, "mitochondrial matrix Krebs", 3)
    assert hits[0].display == "notes/respiration.txt"
    assert src.search(nb.id, "zebra unicorn", 3) == []


def test_a_cited_line_is_read_back_from_the_file(built):
    src, nb = built
    (p,) = [x for x in src.passages(nb.id) if x.display.endswith(".txt")]
    lines = src.context(nb.id, p.path, p.locator(3), radius=1)
    assert [(g, hit) for g, _t, hit in lines] == [("L2", False), ("L3", True),
                                                   ("L4", False)]
    assert lines[1][1] == "The Krebs cycle occurs in the mitochondrial matrix."


def test_the_index_is_rebuilt_only_when_a_document_changes(built, notes):
    src, nb = built
    first = src.index(nb.id)
    assert src.index(nb.id) is first
    (notes / "respiration.txt").write_text(RESP + "Fermentation makes lactate.\n",
                                           encoding="utf-8")
    src.build(nb.id)
    again = src.index(nb.id)
    assert again is not first
    assert src.search(nb.id, "fermentation lactate", 1)


def test_a_passage_survives_a_round_trip_through_json(built):
    src, nb = built
    p = src.passages(nb.id)[0]
    q = ps.Passage.from_dict(p.to_dict())
    assert (q.key, q.cite(), q.text) == (p.key, p.cite(), p.text)


def test_deleting_a_notebook_leaves_the_files(built, notes):
    src, nb = built
    src.delete_notebook(nb.id)
    assert nb.id not in {n.id for n in src.notebooks()}
    assert (notes / "photosynthesis.md").read_text(encoding="utf-8") == PHOTO
    with pytest.raises(ValueError):
        src.delete_notebook("main")


# ------------------------------------------------------------- the tab
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeModels:
    def __init__(self, reply=""):
        self.reply = reply
        self.asked = []

    def available(self):
        return [("granite3.3:8b", "granite3.3:8b")]

    def chat(self, model):
        def chat(messages, *, max_tokens=800, schema=None):
            self.asked.append((model, messages, schema))
            return self.reply(messages, schema) if callable(self.reply) else self.reply
        return chat


def pump(qapp, until, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


def tab_for(src, models=None):
    found = _study()
    ap.load_entry(found.manifest)
    mod = sys.modules["council_plugin_council_study.tab"]
    ctx = ap.PluginContext(manifest=found.manifest,
                           services={"sources": src, "models": models or FakeModels()})
    return mod.build_tab(ctx)


def test_the_tab_builds_from_its_folder_through_plugin_tabs(qapp, tmp_path,
                                                            monkeypatch):
    from council_qt import plugin_tabs
    monkeypatch.setattr(ps.Models, "available", lambda self: [])
    (tmp_path / "vault" / "data_in").mkdir(parents=True)
    page = plugin_tabs.build_page(_study())
    assert type(page).__name__ == "StudyTab"
    assert pump(qapp, lambda: page.model.count() == 1)
    assert page.model.currentData() == ""


def test_the_tab_lists_notebooks_and_searches(qapp, built):
    src, nb = built
    tab = tab_for(src)
    assert tab.notebook_id() == nb.id
    assert pump(qapp, lambda: tab.documents.count() == 2)
    assert pump(qapp, lambda: tab.model.currentData() == "granite3.3:8b")
    page = tab.sources_page
    page.query.setText("thylakoid ATP NADPH")
    page.search()
    assert pump(qapp, lambda: page.results.count() and
                page.results.item(0).text().startswith("notes/photosynthesis.md"))
    page.results.setCurrentRow(0)
    assert pump(qapp, lambda: "thylakoid" in page.preview.toPlainText())
