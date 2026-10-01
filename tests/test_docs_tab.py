"""
council_qt.tabs.docs — the Docs tab, offscreen.

Every tab here is built with DocsActions over temp paths (config, vault,
checks) and a stub model; retrieval is the real bundled server over the
benchmark package, so what the tab shows is what a user would see.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from PySide6.QtCore import QUrl, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import docs_bench, docs_qa, docs_servers as ds  # noqa
from council_core import model_slots  # noqa: E402
from council_qt.tabs.docs import (DocsActions, DocsTab,  # noqa: E402
                                  render_answer)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=10.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


def answers(question_reply, answer_reply):
    def call(messages, *, json_schema=None, temperature=0.1, num_predict=600,
             seed=None, should_stop=None):
        if "search queries" in messages[0]["content"]:
            return question_reply
        return answer_reply(messages) if callable(answer_reply) \
            else answer_reply
    return call


MODELS = [
    {"id": "ollama:llama3.1:8b", "name": "llama3.1:8b", "backend": "ollama",
     "origin": "US"},
    {"id": "ollama:qwen2.5:7b", "name": "qwen2.5:7b", "backend": "ollama",
     "origin": "non-US"},
]


@pytest.fixture
def make_tab(qapp, tmp_path):
    made = []

    def make(model_call=None, servers=None):
        cfg = tmp_path / "docs_servers.json"
        ds.save(servers if servers is not None
                else [docs_bench.bench_server()], cfg)
        actions = DocsActions(config_path=cfg, vault_dir=tmp_path / "vault",
                              checks_path=tmp_path / "checks.json",
                              model_call=model_call,
                              models=lambda: list(MODELS))
        tab = DocsTab(None, actions)
        tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        made.append(tab)
        return tab

    yield make
    for tab in made:
        tab._stop.set()
        pump(qapp, lambda t=tab: not t._busy, 15)
        tab.deleteLater()
    qapp.processEvents()
    ds.close_all()


def test_the_tab_is_registered():
    from council_qt.tabs import REGISTRY, build_docs
    assert any(f is build_docs for _t, f, _e in REGISTRY)


def test_building_starts_nothing_and_lists_the_servers(make_tab):
    before = {t.name for t in threading.enumerate()}
    t0 = time.perf_counter()
    tab = make_tab()
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"building took {elapsed:.2f} s"
    assert tab.server_list.count() == 1
    assert "Docs benchmark" in tab.server_list.item(0).text()
    new = {t.name for t in threading.enumerate()} - before
    assert not any(n.startswith("docs-") for n in new)
    assert "No model is chosen for docs yet" in tab.role_label.text()


def test_asking_shows_a_cited_answer_and_its_source(qapp, make_tab):
    model = answers('{"queries": ["ledger capacity"], "package": '
                    '"glimmerquay"}',
                    '{"answer": "The default is 64 [1].", "sources": [1], '
                    '"covered": true}')
    tab = make_tab(model)
    tab.question.setPlainText("What is the default capacity of a Ledger?")
    tab.on_ask()
    assert tab._busy and tab.stop_btn.isEnabled()
    pump(qapp, lambda: not tab._busy)
    html = tab.answer_view.toHtml()
    assert "src:1" in html and "The default is 64" in html
    assert tab.status.text().startswith("Answered:")
    tab.on_link(QUrl("src:1"))
    assert "capacity: int = 64" in tab.source_view.toPlainText()
    assert not tab.stop_btn.isEnabled()


def test_write_code_fills_the_code_box_and_copy_copies(qapp, make_tab):
    code = ("from glimmerquay import encode_frame\n\ndef frame_hello():\n"
            "    return encode_frame(b'hello', checksum='xor8', pad_to=4)\n")
    model = answers('{"queries": ["encode_frame"], "package": '
                    '"glimmerquay"}',
                    json.dumps({"answer": "Use encode_frame [1].",
                                "sources": [1], "covered": True,
                                "code": code}))
    tab = make_tab(model)
    tab.question.setPlainText("Write frame_hello() with xor8, pad_to 4")
    tab.write_code.setChecked(True)
    tab.on_ask()
    pump(qapp, lambda: not tab._busy)
    assert tab.code.toPlainText() == code.rstrip("\n") or \
        tab.code.toPlainText() == code
    assert "no problems found" in tab.code_status.text()
    tab.on_copy()
    assert QApplication.clipboard().text().startswith(
        "from glimmerquay import encode_frame")


def test_stop_stops_a_slow_model(qapp, make_tab):
    started = threading.Event()

    def slow(messages, *, should_stop=None, **kw):
        started.set()
        while not should_stop():
            time.sleep(0.01)
        raise RuntimeError("cancelled")

    tab = make_tab(slow)
    tab.question.setPlainText("What is the default capacity of a Ledger?")
    tab.on_ask()
    assert started.wait(10)
    t0 = time.monotonic()
    tab.on_stop()
    pump(qapp, lambda: not tab._busy, 5)
    assert time.monotonic() - t0 < 2.0
    assert tab.status.text() == "Stopped."


def test_adding_testing_and_removing_a_server(qapp, make_tab):
    tab = make_tab()
    fixture = ROOT / "tests" / "data" / "mcp_fixture_server.py"
    tab.kind.setCurrentIndex(1)
    tab.add_name.setText("fixture")
    tab.add_target.setText(f'"{sys.executable}" "{fixture}"')
    tab.on_add()
    assert tab.server_list.count() == 2
    assert tab.server_list.currentRow() == 1
    tab.on_test()
    pump(qapp, lambda: not tab._busy)
    info = tab.server_info.toPlainText()
    assert "Connected" in info and "search_docs" in info
    tab.on_remove()
    assert tab.server_list.count() == 1


def test_a_remote_url_needs_the_tick(make_tab):
    tab = make_tab()
    tab.kind.setCurrentIndex(2)
    tab.add_target.setText("http://docs.example.com/mcp")
    tab.on_add()
    assert "Allow remote" in tab.status.text()
    assert tab.server_list.count() == 1
    tab.allow_remote.setChecked(True)
    tab.on_add()
    assert tab.server_list.count() == 2


def test_the_model_picker_marks_non_us_and_saves_the_choice(qapp, make_tab,
                                                            tmp_path):
    tab = make_tab()
    tab.refresh_models()
    pump(qapp, lambda: tab.model_combo.count() == 3)
    texts = [tab.model_combo.itemText(i) for i in range(3)]
    assert "non-US: for measurement only" in texts[2]
    assert "non-US" not in texts[1]
    assert tab.model_combo.currentIndex() == 0, \
        "nothing may be preselected that the user did not choose"
    tab.model_combo.setCurrentIndex(1)
    tab.on_use_model()
    cfg = model_slots.load(tmp_path / "vault")
    assert cfg.slots[cfg.roles["docs"]].path == "ollama:llama3.1:8b"
    assert "llama3.1:8b (Ollama)" in tab.role_label.text()
    assert tab.role_label.text().startswith("Answers come from the docs role")


def test_the_capability_check_reports_and_is_remembered(qapp, make_tab,
                                                        tmp_path):
    tab = make_tab(docs_bench.oracle_model_call())
    qa_slot = docs_qa.assign_docs_model("ollama:llama3.1:8b",
                                        tmp_path / "vault")
    assert "llama3.1:8b" in qa_slot
    tab.on_check()
    pump(qapp, lambda: not tab._busy, 60)
    assert "5/5 passed" in tab.check_result.text()
    assert "good for docs questions" in tab.check_result.text()
    assert "llama3.1:8b (Ollama): 5/5" in tab.checks_label.text()
    # ...and the picker now marks it, and only it.
    tab.refresh_models()
    pump(qapp, lambda: tab.model_combo.count() == 3)
    texts = [tab.model_combo.itemText(i) for i in range(3)]
    assert "★ passed the docs check" in texts[1]
    assert "★" not in texts[2], "a non-US model is never marked as a pick"


def test_a_non_us_model_is_measured_but_never_recommended(qapp, make_tab,
                                                          tmp_path):
    tab = make_tab(docs_bench.oracle_model_call())
    docs_qa.assign_docs_model("ollama:qwen2.5:7b", tmp_path / "vault")
    tab.on_check()
    pump(qapp, lambda: not tab._busy, 60)
    assert "5/5 passed" in tab.check_result.text()
    assert "non-US: measured only, not recommended" in \
        tab.check_result.text()
    assert "good for docs" not in tab.check_result.text()
    assert "qwen2.5:7b (Ollama): 5/5" in tab.checks_label.text()
    assert "non-US, measured only" in tab.checks_label.text()
    tab.refresh_models()
    pump(qapp, lambda: tab.model_combo.count() == 3)
    assert not any("★" in tab.model_combo.itemText(i) for i in range(3))


def test_render_answer_links_only_real_sources():
    a = docs_qa.DocsAnswer(ok=True, covered=True,
                           answer="Yes [1]; see x[2] and [9].",
                           sources=[docs_qa.Source(1, "srv", "a.b", "a.b",
                                                   "text")], cited=[1])
    html = render_answer(a)
    assert "href='src:1'" in html
    assert "src:9" not in html and "src:2" not in html
    assert "x[2]" in html
