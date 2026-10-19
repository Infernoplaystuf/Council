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
