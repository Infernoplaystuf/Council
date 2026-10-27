"""A model reads each table (council_core/kg_table_read.py) - Part C of the
proposal agreed with the user, 2026-10-27: the model reads a table's card and
proposes what its columns mean; the user ticks a proposal and every row is
read with it. Models are scripted; synthetic files in tmp_path only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import kg_table_read as ktr  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402

ANSWER = {"description": "Eddy-current inspections of coupons by technician.",
          "columns": [{"column": "tech", "holds": "PERSON"},       # its case, not the file's
                      {"column": "Coupon", "holds": "PART"},
                      {"column": "Reading", "holds": "PART"},      # measurements
                      {"column": "Result", "holds": "PERSON"},     # Pass / Fail
                      {"column": "Shift lead", "holds": "PERSON"},  # no such column
                      {"column": "Coupon", "holds": "SUPPLIER"}]}


class Model:
    def __init__(self, answer=ANSWER, fail=False):
        self.answer, self.fail, self.calls = answer, fail, 0

    def __call__(self, messages, **kw):
        self.calls += 1
        if self.fail:
            raise ConnectionError("Ollama is not running")
        assert kw.get("schema") is ktr.SCHEMA
        assert "Tech" in messages[-1]["content"]          # it is shown the card
        return json.dumps(self.answer)


def _vault(tmp_path, techs=("Dana Ruiz", "Omar Haddad", "Lee, Carol")):
    v = tmp_path / "v"
    (v / "data_in").mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "wk41"
    ws.append(["Coupon", "Tech", "Reading", "Result", "Project"])
    for i, t in enumerate(techs):
        ws.append([f"CP-{510 + i}", t, 60 + i, "Pass" if i % 2 else "Fail", "Halcyon"])
    wb.save(v / "data_in" / "ec_coupons.xlsx")
    return v


def _rules(kg):
    return {r.label: (r.type, s) for r, s in kg.field_rules()}


def test_the_model_s_column_meanings_are_checked_and_proposed(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path), image_reader=None) as kg:
        kg.seed()
        stats = kg.read_tables(Model(), model="m")
        assert stats["read"] == 1 and stats["labels"] == ["Tech", "Coupon"]
        assert stats["rejected"] == 4
        rules = _rules(kg)
        assert rules["Tech"] == ("PERSON", "proposed")
        assert rules["Coupon"] == ("PART", "proposed")
        assert "Reading" not in rules and "Result" not in rules
        # the sentence is the table's description; Fields… can say where
        tid = kg.db.execute("SELECT id FROM entities WHERE type='TABLE'").fetchone()[0]
        assert kg.entity(tid)["description"].startswith("Eddy-current inspections")
        assert kg.model_rule_tables() == {"Tech": ["ec_coupons.xlsx › wk41"],
                                          "Coupon": ["ec_coupons.xlsx › wk41"]}


def _links(kg):
    return {(r[0], r[1], r[2]) for r in kg.db.execute(
        "SELECT s.name, r.predicate, o.name FROM relations r JOIN entities s ON"
        " s.id=r.subject_id JOIN entities o ON o.id=r.object_id WHERE r.status='seeded'"
        " AND s.type != 'TABLE' AND o.type != 'DOCUMENT'")}


def test_nothing_is_read_with_a_proposal_until_it_is_ticked(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path), image_reader=None) as kg:
        kg.seed()
        kg.read_tables(Model(), model="m")
        kg.seed()
        assert not {x for x in _links(kg) if x[1] == "WORKS_ON"}
        parts = {r[0] for r in kg.db.execute("SELECT name FROM entities WHERE type='PART'")}
        assert "CP-511" not in parts
        kg.confirm_rules(["Tech", "Coupon"])
        kg.seed()
        # every row read with them
        assert {("Dana Ruiz", "WORKS_ON", "Halcyon"), ("Omar Haddad", "WORKS_ON", "Halcyon"),
                ("Carol Lee", "WORKS_ON", "Halcyon")} <= _links(kg)
        parts = {r[0] for r in kg.db.execute("SELECT name FROM entities WHERE type='PART'")}
        assert {"CP-510", "CP-511", "CP-512"} <= parts


def test_a_table_is_read_once_per_version(tmp_path):
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v, image_reader=None) as kg:
        kg.seed()
        kg.read_tables(Model(), model="m")
        again = Model()
        assert kg.read_tables(again, model="m")["read"] == 0 and again.calls == 0
        wb = openpyxl.load_workbook(v / "data_in" / "ec_coupons.xlsx")
        wb.active.append(["CP-999", "Wes Park", 70, "Pass"])
        wb.save(v / "data_in" / "ec_coupons.xlsx")
        kg.seed()                               # the file changed: read again
        later = Model()
        assert kg.read_tables(later, model="m")["read"] == 1 and later.calls == 1


def test_the_user_s_no_stands(tmp_path):
    v = _vault(tmp_path)
    with kgm.KnowledgeGraph(v, image_reader=None) as kg:
        kg.seed()
        kg.read_tables(Model(), model="m")
        kg.set_rule_status("Tech", "rejected")
        stats = kg.read_tables(Model(), model="other model")
        assert stats["read"] == 1 and "Tech" not in stats["labels"]
        assert _rules(kg)["Tech"] == ("PERSON", "rejected")


def test_a_rule_the_graph_already_has_is_left_as_it_is(tmp_path):
    answer = {"description": "x", "columns": [{"column": "Coupon", "holds": "PROJECT"}]}
    with kgm.KnowledgeGraph(_vault(tmp_path), image_reader=None) as kg:
        kg.seed()
        kg.add_label_synonym("Coupon", "Sample ID")         # the user said: a part
        stats = kg.read_tables(Model(answer), model="m")
        assert stats["labels"] == [] and _rules(kg)["Coupon"] == ("PART", "confirmed")


def test_no_model_running_is_counted_and_read_again_later(tmp_path):
    with kgm.KnowledgeGraph(_vault(tmp_path), image_reader=None) as kg:
        kg.seed()
        stats = kg.read_tables(Model(fail=True), model="m")
        assert stats["errors"] == 1 and "Ollama is not running" in stats["first_error"]
        assert kg.read_tables(Model(), model="m")["labels"] == ["Tech", "Coupon"]


def test_values_must_fit_the_type():
    assert ktr.fits("PERSON", ["Dana Ruiz", "JB", "Lee, Carol"])
    assert not ktr.fits("PERSON", ["Pass", "Fail", "Pass"])
    assert ktr.fits("PART", ["CP-510", "CP-511"])
    assert not ktr.fits("PART", ["bracket", "fan", "CP-1"])
    assert not ktr.fits("PROJECT", [])


def test_what_a_careless_model_got_through_is_refused():
    # each of these got past the first checks when a stand-in model called
    # every column of the synthetic test set a person, part or project
    assert not ktr.fits("PART", ["320", "195", "45"], "1A")      # readings
    assert ktr.fits("PART", ["4001", "4002"], "Sample ID")
    assert not ktr.fits("PART", ["0.245 mm", "0.345 mm"], "FOD")
    assert not ktr.fits("PROJECT", ["used", "new"], "Tub")
    assert not ktr.fits("PROJECT", ["JB", "RT", "MK"], "Initials")
    assert not ktr.fits("PROJECT", ["2026-03", "2026-02"], "Completed")
    assert not ktr.fits("PERSON", ["W-500", "W-501"], "Weld ID")
    labels = ["Start Date", "Notes", "Unit Price", "Inspector", "MK — 2026-09-19"]
    got = ktr.check({"columns": [{"column": x, "holds": "PROJECT"} for x in labels]},
                    labels, lambda _l: ["Halcyon", "Cobalt"])
    assert not got.meanings and len(got.rejected) == 5


def test_an_answer_that_is_not_json_is_an_error_not_a_crash():
    res = ktr.read("Table t. Columns: Tech, Coupon.", ["Tech"], lambda _l: [],
                   lambda *a, **k: "I think Tech is a person.")
    assert res.error.startswith("ValueError") and not res.meanings


# ── the Connections tab (offscreen) ──────────────────────────────────────
def test_build_graph_has_the_model_read_the_tables(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")
    from PySide6.QtWidgets import QApplication
    from council_core import kg_extract as kx
    from tests.test_connections_morphik import wait
    from council_qt.tabs.connections import ConnectionsActions, ConnectionsTab
    qapp = QApplication.instance() or QApplication([])

    def chat(messages, **kw):
        if kw.get("schema") is ktr.SCHEMA:
            return json.dumps(ANSWER)
        return json.dumps({"entities": [], "links": []})
    monkeypatch.setattr(kx, "ollama_chat", lambda model, host=None: chat)

    class Actions(ConnectionsActions):
        def local_models(self):
            return ["gemma3:12b"]

        def gguf_models(self):
            return []

        def pi_nodes(self):
            return [], []

    v = _vault(tmp_path)
    (v / "data_in" / "notes.md").write_text("Coupon CP-510 was re-tested on Monday.\n",
                                            encoding="utf-8")
    view = ConnectionsTab(actions=Actions(v), ask_yes_no=lambda *a, **k: True)
    try:
        wait(qapp, view)
        view.on_build()
        for _ in range(3):
            wait(qapp, view)
        assert "It read 1 table(s) and proposes reading 2 column(s)" in view.status.text(), \
            view.status.text()
        assert {"Tech", "Coupon"} <= set(view._to_tick) and not view.tick_btn.isHidden()
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_fields_says_which_table_the_model_read_it_in():
    pytest.importorskip("PySide6", reason="the dialog needs PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.connections_dialogs import FieldsDialog
    QApplication.instance() or QApplication([])
    dlg = FieldsDialog([(ktr.rule_for("Tech", "PERSON"), "proposed")], [],
                       proposed_by={"Tech": ["ec_coupons.xlsx › wk41"]})
    cb = dlg.rule_boxes["Tech"]
    assert "the model read it so in ec_coupons.xlsx › wk41" in cb.text()
    assert not cb.isChecked()
    dlg.deleteLater()


def test_what_a_real_model_got_through_is_refused():
    # gemma3:12b and gemma4:26b on the synthetic test set, 2026-10-27
    assert not ktr.fits("PROJECT", ["3x", "2x", "3x"], "Beam Expander")
    assert not ktr.fits("PROJECT", ["Y", "N", "Y"], "FPI")
    assert not ktr.fits("PROJECT", ["1A", "2A", "3B"], "Flaw #")
    assert ktr.fits("PROJECT", ["Halcyon", "PRJ-0915", "KA-101"], "Project")
    labels = ["Vendor", "Course", "door", "Line item", "Flaw #", "Customer Contact"]
    got = ktr.check({"columns": [{"column": x, "holds": "PROJECT"} for x in labels[:5]]
                     + [{"column": "Customer Contact", "holds": "PERSON"}]},
                    labels, lambda _l: ["Acme Supply", "Dana Ruiz"])
    assert [m.column for m in got.meanings] == ["Customer Contact"]
    assert len(got.rejected) == 5
