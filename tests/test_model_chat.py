"""
council_core.model_chat — "what models can I download?" and "download X" in
the Council chat, plus the Council tab routing them before a turn.

Nothing touches the network: model_jobs is replaced by a stand-in whose
methods have the real ones' names and shapes.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_catalog
from council_core import model_chat, model_jobs


class Jobs:
    """model_jobs, minus the hardware probe and the network."""

    def __init__(self, tmp_path, rows=None, space_problem=None):
        self.dir = tmp_path
        self.rows = rows if rows is not None else [
            model_jobs.to_row({**vars(m), "fits_vram": i < 2,
                               "origin_verified": True})
            for i, m in enumerate(model_catalog.MODELS)]
        self.space_problem = space_problem
        self.downloads = []

    def detect_hardware(self):
        return model_jobs.Hardware(gpu="RTX 5080", vram_gb=16, ram_gb=64)

    def find(self, hardware):
        return model_jobs.FindResult(True, f"{len(self.rows)} model(s).",
                                     rows=self.rows)

    def models_dir(self):
        return self.dir

    def check_space(self, dest, size):
        return self.space_problem

    def download(self, repo, filename, *, on_progress=None, size_gb=None):
        self.downloads.append((repo, filename))
        for done in (0, 50, 100):
            on_progress(done, 100)
        return self.dir / filename

    progress_line = staticmethod(model_jobs.progress_line)


@pytest.fixture
def chat(tmp_path):
    said = []
    c = model_chat.ModelChat(lambda w, t, k: said.append((w, t, k)),
                             jobs=Jobs(tmp_path))
    c.said = said
    return c


def run(chat, text):
    job = chat.plan(text)
    assert job is not None, text
    job()
    return chat.said[-1][1]


@pytest.mark.parametrize("text", [
    "what models can I download?",
    "which models can I run",
    "what LLM models should I use",
    "which models fit my machine?",
    "what models are available",
    "list downloadable models",
    "show me the models I can download",
    "recommend a model",
    "suggest some coding models",
])
def test_availability_phrasings(chat, text):
    assert chat.plan(text) == chat.available


@pytest.mark.parametrize("text", [
    "list pipelines", "what is a model of grain growth?",
    "download the report as csv", "tell me about Sarah Smith",
])
def test_other_messages_are_left_alone(chat, text):
    assert chat.plan(text) is None


def test_the_list_names_hardware_fit_and_how_to_download(chat):
    text = run(chat, "what models can I download?")
    assert "GPU: RTX 5080" in text
    assert "fits your GPU" in text and "runs on CPU" in text
    assert "[granite-3.1-8b-q4]" in text
    assert 'say "download <name>"' in text
    assert "good for code" in text


def test_download_by_catalog_id(chat):
    text = run(chat, "download granite-3.1-8b-q4")
    assert chat.jobs.downloads == [("bartowski/granite-3.1-8b-instruct-GGUF",
                                    "granite-3.1-8b-instruct-Q4_K_M.gguf")]
    assert "not the active model yet" in text
    assert "not verified" not in text


def test_download_by_name_words(chat):
    run(chat, "download llama 3.2 3b")
    assert chat.jobs.downloads[0][1] == "Llama-3.2-3B-Instruct-Q5_K_M.gguf"


def test_an_ambiguous_name_lists_the_choices(chat):
    text = run(chat, "download granite 8b")
    assert "matches 2 models" in text and chat.jobs.downloads == []


def test_progress_is_reported_every_ten_percent_not_every_chunk(chat):
    run(chat, "download phi 4")
    progress = [t for w, t, k in chat.said if w == "Workflow"]
    assert len(progress) == 3 and "(100%)" in progress[-1]


def test_a_raw_repo_download_works_with_an_origin_note(chat):
    text = run(chat, "download someone/cool-GGUF cool-7b.Q4_K_M.gguf")
    assert chat.jobs.downloads == [("someone/cool-GGUF",
                                    "cool-7b.Q4_K_M.gguf")]
    assert "origin was not verified" in text


def test_a_non_us_model_is_refused(chat):
    text = run(chat, "download Qwen/Qwen2.5-7B-GGUF qwen2.5-7b.gguf")
    assert "non-US" in text and chat.jobs.downloads == []


def test_no_room_is_said_before_downloading(tmp_path):
    said = []
    c = model_chat.ModelChat(lambda w, t, k: said.append(t),
                             jobs=Jobs(tmp_path, space_problem="Not enough "
                                       "room: 9 GB needed"))
    c.plan("download phi 4")()
    assert said[-1].startswith("Not enough room")
    assert c.jobs.downloads == []


def test_one_download_at_a_time(chat):
    model_chat.ModelChat._download_lock.acquire()
    try:
        assert "already running" in run(chat, "download phi 4")
    finally:
        model_chat.ModelChat._download_lock.release()
    assert chat.jobs.downloads == []


def test_a_failed_download_says_so(chat):
    def boom(*a, **k):
        raise RuntimeError("HTTP 404")
    chat.jobs.download = boom
    assert run(chat, "download phi 4") == "Download failed: HTTP 404"


def test_catalog_matching_is_whole_words():
    assert model_chat.catalog_matches("gem") == []       # not a substring
    assert [m.id for m in model_chat.catalog_matches("gemma")] == \
        ["gemma-2-9b-q4"]


# ============================================================
# Through the Council tab
# ============================================================

pytest.importorskip("PySide6", reason="the Council tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def test_the_council_answers_model_questions_without_a_turn(qapp, tmp_path):
    from council_qt.tabs.council import CouncilActions, CouncilTab

    class Actions(CouncilActions):
        def send(self, *a, **k):
            raise AssertionError("a model command must not start a turn")

    tab = CouncilTab(actions=Actions(vault_dir=tmp_path))
    tab._model_chat = model_chat.ModelChat(
        lambda w, t, k: tab._to_ui(tab.append, w, t, k), jobs=Jobs(tmp_path))
    tab.input.setPlainText("what models can I download?")
    tab.on_send()
    deadline = time.time() + 5
    while "best fit first" not in tab.transcript.toPlainText() \
            and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert "best fit first" in tab.transcript.toPlainText()
    assert not tab._turn_active
    tab.deleteLater()
    qapp.processEvents()
