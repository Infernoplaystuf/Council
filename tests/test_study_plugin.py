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


# ------------------------------------------------------------- the review
from council_plugins.study.engine import common as cm  # noqa: E402
from council_plugins.study.engine import review as rv  # noqa: E402

OUTLINE = ('{"title": "Energy in cells", "sections": ['
           '{"heading": "Photosynthesis", "question": "Where do the Calvin cycle and light reactions happen?"},'
           '{"heading": "Respiration", "question": "Where are the Krebs cycle and glycolysis?"}]}')


def fake_writer(messages, schema):
    """An outline for the planning call; for a section, one supported
    sentence citing the first source it was shown, one invented, one with
    no citation."""
    if schema is not None:
        return OUTLINE
    import re
    prompt = messages[-1]["content"]
    if "Section: Photosynthesis" in prompt:
        good, file = "The Calvin cycle occurs in the stroma and fixes carbon dioxide", "photo"
    else:
        good, file = "The Krebs cycle occurs in the mitochondrial matrix", "resp"
    first = int(re.search(r"^\[(\d+)\] notes/" + file, prompt, re.M).group(1))
    return (f"{good} [{first}]. Zebras migrate across savannas every winter [{first}]. "
            f"Scientists agree this matters greatly.")


def test_a_review_keeps_only_supported_cited_sentences(built):
    src, nb = built
    models = FakeModels(fake_writer)
    review = rv.write_review(models.chat("granite3.3:8b"), src, nb.id, model="granite")
    assert review.title == "Energy in cells"
    assert [s.heading for s in review.sections] == ["Photosynthesis", "Respiration"]
    photo, resp = review.sections
    assert [s.text for s in photo.sentences] == [
        "The Calvin cycle occurs in the stroma and fixes carbon dioxide."]
    assert review.sources[photo.sentences[0].cites[0]].display == "notes/photosynthesis.md"
    assert resp.sentences[0].text.startswith("The Krebs cycle")
    whys = sorted(x.why for x in photo.left_out)
    assert whys == ["cites nothing", "not in what it cites"]
    c = review.counts()
    assert (c["sentences"], c["left_out"]) == (2, 4)


def test_a_review_round_trips_and_renders_its_citations(built):
    src, nb = built
    review = rv.write_review(FakeModels(fake_writer).chat("m"), src, nb.id)
    again = rv.Review.from_dict(review.to_dict())
    assert again.to_dict() == review.to_dict()
    html = again.to_html(lambda t: t)
    n = again.sections[0].sentences[0].cites[0]
    assert f"href='cite:{n}'" in html and "Left out as unsupported" in html
    md = again.to_markdown()
    assert md.startswith("# Energy in cells") and "## Sources" in md


def test_a_sentence_citing_a_passage_it_was_not_shown_is_left_out():
    sources = {1: rv.Source(1, "d", "p", "p", None, [1], "The sky is blue.")}
    sec = rv.Section("h", "q", shown=[1])
    rv.check_section(sec, "The sky is blue [1]. The sky is blue [4]. "
                          "The sources do not cover clouds.", sources)
    assert [s.text for s in sec.sentences] == ["The sky is blue.",
                                               "The sources do not cover clouds."]
    assert sec.sentences[1].gap
    assert sec.left_out[0].why.startswith("cites [4]")


def test_stop_ends_a_review(built):
    src, nb = built
    with pytest.raises(cm.Stopped):
        rv.write_review(FakeModels(fake_writer).chat("m"), src, nb.id,
                        should_stop=lambda: True)


def test_an_empty_notebook_cannot_be_reviewed(tmp_path, notes):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    src = ps.Sources(vault=vault)
    nb = src.create_notebook("Empty", [notes])
    with pytest.raises(ValueError, match="build it first"):
        rv.write_review(FakeModels(fake_writer).chat("m"), src, nb.id)


def test_the_review_page_writes_saves_and_shows_a_citation(qapp, built, app_dir):
    src, nb = built
    tab = tab_for(src, FakeModels(fake_writer))
    assert pump(qapp, lambda: tab.model.currentData() == "granite3.3:8b")
    page = tab.review_page
    page.write()
    assert pump(qapp, lambda: page.review is not None)
    assert "Calvin cycle occurs in the stroma" in page.text.toPlainText()
    saved = app_dir / "plugin_data" / "council.study" / f"{nb.id}.review.json"
    assert saved.exists()
    from PySide6.QtCore import QUrl
    n = page.review.sections[0].sentences[0].cites[0]
    page.cite_clicked(QUrl(f"cite:{n}"))
    assert pump(qapp, lambda: "stroma" in page.cited.toPlainText())
    again = tab_for(src, FakeModels(fake_writer))
    assert again.review_page.review is not None


# ------------------------------------------------------------- study documents
from council_plugins.study.engine import study_docs as sd  # noqa: E402
import json as _json  # noqa: E402
import re as _re  # noqa: E402


def _num(prompt: str, file: str) -> int:
    return int(_re.search(r"^\[(\d+)\] notes/" + file, prompt, _re.M).group(1))


def fake_teacher(messages, schema):
    """The review's replies, then for each study kind one good item, one
    the source does not back, and one citing a source never shown."""
    if schema == rv.OUTLINE_SCHEMA:
        return OUTLINE
    if schema is None:
        return fake_writer(messages, schema)
    prompt = messages[-1]["content"]
    photo = "Section: Photosynthesis" in prompt or "— Photosynthesis" in prompt
    n = _num(prompt, "photo" if photo else "resp")
    if schema == sd.SCHEMAS["cards"]:
        good = ({"front": "Where does the Calvin cycle occur?", "back": "In the stroma"}
                if photo else {"front": "Where is the Krebs cycle?",
                               "back": "The mitochondrial matrix"})
        return _json.dumps({"cards": [dict(good, source=n),
                                      {"front": "Who found it?", "back": "Melvin Calvin in 1950",
                                       "source": n},
                                      dict(good, source=99)]})
    if schema == sd.SCHEMAS["quiz"]:
        q = {"kind": "choice", "question": "Where does the Calvin cycle occur?",
             "choices": ["Stroma", "Thylakoid", "Nucleus", "Cytoplasm"],
             "answer": "stroma", "explanation": "It occurs in the stroma.", "source": n}
        bad = dict(q, choices=["Stroma", "Stroma", "Nucleus", "Cytoplasm"],
                   question="Twice?")
        short = {"kind": "short", "question": "What does glycolysis yield?",
                 "answer": "Two ATP per glucose", "source": n}
        return _json.dumps({"questions": [q, bad] if photo else [short]})
    if schema == sd.SCHEMAS["guide"]:
        text = ("Chlorophyll a absorbs red and blue light." if photo
                else "The electron transport chain makes most of the ATP.")
        return _json.dumps({"points": [{"text": text, "source": n}]})
    if schema == sd.SCHEMAS["glossary"]:
        return _json.dumps({"terms": [
            {"term": "Stroma" if photo else "Glycolysis",
             "definition": ("where the Calvin cycle occurs and fixes carbon dioxide"
                            if photo else "happens in the cytoplasm and yields two ATP"),
             "source": n},
            {"term": "Rubisco", "definition": "an enzyme", "source": n}]})
    raise AssertionError(schema)


@pytest.fixture
def review(built):
    src, nb = built
    return rv.write_review(FakeModels(fake_teacher).chat("m"), src, nb.id)


def test_study_documents_keep_only_items_their_source_backs(review):
    st = sd.make_study_set(FakeModels(fake_teacher).chat("m"), review)
    assert [c["front"] for c in st.cards] == ["Where does the Calvin cycle occur?",
                                              "Where is the Krebs cycle?"]
    assert st.cards[0]["back"] == "In the stroma"
    assert [q["question"] for q in st.quiz] == ["Where does the Calvin cycle occur?",
                                                "What does glycolysis yield?"]
    assert st.quiz[0]["answer"] == "Stroma"           # the choice, as written
    assert [g["heading"] for g in st.guide] == ["Photosynthesis", "Respiration"]
    assert sorted(t["term"] for t in st.glossary) == ["Glycolysis", "Stroma"]
    assert st.dropped == {"cites no passage this section was shown": 2,
                          "not in the passage it cites": 4,
                          "not four different choices": 1}
    for n in st.cited("cards") + st.cited("quiz") + st.cited("glossary"):
        assert st.sources[n].text


def test_a_study_set_round_trips_and_renders(review):
    st = sd.make_study_set(FakeModels(fake_teacher).chat("m"), review)
    again = sd.StudySet.from_dict(_json.loads(_json.dumps(st.to_dict())))
    assert again.to_dict() == st.to_dict()
    html = again.to_html("quiz", lambda t: t)
    assert "Answer key" in html and "A. Stroma" in html
    assert "Answer key" not in again.to_html("quiz", lambda t: t, answers=False)
    assert "Chlorophyll a absorbs" in again.to_html("guide", lambda t: t)


def test_only_the_kinds_asked_for_are_made(review):
    st = sd.make_study_set(FakeModels(fake_teacher).chat("m"), review, ["cards"])
    assert st.cards and not st.quiz and not st.guide and not st.glossary
    with pytest.raises(ValueError):
        sd.make_study_set(FakeModels(fake_teacher).chat("m"), review, [])


def test_a_reply_that_is_not_json_is_counted_not_crashed(review):
    st = sd.make_study_set(FakeModels("sorry, I cannot").chat("m"), review, ["cards"])
    assert not st.cards
    assert st.dropped == {"the model's reply was not the JSON asked for": 2}


def test_the_study_page_makes_from_the_review_and_saves(qapp, built, app_dir):
    src, nb = built
    tab = tab_for(src, FakeModels(fake_teacher))
    assert pump(qapp, lambda: tab.model.currentData() == "granite3.3:8b")
    tab.docs_page.make()
    assert "Write the review first" in tab.status.text()
    tab.review_page.write()
    assert pump(qapp, lambda: tab.review_page.review is not None)
    tab.docs_page.make()
    assert pump(qapp, lambda: tab.docs_page.set is not None)
    assert "Where does the Calvin cycle occur?" in tab.docs_page.view.text.toPlainText()
    assert (app_dir / "plugin_data" / "council.study" / f"{nb.id}.study.json").exists()
    tab.docs_page.show_kind.setCurrentIndex(sd.KINDS.index("glossary"))
    assert "Glycolysis" in tab.docs_page.view.text.toPlainText()
