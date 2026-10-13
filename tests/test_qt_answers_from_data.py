"""
"Answers from your data" (Batch 2) — the real Qt Council tab, the real engine,
a fake Ollama that records every prompt, a temp vault with a planted fact.

The survey (docs/qt_migration/remaining_scope_2026-10-06.md section 1): the Qt
Council sent only the typed question to the models; a fact planted in a temp
vault reached 0 of 13 prompts with deliberation on and 0 of 1 in the default
build, while Tk put it in the Writer's prompt and showed a file chip. The Qt
turn now goes through council_core.turn_context.prepare_turn — the function
the Tk shell calls too.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

FACT = "ORCHID"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress

    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "pumps.csv").write_text(
        f"pump,bay,name,pressure_bar\nP1,4,{FACT},12.5\nP2,5,LILAC,9.1\n",
        encoding="utf-8")
    with FakeOllama() as fake:
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_MEMORY_SCOPE",
                    "COUNCIL_DEMO_SILO", "COUNCIL_REMOTE_NODES"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refused = refuse_egress(monkeypatch)
        yield SimpleNamespace(ce=ce, vault=vault, fake=fake, refused=refused)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _council(vault, *, demo_mode=True, stub_open=True):
    """The real tab. ``stub_open``: a chip click is recorded instead of
    shown (most tests only need to know which file a chip names)."""
    from council_qt.tabs.council import CouncilActions, CouncilTab
    from council_qt.window import CouncilWindow
    window = CouncilWindow()
    view = CouncilTab(window, actions=CouncilActions(vault_dir=vault,
                                                     demo_mode=demo_mode),
                      demo_mode=demo_mode)
    opened = []
    if stub_open:
        view.open_source = opened.append
    # Never the real file manager: the desktop is not the test's to open.
    view.reveal_source = opened.append
    return window, view, opened


def _ask(qapp, view, question, timeout=120.0):
    view.input.setPlainText(question)
    view.on_send()
    assert _pump(qapp, lambda: not view._turn_active, timeout=timeout), (
        "the turn never finished")
    for _ in range(20):
        qapp.processEvents()


def _writer_prompts(fake, since=0):
    return ["\n".join(m.get("content") or "" for m in body.get("messages")
                      or [] if m.get("role") != "system")
            for body in fake.state.chats[since:]]


def _close(qapp, window, view):
    window.request_close()
    view.deleteLater()
    qapp.processEvents()


def test_the_default_build_answers_with_the_file_in_front_of_it(qapp,
                                                                engine):
    """The default build is one Writer (DEMO_MODE): it was a plain chatbot."""
    window, view, opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        prompts = _writer_prompts(engine.fake)
        answering = [p for p in prompts if "USER TASK" in p]
        assert answering, f"no personality prompt reached the model: {prompts}"
        assert FACT in answering[-1], answering[-1]
        assert answering[-1].rstrip().endswith("Summarize pumps.csv")

        # The chip under the answer, and a click on it.
        assert view.sources_frame.isVisibleTo(view)
        assert [c.text() for c in view.source_chips] == ["📄 pumps.csv"]
        view.source_chips[0].click()
        assert [Path(p).name for p in opened] == ["pumps.csv"]
        assert "Sources: pumps.csv" in view.transcript.toPlainText()
        assert not engine.refused
    finally:
        _close(qapp, window, view)
    rows = [json.loads(line) for line in next(
        (engine.vault / "conversations").glob("*.jsonl"))
        .read_text(encoding="utf-8").splitlines()]
    answer = next(r for r in rows if r["who"] == "Writer")
    assert [Path(p).name for p in answer["provenance"]["sources"]] == [
        "pumps.csv"]
    assert rows[0]["text"] == "Summarize pumps.csv", (
        "the conversation keeps what was TYPED, not the vault context")


def test_a_chip_opens_the_file_read_only_in_the_app(qapp, engine):
    """A click shows the file in a preview inside the app — its first rows
    and its columns, read-only (Tk's _preview_data_file) — with a "Show in
    folder" button. Before, a click opened the folder in the file manager
    and nothing was shown in the app. Nothing is written to the file."""
    window, view, revealed = _council(engine.vault, stub_open=False)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        source = engine.vault / "data_in" / "pumps.csv"
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        assert "preview" in view.source_chips[0].toolTip().lower()
        view.source_chips[0].click()
        preview = view.source_preview
        assert preview is not None and preview.isVisible()
        assert _pump(qapp, lambda: not preview.loading, timeout=30)
        assert "pumps.csv" in preview.windowTitle()
        assert FACT in preview.rows.toPlainText()
        assert "pressure_bar" in preview.schema.toPlainText()
        assert preview.rows.isReadOnly() and preview.schema.isReadOnly()
        assert not revealed, "the file manager was opened without a click"
        preview.reveal_button.click()
        assert [Path(p).name for p in revealed] == ["pumps.csv"]
        preview.close()
        assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    finally:
        _close(qapp, window, view)


def test_a_chip_for_a_file_that_has_gone_says_so(qapp, engine):
    window, view, _revealed = _council(engine.vault, stub_open=False)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        source = engine.vault / "data_in" / "pumps.csv"
        source.rename(source.with_name("pumps_moved.csv"))
        view.source_chips[0].click()
        qapp.processEvents()
        assert "pumps.csv is no longer there" in view.transcript.toPlainText()
        preview = view.source_preview
        assert preview is None or not preview.isVisible()
    finally:
        _close(qapp, window, view)


def test_a_chip_click_reads_the_file_off_the_gui_thread(qapp, engine,
                                                       monkeypatch):
    """Found in review: the click read the file on the GUI thread (a 53 MB
    JSON froze the window for 4 s). The dialog opens at once, says it is
    reading, and is filled when the read — on a worker — is done."""
    import threading
    from council_core import turn_context
    window, view, _revealed = _council(engine.vault, stub_open=False)
    release, readers = threading.Event(), []
    real = turn_context._data_preview_text

    def slow(path, *a, **k):
        readers.append(threading.current_thread())
        release.wait(30)
        return real(path, *a, **k)

    try:
        _ask(qapp, view, "Summarize pumps.csv")
        monkeypatch.setattr(turn_context, "_data_preview_text", slow)
        started = time.monotonic()
        view.source_chips[0].click()
        assert time.monotonic() - started < 1.0, "the click waited for it"
        preview = view.source_preview
        assert preview.isVisible() and preview.loading
        assert "Reading" in preview.rows.toPlainText()
        assert _pump(qapp, lambda: bool(readers), timeout=10)
        assert readers[0] is not threading.main_thread()
        release.set()
        assert _pump(qapp, lambda: not preview.loading, timeout=30)
        assert FACT in preview.rows.toPlainText()
    finally:
        release.set()
        _close(qapp, window, view)


def test_a_folder_chip_shows_the_folder(qapp, engine):
    """Found in review: a chip for a folder (a question naming one) said
    "<name> is no longer there" although the folder was there — the click
    took only files. It lists the folder now, read-only."""
    window, view, _revealed = _council(engine.vault, stub_open=False)
    try:
        folder = engine.vault / "data_in"
        view.show_sources([folder])
        assert [c.text() for c in view.source_chips] == ["📁 data_in"]
        view.source_chips[0].click()
        preview = view.source_preview
        assert preview is not None and preview.isVisible()
        assert _pump(qapp, lambda: not preview.loading, timeout=30)
        assert "pumps.csv" in preview.rows.toPlainText()
        assert "no longer there" not in view.transcript.toPlainText()
        preview.reveal_button.click()
        assert _revealed == [folder]
    finally:
        _close(qapp, window, view)


def test_show_in_folder_selects_the_file(tmp_path):
    """The command "Show in folder" runs on each platform (never run here):
    the file selected in Explorer and Finder, its folder elsewhere."""
    from council_qt.widgets import source_preview
    path = tmp_path / "a folder" / "pumps.csv"
    assert source_preview.reveal_command(path, "win32") == (
        f'explorer /select,"{path}"')
    assert source_preview.reveal_command(path, "darwin") == ["open", "-R",
                                                      str(path)]
    assert source_preview.reveal_command(path, "linux") == ["xdg-open",
                                                     str(path.parent)]


def test_a_value_question_finds_the_file_without_being_told(qapp, engine):
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Which pump is in bay 4?")
        answering = [p for p in _writer_prompts(engine.fake)
                     if "USER TASK" in p]
        assert FACT in answering[-1]
        assert "📄 pumps.csv" in [c.text() for c in view.source_chips]
    finally:
        _close(qapp, window, view)


def test_a_count_is_answered_from_the_data_without_a_model(qapp, engine):
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "How many files are in the vault?")
        assert view._last_route == "data"
        assert "file(s)" in (view._last_answer or "")
        assert not [p for p in _writer_prompts(engine.fake)
                    if "USER TASK" in p], "a personality was asked anyway"
        assert "no model was asked" in view.transcript.toPlainText()
    finally:
        _close(qapp, window, view)


def test_the_next_turn_clears_the_chips(qapp, engine):
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        assert view.source_chips
        _ask(qapp, view, "Thanks, that is all.")
        assert not view.source_chips and not view.sources_frame.isVisibleTo(
            view)
    finally:
        _close(qapp, window, view)


def test_the_full_council_gives_every_drafting_role_the_file(qapp, engine):
    """With deliberation on, the survey measured 0 of 13 prompts. The vault
    blocks go in front of the question, so every role sees them (as in Tk)."""
    window, view, _opened = _council(engine.vault, demo_mode=False)
    try:
        view._checkboxes["deliberate"].setChecked(True)
        _ask(qapp, view, "Summarize pumps.csv", timeout=240)
        answering = [p for p in _writer_prompts(engine.fake)
                     if "USER TASK" in p or "USER REQUEST" in p]
        assert answering
        with_fact = [p for p in answering if FACT in p]
        assert len(with_fact) >= len(answering) // 2, (
            f"{len(with_fact)} of {len(answering)} prompts carried the file")
        assert view.source_chips
    finally:
        _close(qapp, window, view)


def test_forget_works_where_the_notice_offers_it(qapp, engine):
    """The close-match notice under a vault search says "If any are wrong,
    type: forget WORD". Before, the Qt tab sent "forget zebrafin" to the
    Writer as a question; now it goes on the vault's list (no model asked)
    and the next search does not use it."""
    (engine.vault / "data_in" / "real.txt").write_text(
        "the zebrafin gasket datasheet\n", encoding="utf-8")
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Search the vault for zebrafn")
        assert "forget WORD" in view.transcript.toPlainText()
        asked = len(engine.fake.state.chats)
        view.input.setPlainText("forget zebrafin")
        view.on_send()
        for _ in range(20):
            qapp.processEvents()
        assert not view._turn_active
        assert len(engine.fake.state.chats) == asked, "a model was asked"
        text = view.transcript.toPlainText()
        assert "no longer be used as close matches" in text, text
        assert "zebrafin" in json.loads(
            (engine.vault / "fuzzy_denylist.json").read_text(
                encoding="utf-8"))
        # The next search does not use the forgotten match.
        offered = text.count("zebrafn → zebrafin")
        _ask(qapp, view, "Search the vault for zebrafn")
        assert view.transcript.toPlainText().count(
            "zebrafn → zebrafin") == offered, "the forgotten match was used"
        # "forget …" that is not an offered word is a question.
        asked = len(engine.fake.state.chats)
        _ask(qapp, view, "forget what I said and summarize pumps.csv")
        assert len(engine.fake.state.chats) > asked
    finally:
        _close(qapp, window, view)


def test_a_prompt_too_long_for_the_model_is_said_and_kept(qapp, engine,
                                                         monkeypatch):
    """The context-window notice reaches the Qt transcript in plain words,
    and the conversation keeps it as the app's own line."""
    monkeypatch.setattr(engine.ce, "effective_n_ctx", lambda *_a, **_k: 64)
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        text = view.transcript.toPlainText()
        assert "too long for what the model can read at once" in text, text
        assert "n_ctx" not in text
        assert view._last_answer, "the question was not answered"
    finally:
        _close(qapp, window, view)
    rows = [json.loads(line) for line in next(
        (engine.vault / "conversations").glob("*.jsonl"))
        .read_text(encoding="utf-8").splitlines()]
    assert any(r["who"] == "Council" and "too long" in r["text"]
               for r in rows), rows


#: The marker council_engine._clamp_messages_to_ctx leaves where it cut a
#: prompt that did not fit the window.
CLAMP_MARK = "trimmed to fit the model's context window"


def _writer_requests(fake):
    """(system, the rest) of every request the Writer made."""
    out = []
    for body in fake.state.chats:
        msgs = body.get("messages") or []
        system = "\n".join(m.get("content") or "" for m in msgs
                           if m.get("role") == "system")
        rest = "\n".join(m.get("content") or "" for m in msgs
                         if m.get("role") != "system")
        if "WRITER of the Council" in system:
            out.append((system, rest))
    return out


def test_named_files_reach_the_writer_whole_or_are_said_left_out(qapp,
                                                                  engine):
    """Found in review of 217f2d4: the vault blocks were sized for 75% of
    Ollama's 8,192-token window, as if they were the whole prompt. The
    Writer's call adds its own instructions (about 5,000 characters) and
    this conversation's earlier turns, and keeps 2,400 tokens for the
    answer, so the engine cut the middle out of the Writer's prompt — a
    named file and the fact in it — while the transcript said only "Left
    out 1 part" and the chips still named the file. What the Writer is sent
    now fits as it is sent, and whatever had to go is said."""
    serial = "ZEBRAFIN-7731"
    filler = "Routine inspection text for the line, nothing unusual. " * 140
    names = ("alpha", "bravo", "charlie", "delta")
    for name in names:
        fact = (f"The serial of the spare is {serial}.\n" if name == "bravo"
                else "")
        (engine.vault / "data_in" / f"{name}.txt").write_text(
            fact + filler + "\n" + filler, encoding="utf-8")
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Compare alpha.txt, bravo.txt, charlie.txt and "
                         "delta.txt")
        text = view.transcript.toPlainText()
        chips = [c.text() for c in view.source_chips]
    finally:
        _close(qapp, window, view)
    writer = _writer_requests(engine.fake)
    assert writer, "the Writer was not asked"
    _system, prompt = writer[-1]
    assert CLAMP_MARK not in prompt, (
        "the engine cut the Writer's prompt to fit, and nothing said so")
    for name in names:
        if f"[FILE: {name}.txt" not in prompt:
            assert f"{name}.txt (a file you named)" in text, (name, text)
            assert f"📄 {name}.txt" not in chips, (name, chips)
    assert "[FILE: bravo.txt" in prompt and serial in prompt
    assert not engine.refused


@pytest.mark.parametrize("step", ["prepare_turn", "begin_turn"])
def test_a_failing_context_step_still_answers_and_says_so(qapp, engine,
                                                         monkeypatch, step):
    """The memo and the analyst (begin_turn) run before the rest of the
    turn context now, beside council-map's lookups: either failing costs
    the vault, not the answer."""
    from council_core import turn_context

    def broken(*_a, **_k):
        raise OSError("the share went away")

    monkeypatch.setattr(turn_context, step, broken)
    window, view, _opened = _council(engine.vault)
    try:
        _ask(qapp, view, "Summarize pumps.csv")
        text = view.transcript.toPlainText()
        assert "could not be read for this question" in text
        assert view._last_answer, "the question was not answered"
    finally:
        _close(qapp, window, view)
