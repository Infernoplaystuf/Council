"""The Ledger's questions (council_core.ledger_questions; user, 2026-10-30:
"add a chat function in the ledger process directly ... I have a filled
template in and then the ledger finds it and requests info from me of if it
is a filled copy or not", and "re-run after it runs once and asks questions
and recieves the answers and uses those to see if the data connections it
made was correct").

Run:  python -m pytest tests/test_ledger_questions.py -q
"""
from __future__ import annotations

import csv as csvmod
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import sample_ledger as sl  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_questions as lq  # noqa: E402
from council_core import ledger_review as lv  # noqa: E402
from ledger_fixtures import make_template  # noqa: E402
from test_ledger_examples import filled_copy  # noqa: E402

COPY = "copy|bench_101.xlsx|Sheet"
PART = "part|settings_303.xlsx|303"


def log(path, part, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Run"
    if part:
        ws["A1"], ws["B1"] = "Part Number", part
    for i, (label, v) in enumerate(rows, start=3):
        ws.cell(i, 1, label)
        ws.cell(i, 2, v)
    wb.save(path)


@pytest.fixture
def ledger(tmp_path):
    """A filled copy of part 101 among the sources, a log of 101 that
    disagrees with it on Power, two logs of 202 that disagree, and a file
    whose part only its name gives."""
    src = tmp_path / "src"
    src.mkdir()
    csv = Path(li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv)
    schema = sl.TemplateSchema.load(sl.schema_path_for(csv))
    filled_copy(schema, src / "bench_101.xlsx", "101",
                {"Power (W)": "285", "Scan Speed (mm/s)": "1200", "Frequency (kHz)": "50"})
    log(src / "log_101.xlsx", 101, [("Power (W)", 290), ("Scan Speed (mm/s)", 1200)])
    log(src / "log_202.xlsx", 202, [("Power (W)", 310), ("Scan Speed (mm/s)", 900)])
    log(src / "other_202.xlsx", 202, [("Power (W)", 315)])
    log(src / "settings_303.xlsx", None, [("Power (W)", 270), ("Frequency (kHz)", 25)])
    return csv, src


def _ids(qs):
    return [q["id"] if isinstance(q, dict) else q.id for q in qs]


def _rows(review):
    return {(i.part, i.fields[0], i.value): i.accept for i in lv.load(review).items
            if i.sheet == "Values" and i.fields}


def _csv_rows(csv):
    with open(csv, encoding="utf-8-sig", newline="") as f:
        return list(csvmod.DictReader(f))


def test_extract_asks_about_the_filled_copy_the_guessed_part_and_the_clashes(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    assert _ids(ex.questions) == [COPY, PART, "clash|101||Power (W)", "clash|202||Power (W)"]
    copy = ex.questions[0]
    assert "laid out like the template for part 101" in copy["text"]
    assert [c["key"] for c in copy["choices"]] == ["trust", "guide", "source", "out"]
    assert lv.load(ex.review).filled_copies == [{"where": "bench_101.xlsx › Sheet",
                                                 "parts": ["101"]}]


def test_a_trusted_filled_copy_wins_and_its_clash_is_not_asked(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    lq.save_answer(csv, COPY, lq.COPY_TRUST)
    rnd = lq.recheck(ex.review, csv)
    rows = _rows(ex.review)
    assert rows[("101", "Power (W)", "285")] == "Y" and rows[("101", "Power (W)", "290")] == "N"
    # log_101's Scan Speed agrees with the copy
    assert rnd.outcome.confirmed >= 1 and "agree with your filled copy" in \
        rnd.outcome.describe()
    assert "clash|101||Power (W)" not in _ids(rnd.questions) and COPY not in _ids(rnd.questions)
    assert rnd.filled > 0                       # the filled templates written again
    li.run_apply(ex.review, use_images=False)
    assert {r["Power (W)"] for r in _csv_rows(csv) if r["Part Number"] == "101"} == {"285"}


def test_a_copy_used_only_as_a_guide_or_left_out_gives_no_values(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    for choice in (lq.COPY_GUIDE, lq.COPY_OUT):
        lq.save_answer(csv, COPY, choice)
        lq.recheck(ex.review, csv, write_filled=False)
        rows = _rows(ex.review)
        assert rows[("101", "Power (W)", "285")] == "N"
        # a value log_101 holds too is that file's: it stays
        assert rows[("101", "Scan Speed (mm/s)", "1200")] == "Y"


def test_a_part_answer_retypes_the_part_and_apply_uses_it(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    lq.save_answer(csv, PART, lq.PART_OTHER, "3030")
    rnd = lq.recheck(ex.review, csv)
    assert rnd.outcome.parts_changed == 2 and rnd.outcome.accepted == 2
    assert _rows(ex.review)[("3030", "Power (W)", "270")] == "Y"
    li.run_apply(ex.review, use_images=False)
    parts = {r["Part Number"] for r in _csv_rows(csv)}
    assert "3030" in parts and "303" not in parts


def test_a_file_that_is_no_parts_is_set_aside(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    lq.save_answer(csv, PART, lq.PART_OUT)
    lq.recheck(ex.review, csv, write_filled=False)
    assert _rows(ex.review)[("303", "Power (W)", "270")] == "N"


def test_a_clash_answer_picks_one_value(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    q = next(lq.Question.from_dict(d) for d in ex.questions if d["id"].startswith("clash|202"))
    key = lq.interpret(q, "315 is the right one")
    assert key == ("v:315.0", "")
    lq.save_answer(csv, q.id, *key)
    lq.recheck(ex.review, csv, write_filled=False)
    rows = _rows(ex.review)
    assert rows[("202", "Power (W)", "315")] == "Y" and rows[("202", "Power (W)", "310")] == "N"
    # another value typed: written over the first reading, which is accepted
    lq.save_answer(csv, q.id, lq.CLASH_TYPED, "312")
    lq.recheck(ex.review, csv, write_filled=False)
    rows = _rows(ex.review)
    assert rows[("202", "Power (W)", "312")] == "Y" and ("202", "Power (W)", "315") in rows
    assert rows[("202", "Power (W)", "315")] == "N"


def test_kept_answers_are_used_by_the_next_extract_and_not_asked_again(ledger):
    csv, src = ledger
    li.run_extract(csv, [src], use_images=False)
    lq.save_answer(csv, COPY, lq.COPY_TRUST)
    lq.save_answer(csv, PART, lq.PART_YES)
    ex = li.run_extract(csv, [src], use_images=False)
    assert _ids(ex.questions) == ["clash|202||Power (W)"]
    assert any("Your earlier answers" in x for x in ex.learned)
    rows = _rows(ex.review)
    assert rows[("101", "Power (W)", "285")] == "Y" and rows[("303", "Power (W)", "270")] == "Y"
    assert ex.values == ex.pre_accepted + ex.to_review + sum(v == "N" for v in rows.values())


def test_a_label_the_filled_copy_disagrees_with_is_asked_about(ledger):
    """The check of the Ledger's own matches: 'Zap level' was mapped to Power
    (W); for part 101 it reads 290 where the trusted copy says 285."""
    csv, src = ledger
    se.save_aliases(csv, {"zap level": "Power (W)"}, [])
    log(src / "log_101.xlsx", 101, [("Zap level", 290), ("Scan Speed (mm/s)", 1200)])
    log(src / "log_202.xlsx", 202, [("Zap level", 310), ("Scan Speed (mm/s)", 900)])
    ex = li.run_extract(csv, [src], use_images=False)
    assert "match|zap level|Power (W)" not in _ids(ex.questions)   # nothing trusted yet
    lq.save_answer(csv, COPY, lq.COPY_TRUST)
    rnd = lq.recheck(ex.review, csv, write_filled=False)
    q = next(q for q in rnd.questions if q.kind == "match")
    assert q.id == "match|zap level|Power (W)"
    assert "'Zap level' to Power (W)" in q.text and "290 in log_101.xlsx, 285 in the copy" \
        in q.text
    assert lq.interpret(q, "no, that's not power") == (lq.MATCH_NO, "")
    lq.save_answer(csv, q.id, lq.MATCH_NO)
    rnd = lq.recheck(ex.review, csv, write_filled=False)
    rows = _rows(ex.review)
    assert rows[("202", "Power (W)", "310")] == "N"                  # its other parts too
    assert "clash|202||Power (W)" not in _ids(rnd.questions)


def test_typed_replies_by_plain_words(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    qs = {d["id"]: lq.Question.from_dict(d) for d in ex.questions}
    copy, part = qs[COPY], qs[PART]
    assert lq.interpret(copy, "yes, that's my filled copy") == (lq.COPY_TRUST, "")
    assert lq.interpret(copy, "only use it as a guide") == (lq.COPY_GUIDE, "")
    assert lq.interpret(copy, "no") == (lq.COPY_SOURCE, "")
    assert lq.interpret(copy, "leave it out") == (lq.COPY_OUT, "")
    assert lq.interpret(copy, "2") == (lq.COPY_GUIDE, "")
    assert lq.interpret(part, "yes") == (lq.PART_YES, "")
    assert lq.interpret(part, "no, it's SBS-3030") == (lq.PART_OTHER, "SBS-3030")
    assert lq.interpret(part, "hmm") is None


def test_the_model_reads_a_reply_the_words_do_not(ledger):
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    copy = next(lq.Question.from_dict(d) for d in ex.questions if d["id"] == COPY)
    asked = []

    def chat(prompt, schema):
        asked.append((prompt, schema))
        return '{"key": "guide", "value": ""}', "stand-in"
    assert lq.interpret(copy, "I made that one up to show you how", chat) == \
        (lq.COPY_GUIDE, "")
    assert "Reply: I made that one up" in asked[0][0]
    assert asked[0][1]["properties"]["key"]["enum"][:4] == ["trust", "guide", "source", "out"]

    def unsure(prompt, schema):
        return '{"key": "unclear"}', "stand-in"
    assert lq.interpret(copy, "whatever", unsure) is None


def test_answers_are_kept_beside_the_ledger_and_forgotten(ledger):
    csv, _src = ledger
    q = lq.Question("part|a.xlsx|1", "part", "Is a.xlsx part 1?",
                    [lq.Choice("yes", "Yes, it is 1")])
    lq.save_answer(csv, q.id, "yes", question=q)
    got = lq.load_answers(csv)[q.id]
    assert got["choice"] == "yes" and got["said"] == "Yes, it is 1"
    assert lq.answers_path(csv).name == "l.answers.json"
    assert lq.forget_answer(csv, q.id) == {}


# ── the chat in the Ledger tab ──────────────────────────────────────────
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_pane_asks_one_at_a_time_and_rechecks_when_a_round_is_answered(qapp, ledger):
    from council_qt.tabs.ledger_questions_pane import LedgerQuestionsPane
    csv, src = ledger
    ex = li.run_extract(csv, [src], use_images=False)
    rechecks = []
    pane = LedgerQuestionsPane(lambda: rechecks.append(1),
                               lambda q, t, done: done(lq.interpret(q, t)))
    qs = [lq.Question.from_dict(d) for d in ex.questions]
    pane.start(str(csv), ex.review, qs)
    assert not pane.isHidden() and pane.current.id == COPY
    buttons = [pane.choice_row.itemAt(i).widget() for i in range(pane.choice_row.count())]
    buttons[0].click()                                     # trust
    assert lq.load_answers(csv)[COPY]["choice"] == "trust"
    assert pane.current.id == PART
    pane.reply.setText("no, it's 3030")
    pane.send_btn.click()
    assert not pane.confirm_yes.isHidden() and "It is part 3030" in pane.confirm_label.text()
    pane.confirm_yes.click()
    assert lq.load_answers(csv)[PART] == {**lq.load_answers(csv)[PART], "choice": "other",
                                          "value": "3030"}
    pane.skip_btn.click()
    pane.skip_btn.click()
    assert pane.current is None and rechecks == [1]
    assert "re-checking the review" in pane.transcript.toPlainText()
    pane.next_round(lq.recheck(ex.review, csv))
    text = pane.transcript.toPlainText()
    assert "round 1" in text and "2 given another part" in text
    # the two clashes were skipped: not asked again about this review
    assert pane.queue == [] and "Nothing more to ask" in pane.transcript.toPlainText()


def test_the_pane_stays_hidden_with_nothing_to_ask(qapp, tmp_path):
    from council_qt.tabs.ledger_questions_pane import LedgerQuestionsPane
    pane = LedgerQuestionsPane(lambda: None, lambda q, t, done: done(None))
    pane.start(str(tmp_path / "l.csv"), str(tmp_path / "r.xlsx"), [])
    assert pane.isHidden()


def test_the_tab_shows_the_questions_after_extract(qapp, ledger):
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    csv, src = ledger
    tab = LedgerTab(None, LedgerActions(opener=lambda *_: None))
    tab.csv.setText(str(csv))
    tab.add_sources([src])
    tab.use_images.setChecked(False)
    tab.extract_btn.click()
    assert tab.wait()
    assert not tab.questions.isHidden() and tab.questions.current.id == COPY
    assert "question(s) for you" in tab.log.toPlainText()
    tab.questions.choice_row.itemAt(0).widget().click()   # trust
    tab.questions.choice_row.itemAt(0).widget().click()   # part 303: yes
    for _ in range(2):
        tab.questions.skip_btn.click()
    assert tab.wait()                                     # the re-check ran on the worker
    assert "Re-checked with your answers" in tab.log.toPlainText()
    assert tab.questions.queue == [] and tab.questions.round == 2
    rows = _rows(tab.review.text())
    assert rows[("101", "Power (W)", "285")] == "Y" and rows[("303", "Power (W)", "270")] == "Y"
