"""Related by meaning (council_core/kg_related.py) - user, 2026-10-27: "We
should have these are related connections". No model here: a stand-in
embedder (hashed word counts) gives close vectors to texts that share words.
Synthetic files in tmp_path only.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import kg_related as kr  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402

_STOP = {"the", "a", "of", "and", "on", "in", "to", "is", "was", "we", "for", "with", "at"}


class Words:
    """A stand-in embedding model: hashed counts of a text's words."""

    def __init__(self):
        self.calls = 0

    def __call__(self, texts):
        self.calls += len(texts)
        m = np.zeros((len(texts), 512), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z]{3,}", t.lower()):
                if w not in _STOP:
                    m[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 512] += 1
        return kr.normalise(m)


def _vault(tmp_path):
    v = tmp_path / "v"
    d = v / "data_in"
    d.mkdir(parents=True)
    (d / "coating_review.md").write_text(
        "Coating review. Blistering and delamination of the thermal coating on the "
        "panel edges after the humidity soak. Coating adhesion dropped below spec.\n",
        encoding="utf-8")
    (d / "panel_failure.txt").write_text(
        "Panel failure notes: the thermal coating showed delamination and blistering "
        "at the edges; adhesion of the coating was below spec after humidity.\n",
        encoding="utf-8")
    (d / "chili.txt").write_text("Chili recipe: beans, onions, cumin, simmer two hours.\n",
                                 encoding="utf-8")
    (d / "budget.md").write_text("Budget FY26: travel, licences, training, hardware.\n",
                                 encoding="utf-8")
    return v


def _related(kg):
    return {tuple(sorted((r[0], r[1]))): r[2] for r in kg.db.execute(
        "SELECT s.name, o.name, r.status FROM relations r JOIN entities s ON"
        " s.id=r.subject_id JOIN entities o ON o.id=r.object_id WHERE r.predicate='RELATED_TO'")}


PAIR = ("coating_review.md", "panel_failure.txt")


def test_documents_close_in_meaning_get_a_suggested_link(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path)) as kg:
        kg.seed()
        stats = kg.find_related(Words(), model="words", threshold=0.5)
        assert _related(kg) == {PAIR: "suggested"}
        assert stats["links"] == 1
        ev = kg.db.execute("SELECT quote, method, model FROM evidence e JOIN relations r"
                           " ON r.id=e.relation_id WHERE r.predicate='RELATED_TO'").fetchall()
        assert len(ev) == 2                     # one per end
        assert {e[1] for e in ev} == {"embedding"} and {e[2] for e in ev} == {"words"}
        assert "delamination" in ev[0][0] and "closeness" in ev[0][0]
        # counted apart from the facts between entities
        assert kg.counts()["related_links"] == 1


def test_what_was_embedded_is_not_embedded_again(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path)) as kg:
        kg.seed()
        kg.find_related(Words(), model="words", threshold=0.5)
        again = Words()
        stats = kg.find_related(again, model="words", threshold=0.5)
        assert again.calls == 0 and stats["embedded"] == 0
        assert _related(kg) == {PAIR: "suggested"}


def test_a_rebuild_keeps_the_link_until_a_document_changes(tmp_path):
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        kg.find_related(Words(), model="words", threshold=0.5)
        kg.seed()
        assert _related(kg) == {PAIR: "suggested"}
        (v / "data_in" / "panel_failure.txt").write_text("Shift roster for November.\n",
                                                         encoding="utf-8")
        kg.seed()
        assert _related(kg) == {}                # its evidence was the old text
        kg.find_related(Words(), model="words", threshold=0.5)
        assert _related(kg) == {}


def test_a_rejected_pair_is_never_suggested_again(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path)) as kg:
        kg.seed()
        kg.find_related(Words(), model="words", threshold=0.5)
        rid = kg.db.execute("SELECT id FROM relations WHERE predicate='RELATED_TO'"
                            ).fetchone()[0]
        kg.set_relation_status(rid, "rejected")
        kg.find_related(Words(), model="words", threshold=0.5)
        kg.seed()
        assert _related(kg) == {PAIR: "rejected"}


def test_the_model_and_threshold_are_the_graphs_own(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path)) as kg:
        assert kg.embed_model() == "" and kg.related_threshold() == kr.DEFAULT_THRESHOLD
        kg.set_related("ollama:granite-embedding:30m", 0.8)
        assert kg.embed_model() == "ollama:granite-embedding:30m"
        assert kg.related_threshold() == 0.8


def test_pairs_need_both_ends_in_each_others_top_k():
    # 0 and 1 are close; 2 is closest to 0 but 0 has closer ones with k=1
    v = kr.normalise(np.array([[1, 0.1, 0], [1, 0.12, 0], [0.8, 0, 0.6]], dtype=np.float32))
    assert [(i, j) for i, j, _s in kr.related_pairs(v, 0.5, k=1)] == [(0, 1)]
    # one document's own items are never paired
    assert (0, 1) not in [(i, j) for i, j, _s in
                          kr.related_pairs(v, 0.5, k=1, apart=["d", "d", "e"])]


def test_text_is_cut_into_passages_with_where_they_start():
    lines = [[1, i, "word " * 60] for i in range(1, 8)]
    got = kr.passages(lines, words=200)
    assert [loc for loc, _t in got] == [{"page": 1, "line": 1}, {"page": 1, "line": 5}]


def test_only_embedding_models_on_this_pc_are_offered(monkeypatch, tmp_path):
    from council_core import local_models as lm
    monkeypatch.setattr(lm, "ollama_tags", lambda host=None: [
        {"name": "gemma3:12b", "details": {"family": "gemma3"}},
        {"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}},
        {"name": "granite-embedding:30m", "details": {"family": "bert"}}])
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty"))
    ids = [i for i, _label in kr.available()]
    assert ids == ["ollama:granite-embedding:30m", "ollama:nomic-embed-text:latest"]


# ── the Connections tab (offscreen) ──────────────────────────────────────
def test_build_graph_finds_related_items_last(tmp_path, monkeypatch):
    pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")
    from PySide6.QtWidgets import QApplication
    from tests.test_connections_morphik import Actions, wait
    from council_qt.tabs.connections import ConnectionsTab
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setattr(kr, "make", lambda model_id, host=None: Words())
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v) as kg:
        kg.set_related("ollama:granite-embedding:30m", 0.5)

    class NoModel(Actions):
        def local_models(self):
            return []

    view = ConnectionsTab(actions=NoModel(v), ask_yes_no=lambda *a, **k: True)
    try:
        wait(qapp, view)
        view.on_build()
        for _ in range(3):
            wait(qapp, view)
        assert view.status.text().startswith("Related by meaning: 1 link(s) suggested"), \
            view.status.text()
        with kgm.KnowledgeGraph(v) as kg:
            assert _related(kg) == {PAIR: "suggested"}
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_the_settings_dialog_offers_the_models_on_this_pc():
    pytest.importorskip("PySide6", reason="the dialog needs PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.connections_dialogs import GraphDialog
    QApplication.instance() or QApplication([])
    dlg = GraphDialog(new=False, embed_models=[("ollama:granite-embedding:30m", "granite")],
                      embed_model="ollama:granite-embedding:30m", related_threshold=0.8)
    v = dlg.values()
    assert v["embed_model"] == "ollama:granite-embedding:30m" and v["related_threshold"] == 0.8
    dlg.embed_model.setCurrentIndex(0)
    assert dlg.values()["embed_model"] == ""
    dlg.deleteLater()
