"""
The Ledger tab's "Teach from a sample workbook…" dialog
(council_qt/tabs/ledger_teach_dialog.py), driven like a user would.

The user's request (2026-10-20): "taking a sample workbook from the folder and
I line up where they match the templates, or say data found in the images
and what they are in the template. First I want an LLM guess to be made, then
I give an answer — hopefully this will help train the Ledger and knowledge
graph to work."

What is pinned: the button needs a parent CSV with its schema (a pasted
path is looked up when it is pressed); the dialog lists the tab's sources
and guesses on a 'ledger-teach-guess' worker (the model is never asked on
the GUI thread, which stays live: progress, Cancel); the answers are
pre-filled from the guess and can be changed; the model's guesses are kept
only when confirmed; an inspector's row can be 'Not an inspector'; Save
writes the aliases, ignores, inspectors' labels, people and pattern, and
ticks the knowledge graph on a 'ledger-teach-learn' worker; Guess again
shows what the rules place now; the tab forgets a saved pattern. Offscreen;
synthetic workbooks; a fake model; no file dialogs; a temporary vault.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
openpyxl = pytest.importorskip("openpyxl")
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from test_ledger_teach import (INSPECTOR, FakeModel, _labelled_book,  # noqa: E402
                               _unlabelled_book)

from council_core import knowledge_graph as kgm  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_llm  # noqa: E402
from council_core import ledger_teach as lt  # noqa: E402
from council_qt.tabs.connections import ConnectionsActions  # noqa: E402
from council_qt.tabs.ledger import LedgerActions, LedgerTab  # noqa: E402
from council_qt.tabs.ledger_teach_dialog import (ANSWER_COL, COLUMNS,  # noqa: E402
                                                 CONFIRM_COL, PICTURE_COL, TeachDialog)
from sample_ledger import ParentCSV  # noqa: E402

MODEL = "ollama:granite3.3:8b"
FROM_COL = COLUMNS.index("From")
GUESS_COL = COLUMNS.index("Council's guess")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ledger(tmp_path):
    """A parent CSV with its schema, a folder of two sample workbooks, and
    a temporary Connections vault."""
    ps = li.create_parent(lf.make_template(tmp_path / "template.xlsx"))
    src = tmp_path / "sources"
    src.mkdir()
    untitled = _unlabelled_book(src / "untitled.xlsx")
    sample = _labelled_book(src / "sample.xlsx", with_picture=True)
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    return Path(ps.csv), ParentCSV(ps.csv).schema, src, untitled, sample, vault


class ThreadModel(FakeModel):
    """test_ledger_teach's fake model, noting the thread each question was
    asked on."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.threads = []

    def __call__(self, prompt, schema):
        self.threads.append(threading.current_thread())
        return super().__call__(prompt, schema)


def use_model(monkeypatch, model):
    """Council's model, as ledger_llm.chat_for would give it (the US-origin
    check is ledger_teach's own test)."""
    asked_for = []

    def chat_for(model_id, timeout=120, should_stop=None):
        asked_for.append(model_id)
        return model
    monkeypatch.setattr(ledger_llm, "chat_for", chat_for)
    return asked_for


def make_tab(qapp, csv, vault, sources=(), connections=None):
    actions = LedgerActions(image_reader=lf.fake_reader(lf.CONCEALMENT_IMAGE_ROWS),
                            opener=[].append,
                            connections=connections or ConnectionsActions(vault_dir=vault))
    tab = LedgerTab(None, actions)
    picked = []

    def pick(parent=None):
        picked.append(parent)
        return MODEL
    tab._pick_model = pick
    tab._pick_workbook = lambda parent=None: ""
    tab.csv.setText(str(csv))
    tab._on_csv_changed()                       # what editingFinished does
    tab.add_sources(sources)
    return tab, picked


def open_teach(tab) -> TeachDialog:
    assert tab.teach_btn.isEnabled()
    tab.teach_btn.click()
    dlg = tab.teach_dialog
    assert isinstance(dlg, TeachDialog) and dlg.isVisible()
    assert dlg.wait()                           # the sources are listed
    return dlg


def choose(dlg: TeachDialog, path: Path) -> None:
    i = dlg.workbook_choice.findData(str(path))
    assert i >= 0, f"{path.name} is not listed"
    dlg.workbook_choice.setCurrentIndex(i)


def guess(dlg: TeachDialog) -> lt.TeachSession:
    assert dlg.guess_btn.isEnabled()
    dlg.guess_btn.click()
    assert dlg.wait()
    assert dlg.session is not None, dlg.status.text()
    return dlg.session


def row_of(dlg: TeachDialog, label: str) -> int:
    return next(i for i in range(dlg.table.rowCount())
                if dlg.table.item(i, 1).text() == label)


def key_of(dlg: TeachDialog, label: str) -> str:
    return next(r.key for r in dlg.session.rows if r.label == label)


def cell(dlg: TeachDialog, label: str, col: int) -> str:
    return dlg.table.item(row_of(dlg, label), col).text()


def close(dlg: TeachDialog) -> None:
    dlg.reject()
    dlg.wait()
    QApplication.processEvents()


# ── the button ──────────────────────────────────────────────────────────
def test_the_teach_button_needs_a_parent_csv_with_its_schema(qapp, tmp_path):
    vault = tmp_path / "vault"
    template = lf.make_template(tmp_path / "template.xlsx")
    tab, _ = make_tab(qapp, "", vault)
    assert not tab.teach_btn.isEnabled()
    plain = tmp_path / "plain.csv"
    plain.write_text("a,b\n1,2\n", encoding="utf-8")
    tab.csv.setText(str(plain))
    tab._on_csv_changed()
    assert not tab.teach_btn.isEnabled()                        # no schema beside it
    tab.on_teach_workbook()
    assert "no schema" in tab.status.text() and tab.teach_dialog is None
    tab.csv.setText("")
    tab.template.setText(str(template))
    tab.create_btn.click()
    assert tab.wait()
    assert tab.teach_btn.isEnabled()                            # Create made the schema
    # a path typed or pasted - no Enter, no Tab - is looked up when Teach
    # is pressed: the button stayed off until editingFinished (review,
    # 2026-10-20)
    made = tab.csv.text()
    tab.csv.setText(made + "x")
    assert tab.teach_btn.isEnabled()
    tab.teach_btn.click()
    assert "no schema" in tab.status.text() and tab.teach_dialog is None
    assert not tab.teach_btn.isEnabled()                        # looked up: none there
    tab.csv.setText(made)                                       # pasted, not yet looked up
    assert tab.teach_btn.isEnabled()
    tab.teach_btn.click()
    assert isinstance(tab.teach_dialog, TeachDialog)
    close(tab.teach_dialog)
    assert "Teach from a sample workbook" in tab.teach_btn.text()


# ── the guess ───────────────────────────────────────────────────────────
def test_guess_with_councils_model_fills_the_answers(qapp, ledger, monkeypatch):
    csv, schema, src, untitled, sample, vault = ledger
    freq = next(f.name for f in schema.fields if "Frequency" in f.name)
    model = ThreadModel({"Zorp rate": freq, "Tech Sig": INSPECTOR})
    asked_for = use_model(monkeypatch, model)
    tab, picked = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    # the tab's sources, listed on a worker
    listed = {dlg.workbook_choice.itemData(i) for i in range(dlg.workbook_choice.count())}
    assert listed == {str(untitled), str(sample)}
    choose(dlg, untitled)
    s = guess(dlg)
    # the model the user picked (asked once, with the dialog as parent) is
    # asked on the guess's worker, never on the GUI thread
    assert picked == [dlg] and asked_for == [MODEL]
    assert model.threads and all(t.name == "ledger-teach-guess" for t in model.threads)
    assert threading.main_thread() not in model.threads
    # one row per thing found, the guess pre-selected
    assert dlg.table.rowCount() == len(s.rows) > 0
    assert dlg.answers() == lt.answers_from_guesses(s)
    assert cell(dlg, "Zorp rate", GUESS_COL) == freq
    assert cell(dlg, "Zorp rate", FROM_COL).startswith("Council's model")
    assert cell(dlg, "Power (W)", FROM_COL).startswith("the Ledger's rules")
    assert cell(dlg, "Remarks to self", FROM_COL) == "nobody placed it"
    assert cell(dlg, "Zorp rate", 2) == "55"
    box = dlg.table.cellWidget(row_of(dlg, "Tech Sig"), ANSWER_COL)
    assert box.currentData() == lt.ROLE_INSPECTOR
    assert dlg.table.isColumnHidden(PICTURE_COL)                 # no pictures in it
    # the People section and the part number
    assert list(dlg.people_edits) == ["JB"] and dlg.people_edits["JB"].text() == ""
    assert dlg.part_edit.text() == "XYZ9AB123-R2" and "check it" in dlg.part_how.text()
    assert "@@@#@@###-@#" in dlg.save_pattern.text() and not dlg.save_pattern.isChecked()
    assert "guessed by Council's model" in dlg.status.text()
    assert "Teach — untitled.xlsx" in dlg.result.toPlainText()
    log = tab.log.toPlainText()
    assert "Teach: guessing what is in untitled.xlsx (Council's model: granite3.3:8b" in log
    assert "Teach — untitled.xlsx" in log
    assert dlg.save_btn.isEnabled() and not dlg.again_btn.isEnabled()
    # a second guess does not ask which model again
    guess(dlg)
    assert len(picked) == 1
    close(dlg)


def test_rules_only_asks_no_model(qapp, ledger, monkeypatch):
    csv, _schema, src, untitled, _sample, vault = ledger

    def no_model(*a, **k):
        raise AssertionError("no model was to be asked")
    monkeypatch.setattr(ledger_llm, "chat_for", no_model)
    tab, picked = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    dlg.use_model.setChecked(False)
    assert not dlg.model_btn.isEnabled()
    choose(dlg, untitled)
    s = guess(dlg)
    assert picked == [] and s.model == "" and not s.model_picked
    assert cell(dlg, "Zorp rate", FROM_COL) == "nobody placed it"
    assert "0 guessed by Council's model" in dlg.status.text()
    assert "the rules only" in tab.log.toPlainText()
    close(dlg)


def test_no_model_picked_guesses_nothing(qapp, ledger):
    csv, _schema, src, untitled, _sample, vault = ledger
    tab, _ = make_tab(qapp, csv, vault, [src])
    tab._pick_model = lambda parent=None: ""                    # the user cancelled
    dlg = open_teach(tab)
    choose(dlg, untitled)
    dlg.guess_btn.click()
    assert not dlg.busy and dlg.session is None
    assert "no model picked" in dlg.status.text()
    close(dlg)


def test_picture_rows_show_what_the_reader_read(qapp, ledger):
    """The tab's image reader reads the pasted picture; the things found in
    it say so, with the text it read."""
    csv, _schema, src, _untitled, sample, vault = ledger
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    dlg.use_model.setChecked(False)
    choose(dlg, sample)
    s = guess(dlg)
    media = next(r for r in s.rows if r.picture and r.label == "Media")
    r = row_of(dlg, "Media")
    assert not dlg.table.isColumnHidden(PICTURE_COL)
    assert dlg.table.item(r, 0).text() == "picture 1 on Build, row 1 column 1"
    assert "ceramic" in dlg.table.item(r, PICTURE_COL).text()
    assert "ceramic" in dlg.table.item(r, 1).toolTip()
    assert dlg.table.item(row_of(dlg, "Zorp rate"), PICTURE_COL).text() == ""
    dlg.table.setCurrentCell(r, 1)
    assert "What the image reader read in picture 1 on Build" in dlg.detail.text()
    assert media.picture_text[:20] in dlg.detail.text()
    # the inspector Council found, named from the people list later
    assert "JB" in dlg.people_edits
    assert dlg.part_edit.text() == "LMM1AB101"
    close(dlg)


# ── the GUI stays live; Cancel ──────────────────────────────────────────
def test_cancel_while_the_model_is_asked_keeps_the_rules_guesses(qapp, ledger,
                                                                 monkeypatch):
    csv, _schema, src, untitled, _sample, vault = ledger
    asked, threads = threading.Event(), []

    def chat_for(model_id, timeout=120, should_stop=None):
        def chat(prompt, schema):                               # a slow model
            threads.append(threading.current_thread())
            asked.set()
            end = time.monotonic() + 30
            while not should_stop() and time.monotonic() < end:
                time.sleep(0.02)
            raise RuntimeError("stopped by the caller")
        return chat
    monkeypatch.setattr(ledger_llm, "chat_for", chat_for)
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    choose(dlg, untitled)
    dlg.guess_btn.click()
    end = time.monotonic() + 30
    while not asked.is_set() and time.monotonic() < end:
        QApplication.processEvents()                             # the GUI thread is free
        time.sleep(0.01)
    assert asked.is_set() and dlg.busy
    QApplication.processEvents()
    # while the model "thinks": the progress shows and Cancel can be pressed
    assert "asking Council's model" in dlg.status.text()
    assert dlg.cancel_btn.isEnabled() and not dlg.guess_btn.isEnabled()
    assert not dlg.save_btn.isEnabled()
    assert [t.name for t in threads] == ["ledger-teach-guess"]
    dlg.cancel_btn.click()
    assert dlg.wait()
    s = dlg.session
    assert s.cancelled and s.rows
    assert dlg.status.text().startswith("Cancelled while Council's model was asked")
    assert cell(dlg, "Zorp rate", FROM_COL) == "nobody placed it"
    assert dlg.save_btn.isEnabled() and not dlg.cancel_btn.isEnabled()
    close(dlg)


def test_closing_while_guessing_stops_the_guess(qapp, ledger, monkeypatch):
    csv, _schema, src, untitled, _sample, vault = ledger
    asked, stopped = threading.Event(), threading.Event()

    def chat_for(model_id, timeout=120, should_stop=None):
        def chat(prompt, schema):
            asked.set()
            end = time.monotonic() + 30
            while not should_stop() and time.monotonic() < end:
                time.sleep(0.02)
            stopped.set()
            raise RuntimeError("stopped by the caller")
        return chat
    monkeypatch.setattr(ledger_llm, "chat_for", chat_for)
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    choose(dlg, untitled)
    dlg.guess_btn.click()
    end = time.monotonic() + 30
    while not asked.is_set() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    dlg.reject()
    assert not dlg.isVisible()
    assert dlg.wait() and stopped.is_set()


# ── the answers and Save ────────────────────────────────────────────────
def test_save_keeps_the_answers_and_guess_again_shows_the_rules(qapp, ledger,
                                                                monkeypatch):
    csv, schema, src, untitled, _sample, vault = ledger
    freq = next(f.name for f in schema.fields if "Frequency" in f.name)
    model = ThreadModel({"Zorp rate": freq, "Tech Sig": INSPECTOR})
    use_model(monkeypatch, model)
    graph_opened_on = []

    class Vault:
        """The temp vault, noting the thread the graph is opened on."""

        def open_graph(self):
            graph_opened_on.append(threading.current_thread().name)
            return kgm.KnowledgeGraph(vault)
    tab, _ = make_tab(qapp, csv, vault, [src], connections=Vault())
    dlg = open_teach(tab)
    choose(dlg, untitled)
    guess(dlg)
    assert cell(dlg, "Remarks to self", FROM_COL) == "nobody placed it"
    # the model's guesses wait for the user's tick (review, 2026-10-20)
    waiting = {"Zorp rate", "Tech Sig", "Serial"}
    assert {r.label for r in dlg.session.rows
            if not dlg.confirm_boxes[r.key].isChecked()} == waiting
    # the user's answers: the note is not data; JB is Jordan Blake; the
    # model's guesses are right; the part is right and its shape is this
    # Ledger's pattern
    dlg.set_answer(key_of(dlg, "Remarks to self"), lt.ROLE_IGNORE)
    assert dlg.changed_label.text().startswith("1 answer(s) changed from Council's guess.")
    assert dlg.table.item(row_of(dlg, "Remarks to self"), 1).font().bold()
    assert dlg.confirm_boxes[key_of(dlg, "Remarks to self")].isChecked()
    assert "3 guess(es) not confirmed" in dlg.changed_label.text()
    for label in waiting:
        dlg.set_confirmed(key_of(dlg, label))
    assert "not confirmed" not in dlg.changed_label.text()
    assert not dlg.confirm_all_btn.isEnabled()
    dlg.people_edits["JB"].setText("Jordan  Blake")
    dlg.save_pattern.setChecked(True)
    dlg.save_btn.click()
    assert dlg.wait()
    data = json.loads(se.aliases_path_for(csv).read_text(encoding="utf-8"))
    assert data["map"] == {"serial": "Part Number", "zorp rate": freq}
    assert data["ignore"] == ["remarks to self"]
    assert data["who_labels"] == ["Tech Sig"]
    assert data["people"] == {"JB": "Jordan Blake"}
    assert data["patterns"] == ["@@@#@@###-@#"]
    assert {e.label for e in lt.examples(csv)} >= {"Zorp rate", "Tech Sig", "Serial",
                                                   "Remarks to self"}
    # the knowledge graph, opened on the save's worker - and on the worker
    # that adds the Ledger to it when its parent CSV is picked - never on
    # the GUI thread
    assert "ledger-teach-learn" in graph_opened_on
    assert set(graph_opened_on) <= {"ledger-teach-learn", "ledger-kg-add"}
    with kgm.KnowledgeGraph(vault) as kg:
        rules = {r.label: st for r, st in kg.field_rules()}
    assert rules["Serial"] == "confirmed"
    # what was learned: in the dialog and the Ledger log
    learned = dlg.result.toPlainText()
    assert learned.startswith("Learned from untitled.xlsx:") and "Zorp rate → " in learned
    assert "Learned from untitled.xlsx:" in tab.log.toPlainText()
    assert dlg.status.text().startswith("Saved.")
    # the tab offers the saved pattern, and wants a graph rebuild (new
    # inspectors' labels and people)
    assert "@@@#@@###-@#" in tab.pattern_mask.placeholderText()
    assert tab._pattern_completer.model().stringList() == ["@@@#@@###-@#"]
    assert tab._kg_dirty and "Rebuild knowledge graph" in tab.status.text()
    # Guess again: the rules and the answers place it all; no model asked
    asked = len(model.prompts)
    assert dlg.again_btn.isEnabled()
    dlg.again_btn.click()
    assert dlg.wait()
    assert len(model.prompts) == asked
    assert cell(dlg, "Zorp rate", FROM_COL).startswith("your earlier answer")
    assert cell(dlg, "Remarks to self", FROM_COL).startswith("your earlier answer")
    again = dlg.result.toPlainText()
    assert again.startswith("Guess again: 4 thing(s) Council's model had to guess")
    assert "nothing left unsure" in again
    assert "Teach — guess again: 4 thing(s)" in tab.log.toPlainText()
    assert dlg.people_edits["JB"].text() == "Jordan Blake"       # from the people list now
    assert dlg.answers() == lt.answers_from_guesses(dlg.session)
    close(dlg)


def test_answering_inspector_adds_their_initials_to_people(qapp, ledger):
    csv, _schema, src, untitled, _sample, vault = ledger
    se.save_people(csv, {"JB": "Jordan Blake"})
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    dlg.use_model.setChecked(False)
    choose(dlg, untitled)
    guess(dlg)
    assert dlg.people_edits == {}                               # the rules saw no one
    assert dlg.people_none.isVisibleTo(dlg)
    dlg.set_answer(key_of(dlg, "Tech Sig"), lt.ROLE_INSPECTOR)
    assert list(dlg.people_edits) == ["JB"]
    assert dlg.people_edits["JB"].text() == "Jordan Blake"      # from the people list
    assert not dlg.people_none.isVisibleTo(dlg)
    dlg.save_btn.click()
    assert dlg.wait()
    assert se.load_taught(csv).who == ["Tech Sig"]
    assert "inspector label(s): Tech Sig" in dlg.result.toPlainText()
    close(dlg)


def test_an_unconfirmed_guess_is_left_out_and_an_inspectors_row_can_be_no_one(
        qapp, ledger, monkeypatch):
    """Save kept every guess left as it was - the model's too - as an alias
    (review, 2026-10-20): Confirm starts unticked on the model's guesses.
    The dropdowns offer no '— Inspector Initials' field, and 'Not an
    inspector' on what the rules read as an inspector's only."""
    csv, schema, src, untitled, sample, vault = ledger
    freq = next(f.name for f in schema.fields if "Frequency" in f.name)
    use_model(monkeypatch, ThreadModel({"Zorp rate": freq}))
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    choose(dlg, untitled)
    guess(dlg)
    zorp = key_of(dlg, "Zorp rate")
    assert not dlg.confirm_boxes[zorp].isChecked()
    assert dlg.confirm_boxes[key_of(dlg, "Power (W)")].isChecked()
    assert dlg.table.cellWidget(row_of(dlg, "Zorp rate"), CONFIRM_COL) is dlg.confirm_boxes[zorp]
    keys = [dlg.answer_boxes[zorp].itemData(i) for i in range(dlg.answer_boxes[zorp].count())]
    assert lt.ROLE_NO_ONE not in keys
    assert not any(lt.initials_field(lt.parse_choice(k)[1]) for k in keys)
    dlg.save_btn.click()
    assert dlg.wait()
    assert se.load_aliases(csv)[0] == {}                        # the model's guesses: left out
    assert "2 guess(es) you did not confirm" in dlg.result.toPlainText()
    assert "you did not confirm were left out" in dlg.status.text()
    assert dlg.confirm_all_btn.isEnabled()
    dlg.confirm_all_btn.click()
    assert all(b.isChecked() for b in dlg.confirm_boxes.values())
    dlg.save_btn.click()
    assert dlg.wait()
    assert se.load_aliases(csv)[0] == {"zorp rate": freq, "serial": "Part Number"}
    # an inspector's label: 'Not an inspector' right after 'Inspector'
    dlg.use_model.setChecked(False)
    choose(dlg, sample)
    guess(dlg)
    insp = key_of(dlg, "Inspector")
    box = dlg.answer_boxes[insp]
    keys = [box.itemData(i) for i in range(box.count())]
    assert keys.index(lt.ROLE_NO_ONE) == keys.index(lt.ROLE_INSPECTOR) + 1
    dlg.set_answer(insp, lt.ROLE_NO_ONE)
    dlg.save_btn.click()
    assert dlg.wait()
    assert se.load_taught(csv).not_who == ["Inspector"]
    assert "not an inspector (names no one" in dlg.result.toPlainText()
    close(dlg)


def test_forget_saved_pattern(qapp, ledger):
    """A pattern Teach saved could not be taken back from the UI (review,
    2026-10-20)."""
    csv, _schema, _src, _untitled, _sample, vault = ledger
    se.save_taught(csv, se.Taught(who=["Tech Sig"], patterns=["@@@#@@###"]))
    tab, _ = make_tab(qapp, csv, vault)
    assert tab.forget_pattern_btn.isEnabled()
    assert "saved pattern @@@#@@### is used as a hint" in tab.pattern_mask.placeholderText()
    tab.forget_pattern_btn.click()
    assert se.load_patterns(csv) == [] and se.load_taught(csv).who == ["Tech Sig"]
    assert not tab.forget_pattern_btn.isEnabled()
    assert "saved pattern" not in tab.pattern_mask.placeholderText()
    assert tab._pattern_completer.model().stringList() == []
    assert "Forgot this Ledger's saved part-name pattern(s): @@@#@@###" in \
        tab.log.toPlainText()
    se.save_taught(csv, se.Taught(patterns=["@@@###"]))
    tab._on_csv_changed()
    assert tab.forget_pattern_btn.isEnabled()
    tab.csv.setText(str(csv) + "x")                             # another Ledger's path
    assert not tab.forget_pattern_btn.isEnabled()


def test_a_damaged_aliases_file_is_said_and_kept(qapp, ledger):
    csv, _schema, src, untitled, _sample, vault = ledger
    tab, _ = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    dlg.use_model.setChecked(False)
    choose(dlg, untitled)
    guess(dlg)
    se.aliases_path_for(csv).write_text("{not json", encoding="utf-8")
    dlg.set_answer(key_of(dlg, "Zorp rate"), lt.ROLE_IGNORE)
    dlg.save_btn.click()
    assert dlg.wait()
    assert dlg.status.text().startswith("Not saved:") and "could not be read" in \
        dlg.status.text()
    assert se.aliases_path_for(csv).read_text(encoding="utf-8") == "{not json"
    assert "Teach: not saved:" in tab.log.toPlainText()
    assert not dlg.again_btn.isEnabled()
    close(dlg)


# ── picking the workbook ────────────────────────────────────────────────
def test_browse_picks_a_workbook_outside_the_sources(qapp, ledger, tmp_path):
    csv, _schema, _src, _untitled, _sample, vault = ledger
    elsewhere = _unlabelled_book(tmp_path / "elsewhere.xlsx")
    tab, _ = make_tab(qapp, csv, vault)                          # no sources
    tab._pick_workbook = lambda parent=None: str(elsewhere)
    dlg = open_teach(tab)
    assert dlg.workbook_choice.count() == 0 and not dlg.guess_btn.isEnabled()
    assert "Browse" in dlg.status.text()
    dlg.browse_btn.click()
    assert dlg.current_workbook() == str(elsewhere) and dlg.guess_btn.isEnabled()
    dlg.use_model.setChecked(False)
    assert guess(dlg).workbook == str(elsewhere)
    close(dlg)


def test_the_list_skips_what_the_tab_skips_and_starts_on_the_selected_file(
        qapp, ledger):
    csv, _schema, src, untitled, sample, vault = ledger
    _unlabelled_book(src / "cover sheet.xlsx")
    tab, _ = make_tab(qapp, csv, vault, [src, untitled])
    tab.ignore_files.setText("cover*.xlsx")
    tab.sources.item(1).setSelected(True)                        # the file itself
    dlg = open_teach(tab)
    listed = [dlg.workbook_choice.itemData(i) for i in range(dlg.workbook_choice.count())]
    assert sorted(listed) == sorted([str(untitled), str(sample)])
    assert dlg.current_workbook() == str(untitled)
    assert "2 workbook(s) in your sources" in dlg.status.text()
    close(dlg)


def test_a_workbook_that_cannot_be_read_is_said(qapp, ledger, tmp_path):
    csv, _schema, _src, _untitled, _sample, vault = ledger
    tab, _ = make_tab(qapp, csv, vault)
    tab._pick_workbook = lambda parent=None: str(tmp_path / "nowhere.xlsx")
    dlg = open_teach(tab)
    dlg.use_model.setChecked(False)
    dlg.browse_btn.click()
    dlg.guess_btn.click()
    assert dlg.wait()
    assert dlg.session is None and dlg.status.text() == "nowhere.xlsx not found"
    assert "Teach: nowhere.xlsx not found" in tab.log.toPlainText()
    assert not dlg.save_btn.isEnabled() and dlg.guess_btn.isEnabled()
    close(dlg)


# ── the Preview dialog reads what Teach taught ──────────────────────────
def test_preview_reads_what_teach_taught(qapp, ledger, monkeypatch):
    from council_qt.tabs.ledger_dialogs import PreviewWorkbookDialog
    csv, schema, _src, untitled, _sample, _vault = ledger
    se.save_taught(csv, se.Taught(who=["Tech Sig"], patterns=["@@@#@@###"]))
    seen = {}

    class Report:
        sheets = []

        def describe(self):
            return "previewed"

    def preview(path, schema, **kw):
        seen.update(kw)
        return Report()
    monkeypatch.setattr(se, "preview_workbook", preview)
    dlg = PreviewWorkbookDialog(str(untitled), schema, csv_path=str(csv),
                                pick_model=lambda parent=None: "")
    end = time.monotonic() + 30
    while dlg.text.toPlainText() != "previewed" and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert dlg.text.toPlainText() == "previewed"
    assert seen["taught"].who == ["Tech Sig"]
    assert seen["pattern_mask"] == "@@@#@@###"                  # the field was empty
    dlg.deleteLater()
