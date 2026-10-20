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
                    _NoProg(), "@@@#@@###", None, None, True, model)
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


def test_a_model_that_cannot_answer_is_asked_once(tmp_path, ledger):
    """It was asked again for every workbook (a GGUF that would not load was
    loaded again each time): the same multi-line error N+1 times (review,
    2026-10-19)."""
    csv, _schema = ledger
    books = [_unlabelled_book(tmp_path / f"untitled_{i}.xlsx") for i in range(3)]
    calls = []

    def broken(prompt, schema):
        calls.append(prompt)
        raise se.LedgerError("granite.gguf could not be loaded: llama-cpp-python is not "
                             "installed. Install with:\n  pip install llama-cpp-python")
    s = li._extract(str(csv), [str(b) for b in books], None, "", False, None, _NoProg(),
                    None, None, None, True, broken)
    assert len(calls) == 1
    assert sum("llama-cpp-python" in p for p in s.problems) == 1
    assert s.assist == ("Council's model was unavailable, so nothing in the review is its "
                        "suggestion: granite.gguf could not be loaded: llama-cpp-python is "
                        "not installed.")


def test_cancel_is_not_a_broken_model():
    rep = ledger_llm.AssistReport()

    def stopped(prompt, schema):
        raise RuntimeError("stopped by the caller")
    ask = ledger_llm.one_shot(stopped, rep, should_stop=lambda: True)
    with pytest.raises(RuntimeError):
        ask("p", {})
    assert not rep.unavailable and not rep.problems


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
            {"name": "granite3.3:8b", "details": {"family": "granite"}}]
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
