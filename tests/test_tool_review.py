"""From Forge's unreviewed tool to one the council uses
(council_core/tool_review.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_built_tools as abt  # noqa: E402
from council_core import tool_review as tr  # noqa: E402

CODE = ("def scale_value(value=2, factor=3):\n"
        "    return value * factor\n")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    ok, msg, name = abt.save_tool("scale_value", "multiply a value", CODE,
                                  vault_dir=v)
    assert ok, msg
    return v


def model(tests):
    return lambda prompt: json.dumps({"tests": tests})


def test_tests_are_written_and_run(vault):
    tests = tr.write_tests("scale_value", "multiply", vault, model([
        {"args": {}, "expect": "number", "equals": 6},
        {"args": {"value": 5, "factor": 2}, "expect": "number", "equals": 10}]))
    assert len(tests) == 2 and tr.load_tests("scale_value", vault) == tests
    results = tr.run_tests("scale_value", vault)
    assert all(ok for ok, _ in results), results
    tr.save_tests("scale_value", [{"args": {}, "expect": "text"}], vault)
    ok, why = tr.run_tests("scale_value", vault)[0]
    assert not ok and "expected text" in why


def test_approval_needs_passing_tests_and_a_role(vault):
    tr.save_tests("scale_value", [{"args": {}, "expect": "number",
                                   "equals": 7}], vault)
    ok, msg = tr.approve("scale_value", ["intern"], vault)
    assert not ok and "test fails" in msg
    tr.save_tests("scale_value", [{"args": {}, "expect": "number",
                                   "equals": 6}], vault)
    assert not tr.approve("scale_value", [], vault)[0]
    ok, msg = tr.approve("scale_value", ["intern"], vault)
    assert ok and tr.status("scale_value", vault) == "approved"


def test_an_approved_tool_reaches_its_roles(vault):
    from council_core import council_tools
    tr.save_tests("scale_value", [{"args": {}, "expect": "number"}], vault)
    tr.approve("scale_value", ["intern"], vault)
    tools = council_tools.make_tools(None, None, vault)
    assert "app_scale_value" in tools.for_role("intern")
    assert "app_scale_value" not in tools.for_role("coder")
    fn = tools.for_role("intern")["app_scale_value"]
    ok, text, _ = fn({"value": 4, "factor": 5})
    assert ok and text.startswith("20")
    assert fn.params["properties"]["factor"]["type"] == "integer"


def test_changing_the_code_unapproves_it_and_keeps_the_old_version(vault):
    from council_core import council_tools
    tr.save_tests("scale_value", [{"args": {}, "expect": "number"}], vault)
    tr.approve("scale_value", ["intern"], vault)
    abt.save_tool("scale_value", "multiply", CODE.replace("*", "+"),
                  vault_dir=vault)
    assert tr.status("scale_value", vault) == "changed since approval"
    tools = council_tools.make_tools(None, None, vault)
    assert "app_scale_value" not in tools
    versions = list((abt.tools_dir(vault) / "versions").glob("scale_value-*.py"))
    assert len(versions) == 1 and "value * factor" in versions[0].read_text()


def test_a_gap_proposal_becomes_a_forge_task():
    task = tr.task_from_proposal({
        "proposed_name": "count_rejects", "description": "Count rejected parts",
        "input_params": {"file": "csv name"}, "output": "an integer",
        "rationale": "asked for 6 times"})
    assert task.startswith("Count rejected parts")
    assert "file (csv name)" in task and "asked for 6 times" in task


def test_the_forge_tab_approves_after_tests(vault, monkeypatch):
    import time
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from council_qt.tabs.forge import ForgeActions, ForgeTab
    tr.save_tests("scale_value", [{"args": {}, "expect": "number",
                                   "equals": 6}], vault)
    tab = ForgeTab(actions=ForgeActions(vault))
    tab.tools.setCurrentRow(0)
    tab.roles.setText("intern, skeptic")

    def wait():
        deadline = time.monotonic() + 10
        while tab._busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    tab.on_tests()
    wait()
    assert "1/1 pass" in tab.output.toPlainText()
    tab.on_approve()
    wait()
    assert tr.status("scale_value", vault) == "approved"
    assert "[approved]" in tab.tools.item(0).text()
    assert tr.approvals(vault)["scale_value"]["roles"] == ["intern", "skeptic"]
