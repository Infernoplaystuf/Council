"""Council's model helps the Ledger where its rules stop (council_core/ledger_llm).

A fake model stands in (tests run offline). What is pinned: every answer is
a suggestion the review shows in amber, a part number the model names must
be written in the workbook, the user's part-name pattern goes in as an
example, and accepting a suggested field teaches it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_llm  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402


class FakeModel:
    """Answers the two questions; remembers the prompts."""
    def __init__(self, part="XYZ9AB123-R2", mapping=None):
        self.part, self.mapping, self.prompts = part, mapping or {}, []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        if "Which part" in prompt:
            return json.dumps({"part": self.part, "evidence": "Serial"}), "fake-granite"
        return json.dumps(self.mapping), "fake-granite"


def _field(schema, word):
    return next(f.name for f in schema.fields if word in f.name)


def _unlabelled_book(path: Path) -> Path:
    """No 'Part Number' label anywhere, an odd serial format, and a label
    no rule knows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Serial: XYZ9AB123-R2"
    ws["A3"], ws["B3"] = "Power (W)", 280
    ws["A4"], ws["B4"] = "Zorp rate", 55
    wb.save(path)
    return path


@pytest.fixture()
def ledger(tmp_path):
    ps = li.create_parent(lf.make_template(tmp_path / "template.xlsx"))
    return Path(ps.csv), ParentCSV(ps.csv).schema


def test_mask_example():
    assert ledger_llm.mask_example("@@@#@@###") == "ABC1DE234"
    assert ledger_llm.mask_example("P/N-####") == "P/N-1234"


def test_the_model_finds_the_part_and_places_the_label(tmp_path, ledger):
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    freq = _field(schema, "Frequency")
    model = FakeModel(mapping={"Zorp rate": freq})
    s = li._extract(str(csv), [str(src)], None, "", False, None,
                    _NoProg(), "=@@@#@@###", None, None, True, model)
    # the pattern went in as an example, with a made-up instance of it
    part_prompt = next(p for p in model.prompts if "Which part" in p)
    assert "'@@@#@@###'" in part_prompt and "ABC1DE234" in part_prompt
    assert "Serial: XYZ9AB123-R2" in part_prompt
    # only labels go to the mapping question, never values
    map_prompt = next(p for p in model.prompts if "template field it means" in p)
    assert "Zorp rate" in map_prompt and "55" not in map_prompt
    rows = [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(s.review)[
        "Values"].iter_rows(min_row=2, values_only=True)]
    zorp = [r for r in rows if r["Source label"] == "Zorp rate"]
    assert zorp and zorp[0]["Field"] == freq and zorp[0]["Matched by"] == "model"
    assert all(r["Part"] == "XYZ9AB123-R2" for r in rows)
    assert all(r["Part from"].startswith(se.MODEL_PART_HOW) for r in rows)
    assert not any(r["Accept"] == "Y" for r in rows)          # nothing on its own
    assert "Council's model (fake-granite)" in s.describe()
    assert "untitled.xlsx → XYZ9AB123-R2" in s.describe()


def test_a_part_the_workbook_does_not_contain_is_dropped(tmp_path, ledger):
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    s = li._extract(str(csv), [str(src)], None, "", False, None,
                    _NoProg(), None, None, None, True, FakeModel(part="QQQ1QQ999"))
    rows = list(openpyxl.load_workbook(s.review)["Values"].iter_rows(min_row=2,
                                                                     values_only=True))
    assert all(not r[1] for r in rows)                          # no part given
    assert any("not written in the workbook" in p for p in s.problems)


def test_the_model_is_not_asked_when_the_rules_found_everything(tmp_path, ledger):
    csv, _schema = ledger
    src = lf.make_laser_log(tmp_path / "laser_101.xlsx")
    model = FakeModel()
    li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None, None,
                None, True, model)
    assert not any("Which part" in p for p in model.prompts)


def test_accepting_a_model_row_applies_it_and_teaches_the_label(tmp_path, ledger):
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    freq = _field(schema, "Frequency")
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None,
                    None, None, True, FakeModel(mapping={"Zorp rate": freq}))
    wb = openpyxl.load_workbook(s.review)
    v = wb["Values"]
    for r in range(2, v.max_row + 1):
        v.cell(r, 1, "Y")
    wb.save(s.review)
    li.run_apply(s.review, use_images=False)
    rows = ParentCSV(csv).rows
    assert any(r.get(schema.id_field) == "XYZ9AB123-R2" and r.get(freq) == "55"
               for r in rows)
    mapping, _ = se.load_aliases(csv)
    assert mapping.get(se.normalize_label("Zorp rate")) == freq


def test_no_model_anywhere_is_a_problem_line_not_a_failure(tmp_path, ledger, monkeypatch):
    csv, _schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")

    def nothing(prompt, schema):
        raise se.LedgerError("no local model answered: set COUNCIL_GGUF_PATH ...")
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None,
                    None, None, True, nothing)
    assert any("COUNCIL_GGUF_PATH" in p for p in s.problems)
    assert Path(s.review).exists()


class _NoProg:
    def update(self, *a, **k):
        pass

    def stage(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass


# ---------------------------------------------- the user picks the model

MODELS = [
    {"id": "C:/models/granite-3.3-8b.Q4_K_M.gguf", "label": "granite-3.3-8b — IBM · US · GGUF",
     "usable": True},
    {"id": "ollama:llama3.1:8b", "label": "llama3.1:8b — Meta · US · Ollama", "usable": True},
    {"id": "ollama:qwen2.5:7b", "label": "qwen2.5:7b — Alibaba · non-US · Ollama   "
     "(not US-origin: not used by Council)", "usable": False},
]


def _qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait(app, cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_the_picker_lists_every_model_and_refuses_a_non_us_one():
    from PySide6.QtCore import Qt
    from council_qt.tabs.ledger_dialogs import PickModelDialog
    app = _qapp()
    dlg = PickModelDialog(current="ollama:llama3.1:8b", lister=lambda: MODELS)
    _wait(app, lambda: dlg.list.count() == 3)
    items = [dlg.list.item(i) for i in range(3)]
    assert [bool(i.flags() & Qt.ItemFlag.ItemIsEnabled) for i in items] == [True, True, False]
    assert dlg.list.currentItem() is items[1]                # last time's choice
    dlg._ok()
    assert dlg.model_id == "ollama:llama3.1:8b"
    dlg.deleteLater()


def test_the_picker_says_what_to_do_when_nothing_is_downloaded():
    from PySide6.QtWidgets import QDialogButtonBox
    from council_qt.tabs.ledger_dialogs import PickModelDialog
    app = _qapp()
    dlg = PickModelDialog(lister=lambda: [], browse=lambda: "D:/other/phi-4.gguf")
    _wait(app, lambda: "No downloaded models" in dlg.message.text())
    assert not dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dlg.on_browse()                                          # a GGUF anywhere on disk
    assert dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dlg._ok()
    assert dlg.model_id == "D:/other/phi-4.gguf"
    dlg.deleteLater()


def test_ask_council_in_the_preview_uses_the_picked_model(tmp_path, ledger, monkeypatch):
    from council_qt.tabs.ledger_dialogs import PreviewWorkbookDialog
    app = _qapp()
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    freq = _field(schema, "Frequency")
    picked = []
    used = []

    def fake_chat_for(model_id, **kw):
        used.append(model_id)
        return FakeModel(mapping={"Zorp rate": freq})
    monkeypatch.setattr(ledger_llm, "chat_for", fake_chat_for)
    dlg = PreviewWorkbookDialog(str(src), schema, csv_path=str(csv),
                                pick_model=lambda parent: picked.append(1) or "")
    _wait(app, lambda: dlg.ask_btn.isEnabled())
    dlg.on_ask_llm()                                         # cancelled: nothing asked
    assert picked and not used
    dlg._pick_model = lambda parent: "ollama:llama3.1:8b"
    dlg.on_ask_llm()
    _wait(app, lambda: "Council mapping" in dlg.text.toPlainText())
    assert used == ["ollama:llama3.1:8b"]
    assert "'Zorp rate' → " in dlg.text.toPlainText()
    dlg.deleteLater()


def test_the_extract_asks_which_model_when_the_box_is_ticked(tmp_path):
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    app = _qapp()
    seen = {}

    class Acts(LedgerActions):
        def extract(self, csv, sources, part, use_images, progress, **kw):
            seen.update(kw)
            raise se.LedgerError("stop here")

    tab = LedgerTab(actions=Acts())
    try:
        tab.csv.setText(str(tmp_path / "p.csv"))
        tab.add_sources([str(tmp_path / "a.xlsx")])
        tab.ask_model.setChecked(True)
        tab._pick_model = lambda parent: ""
        tab.on_extract()
        assert not tab.busy and "no model picked" in tab.status.text()
        tab._pick_model = lambda parent: "ollama:llama3.1:8b"
        tab.on_extract()
        assert tab.wait(10)
        assert seen.get("assist") == "ollama:llama3.1:8b"
    finally:
        tab.deleteLater()


def test_chat_for_an_ollama_model_goes_through_councils_local_chat(monkeypatch):
    import council_engine as ce
    calls = []

    def fake_local_chat(messages, **kw):
        calls.append(kw)
        return '{"a": null}'
    monkeypatch.setattr(ce, "local_chat", fake_local_chat)
    text, label = ledger_llm.chat_for("ollama:granite3.3:8b")("prompt", {"type": "object"})
    assert text == '{"a": null}' and label == "granite3.3:8b"
    assert calls[0]["model"] == "ollama:granite3.3:8b"
    with pytest.raises(se.LedgerError):
        ledger_llm.chat_for("ollama:qwen2.5:7b")


def test_chat_for_a_gguf_file_loads_that_file(monkeypatch, tmp_path):
    loaded = []

    class FakeLlama:
        def create_chat_completion(self, **kw):
            assert kw["response_format"]["type"] == "json_object"
            return {"choices": [{"message": {"content": '{"part": "", "evidence": ""}'}}]}

    def fake_load(path):
        loaded.append(path)
        return FakeLlama()
    monkeypatch.setattr(ledger_llm, "_gguf_llm", fake_load)
    monkeypatch.delenv("COUNCIL_GGUF_PATH", raising=False)
    path = str(tmp_path / "granite.gguf")
    text, label = ledger_llm.chat_for(path)("p", ledger_llm._PART_SCHEMA)
    assert loaded == [path] and label == "granite.gguf" and '"part"' in text


def test_mapping_with_a_picked_model_skips_the_auto_search(ledger, monkeypatch):
    _csv, schema = ledger
    freq = _field(schema, "Frequency")
    monkeypatch.setattr(se, "_council_chat",
                        lambda *a, **k: pytest.fail("the picked model is used"))
    mapping, label = se.suggest_mappings_for_labels(
        ["Zorp rate"], schema, chat=FakeModel(mapping={"Zorp rate": freq}))
    assert mapping == {"Zorp rate": freq} and label == "fake-granite"


def test_words_around_the_identifier_are_left_out(tmp_path, ledger):
    """granite3.3:8b answered 'XYZ9AB123-R2 (rev B)' for a cell 'S/N
    XYZ9AB123-R2  (rev B)'."""
    csv, _schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None,
                    None, None, True, FakeModel(part="XYZ9AB123-R2 (rev B)"))
    parts = {r[1] for r in openpyxl.load_workbook(s.review)["Values"].iter_rows(
        min_row=2, values_only=True)}
    assert parts == {"XYZ9AB123-R2"}


def test_a_model_that_answers_in_the_old_shape_is_still_read(ledger):
    _csv, schema = ledger
    freq = _field(schema, "Frequency")
    plain = freq.split(": ", 1)[-1]

    def old_shape(prompt, schema_):
        return json.dumps({"Zorp rate": plain.upper(), "Other": "nonsense"}), "m"
    mapping, _ = ledger_llm.map_labels(["Zorp rate", "Other"],
                                       [f.name for f in schema.fields], old_shape)
    assert mapping == {"Zorp rate": freq, "Other": None}


# ---------------------------------------------- Connections' Suggest links takes a GGUF

def test_suggest_links_lists_gguf_files_and_runs_on_one(tmp_path, monkeypatch):
    from council_core import kg_extract as kx
    from council_core import local_models as lm
    from council_qt.tabs.connections import ConnectionsActions
    monkeypatch.setattr(lm, "list_local_models", lambda **kw: [
        {"id": "C:/m/granite.gguf", "path": "C:/m/granite.gguf", "name": "Granite 3.1 8b",
         "origin": "US", "backend": "gguf"},
        {"id": "C:/m/qwen.gguf", "path": "C:/m/qwen.gguf", "name": "Qwen", "origin": "non-US",
         "backend": "gguf"}])
    acts = ConnectionsActions(vault_dir=tmp_path / "v")
    assert acts.gguf_models() == [("GGUF: Granite 3.1 8b", "C:/m/granite.gguf")]

    class FakeLlama:
        def create_chat_completion(self, **kw):
            return {"choices": [{"message": {"content": '{"links": []}'}}]}
    monkeypatch.setattr(ledger_llm, "_gguf_llm", lambda path: FakeLlama())
    monkeypatch.delenv("COUNCIL_GGUF_PATH", raising=False)
    assert kx.gguf_chat("C:/m/granite.gguf")([{"role": "user", "content": "x"}]) == \
        '{"links": []}'


# ---------------------------------------------- pictures: only models that see

def test_the_reader_dropdown_offers_only_models_that_read_pictures(monkeypatch):
    from council_core import local_models as lm
    from council_core import ollama_install
    monkeypatch.setattr(lm, "list_local_models", lambda **kw: [
        {"name": "granite3.3:8b", "origin": "US", "capabilities": ["completion", "tools"]},
        {"name": "gemma3:12b", "origin": "US", "capabilities": ["completion", "vision"]},
        {"name": "qwen2.5vl:7b", "origin": "non-US", "capabilities": ["vision"]},
        {"name": "llava:7b", "origin": "US", "capabilities": []}])     # an old Ollama
    assert ollama_install.vision_models() == ["gemma3:12b", "llava:7b"]


def test_apply_uses_the_vision_model_the_extract_used(tmp_path):
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    _qapp()
    seen = {}

    class Acts(LedgerActions):
        def apply(self, review, fill_dir, use_images, progress, **kw):
            seen.update(kw)
            raise se.LedgerError("stop")

    tab = LedgerTab(actions=Acts())
    try:
        tab.image_reader_choice.addItem("Ollama: gemma3:12b", "gemma3:12b")
        tab.image_reader_choice.setCurrentIndex(tab.image_reader_choice.count() - 1)
        tab.review.setText(str(tmp_path / "r.xlsx"))
        tab.on_apply()
        assert tab.wait(10)
        assert seen == {"vision_model": "gemma3:12b"}
    finally:
        tab.deleteLater()


# ---------------------------------------------- review fixes (2026-10-19, real models)

def _titled_book(path: Path) -> Path:
    """The tester's book_lowercase.xlsx: a title, the part id 'efg5hj678'
    alone in A2, labels with measured values and a date - no part label."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"] = "Laser build record"
    ws["A2"] = "efg5hj678"
    rows = [("Pulse freq", 61.35), ("Travel speed mm per s", 1238.54),
            ("Beam power", 291.29), ("Laser wattage", 295.79), ("Probe gain dB", 46.165),
            ("Operator name", "Cornelius Dappleby"), ("Rep rate", 65.54),
            ("Feed velocity", 1115.29), ("Coolant brand", "Xylofrost Nine E"),
            ("Date", "2026-10-05")]
    for r, (lab, val) in enumerate(rows, start=4):
        ws.cell(r, 1, lab)
        ws.cell(r, 2, val)
    wb.save(path)
    return path


QUOTE_CASES = [  # (the model's answer, accepted?) - the tester's s05_quote_check.py
    ("efg5hj678", True),                 # the real part
    ("EFG5HJ678", True),                 # case differs only
    ("Laser build record", False),       # a title (llama3.1:8b's real answer)
    ("Build", False),                    # the sheet name, not an id
    ("efg5hj67", False),                 # cut short: a substring of the real one
    ("5hj6", False),                     # a fragment of the id
    ("1", False),                        # one digit (in a value / date)
    ("2026", False),                     # part of the date cell
    ("61.35", False),                    # a measured VALUE (Pulse freq)
    ("Probe gain dB", False),            # a label
    ("XYZ9AB999", False),                # not written anywhere
    ("book_lowercase", False),           # the file name stem
]


@pytest.mark.parametrize("answer,accepted", QUOTE_CASES)
def test_the_part_must_be_an_identifier_written_as_a_whole_word(tmp_path, answer, accepted):
    """The quote check took any substring of the workbook: llama3.1:8b
    named 'Laser build record' and three workbooks merged into that
    phantom part (review, 2026-10-19)."""
    src = _titled_book(tmp_path / "book_lowercase.xlsx")
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()

    def chat(prompt, schema):
        return json.dumps({"part": answer, "evidence": "stub"}), "stub"
    part, where = ledger_llm.find_part(src.name, grids, "@@@#@@###", chat, rep)
    if accepted:
        # given as it is written in the workbook, where it is written
        assert (part, where) == ("efg5hj678", "book_lowercase.xlsx › Build!A2")
        assert rep.parts_found == [(src.name, "efg5hj678")] and not rep.problems
    else:
        assert (part, where) == ("", "") and not rep.parts_found
        assert any("dropped" in p for p in rep.problems)


def test_what_looks_like_a_part_number():
    looks = ledger_llm.looks_like_part
    assert looks("ABC1DE234") and looks("DEF-4GH-567") and looks("12345")
    assert not looks("1234") and not looks("2026-10-05") and not looks("-12345")
    assert not looks("1,234") and not looks("ABC1 DE234")
    # the user's own pattern is a part number however it looks
    assert looks("1234", "####") and looks("ABC", "@@@")
    assert looks("1234", "#####")                       # an all-digit pattern
    assert not looks("2026", "#####")                   # but not a year


def test_the_part_prompt_shows_ids_from_every_sheet_before_numbers():
    """Only the first 200 cells went, numbers counted: an id below a data
    table or on a later sheet never reached the model (review, 2026-10-19)."""
    table = {(1, 1): "Scan data"}
    for r in range(2, 152):
        for c in range(1, 4):
            table[(r, c)] = r * 1.5 + c / 4
    table[(160, 1)] = "S/N: XYZ9AB123"                  # below the table
    notes = {(1, 1): "Remarks", (2, 1): "Lot", (2, 2): "QRS4TU567"}
    grids = [se.Grid("big.xlsx", "Data", table), se.Grid("big.xlsx", "Notes", notes)]
    model = FakeModel(part="")
    ledger_llm.find_part("big.xlsx", grids, None, model, ledger_llm.AssistReport())
    cells = json.loads(model.prompts[0].split("Cells:\n", 1)[1])
    assert cells[:2] == ["S/N: XYZ9AB123", "QRS4TU567"]           # ids first
    assert cells[2:5] == ["Scan data", "Remarks", "Lot"]          # then the labels
    assert len(cells) == ledger_llm.MAX_CELLS                     # numbers fill the rest


def _sentinel(monkeypatch, tmp_path):
    import council_engine as ce
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setattr(ce, "_GPU_CONFIRMED_THIS_PROCESS", False)
    marker = ce._gpu_sentinel_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("n_gpu_layers=99\n", encoding="utf-8")      # as _load_gguf leaves it
    return marker


def test_a_gguf_answer_clears_the_gpu_crash_marker(monkeypatch, tmp_path):
    """A clean Ledger / Suggest-links answer on a GGUF left <vault>/.gpu_attempt
    behind, so the next Council process loaded every GGUF on the CPU
    (review, 2026-10-19)."""
    from council_core import kg_extract as kx

    class FakeLlama:
        def create_chat_completion(self, **kw):
            return {"choices": [{"message": {"content": '{"links": []}'}}]}
    monkeypatch.setattr(ledger_llm, "_gguf_llm", lambda path: FakeLlama())
    monkeypatch.delenv("COUNCIL_GGUF_PATH", raising=False)
    marker = _sentinel(monkeypatch, tmp_path)
    ledger_llm.chat_for(str(tmp_path / "granite.gguf"))("p", ledger_llm._PART_SCHEMA)
    assert not marker.exists()
    marker = _sentinel(monkeypatch, tmp_path)
    kx.gguf_chat(str(tmp_path / "granite.gguf"))([{"role": "user", "content": "x"}])
    assert not marker.exists()


def test_a_model_that_cannot_answer_is_asked_once(tmp_path, ledger, monkeypatch):
    """It was asked again for every workbook (a GGUF that would not load was
    loaded again each time): the same multi-line error N+1 times (review,
    2026-10-19)."""
    import council_engine as ce
    csv, _schema = ledger
    books = [_unlabelled_book(tmp_path / f"untitled_{i}.xlsx") for i in range(3)]
    calls = []

    def broken(path, **kw):             # council_engine's own words, as _load_gguf says
        calls.append(path)
        raise ce.BackendUnavailable("llama-cpp-python is not installed. Install with:\n"
                                    "  pip install llama-cpp-python")
    monkeypatch.setattr(ce, "_load_gguf", broken)
    monkeypatch.delenv("COUNCIL_GGUF_PATH", raising=False)
    # the user's pick: a GGUF file, through chat_for as the Ledger tab sends it
    s = li._extract(str(csv), [str(b) for b in books], None, "", False, None, _NoProg(),
                    None, None, None, str(tmp_path / "granite.gguf"))
    assert len(calls) == 1
    assert sum("llama-cpp-python" in p for p in s.problems) == 1
    assert s.assist == ("Council's model was unavailable, so nothing in the review is its "
                        "suggestion: granite.gguf could not be loaded (llama-cpp-python is "
                        "not installed) — pick an Ollama model instead, or install "
                        "llama-cpp-python.")


def test_cancel_is_not_a_broken_model():
    rep = ledger_llm.AssistReport()

    def stopped(prompt, schema):
        raise RuntimeError("stopped by the caller")
    ask = ledger_llm.one_shot(stopped, rep, should_stop=lambda: True)
    with pytest.raises(RuntimeError):
        ask("p", {})
    assert not rep.unavailable and not rep.problems


def _stopped_by_cancel(cancel, on):
    """A model that Cancel stops while it answers the question ``on`` is
    in ('Which part' or the labels), as council_engine's does."""
    def chat(prompt, _schema):
        if ("Which part" in prompt) == (on == "part"):
            cancel.set()                                   # pressed while it answers
            raise se.LedgerError("granite3.3:8b (Ollama) did not answer: stopped by the "
                                 "caller")
        return json.dumps({"part": "", "evidence": ""}), "granite3.3:8b"
    return chat


def test_cancel_while_the_model_is_asked_about_the_labels_is_a_cancelled_run(tmp_path, ledger):
    """The run stopped, but it was not a cancelled one: the tab said 'Review
    written…' and the log that the model had not answered - 'Council's
    model (none answered)', '... did not answer: stopped by the caller'
    (review, 2026-10-20)."""
    import threading
    csv, _schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    cancel = threading.Event()
    s = li._extract(str(csv), [str(src)], tmp_path / "review.xlsx", "", False, None,
                    _NoProg(), None, cancel, None, True, _stopped_by_cancel(cancel, "labels"))
    assert s.cancelled and s.read_all and s.values and Path(s.review).exists()
    text = s.describe()
    assert text.startswith("CANCELLED after every workbook was read — ")
    assert "did not answer" not in text and "none answered" not in text
    assert s.assist == ("Council's model: Cancel stopped it while it was asked about the "
                        "labels left over, so nothing in the review is its suggestion.")
    assert "Cancelled by user while Council's model was asked about the labels left over; " \
           "every value the rules found is in this review." in s.problems


def test_a_model_that_answers_despite_cancel_says_its_values_are_in(tmp_path, ledger):
    """A model Cancel cannot stop (a GGUF in this process) answers anyway:
    its values are placed, amber - the log said only that every value the
    rules found was in the review (second review, 2026-10-20)."""
    import threading
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    cancel = threading.Event()
    model = FakeModel(mapping={"Zorp rate": _field(schema, "Frequency")})

    def chat(prompt, schema_):
        if "Which part" not in prompt:
            cancel.set()                                   # pressed; it answers anyway
        return model(prompt, schema_)
    s = li._extract(str(csv), [str(src)], tmp_path / "review.xlsx", "", False, None,
                    _NoProg(), None, cancel, None, True, chat)
    assert s.cancelled and s.read_all
    assert "asked which field 2 unplaced label(s) are: 1 given a field, 1 value(s) placed" in s.assist
    assert "all amber in the review: check them" in s.assist
    assert ("Cancelled by user while Council's model was asked about the labels left over, "
            "but it answered first; every value the rules found is in this review, and the "
            "1 value(s) it placed are too (amber: check them).") in s.problems


def test_cancel_while_the_model_is_asked_for_a_part_is_not_its_failure(tmp_path, ledger):
    import threading
    csv, _schema = ledger
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["B1"] = "Power (W)", 280      # no part anywhere
    wb.save(tmp_path / "nopart.xlsx")
    cancel = threading.Event()
    s = li._extract(str(csv), [str(tmp_path / "nopart.xlsx")], tmp_path / "review.xlsx", "",
                    False, None, _NoProg(), None, cancel, None, True,
                    _stopped_by_cancel(cancel, "part"))
    assert s.cancelled and not s.read_all                  # the workbook was cut short
    text = s.describe()
    assert "did not answer" not in text and "none answered" not in text
    assert s.assist == ("Council's model: Cancel stopped it while it was asked about "
                        "nopart.xlsx, so nothing in the review is its suggestion.")


def test_a_missing_ollama_model_says_how_to_install_it(monkeypatch):
    """'... in the Models tab.. Is Ollama running?' for a model Ollama - which
    had answered - does not have (review, 2026-10-19)."""
    import council_engine as ce
    name = "granite9.9:404b"

    def missing(messages, **kw):        # council_engine's own message
        raise RuntimeError(
            f"Ollama at http://127.0.0.1:11434 has no model '{name}'. Install it yourself "
            f"(`ollama pull {name}` — the council never downloads), or pick "
            "another model for this role in the Models tab.")
    monkeypatch.setattr(ce, "local_chat", missing)
    with pytest.raises(se.LedgerError) as e:
        ledger_llm.chat_for(f"ollama:{name}")("p", {})
    assert str(e.value) == (f"{name} is not installed in Ollama — pick another model, or "
                            f"install it (ollama pull {name})")

    def down(messages, **kw):
        raise ce.BackendUnavailable("No Ollama server answers at http://127.0.0.1:11434.")
    monkeypatch.setattr(ce, "local_chat", down)
    with pytest.raises(se.LedgerError) as e:
        ledger_llm.chat_for(f"ollama:{name}")("p", {})
    assert str(e.value).endswith("11434. Is Ollama running?") and ".." not in str(e.value)

    def slow(messages, **kw):
        raise RuntimeError("Ollama (granite) at http://127.0.0.1:11434 timed out.")
    monkeypatch.setattr(ce, "local_chat", slow)
    with pytest.raises(se.LedgerError) as e:
        ledger_llm.chat_for(f"ollama:{name}")("p", {})
    assert "Is Ollama running?" not in str(e.value) and ".." not in str(e.value)


def test_a_label_the_model_placed_is_not_left_to_map(tmp_path, ledger):
    """It stayed on 'Labels to map' with the rules' contradicting guesses
    and was counted as unmatched (review, 2026-10-19)."""
    csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    freq = _field(schema, "Frequency")
    before = li._extract(str(csv), [str(src)], tmp_path / "r1.xlsx", "", False, None,
                         _NoProg(), None, None, None, False)
    s = li._extract(str(csv), [str(src)], tmp_path / "r2.xlsx", "", False, None, _NoProg(),
                    None, None, None, True, FakeModel(mapping={"Zorp rate": freq}))
    to_map = [r[2] for r in openpyxl.load_workbook(s.review)["Labels to map"].iter_rows(
        min_row=2, values_only=True)]
    assert "Zorp rate" not in to_map
    assert s.unmatched == before.unmatched - 1


def test_the_preview_can_ask_again_after_an_answer(tmp_path, ledger, monkeypatch):
    """'Ask Council for mappings' stayed disabled after a good answer
    (review, 2026-10-19)."""
    from council_qt.tabs.ledger_dialogs import PreviewWorkbookDialog
    app = _qapp()
    _csv, schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    model = FakeModel(mapping={"Zorp rate": _field(schema, "Frequency")})
    monkeypatch.setattr(ledger_llm, "chat_for", lambda model_id, **kw: model)
    dlg = PreviewWorkbookDialog(str(src), schema, pick_model=lambda parent: "ollama:x")
    try:
        _wait(app, lambda: dlg.ask_btn.isEnabled())
        dlg.on_ask_llm()
        _wait(app, lambda: "Council mapping" in dlg.text.toPlainText())
        assert "'Zorp rate' → " in dlg.text.toPlainText()
        assert dlg.ask_btn.isEnabled()
    finally:
        dlg.deleteLater()


def test_the_preview_never_offers_the_part_field(ledger):
    """The Preview offered 'Part Number' for a label; the extract
    (place_labels) never does (review, 2026-10-19)."""
    _csv, schema = ledger
    model = FakeModel(mapping={"mappings": [{"label": "Zorp rate",
                                             "field": schema.id_field}]})
    mapping, _ = se.suggest_mappings_for_labels(["Zorp rate"], schema, chat=model)
    assert mapping == {"Zorp rate": None}
    fields = json.loads(model.prompts[0].split("Template fields:\n", 1)[1]
                        .split("\n\nLabels:", 1)[0])
    assert fields and schema.id_field not in fields


def test_skip_patterns_typed_before_the_csv_exists_are_kept(tmp_path):
    """They were dropped: the field was cleared when a CSV path was typed,
    or kept showing them after Create while the extract read every file
    (review, 2026-10-19)."""
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    _qapp()
    # the real create_parent; the graph rebuild (the vault) stubbed
    tab = LedgerTab(actions=LedgerActions(rebuild_kg=lambda progress: {}))
    try:
        # typed: the CSV path is typed after the patterns
        csv = tmp_path / "typed.csv"
        tab.ignore_files.setText("cover*.xlsx")
        tab._save_ignore_patterns()                  # editingFinished, no CSV yet
        tab.csv.setText(str(csv))
        tab._on_csv_changed()                        # editingFinished on the CSV
        assert se.load_ignore_files(csv) == ["cover*.xlsx"]
        assert tab.ignore_files.text() == "cover*.xlsx"
        # Create: the CSV is made beside the template
        tab.csv.setText("")
        tab._on_csv_changed()
        tab.template.setText(str(lf.make_template(tmp_path / "template.xlsx")))
        tab.ignore_files.setText("agenda*.xlsx")
        tab._save_ignore_patterns()
        tab.on_create()
        assert tab.wait(30)
        made = Path(tab.csv.text())
        assert made.exists() and se.load_ignore_files(made) == ["agenda*.xlsx"]
        assert tab.ignore_files.text() == "agenda*.xlsx"
        # a CSV with its own patterns: the typed ones are added, not dropped
        se.save_ignore_files(tmp_path / "own.csv", ["meeting*.xlsx"])
        tab.csv.setText("")
        tab._on_csv_changed()
        tab.ignore_files.setText("notes*.xlsx")
        tab._save_ignore_patterns()
        tab.csv.setText(str(tmp_path / "own.csv"))
        tab._on_csv_changed()
        assert se.load_ignore_files(tmp_path / "own.csv") == ["meeting*.xlsx",
                                                               "notes*.xlsx"]
    finally:
        tab.deleteLater()


def test_the_summary_says_which_files_were_left_out_and_why(tmp_path):
    """Only a progress line said so, and it went by (review, 2026-10-19)."""
    tpl = lf.make_template(tmp_path / "template.xlsx")
    csv = Path(li.create_parent(tpl).csv)
    lf.make_laser_log(tmp_path / "laser_101.xlsx")
    lf.make_laser_log(tmp_path / "cover_sheet.xlsx")
    se.save_ignore_files(csv, ["cover*"])
    first = li._extract(str(csv), [str(tmp_path)], tmp_path / "review_1.xlsx", "", False,
                        None, _NoProg(), None, None, None, False)
    assert sorted(first.skipped) == [("cover_sheet.xlsx", "Skip pattern 'cover*'"),
                                     ("template.xlsx", "the template")]
    assert "left out 2 file(s): " in first.describe()
    assert "cover_sheet.xlsx (Skip pattern 'cover*')" in first.describe()
    again = li._extract(str(csv), [str(tmp_path)], tmp_path / "review_2.xlsx", "", False,
                        None, _NoProg(), None, None, None, False)
    assert ("review_1.xlsx", "a Ledger review") in again.skipped


# ---------------------------------------------- one origin rule for every model list

def test_connections_lists_models_by_the_same_origin_as_the_ledger(monkeypatch, tmp_path):
    """llava:7b was US (Meta) in the Ledger's list and non-US in Suggest
    links' (pi_models' name prefixes) - review, 2026-10-19."""
    import council_engine as ce
    from council_core import local_models as lm
    from council_qt.tabs.connections import ConnectionsActions
    tags = [{"name": "llava:7b", "details": {"family": "llama", "families": ["llama", "clip"]}},
            {"name": "qwen2.5:7b", "details": {"family": "qwen2"}},
            {"name": "llama-pro:8b", "details": {"family": "llama"}},     # Tencent
            {"name": "mystery:1b", "details": {"family": "mystery"}},     # unknown: not US
            {"name": "granite3.3:8b", "details": {"family": "granite"}},
            # family "llama", not Meta's: Mistral fine-tunes and a TinyLlama
            # ('phi' in 'dolphin') - second review, 2026-10-19
            {"name": "zephyr:7b", "details": {"family": "llama"}},
            {"name": "openhermes:7b", "details": {"family": "llama"}},
            {"name": "tinydolphin:1.1b", "details": {"family": "llama"}}]
    monkeypatch.setattr(ce, "_ollama_tags", lambda host, **kw: {"models": tags})
    got = ConnectionsActions(vault_dir=tmp_path / "v").local_models()
    assert got == ["granite3.3:8b", "llava:7b"]
    assert got == sorted(t["name"] for t in tags if lm.ollama_entry(
        t, fill_from_show=False)["origin"] == "US")
    # a Pi node's model by the same rule: llama-pro is Tencent's, not Meta's
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "1")
    vault = tmp_path / "v"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "node_registry.json").write_text(json.dumps({"nodes": [
        {"name": "NodePrimus", "host": "192.0.2.10", "model": "llama-pro:8b"},
        {"name": "NodeTertius", "host": "192.0.2.12", "model": "llama3.2:3b"}]}),
        encoding="utf-8")
    usable, skipped = ConnectionsActions(vault_dir=vault).pi_nodes()
    assert [n for n, _u, _m in usable] == ["NodeTertius"]
    assert skipped == [("NodePrimus", "llama-pro:8b")]


# ---------------------------------------------- second review of the fixes (2026-10-19)

def _one_value_book(path: Path, label: str, value, sheet: str = "Build") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws["A1"], ws["B1"] = label, value
    wb.save(path)
    return path


def _stub(answer):
    def chat(prompt, schema):
        return json.dumps({"part": answer, "evidence": "stub"}), "stub"
    return chat


def test_the_default_sheet_name_is_not_a_part(tmp_path):
    """'Sheet1' is in almost every workbook and passed as an identifier:
    three unrelated workbooks merged into one phantom part 'Sheet1'
    (review, 2026-10-19)."""
    for name in ("log_a.xlsx", "log_b.xlsx", "log_c.xlsx"):
        src = _one_value_book(tmp_path / name, "Pulse freq", 61.35, sheet="Sheet1")
        grids, _imgs = se.load_workbook_grids(src, [])
        rep = ledger_llm.AssistReport()
        assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub("Sheet1"),
                                    rep) == ("", "")
        assert any("dropped" in p for p in rep.problems)
    for generic in ("Sheet1", "Sheet 2", "Book1", "Table3", "Run2", "IMG_1234"):
        assert not ledger_llm.looks_like_part(generic), generic
    assert ledger_llm.looks_like_part("Sheet1", "@@@@@#")     # the user's own pattern


NON_IDS = [  # (label, the cell and the model's answer) - none is a part number
    ("Start time", "10:30"),
    ("Date", "05-Oct-2026"),
    ("Time", "2026-10-05T10:30"),                   # its piece '2026-10-05T10' too
    ("Thickness", "61.35mm"),
    ("Freq", "100kHz"),
    ("Yield", "12.5%"),
    ("Run", "Run2"),
]


@pytest.mark.parametrize("label,value", NON_IDS)
def test_a_value_with_a_unit_a_time_or_a_date_is_not_a_part(tmp_path, label, value):
    """They passed the identifier check and the whole-word check (review,
    2026-10-19)."""
    src = _one_value_book(tmp_path / "x.xlsx", label, value)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(value), rep) == ("", "")
    assert not rep.parts_found


def test_the_file_name_itself_is_not_a_part_unless_it_is_one_code(tmp_path):
    """'inspection_run_3' (from inspection_run_3.xlsx) passed: '_' is a word
    boundary (review, 2026-10-19). A file named by its part still gives it."""
    src = _one_value_book(tmp_path / "inspection_run_3.xlsx", "Pulse freq", 61.35)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, None, _stub("inspection_run_3"),
                                rep) == ("", "")
    src = _one_value_book(tmp_path / "AB12CD345.xlsx", "Pulse freq", 61.35)
    grids, _imgs = se.load_workbook_grids(src, [])
    assert ledger_llm.find_part(src.name, grids, None, _stub("AB12CD345"),
                                ledger_llm.AssistReport()) == (
        "AB12CD345", "file name 'AB12CD345.xlsx'")


def test_real_part_numbers_still_pass():
    looks = ledger_llm.looks_like_part
    for s in ("ABC1DE234", "XYZ9AB123-R2", "SN#4471", "XJ-9", "AB12", "4471A", "12345"):
        assert looks(s), s


SPACED_CASES = [  # (the cell, the model's answer, the part found) - none of them ''
    ("QRS 6TU 789", "QRS 6TU 789", "QRS 6TU 789"),      # gemma3, granite3.3, llama3.1, phi4
    ("QRS 6TU 789", "QRS6TU789", "QRS 6TU 789"),        # the answer without the spaces
    ("QRS 6TU 789", "qrs6tu789", "QRS 6TU 789"),        # ... as the cell writes it
    ("QRS 6TU 789", "Part QRS 6TU 789", "QRS 6TU 789"),  # a word around it
    ("QRS6TU789", "QRS 6TU 789", "QRS6TU789"),          # the other way round
    ("S/N AB12 CD34", "S/N AB12 CD34", "AB12 CD34"),    # the label is not the id
    ("S/N XYZ9AB123-R2  (rev B)", "XYZ9AB123-R2 (rev B)", "XYZ9AB123-R2"),
    # a model that copies the cell: the words next to the id are not of it -
    # they were glued to it, one sample split in two (second review, 2026-10-19)
    ("JD ABC1DE234", "JD ABC1DE234", "ABC1DE234"),      # an inspector's initials
    ("ABC1DE234 TOP", "ABC1DE234 TOP", "ABC1DE234"),    # a side
    ("UT ABC1DE234", "UT ABC1DE234", "ABC1DE234"),      # a method
    ("ABC1DE234 PASS", "ABC1DE234 PASS", "ABC1DE234"),
    ("ABC1DE234 Rev B", "ABC1DE234 Rev B", "ABC1DE234"),
    ("ABC1DE234 XYZ9AB123", "ABC1DE234 XYZ9AB123", "ABC1DE234"),   # a second id
    ("UT QRS 6TU 789 A", "UT QRS 6TU 789 A", "QRS 6TU 789"),
    ("QRS 6TU 789 Rev B", "QRS 6TU 789 Rev B", "QRS 6TU 789"),
    # ... nor do they make the id with spaces a piece of a longer one: dropped
    ("QRS 6TU 789 A", "QRS 6TU 789", "QRS 6TU 789"),
    ("QRS 6TU 789 A", "QRS6TU789", "QRS 6TU 789"),
    ("QRS 6TU 789 TOP", "QRS 6TU 789", "QRS 6TU 789"),
    ("UT QRS 6TU 789", "QRS 6TU 789", "QRS 6TU 789"),
    ("Tag QRS 6TU 789", "QRS 6TU 789", "QRS 6TU 789"),
    ("COUPON QRS 6TU 789", "QRS 6TU 789", "QRS 6TU 789"),
]
SPACED_NON_IDS = ["Oct 5 2026", "2026 10 05", "12 345", "280 W", "61.35 mm", "Sheet 1",
                  "Laser build 3", "Power 280",
                  # a label and its number, a standard, a quarter, a day, a time, axes:
                  # all passed as ids with spaces (second review, 2026-10-19)
                  "ISO 9001", ("Spec: ASTM E1444 / AMS 2644", "AMS 2644"), "NAS 410",
                  "EN 4179", "CH 12", "OP 20", "GAIN 40", "Qty 12", "WK 42", "Q3 2026",
                  "FY 2026", "2026 Q3", "REV A 2026", "12 Oct", "10 30 AM", "PASS 2 OF 3",
                  "X1 Y2", "X12 Y34", "CH1 CH2", "Lab 3 Bay 12"]


@pytest.mark.parametrize("cell,answer,part", SPACED_CASES)
def test_an_id_written_with_spaces_is_kept_whole(tmp_path, cell, answer, part):
    """'QRS 6TU 789' in a cell was cut to '6TU' or dropped - an identifier
    had no spaces - and the model's 'QRS6TU789' for it was dropped as not
    written there (review, 2026-10-19). The whole answer is compared
    ignoring the spaces inside it, and given as the cell writes it."""
    src = _one_value_book(tmp_path / "insp.xlsx", "Inspection record", None, sheet="Record")
    wb = openpyxl.load_workbook(src)
    wb["Record"]["A2"] = cell
    wb.save(src)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(answer), rep) == (
        part, "insp.xlsx › Record!A2")
    assert rep.parts_found == [(src.name, part)] and not rep.problems


@pytest.mark.parametrize("text", SPACED_NON_IDS)
def test_spaces_do_not_make_a_value_or_a_title_a_part(tmp_path, text):
    """The identifier rules still hold for an id written with spaces: a
    date, a number in groups, a value with its unit, 'Sheet 1', a title,
    a label with its number. Two workbooks with 'CH 12' merged into one
    phantom part 'CH 12' (second review, 2026-10-19)."""
    cell, answer = text if isinstance(text, tuple) else (text, text)
    src = _one_value_book(tmp_path / "insp.xlsx", cell, None, sheet="Record")
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(answer), rep) == ("", "")
    assert not rep.parts_found and any("dropped" in p for p in rep.problems)


@pytest.mark.parametrize("answer", ["12 345", "12345"])
def test_a_number_in_groups_is_not_a_part_of_an_all_digit_pattern(answer):
    """'#####' matched 'Freq 12 345' with its spaces left out (second
    review, 2026-10-19): a number written in groups is a value."""
    grids = [se.Grid("insp.xlsx", "Record", {(1, 1): "Freq 12 345"})]
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part("insp.xlsx", grids, "#####", _stub(answer), rep) == ("", "")
    assert not rep.parts_found


FRAGMENTS = [  # (the cell, the model's answer, the user's pattern) - a piece of the id
    ("AB12 CD34 EF56", "AB12 CD34", None),
    ("AB12 CD34 EF56", "AB12", None),
    ("ABC1 DE23 4XY", "ABC1 DE23", "@@@#@@###"),
    ("QRS 6TU 789", "QRS 6TU", "@@@#@@###"),
    ("QRS 6TU 789", "6TU 789", "@@@#@@###"),
]


@pytest.mark.parametrize("cell,answer,mask", FRAGMENTS)
def test_a_piece_of_an_id_written_with_spaces_is_not_the_part(cell, answer, mask):
    """'AB12 CD34' of a cell 'AB12 CD34 EF56' was rejected as a piece, and
    then its own piece 'AB12' was given as the part: the cut the first fix
    was for (second review, 2026-10-19). Dropped, not cut."""
    grids = [se.Grid("insp.xlsx", "Record", {(1, 1): cell})]
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part("insp.xlsx", grids, mask, _stub(answer), rep) == ("", "")
    assert not rep.parts_found and any("dropped" in p for p in rep.problems)


def test_the_part_is_given_as_the_model_wrote_it_when_the_workbook_has_both():
    """With 'QRS 6TU 789' in A1 and 'QRS6TU789' in A2, the answer
    'QRS6TU789' was given as A1 writes it: the ledger compares ids with
    their spaces, so one sample became two (second review, 2026-10-19)."""
    grids = [se.Grid("insp.xlsx", "Record", {(1, 1): "QRS 6TU 789", (2, 1): "QRS6TU789"})]
    for answer, row in (("QRS6TU789", 2), ("qrs6tu789", 2), ("QRS 6TU 789", 1)):
        assert ledger_llm.find_part("insp.xlsx", grids, "@@@#@@###", _stub(answer),
                                    ledger_llm.AssistReport()) == (
            ("QRS6TU789", "QRS 6TU 789")[row == 1], f"insp.xlsx › Record!A{row}")


def test_words_copied_with_the_id_do_not_split_a_sample_in_the_review(tmp_path, ledger):
    """End to end, a model that answers with the cell's text: 'ABC1DE234 TOP'
    and 'ABC1DE234' were two parts in the review, and two workbooks with
    'CH 12' one phantom part (second review, 2026-10-19)."""
    csv, _schema = ledger
    cells = {"top.xlsx": "ABC1DE234 TOP", "bot.xlsx": "ABC1DE234", "one.xlsx": "JD ABC1DE234",
             "p.xlsx": "CH 12", "q.xlsx": "CH 12", "u.xlsx": "QRS 6TU 789 A"}
    for i, (name, a1) in enumerate(cells.items()):
        _one_value_book(tmp_path / name, a1, None, sheet="Sheet1")
        wb = openpyxl.load_workbook(tmp_path / name)
        wb["Sheet1"]["A3"], wb["Sheet1"]["B3"] = "Power (W)", 100 + i
        wb.save(tmp_path / name)

    def echo(prompt, schema):                  # the first cell, as the model saw it
        if "Which part" not in prompt:
            return json.dumps({"mappings": []}), "echo"
        name = prompt.split("File name: ", 1)[1].split("\n", 1)[0]
        part = cells[name] if name != "u.xlsx" else "QRS 6TU 789"
        return json.dumps({"part": part, "evidence": "A1"}), "echo"
    s = li._extract(str(csv), [str(tmp_path / n) for n in cells], None, "", False, None,
                    _NoProg(), "@@@#@@###", None, None, True, echo)
    rows = [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(s.review)[
        "Values"].iter_rows(min_row=2, values_only=True)]
    by_book = {r["Source"].split(" › ", 1)[0]: r["Part"] for r in rows}
    assert by_book == {"top.xlsx": "ABC1DE234", "bot.xlsx": "ABC1DE234",
                       "one.xlsx": "ABC1DE234", "p.xlsx": None, "q.xlsx": None,
                       "u.xlsx": "QRS 6TU 789"}


def test_spaces_inside_an_id_only_as_a_cell_writes_them(tmp_path):
    """A sheet or file name with spaces in it is not taken as a spaced id;
    one written without them still is, in its own case."""
    src = _one_value_book(tmp_path / "insp.xlsx", "Pulse freq", 61.35, sheet="QRS 6TU 789")
    grids, _imgs = se.load_workbook_grids(src, [])
    for answer in ("QRS 6TU 789", "QRS6TU789"):
        assert ledger_llm.find_part(src.name, grids, None, _stub(answer),
                                    ledger_llm.AssistReport()) == ("", "")
    src = _one_value_book(tmp_path / "insp2.xlsx", "Pulse freq", 61.35, sheet="EFG5HJ678")
    grids, _imgs = se.load_workbook_grids(src, [])
    assert ledger_llm.find_part(src.name, grids, None, _stub("efg5hj 678"),
                                ledger_llm.AssistReport()) == (
        "EFG5HJ678", "sheet name 'EFG5HJ678'")


def test_a_spaced_id_reaches_the_review_whole(tmp_path, ledger):
    """End to end: the review's Part column holds the id as the cell writes it."""
    csv, _schema = ledger
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    wb = openpyxl.load_workbook(src)
    wb["Sheet1"]["A1"] = "QRS 6TU 789"
    wb.save(src)
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), "@@@#@@###",
                    None, None, True, FakeModel(part="QRS6TU789"))
    parts = {r[1] for r in openpyxl.load_workbook(s.review)["Values"].iter_rows(
        min_row=2, values_only=True)}
    assert parts == {"QRS 6TU 789"}
    assert "untitled.xlsx → QRS 6TU 789" in s.describe()


def _sent(grids, mask):
    model = FakeModel(part="")
    ledger_llm.find_part("big.xlsx", grids, mask, model, ledger_llm.AssistReport())
    return json.loads(model.prompts[0].split("Cells:\n", 1)[1])


def test_whole_number_readings_do_not_crowd_out_the_part():
    """Excel stores 10200 Hz as 10200.0: a table of whole numbers went first
    as 'ids' and the S/N below it never reached the model (review,
    2026-10-19)."""
    table = {(1, 1): "Freq (Hz)", (1, 2): "Gain", (1, 3): "Phase"}
    for r in range(2, 152):
        for c in range(1, 4):
            table[(r, c)] = 10000.0 + r * 100 + c
    table[(160, 1)] = "S/N: XYZ9AB123"
    grids = [se.Grid("big.xlsx", "Data", table)]
    for mask in ("@@@#@@###", None):
        cells = _sent(grids, mask)
        assert cells[:4] == ["S/N: XYZ9AB123", "Freq (Hz)", "Gain", "Phase"]
    # an all-digit pattern: what it matches whole comes after the text ids,
    # and readings it does not match go last - with the labels before them
    loads = {(1, 1): "Load (N)", (230, 1): "Part", (230, 2): "48213"}
    loads.update({(r, 1): 1000.0 + r for r in range(2, 220)})
    cells = _sent([se.Grid("big.xlsx", "Data", loads)], "#####")
    assert cells[:3] == ["48213", "Load (N)", "Part"]


def test_skip_patterns_do_not_leak_from_one_ledger_into_another(tmp_path):
    """Switching from a ledger with Skip patterns to one without wrote the
    first one's patterns into the second's aliases file (review,
    2026-10-19). Only patterns TYPED with no CSV are carried over."""
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    _qapp()
    a, b = tmp_path / "ledger_a.csv", tmp_path / "ledger_b.csv"
    se.save_ignore_files(a, ["cover*.xlsx", "agenda*"])
    tab = LedgerTab(actions=LedgerActions(rebuild_kg=lambda progress: {}))
    try:
        tab.csv.setText(str(a))
        tab._on_csv_changed()
        assert se.load_ignore_files(a) and tab.ignore_files.text()
        tab.csv.setText(str(b))
        tab._on_csv_changed()
        assert tab.ignore_files.text() == ""
        assert se.load_ignore_files(b) == []
        # typed with no CSV, the field not left yet (no editingFinished)
        tab.csv.setText("")
        tab._on_csv_changed()
        tab.ignore_files.setText("draft*")
        tab.ignore_files.textEdited.emit("draft*")      # as typing does
        tab.csv.setText(str(b))
        tab._on_csv_changed()
        assert se.load_ignore_files(b) == ["draft*"]
    finally:
        tab.deleteLater()


#: A question that failed at once - Ollama's own words when its runner died
#: on one request - not a timeout: the model is asked again.
_ONE_FAILED = "Ollama (granite3.3:8b): llama runner process has terminated: exit status 2"


def test_one_failed_question_does_not_stop_the_model():
    """The breaker tripped on any failure: one failed question and no other
    workbook or label was asked, and the summary called an answering model
    unavailable (review, 2026-10-19). A timeout does stop it at once:
    test_a_timeout_stops_the_model_at_once."""
    calls = []

    def flaky(prompt, schema):
        calls.append(prompt)
        if "first.xlsx" in prompt:
            raise RuntimeError(_ONE_FAILED)
        return json.dumps({"part": "ABC1DE234", "evidence": "A1"}), "granite3.3:8b"
    rep = ledger_llm.AssistReport()
    ask = ledger_llm.one_shot(flaky, rep)
    got = [ledger_llm.find_part(n, [se.Grid(n, "Data", {(1, 1): "ABC1DE234"})],
                                "@@@#@@###", ask, rep)[0]
           for n in ("first.xlsx", "second.xlsx", "third.xlsx")]
    assert len(calls) == 3 and got == ["", "ABC1DE234", "ABC1DE234"]
    assert not rep.unavailable
    assert "unavailable" not in rep.describe()
    assert sum("runner process has terminated" in p for p in rep.problems) == 1


def test_a_model_that_fails_twice_in_a_row_is_not_asked_again():
    calls = []

    def broken(prompt, schema):
        calls.append(prompt)
        raise RuntimeError(_ONE_FAILED)
    rep = ledger_llm.AssistReport()
    ask = ledger_llm.one_shot(broken, rep)
    for n in ("a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"):
        ledger_llm.find_part(n, [se.Grid(n, "Data", {(1, 1): "ABC1DE234"})], None, ask, rep)
    assert len(calls) == 2
    assert sum("runner process" in p for p in rep.problems) == 1     # said once
    assert rep.describe() == (
        "Council's model did not answer twice in a row and was not asked again, so "
        "nothing in the review is its suggestion: " + _ONE_FAILED + ".")


#: council_engine's own words when Ollama does not start answering in time.
_NO_START = ("Ollama (granite3.3:8b) did not start answering within 300 s (loading or "
             "prefilling the model; COUNCIL_OLLAMA_LOAD_TIMEOUT raises the limit); the "
             "request was cancelled, not re-sent.")


def test_a_timeout_stops_the_model_at_once(tmp_path, ledger, monkeypatch):
    """In the real app every part-less workbook waited the full 300 s
    Ollama timeout again after the model had already timed out: the breaker
    stopped only after two failures in a row (review, 2026-10-19). Through
    the Ledger's own path - the picked Ollama model, chat_for, council_engine
    raising its LocalChatTimeout after a wait - the second workbook and the
    labels are not asked, and the summary says why."""
    import time
    import council_engine as ce
    csv, _schema = ledger
    books = [_unlabelled_book(tmp_path / f"untitled_{i}.xlsx") for i in range(3)]
    calls = []

    def times_out(messages, **kw):
        calls.append(messages[0]["content"])
        time.sleep(0.3)                                 # the wait, shortened
        raise ce.LocalChatTimeout(_NO_START)
    monkeypatch.setattr(ce, "local_chat", times_out)
    s = li._extract(str(csv), [str(b) for b in books], None, "", False, None, _NoProg(),
                    "=@@@#@@###", None, None, "ollama:granite3.3:8b")
    # one wait, not one per workbook - and the labels were not asked either
    assert len(calls) == 1 and "File name: untitled_0.xlsx" in calls[0]
    stopped = [p for p in s.problems if "ran out of time" in p]
    assert len(stopped) == 1 and "did not start answering within 300 s" in stopped[0]
    assert stopped[0].startswith("Council's model ran out of time on untitled_0.xlsx and was "
                                 "not asked again in this run (each later question could "
                                 "have waited as long): ")
    assert s.assist.startswith(
        "Council's model ran out of time on untitled_0.xlsx and was not asked again in this "
        "run (each later question could have waited as long), so nothing in the review is "
        "its suggestion: granite3.3:8b (Ollama) did not answer: Ollama (granite3.3:8b) did "
        "not start answering within 300 s")
    assert "unavailable" not in s.assist and "Is Ollama running" not in s.assist


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"),                                       # a socket timeout
    __import__("urllib.error").error.URLError(TimeoutError("timed out")),
    RuntimeError("Ollama (granite3.3:8b): timed out waiting for llama runner to start"),
    se.LedgerError("granite3.3:8b (Ollama) did not answer: Ollama (granite3.3:8b) sent "
                   "nothing for 120 s; the request was cancelled, not re-sent"),
])
def test_every_kind_of_timeout_stops_the_model_and_covers_the_labels(ledger, error):
    """After the parts the labels are asked too (place_labels): a timeout
    on a part stops that question as well, and one on the labels says so."""
    _csv, schema = ledger
    calls = []

    def slow(prompt, schema_):
        calls.append(prompt)
        raise error
    rep = ledger_llm.AssistReport()
    ask = ledger_llm.one_shot(slow, rep)
    grids = [se.Grid("big.xlsx", "Data", {(1, 1): "ABC1DE234"})]
    assert ledger_llm.find_part("big.xlsx", grids, None, ask, rep) == ("", "")
    assert ledger_llm.find_part("next.xlsx", grids, None, ask, rep) == ("", "")
    run = se.Extraction(sources=["x.xlsx"])
    run.unmatched.append(se.Obs("", "", "", "55", 0.0, "none", "Zorp rate", "x.xlsx › Data!B1"))
    ledger_llm.place_labels(run, schema, ask, rep)
    assert len(calls) == 1 and rep.timed_out and rep.stopped_on == "big.xlsx"
    assert len(rep.problems) == 1 and "ran out of time on big.xlsx" in rep.problems[0]
    # a timeout on the labels question itself names it
    rep, calls[:] = ledger_llm.AssistReport(), []
    ledger_llm.place_labels(run, schema, ledger_llm.one_shot(slow, rep), rep)
    assert len(calls) == 1 and "ran out of time on the labels left over" in rep.describe()


def test_the_summary_does_not_add_a_full_stop_after_a_question_mark():
    """'... Is Ollama running?.' (review, 2026-10-19)."""
    why = "granite3.3:8b (Ollama) did not answer: No Ollama server answers. Is Ollama running?"
    rep = ledger_llm.AssistReport(unavailable=why)
    assert rep.describe().endswith("Is Ollama running?")
    rep = ledger_llm.AssistReport(unavailable=why, parts_asked=2, model="granite3.3:8b",
                                  parts_found=[("a.xlsx", "ABC1DE234")])
    assert rep.describe().endswith("not asked again: " + why)
    rep = ledger_llm.AssistReport(parts_asked=1, model="m")
    assert rep.describe().endswith("nothing in the review is its suggestion.")
    rep = ledger_llm.AssistReport(parts_asked=1, model="m", parts_found=[("a.xlsx", "X1Y2Z3")])
    assert rep.describe().endswith("check them.")


def test_the_summary_cuts_a_long_error_between_clauses_not_inside_a_word():
    """council_engine's own timeout words ended '... the request was canc.'
    in the summary: cut at 200 characters (second review, 2026-10-19). The
    problem line keeps it all."""
    why = "granite3.3:8b (Ollama) did not answer: " + _NO_START
    rep = ledger_llm.AssistReport(unavailable=why, timed_out=True, stopped_on="b0.xlsx")
    assert rep.describe().endswith(
        "did not start answering within 300 s (loading or prefilling the model; "
        "COUNCIL_OLLAMA_LOAD_TIMEOUT raises the limit).")
    rep = ledger_llm.AssistReport(unavailable=why, timed_out=True, parts_asked=2,
                                  model="granite3.3:8b", parts_found=[("a.xlsx", "ABC1DE234")])
    assert rep.describe().endswith("COUNCIL_OLLAMA_LOAD_TIMEOUT raises the limit).")
    # no clause to end at: at a word, and said to go on
    rep = ledger_llm.AssistReport(unavailable=" ".join(["overloaded"] * 40))
    assert rep.describe().endswith(": " + " ".join(["overloaded"] * 18) + "…")


@pytest.mark.parametrize("name", ["zephyr:7b", "openhermes:7b", "neural-chat:7b",
                                  "vicuna:7b", "orca-mini:3b", "tinydolphin:1.1b",
                                  "llava:34b"])
def test_a_llama_family_alone_does_not_make_a_model_us(name):
    """Ollama reports family 'llama' for every Llama- or Mistral-architecture
    model; these came out ('Meta', 'US') and Suggest links ran on them, and
    'phi' inside 'tinydolphin' read as Microsoft (review, 2026-10-19)."""
    from council_core import local_models as lm
    assert lm.maker_and_origin(name, "llama", ["llama", "clip"])[1] != "US"


def test_the_ledger_and_connections_use_one_rule_for_an_unknown_ollama_model(monkeypatch,
                                                                            tmp_path):
    """An unknown-origin Ollama model was usable in the Ledger and left out
    of Suggest links (review, 2026-10-19). A GGUF is always accepted unless
    it is known to be non-US."""
    import council_engine as ce
    from council_core import local_models as lm
    from council_qt.tabs.connections import ConnectionsActions
    tags = [{"name": "zephyr:7b", "details": {"family": "llama"}},
            {"name": "mystery:1b", "details": {"family": "mystery"}},
            {"name": "granite3.3:8b", "details": {"family": "granite"}}]
    monkeypatch.setattr(ce, "_ollama_tags", lambda host, **kw: {"models": tags})
    assert ConnectionsActions(vault_dir=tmp_path / "v").local_models() == ["granite3.3:8b"]
    entries = [lm.ollama_entry(t, fill_from_show=False) for t in tags] + [
        {"id": "C:/m/model.gguf", "name": "model", "backend": "gguf", "origin": "unknown"},
        {"id": "C:/m/qwen.gguf", "name": "qwen", "backend": "gguf", "origin": "non-US"}]
    monkeypatch.setattr(lm, "list_local_models", lambda **kw: [dict(e) for e in entries])
    usable = {e["id"]: e["usable"] for e in ledger_llm.available_models()}
    assert usable == {"ollama:zephyr:7b": False, "ollama:mystery:1b": False,
                      "ollama:granite3.3:8b": True, "C:/m/model.gguf": True,
                      "C:/m/qwen.gguf": False}
    with pytest.raises(se.LedgerError, match="cannot tell where mystery:1b comes from"):
        ledger_llm.chat_for("ollama:mystery:1b")


@pytest.mark.parametrize("sep", [":", ",", ";"])
def test_a_spaced_part_after_a_run_of_separators_is_read_at_once(sep):
    """_inside_a_value tried r'\\S+[\\s,;:]*$' from every character before a
    spaced part id the model named: 1,000 ':' and a word before it held the
    GIL for 0.8 s, 2,000 for 6 s, while the extract asked for the part
    (review, 2026-10-20). The same reading, from the end."""
    import re
    import time
    for n in (1000, 30000):
        cell = sep * n + " end QRS 6TU 789"
        t0 = time.perf_counter()
        got = ledger_llm._whole_word("QRS 6TU 789", cell)
        assert time.perf_counter() - t0 < 0.05 and got == "QRS 6TU 789"
    for s in ("", "  ", " :: ", "AB 12::", "x", "ab:cd:", "AB 12 ::: ", "a ;\t,b , ",
              "Oct 5,", ":, ;", "12:30 \n", "\xa0x\u3000:"):
        m = re.search(r"\S+[\s,;:]*$", s)
        assert ledger_llm._word_before(s) == (m.start() if m else None), repr(s)


# ---------------------------------------------- wave 3 review (2026-10-20, real models)

FILE_NAME_CASES = [  # (the workbook's file name, the model's answer, the part kept)
    ("wb08.xlsx", "wb08.xlsx", ""),                       # the file name, extension and all
    ("wb08.xlsx", "wb08", ""),                            # a generic stem
    ("EC_log_week41.xlsx", "EC_log_week41.xlsx", ""),
    ("EC_log_week41.xlsx", "EC_log_week41", ""),          # words, no 4+-letter one alone
    ("laser_run2.xlsx", "laser_run2.xlsx", ""),
    ("Inspection Results 2026.xlsx", "Inspection Results 2026.xlsx", ""),   # not '2026.xlsx'
    ("data_2.xlsx", "data_2", ""),
    ("book1.xlsx", "book1", ""),
    ("Copy of run 7.xlsx", "Copy of run 7", ""),
    ("Results2026.xlsx", "Results2026", ""),
    # an identifier in the stem is still the part, as the name writes it
    ("Copy of AB12CD345.xlsx", "AB12CD345", "AB12CD345"),
    ("Copy of AB12CD345.xlsx", "Copy of AB12CD345", "AB12CD345"),
    ("AB12CD345.xlsx", "AB12CD345", "AB12CD345"),
    ("RST-4UV-567.xlsx", "RST-4UV-567", "RST-4UV-567"),
    ("LMM3EF306 results.xlsx", "LMM3EF306", "LMM3EF306"),
    ("wb07_GHJ7KL890.xlsx", "wb07_GHJ7KL890", "GHJ7KL890"),   # what the pattern matches in it
]


@pytest.mark.parametrize("name,answer,part", FILE_NAME_CASES)
def test_the_file_name_is_never_the_part_only_an_identifier_in_its_stem(tmp_path, name,
                                                                        answer, part):
    """llama3.2:1b answered each workbook's own file name and all seven
    were accepted: the name with its extension ('wb08.xlsx'), a stem with
    no word of 4+ letters ('EC_log_week41'), and '2026.xlsx' cut from
    'Inspection Results 2026.xlsx' (review, 2026-10-20)."""
    src = _one_value_book(tmp_path / name, "Pulse freq", 61.35)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    got = ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(answer), rep)
    if part:
        assert got == (part, f"file name {name!r}") and rep.parts_found == [(name, part)]
    else:
        assert got == ("", "") and not rep.parts_found
        assert any("dropped" in p for p in rep.problems)


STANDARDS = ["AMS2750", "AMS 2750", "ASTM E1444", "ISO 9001", "ISO9001:2015",
             "MIL-STD-1916", "NAS410", "AWS D17.1", "ASME Y14.5", "SAE AS9100",
             "BS EN 10204", "EN4179", "ARP1923"]


@pytest.mark.parametrize("spec", STANDARDS)
def test_a_standards_number_is_not_a_part(tmp_path, spec):
    """'Spec AMS2750 rev G' in a part-less workbook: llama3.1:8b, granite3.3:2b
    and gemma3:1b answered 'AMS2750' and it was kept (review, 2026-10-20)."""
    src = _one_value_book(tmp_path / "furnace.xlsx", f"Spec {spec} rev G", None)
    grids, _imgs = se.load_workbook_grids(src, [])
    for answer in (spec, f"Spec {spec} rev G"):
        rep = ledger_llm.AssistReport()
        assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(answer),
                                    rep) == ("", ""), answer
        assert not rep.parts_found and any("dropped" in p for p in rep.problems)


def test_a_standards_number_is_a_part_when_the_users_pattern_says_so(tmp_path):
    src = _one_value_book(tmp_path / "furnace.xlsx", "Spec AMS2750 rev G", None)
    grids, _imgs = se.load_workbook_grids(src, [])
    assert ledger_llm.find_part(src.name, grids, "@@@####", _stub("AMS2750"),
                                ledger_llm.AssistReport())[0] == "AMS2750"
    for real in ("ENG4471", "NASA123", "MILL123", "ISOLATOR-3", "ABC1DE234"):
        assert ledger_llm.looks_like_part(real), real


def test_part_less_workbooks_do_not_merge_into_a_standards_number(tmp_path, ledger):
    """The phantom part: two furnace logs with no part, the model's 'AMS2750'
    for both, every value one sample's (review, 2026-10-20)."""
    csv, _schema = ledger
    books = []
    for name, watt in (("furnace_A.xlsx", 289.1), ("furnace_B.xlsx", 301.7)):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A3"] = "Spec AMS2750 rev G"
        ws["A5"], ws["B5"] = "Power (W)", watt
        wb.save(tmp_path / name)
        books.append(str(tmp_path / name))
    s = li._extract(str(csv), books, None, "", False, None, _NoProg(), "@@@#@@###", None,
                    None, True, FakeModel(part="AMS2750"))
    rows = [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(s.review)[
        "Values"].iter_rows(min_row=2, values_only=True)]
    assert rows and not any(r["Part"] for r in rows)
    assert not any(r["Other values offered"] for r in rows)          # no clash of two books


@pytest.mark.parametrize("cell,answer,part", [
    ("Tag RST-4UV-567", "RST4UV567", "RST-4UV-567"),       # the tester's wb04_separators
    ("RST_4UV_567", "rst4uv567", "RST_4UV_567"),
    ("RST.4UV.567", "RST-4UV-567", "RST.4UV.567"),
    ("RST4UV567", "RST-4UV-567", "RST4UV567"),             # the other way round
])
def test_an_answer_without_the_separators_is_given_as_the_cell_writes_it(tmp_path, cell,
                                                                         answer, part):
    """'RST4UV567' for a cell 'Tag RST-4UV-567' was dropped as not written
    in the workbook (review, 2026-10-20)."""
    src = _one_value_book(tmp_path / "insp.xlsx", "Inspection record", None, sheet="Record")
    wb = openpyxl.load_workbook(src)
    wb["Record"]["A2"] = cell
    wb.save(src)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, "@@@#@@###", _stub(answer), rep) == (
        part, "insp.xlsx › Record!A2")


@pytest.mark.parametrize("cell,answer", [("ABC-1DE-234", "ABC1DE"),
                                         ("ABC-1DE-234", "1DE234"),
                                         ("2026-10-05", "20261005")])
def test_separators_aside_a_piece_of_an_id_or_a_date_is_still_not_the_part(tmp_path, cell,
                                                                          answer):
    src = _one_value_book(tmp_path / "insp.xlsx", "Inspection record", None, sheet="Record")
    wb = openpyxl.load_workbook(src)
    wb["Record"]["A2"] = cell
    wb.save(src)
    grids, _imgs = se.load_workbook_grids(src, [])
    assert ledger_llm.find_part(src.name, grids, None, _stub(answer),
                                ledger_llm.AssistReport()) == ("", "")


def _probe_book(path: Path) -> Path:
    """A part the rules find, the inspector who signed it, a per-flaw
    reading under a label with no flaw code beside it, and a part-level
    one. The inspector's own column of the per-flaw one ('<field> [JB]')
    went to every flaw row too (second review, 2026-10-20)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"], ws["B1"] = "Part Number", "KTV4MW517"
    ws["A2"], ws["B2"] = "Inspector:", "JB"
    ws["A3"], ws["B3"] = "Probe gain dB", 46
    ws["A4"], ws["B4"] = "Zorp rate", 55
    wb.save(path)
    return path


def _flaw_rows(csv, schema, fname):
    return {r.get(schema.item_field): r.get(fname) for r in ParentCSV(csv).rows
            if r.get(schema.id_field) == "KTV4MW517"}


def test_a_per_flaw_field_is_not_placed_on_a_value_with_no_flaw(tmp_path, ledger):
    """Council's model placed values under labels with no flaw code: a
    9-way red clash for a per-flaw field, and the one accepted went to
    every flaw row (review, 2026-10-20). It stays on 'Labels to map', the
    model's field in its note; a part-level field is still placed."""
    csv, schema = ledger
    src = _probe_book(tmp_path / "probe.xlsx")
    post = _field(schema, "Post-Conceal")
    freq = _field(schema, "Frequency")
    model = FakeModel(mapping={"Probe gain dB": post, "Zorp rate": freq})
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None, None,
                    None, True, model)
    wb = openpyxl.load_workbook(s.review)
    rows = [dict(zip(se.VAL_HEAD, r)) for r in wb["Values"].iter_rows(min_row=2,
                                                                      values_only=True)]
    assert not [r for r in rows if r["Source label"] == "Probe gain dB"]
    assert [r["Field"] for r in rows if r["Source label"] == "Zorp rate"] == [freq]
    lab = [r for r in wb["Labels to map"].iter_rows(min_row=2, values_only=True)
           if r[2] == "Probe gain dB"]
    assert lab and post in str(lab[0][-1]) and "not placed" in str(lab[0][-1])
    assert any("'Probe gain dB'" in p and "left on 'Labels to map'" in p for p in s.problems)
    assert "1 value(s) left on Labels to map" in s.assist
    # accepting everything in the review writes nothing to the flaw rows
    for r in range(2, wb["Values"].max_row + 1):
        wb["Values"].cell(r, 1, "Y")
    wb.save(s.review)
    li.run_apply(s.review, use_images=False)
    assert not any(_flaw_rows(csv, schema, post).values())
    assert not any(_flaw_rows(csv, schema, f"{post} [JB]").values())


def test_a_label_mapped_by_hand_to_a_per_flaw_field_is_not_written_to_every_flaw(tmp_path,
                                                                                ledger):
    """The same through 'Labels to map': mapped there, Apply put the one
    reading on all nine flaw rows, and the next extract pre-accepted it
    (review, 2026-10-20). So did the inspector's own column of it,
    '<field> [JB]', and Apply's warning said to type the flaw in the
    review's Item column - which this value has no row for (second review,
    2026-10-20)."""
    csv, schema = ledger
    src = _probe_book(tmp_path / "probe.xlsx")
    post = _field(schema, "Post-Conceal")
    jb = f"{post} [JB]"
    assert schema.takes_inspectors(post)
    s = li._extract(str(csv), [str(src)], tmp_path / "r1.xlsx", "", False, None, _NoProg(),
                    None, None, None, False)
    wb = openpyxl.load_workbook(s.review)
    for row in wb["Labels to map"].iter_rows(min_row=2):
        if row[2].value == "Probe gain dB":
            row[0].value = post
            row[8].value = "2B"                  # typed there: not read, and not asked for
    wb.save(s.review)
    a = li.run_apply(s.review, use_images=False)
    assert not any(_flaw_rows(csv, schema, post).values())
    assert not any(_flaw_rows(csv, schema, jb).values())
    held = [p for p in a.problems if "not written" in p]
    assert held == [f"1 value(s) under a label you mapped to {post!r} on 'Labels to map' not "
                    "written: the field is kept per Flaw # and they have no Flaw #. The "
                    "mapping is saved — run Extract again, type the Flaw # on each value's "
                    "Values row, then Apply"]
    # the next extract: the learned mapping offers it, in amber, saying why -
    # the inspector's own column too
    s = li._extract(str(csv), [str(src)], tmp_path / "r2.xlsx", "", False, None, _NoProg(),
                    None, None, None, False)
    wb = openpyxl.load_workbook(s.review)
    rows = [dict(zip(se.VAL_HEAD, r)) for r in wb["Values"].iter_rows(min_row=2,
                                                                      values_only=True)]
    probe = [r for r in rows if r["Source label"] == "Probe gain dB"]
    assert sorted(r["Field"] for r in probe) == [post, jb]
    for r in probe:
        assert r["Accept"] != "Y" and schema.item_field in (r["Note"] or ""), r
    # accepted anyway: not written, and Apply says so
    for r in range(2, wb["Values"].max_row + 1):
        if wb["Values"].cell(r, 8).value == "Probe gain dB":
            wb["Values"].cell(r, 1, "Y")
    wb.save(s.review)
    a = li.run_apply(s.review, use_images=False)
    assert not any(_flaw_rows(csv, schema, post).values())
    assert not any(_flaw_rows(csv, schema, jb).values())
    assert sorted(p for p in a.problems if "not written" in p) == sorted(
        f"1 accepted value(s) of {f!r} not written: the field is kept per Flaw # and they "
        "have no Flaw # — type it in the review's Item column and Apply again"
        for f in (post, jb))
    # ... and with its flaw typed in, it goes to that flaw alone
    for r in range(2, wb["Values"].max_row + 1):
        if wb["Values"].cell(r, 8).value == "Probe gain dB":
            wb["Values"].cell(r, 3, "2B")
    wb.save(s.review)
    li.run_apply(s.review, use_images=False)
    assert {k: v for k, v in _flaw_rows(csv, schema, post).items() if v} == {"2B": "46"}
    assert {k: v for k, v in _flaw_rows(csv, schema, jb).items() if v} == {"2B": "46"}


def test_an_inspectors_own_column_of_a_per_flaw_value_with_no_flaw_needs_its_flaw(ledger):
    """needs_item looked the field up by name, so '<field> [JB]' - decide's
    copy of a signed reading - was never held (second review, 2026-10-20)."""
    _csv, schema = ledger
    post = _field(schema, "Post-Conceal")
    assert se.needs_item(schema, f"{post} [JB]", "")
    assert not se.needs_item(schema, f"{post} [JB]", "2B")
    assert not se.needs_item(schema, _field(schema, "Frequency"), "")
    run = se.Extraction(sources=["probe.xlsx"], obs=[
        se.Obs("KTV4MW517", "", post, "46", 1.0, "alias", "Probe gain dB",
               "probe.xlsx › Build!B3", inspector="JB")])
    ds = se.decide(run, schema)
    assert sorted(d.obs.field for d in ds) == [post, f"{post} [JB]"]
    for d in ds:                                  # neither is pre-accepted, both say why
        assert not d.accept and "type the Flaw # in Item" in d.obs.note, d.obs


def test_the_model_is_not_asked_for_a_workbook_whose_rows_name_their_part(tmp_path, ledger):
    """summary.xlsx has a Part Number column: every value had its part, yet
    the model was asked which part the workbook is, and its '101' became a
    problem line (review, 2026-10-20). A sheet beside it with values of no
    part is still asked about, and only those values get the answer."""
    csv, _schema = ledger
    src = lf.make_summary(tmp_path / "summary.xlsx")
    model = FakeModel(part="101")
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None, None,
                    None, True, model)
    assert not any("Which part" in p for p in model.prompts)
    assert not any("Council's model" in p for p in s.problems)
    wb = openpyxl.load_workbook(src)
    notes = wb.create_sheet("Notes")
    notes["A1"] = "Serial: XYZ9AB123-R2"
    notes["A3"], notes["B3"] = "Scan Speed", 1200
    wb.save(src)
    model = FakeModel()
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None, None,
                    None, True, model)
    assert sum("Which part" in p for p in model.prompts) == 1
    rows = [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(s.review)[
        "Values"].iter_rows(min_row=2, values_only=True)]
    parts = {(r["Field"], r["Part"]) for r in rows}
    assert ("Scan Speed (mm/s)", "XYZ9AB123-R2") in parts
    assert {p for f, p in parts if f == "Power (W)"} == {"101", "102"}


def test_a_gguf_that_will_not_load_says_what_to_do(monkeypatch, tmp_path):
    """llama-cpp-python's own words were the whole message: 'Failed to load
    shared library ...llama.dll': Could not find module ... Try using the
    full path with constructor syntax.' (review, 2026-10-20)."""
    import council_engine as ce
    dll = r"C:\Python312\Lib\site-packages\llama_cpp\lib\llama.dll"

    def broken(path, **kw):
        raise RuntimeError(f"Failed to load shared library '{dll}': Could not find module "
                           f"'{dll}' (or one of its dependencies). Try using the full path "
                           "with constructor syntax.")
    monkeypatch.setattr(ce, "_load_gguf", broken)
    monkeypatch.setattr(ledger_llm, "_GGUF", {"key": None, "llm": None})
    monkeypatch.delenv("COUNCIL_GGUF_PATH", raising=False)
    with pytest.raises(se.LedgerError) as e:
        ledger_llm.chat_for(str(tmp_path / "granite.gguf"))("p", ledger_llm._PART_SCHEMA)
    assert str(e.value) == ("granite.gguf could not be loaded (llama-cpp-python's llama "
                            "library, or one it needs, would not load) — pick an Ollama "
                            "model instead, or reinstall llama-cpp-python")
    assert getattr(e.value, "cannot_answer", False)

    def corrupt(path, **kw):
        raise ValueError("Failed to load model from file: granite.gguf")
    monkeypatch.setattr(ce, "_load_gguf", corrupt)
    with pytest.raises(se.LedgerError, match=r"^granite\.gguf could not be loaded \(Failed to "
                                             r"load model from file: granite\.gguf\) — pick "
                                             r"an Ollama model instead"):
        ledger_llm.chat_for(str(tmp_path / "granite.gguf"))("p", ledger_llm._PART_SCHEMA)


class _PerBook:
    """Answers the part question by the workbook's file name."""
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, prompt, schema):
        if "Which part" in prompt:
            name = prompt.split("File name: ", 1)[1].splitlines()[0].strip()
            return (json.dumps({"part": self.answers.get(name, ""), "evidence": ""}),
                    "fake-granite")
        return json.dumps({"mappings": []}), "fake-granite"


def test_the_assist_summary_says_what_was_asked_answered_placed_and_dropped(tmp_path,
                                                                           ledger):
    """'part numbers for 7 workbook(s): <the 3 it found> — all amber' read as
    seven parts found, and Council's own route was 'Council's model
    (Council's model)' (review, 2026-10-20)."""
    csv, _schema = ledger
    books = []
    for name, cell in (("found.xlsx", "KTV4MW517"), ("dropped.xlsx", None),
                       ("nopart.xlsx", None)):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "Laser build record"
        if cell:
            ws["A2"] = cell
        ws["A5"], ws["B5"] = "Power (W)", 280
        wb.save(tmp_path / name)
        books.append(str(tmp_path / name))
    model = _PerBook({"found.xlsx": "KTV4MW517", "dropped.xlsx": "QQQ1QQ999"})
    s = li._extract(str(csv), books, None, "", False, None, _NoProg(), None, None, None,
                    True, model)
    assert s.assist == (
        "Council's model (fake-granite): asked which part 3 workbook(s) are about: 1 found "
        "(found.xlsx → KTV4MW517), 1 answer(s) dropped as not a part number written there "
        "(see the problems), 1 with no part in its answer — all amber in the review: "
        "check them.")
    rep = ledger_llm.AssistReport(model="Council's model", parts_asked=2,
                                  labels_asked=3, labels_mapped=1, values_placed=2)
    assert rep.describe() == (
        "Council's model: asked which part 2 workbook(s) are about: 0 found, 2 not "
        "answered; asked which field 3 unplaced label(s) are: 1 given a field, 2 value(s) "
        "placed — all amber in the review: check them.")
    rep = ledger_llm.AssistReport(model="m", parts_asked=1, parts_none=1)
    assert rep.describe().endswith("1 with no part in its answer — nothing in the review is "
                                   "its suggestion.")


# ------------------------------------------- second review of wave 3 (2026-10-20)

@pytest.mark.parametrize("name,cell", [
    ("PANEL1234.xlsx", "Inspection of PANEL1234"),
    ("BRKT4471.xlsx", "Bracket BRKT4471 - laser record"),
    ("ABCD1234.xlsx", "S/N ABCD1234"),
])
def test_an_id_a_cell_writes_is_the_part_though_it_is_also_the_file_name(tmp_path, name,
                                                                         cell):
    """The file name's rule was applied wherever the answer was found: with
    no part-name pattern (the Ledger tab's default) a real id with 4+
    letters in a row that is also the file's name was dropped, though A1
    writes it - 'does not look like a part number' (second review,
    2026-10-20)."""
    part = Path(name).stem
    src = _one_value_book(tmp_path / name, cell, None)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, None, _stub(part), rep) == (
        part, f"{name} › Build!A1")
    assert not rep.problems


def test_a_workbook_named_by_its_part_gets_it_from_the_cell(tmp_path, ledger):
    """The same through Extract, with no part-name pattern: Power (W) had
    no part (second review, 2026-10-20)."""
    csv, _schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Inspection of PANEL1234"
    ws["A5"], ws["B5"] = "Power (W)", 280
    wb.save(tmp_path / "PANEL1234.xlsx")
    s = li._extract(str(csv), [str(tmp_path / "PANEL1234.xlsx")], None, "", False, None,
                    _NoProg(), None, None, None, True, FakeModel(part="PANEL1234"))
    rows = [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(s.review)[
        "Values"].iter_rows(min_row=2, values_only=True)]
    assert {r["Part"] for r in rows if r["Field"] == "Power (W)"} == {"PANEL1234"}
    assert not any("dropped" in p for p in s.problems)


@pytest.mark.parametrize("name,answer,mask,only_in_name", [
    ("EC_log_week41.xlsx", "week41", None, True),
    ("wb08_final.xlsx", "wb08", None, True),
    ("wb08_final.xlsx", "wb08", "@@@#@@###", True),
    ("Inspection Results2026.xlsx", "Results2026", None, True),
    ("Inspection_Results_2026.xlsx", "Results2026", None, False),   # 'Results_2026'
])
def test_a_piece_of_the_file_name_is_the_part_only_when_it_is_one_code(tmp_path, name, answer,
                                                                       mask, only_in_name):
    """Only the whole stem had to be one code: 'week41' of EC_log_week41,
    'wb08' of wb08_final and 'Results2026' were taken from the file name,
    and the separator pass added 'Results_2026' (second review,
    2026-10-20)."""
    src = _one_value_book(tmp_path / name, "Pulse freq", 61.35)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, mask, _stub(answer), rep) == ("", "")
    assert rep.parts_dropped == [(name, answer)] and not rep.parts_found
    assert ("written only in the file name" in rep.problems[-1]) == only_in_name


@pytest.mark.parametrize("cell,answer", [("RST_4UV_567", "RST4UV"),
                                         ("RST_4UV_567", "4UV567"),
                                         ("ABC_1DE_234_X9", "ABC1DE234")])
def test_separators_aside_a_piece_of_an_id_joined_by_underscores_is_not_the_part(
        tmp_path, cell, answer):
    """'_' joins an id's groups as '-' does: 'RST_4UV' of 'RST_4UV_567' was
    taken (second review, 2026-10-20)."""
    src = _one_value_book(tmp_path / "insp.xlsx", "Inspection record", None, sheet="Record")
    wb = openpyxl.load_workbook(src)
    wb["Record"]["A2"] = cell
    wb.save(src)
    grids, _imgs = se.load_workbook_grids(src, [])
    assert ledger_llm.find_part(src.name, grids, None, _stub(answer),
                                ledger_llm.AssistReport()) == ("", "")


@pytest.mark.parametrize("cell,answer,mask,part", [
    ("S/N EN12AB345", "EN12AB345", None, "EN12AB345"),
    ("S/N DIN7AB123", "DIN7AB123", None, "DIN7AB123"),
    ("S/N ISO1AB234", "ISO1AB234", None, "ISO1AB234"),
    ("S/N BS-4471A", "BS-4471A", None, "BS-4471A"),
    ("BS AB12CD345", "AB12CD345", None, "AB12CD345"),          # initials before it
    ("Inspected EN AB12CD345", "AB12CD345", None, "AB12CD345"),
    ("AMS ABC1DE234", "ABC1DE234", None, "ABC1DE234"),
    ("Tag AMS-4UV-567", "AMS4UV567", "@@@#@@###", "AMS-4UV-567"),
])
def test_an_id_that_starts_like_a_standard_is_still_a_part(tmp_path, cell, answer, mask,
                                                           part):
    """Any of the prefixes followed by a digit was a standard's number, and
    so was an id after one of them as a word: real ids were dropped with no
    pattern set (second review, 2026-10-20)."""
    src = _one_value_book(tmp_path / "insp.xlsx", cell, None)
    grids, _imgs = se.load_workbook_grids(src, [])
    rep = ledger_llm.AssistReport()
    assert ledger_llm.find_part(src.name, grids, mask, _stub(answer), rep) == (
        part, "insp.xlsx › Build!A1"), rep.problems


@pytest.mark.parametrize("spec", ["AMS2750E", "ASTM E1444/E1444M", "ISO/IEC 17025",
                                  "MIL-A-8625F", "BS EN ISO 9712", "EN 4179", "ASTM E1444-22"])
def test_more_standards_numbers_are_still_not_a_part(tmp_path, spec):
    src = _one_value_book(tmp_path / "furnace.xlsx", f"Spec {spec} rev G", None)
    grids, _imgs = se.load_workbook_grids(src, [])
    for answer in (spec, f"Spec {spec} rev G"):
        rep = ledger_llm.AssistReport()
        assert ledger_llm.find_part(src.name, grids, None, _stub(answer),
                                    rep) == ("", ""), answer


def test_a_label_question_that_failed_is_not_called_answered(tmp_path, ledger):
    """The label question raised 'reply was not JSON' and the summary read
    'asked which field 2 unplaced label(s) are: 0 given a field, 0 value(s)
    placed' (second review, 2026-10-20)."""
    csv, _schema = ledger
    src = _unlabelled_book(tmp_path / "u.xlsx")

    def chat(prompt, schema):
        if "Which part" in prompt:
            return json.dumps({"part": "XYZ9AB123-R2", "evidence": ""}), "fake-granite"
        raise RuntimeError("reply was not JSON")
    s = li._extract(str(csv), [str(src)], None, "", False, None, _NoProg(), None, None, None,
                    True, chat)
    assert s.assist == (
        "Council's model (fake-granite): asked which part 1 workbook(s) are about: 1 found "
        "(u.xlsx → XYZ9AB123-R2); asked which field 2 unplaced label(s) are: not answered "
        "(see the problems) — all amber in the review: check them.")
    assert any("reply was not JSON" in p for p in s.problems)
    rep = ledger_llm.AssistReport(model="m", labels_asked=2, labels_failed=True)
    assert rep.describe() == ("Council's model (m): asked which field 2 unplaced label(s) "
                              "are: not answered (see the problems) — nothing in the review "
                              "is its suggestion.")
