"""
The council's tools (council_core/tool_kit.py, council_core/council_tools.py):
each tool against a temporary vault, the role split, the per-question cache,
the usage meter, and the Judge's quote checks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import council_tools, tool_kit  # noqa: E402
from council_core import council_turn as ct  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    import data_index
    data = data_index.input_dir(v)
    data.mkdir(parents=True)
    (data / "runs.csv").write_text(
        "pump,runtime,owner\nA,10,Bob\nB,20,Ann\nA,30,Bob\nC,40,Ann\n",
        encoding="utf-8")
    (v / "manual.md").write_text(
        "# Manual\n\nIntro text.\n\n## Startup\n\nThe pump must be primed "
        "before every start of the day.\nCheck the seals.\n\n## Shutdown\n\n"
        "Close the valve first.\n", encoding="utf-8")
    (v / "contacts.txt").write_text("Project: North\nPoint of Contact: Bob\n",
                                    encoding="utf-8")
    code = v / "proj"
    code.mkdir()
    (code / "report.py").write_text(
        "def total(values: list, scale: float = 1.0) -> float:\n"
        "    \"\"\"Sum, scaled.\"\"\"\n"
        "    return sum(values) * scale\n\n"
        "class Report:\n"
        "    def render(self, title):\n"
        "        return title\n", encoding="utf-8")
    (code / "test_report.py").write_text(
        "from report import total\n\n"
        "def test_total():\n    assert total([1, 2]) == 3\n", encoding="utf-8")
    hidden = v / ".council_memory"
    hidden.mkdir()
    (hidden / "secret.md").write_text("the pump must be primed hidden copy",
                                      encoding="utf-8")
    return v


@pytest.fixture
def tools(vault):
    return council_tools.make_tools(None, None, vault)


def call(tools, _tool, **args):
    return tools[_tool](args)


# ============================================================
# The set
# ============================================================

def test_twenty_new_tools_beside_the_original_seven(tools):
    original = {"run_python", "vault_save", "vault_list", "vault_read",
                "vault_search", "api_search", "api_signature"}
    assert original <= set(tools)
    assert len(set(tools) - original) == 20
    assert all(getattr(fn, "help", "") for fn in tools.values())


def test_every_role_tool_exists(tools):
    for role, names in tool_kit.ROLE_TOOLS.items():
        assert set(names) <= set(tools), role
        assert len(names) <= 13, f"{role}: keep the menu short"


# ============================================================
# Speed
# ============================================================

def test_cached_result_serves_a_fresh_table_and_drops_a_stale_one(tools, vault):
    import data_index
    import derived_results
    src = data_index.input_dir(vault) / "runs.csv"
    out = derived_results.derived_dir(vault) / "avg.csv"
    out.write_text("pump,avg\nA,20\n", encoding="utf-8")
    derived_results.DerivedStore(vault).record(
        label="average runtime by pump", output=out, sources=[src],
        operation="mean(runtime)")
    ok, msg, payload = call(tools, "cached_result",
                            query="average runtime by pump")
    assert ok and payload["found"] and "A,20" not in msg and "20" in msg
    src.write_text(src.read_text() + "D,50,Cy\n", encoding="utf-8")
    ok, msg, payload = call(tools, "cached_result",
                            query="average runtime by pump")
    assert payload["found"] is False


def test_recall_decision(tools, vault):
    from council_core import past_decisions
    past_decisions.record(vault, "how often should the pump be primed",
                          "Every morning.", "PASS", ["writer"])
    ok, msg, payload = call(tools, "recall_decision",
                            question="how often is the pump primed")
    assert ok and "Every morning." in msg


def test_data_digest_and_table_peek(tools):
    ok, msg, _ = call(tools, "data_digest")
    assert ok and "runs.csv" in msg
    ok, msg, payload = call(tools, "table_peek", file="runs.csv", rows=2)
    assert ok and payload["columns"] == ["pump", "runtime", "owner"]
    assert "B" in msg and "C" not in msg.split("\n", 1)[1]


# ============================================================
# Accuracy
# ============================================================

@pytest.mark.parametrize("expr,want", [
    ("2*(3+4)/7", "2"), ("sqrt(16) + 2^3", "12"),
    ("mean([10, 20, 30, 40])", "25"), ("median(1, 5, 9)", "5"),
    ("round(pi, 3)", "3.142")])
def test_calc(expr, want):
    ok, msg, _ = tool_kit.calc({"expr": expr})
    assert ok and msg.endswith("= " + want)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')", "open('x')", "(1).__class__",
    "[x for x in range(3)]", "lambda: 1", "9**9**9", "a"])
def test_calc_refuses_anything_but_arithmetic(expr):
    ok, msg, _ = tool_kit.calc({"expr": expr})
    assert not ok


def test_calc_units_and_dates():
    ok, msg, p = tool_kit.calc({"convert": [12, "in", "cm"]})
    assert ok and abs(p["value"] - 30.48) < 1e-9
    ok, msg, p = tool_kit.calc({"convert": [212, "F", "C"]})
    assert ok and abs(p["value"] - 100) < 1e-9
    ok, msg, _ = tool_kit.calc({"convert": [1, "kg", "m"]})
    assert not ok and "mass" in msg
    ok, msg, p = tool_kit.calc({"days_between": ["2026-01-01", "2026-03-01"]})
    assert ok and p["days"] == 59
    ok, msg, _ = tool_kit.calc({"today": True})
    assert ok and "Today is" in msg


def test_quote_check(tools):
    ok, msg, p = call(tools, "quote_check",
                      quote="The pump must be primed before every start")
    assert ok and msg.startswith("VERIFIED") and p["file"] == "manual.md"
    ok, msg, p = call(tools, "quote_check",
                      quote="The pump must be primed before each start")
    assert msg.startswith("NOT FOUND as written") and "manual.md" in msg
    ok, msg, p = call(tools, "quote_check",
                      quote="Totally unrelated sentence about turbines")
    assert msg.startswith("NOT FOUND: nothing")


def test_reading_tools_refuse_the_apps_own_folders(tools):
    ok, msg, _ = call(tools, "read_section", name=".council_memory/secret.md")
    assert not ok and "app" in msg
    ok, msg, _ = call(tools, "read_section", name="../../etc/passwd")
    assert not ok
    ok, msg, _ = call(tools, "quote_check", quote="primed hidden copy")
    assert "NOT FOUND" in msg


def test_field_lookup_skips_the_apps_own_folders(tools, vault):
    (vault / ".council_memory" / "c.txt").write_text(
        "Point of Contact: Bob\n", encoding="utf-8")
    ok, msg, p = call(tools, "field_lookup", field="Point of Contact",
                      value="Bob")
    assert p["files"] and not any(".council_memory" in f for f in p["files"])


def test_field_lookup(tools):
    ok, msg, p = call(tools, "field_lookup", field="Point of Contact",
                      value="Bob")
    assert ok and any("contacts.txt" in f for f in p["files"])
    ok, msg, p = call(tools, "field_lookup", field="Point of Contact",
                      file="contacts.txt")
    assert p["values"] == ["Bob"]


def test_column_stats_are_exact(tools):
    ok, msg, p = call(tools, "column_stats", file="runs.csv", column="runtime")
    st = p["columns"]["runtime"]
    assert ok and st["mean"] == 25 and st["min"] == 10 and st["count"] == 4
    ok, msg, _ = call(tools, "column_stats", file="runs.csv", column="nope")
    assert not ok and "runtime" in msg


def test_data_query_runs_in_the_sandbox(tools):
    ok, msg, p = call(tools, "data_query", code=(
        "import pandas as pd\n"
        "df = pd.read_csv('runs.csv')\n"
        "result = df.groupby('pump', as_index=False)['runtime'].sum()\n"))
    assert ok and p["rows"] == 3 and "40" in msg
    ok, msg, _ = call(tools, "data_query",
                      code="import os\nresult = os.listdir('/')\n")
    assert not ok


# ============================================================
# Reading
# ============================================================

def test_read_section_by_heading_and_by_lines(tools):
    ok, msg, p = call(tools, "read_section", name="manual.md",
                      heading="startup")
    assert ok and "primed" in msg and "Close the valve" not in msg
    ok, msg, p = call(tools, "read_section", name="manual.md", start=1, end=2)
    assert ok and p == {"start": 1, "end": 2, "total": 12}
    ok, msg, _ = call(tools, "read_section", name="manual.md", heading="nope")
    assert not ok and "## Startup" in msg


def test_condense_file_keeps_the_focus(tools, vault):
    big = "\n".join(f"filler line {i} about nothing much" for i in range(2000))
    (vault / "big.txt").write_text("HEADER\n" + big + "\nvalve torque 40 Nm\n"
                                   + big, encoding="utf-8")
    ok, msg, p = call(tools, "condense_file", name="big.txt",
                      focus="valve torque", tokens=300)
    assert ok and "valve torque 40 Nm" in msg and p["chars_out"] < 5000


def test_semantic_search_uses_keyword_search_without_an_index(tools):
    ok, msg, p = call(tools, "semantic_search", query="pump primed")
    assert ok and p.get("backend") == "keyword" and "manual.md" in msg


# ============================================================
# Code
# ============================================================

def test_code_outline(tools):
    ok, msg, _ = call(tools, "code_outline", path="proj/report.py")
    assert "total(values: list, scale: float=1.0) -> float" in msg
    assert "Report.render(self, title)" in msg
    ok, msg, p = call(tools, "code_outline", path="proj")
    assert "proj/report.py" in msg


def test_code_grep(tools):
    ok, msg, p = call(tools, "code_grep", pattern=r"def \w+", path="proj",
                      glob="*.py")
    assert ok and "proj/report.py:1:" in msg and p["hits"] == 3


def test_lint_check(tools):
    ok, msg, p = call(tools, "lint_check", code="def f(:\n  pass\n")
    assert "SyntaxError" in msg
    ok, msg, p = call(tools, "lint_check", code="import os\nprint(x)\n")
    pytest.importorskip("pyflakes")
    assert any("undefined name 'x'" in s for s in p["problems"])
    assert any("'os' imported but unused" in s for s in p["problems"])
    ok, msg, p = call(tools, "lint_check", path="proj/report.py")
    assert p["problems"] == []


#: Code that binds every name it reads, in the ways pyflakes accepts: a
#: function's `global`, a walrus in a comprehension, an except-as, a dotted
#: import, a class defined after its use, a method's __class__, the builtins.
VALID = '''
from __future__ import annotations
import os
import os.path as osp
import xml.etree.ElementTree
def f(p: "Later") -> Later:
    global counter
    counter = 1
    total = [i * scale for i in range(3) if (j := i) > 0]
    return os.sep + osp.sep + str(j) + f"{p!r} {__name__}" + str(xml) + str(total)
class Cart:
    tax = 0.2
    label = f"{tax:.0%} {__qualname__}"
    def total(self):
        return super().total() * __class__.tax
try:
    import tomllib
except ImportError as exc:
    tomllib = exc
print(counter, Later, len([]), sorted({}))
scale = 2
class Later:
    pass
'''


def test_undefined_names_are_found_without_pyflakes(monkeypatch):
    """pyflakes is not one of the app's requirements (and not in its own
    environment), and without it lint found nothing at all — the Judge's code
    check then told it "no syntax errors or undefined names" about code that
    reads an undefined name (tests/test_judge_view.py::test_code_check failed
    on this PC). The standard library's symtable finds them; pyflakes is
    hidden here so the check is the fallback's wherever pyflakes is
    installed."""
    from council_core import judge_view
    monkeypatch.setitem(sys.modules, "pyflakes", None)
    bad = ("import os\ndef f():\n    return x + os.sep\nprint(y)\n"
           "class C:\n    k = 1\n    def m(self):\n        return k\n")
    assert tool_kit.lint(bad) == [
        "line 3:12: undefined name 'x'", "line 4:7: undefined name 'y'",
        "line 8:16: undefined name 'k'"]
    assert tool_kit.lint(VALID) == []
    # A star import may bind anything: no guesses.
    assert tool_kit.lint("from os import *\nprint(sep, nothing)\n") == []
    assert tool_kit.lint("def f(:\n  pass\n")[0].startswith("line 1: Syntax")
    check = judge_view.code_check("```python\n" + bad + "```")
    assert "block 1: line 3:12: undefined name 'x'" in check
    assert judge_view.has_problems(check)
    assert not judge_view.has_problems(
        judge_view.code_check("```python\n" + VALID + "```"))


def test_run_tests(vault, tmp_path):
    """In a code root the caller names (a copy of the vault's proj/ here):
    run_tests never runs a file in the vault — that ran model-saved code
    around run_python's sandbox (tests/test_run_python_fence.py)."""
    import shutil
    code = tmp_path / "code"
    shutil.copytree(vault / "proj", code)
    tools = council_tools.make_tools(None, None, vault, roots=[code])
    ok, msg, p = call(tools, "run_tests", path="test_report.py")
    assert ok and p["passed"], msg
    (code / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    ok, msg, p = call(tools, "run_tests", path="test_bad.py")
    assert ok and not p["passed"] and "FAILED" in msg
    # The vault is not a code root: its copy is not found there.
    ok, msg, p = tools.for_role("coder")["run_tests"](
        {"path": str(vault / "proj" / "test_report.py")})
    assert not ok and "not found" in msg, msg
    no_roots = council_tools.make_tools(None, None, vault)
    ok, msg, p = call(no_roots, "run_tests", path="proj/test_report.py")
    assert not ok and "none is open here" in msg, msg


def test_diff_preview_never_writes(tools, vault):
    path = vault / "proj" / "report.py"
    before = path.read_text()
    ok, msg, p = call(tools, "diff_preview", path="proj/report.py",
                      content=before.replace("* scale", "* scale * 2"))
    assert ok and p == {"changed": True, "added": 1, "removed": 1}
    assert path.read_text() == before
    ok, msg, p = call(tools, "diff_preview", path="proj/new.py",
                      content="x = 1\n")
    assert p["added"] == 1 and not (vault / "proj" / "new.py").exists()


# ============================================================
# Coordination
# ============================================================

def test_make_chart_lists_then_writes_a_new_file(tools, vault):
    pytest.importorskip("matplotlib")
    ok, msg, p = call(tools, "make_chart", file="runs.csv",
                      columns=["pump", "runtime"])
    assert ok and p["choices"]
    kind = p["choices"][0]
    ok, msg, p1 = call(tools, "make_chart", file="runs.csv", kind=kind,
                       columns=["pump", "runtime"])
    assert ok, msg
    first = Path(p1["path"])
    assert first.exists() and "data_out" in first.parts
    tools.new_turn()                       # a second question, no cache
    ok, msg, p2 = call(tools, "make_chart", file="runs.csv", kind=kind,
                       columns=["pump", "runtime"])
    assert Path(p2["path"]) != first and first.exists()


def test_node_status_with_no_machines(tools):
    ok, msg, p = call(tools, "node_status")
    assert ok and "No other machines" in msg and p["nodes"] == []


def test_vault_save_never_overwrites(vault):
    import council_engine
    lib = council_engine.Librarian(vault, vault / "logs" / "council.log")
    tools = council_tools.make_tools(None, lib, vault)
    ok, msg, p = call(tools, "vault_save", name="manual.md", content="new")
    assert ok and Path(p["path"]).name == "manual_2.md"
    assert (vault / "manual.md").read_text().startswith("# Manual")


def _app_files(vault):
    """The app's own files at the vault root, each holding a word an older
    conversation said (found in review: they reached members' prompts)."""
    import question_history
    question_history.QuestionHistory(vault).add(
        "what is the spare impeller called - it is MARZIPAN")
    (vault / "verdict_history.jsonl").write_text(json.dumps(
        {"session_id": "older", "query": "the spare impeller is TANGERINE",
         "passed": True}) + "\n", encoding="utf-8")
    (vault / "librarian_wishlist.md").write_text(
        "- [ ] writer | the spare impeller CINNAMON\n", encoding="utf-8")
    (vault / "trends.md").write_text("impeller trend SAFFRON\n",
                                     encoding="utf-8")
    (vault / ".onboarded").write_text("impeller PAPRIKA\n", encoding="utf-8")
    (vault / "pumps.md").write_text("The spare impeller is part P-200.\n",
                                    encoding="utf-8")
    return ("MARZIPAN", "TANGERINE", "CINNAMON", "SAFFRON", "PAPRIKA")


def test_vault_list_read_and_search_never_reach_the_apps_own_files(vault):
    """vault_list / vault_read / vault_search (council-map's Tools) read
    every file at the vault root: question_history.json and
    verdict_history.jsonl hold every older conversation's questions, and a
    search for any word of the question hit them. They now refuse what
    tool_kit's own reading tools refuse (conversation_logger-protected
    files and dot-files)."""
    import council_engine
    words = _app_files(vault)
    lib = council_engine.Librarian(vault, vault / "logs" / "council.log")
    tools = council_tools.make_tools(None, lib, vault)

    ok, listed, payload = call(tools, "vault_list")
    assert ok and "pumps.md" in payload["items"]
    for name in ("question_history.json", "verdict_history.jsonl",
                 "librarian_wishlist.md", "trends.md", ".onboarded"):
        assert name not in payload["items"], payload["items"]

    ok, found, payload = call(tools, "vault_search", query="impeller")
    assert ok and [m["file"] for m in payload["matches"]] == ["pumps.md"]
    assert not [w for w in words if w in found], found

    for name in ("verdict_history.jsonl", "question_history.json",
                 "trends.md", ".onboarded", "../outside.txt"):
        ok, text, _ = call(tools, "vault_read", name=name)
        assert not ok, (name, text)
        assert not [w for w in words if w in text], (name, text)
    ok, text, _ = call(tools, "vault_read", name="pumps.md")
    assert ok and "P-200" in text


@pytest.mark.parametrize("name", [
    "node_routing.json", "council_instructions.json", "model_slots.json",
    "backend_settings.json", "question_history.json", "Node_Routing.JSON",
    "trends.md", ".onboarded", "../node_routing.json",
    # Windows drops a name's trailing dots and spaces when it creates the
    # file: saved as "node_routing.json.", the file IS node_routing.json.
    "node_routing.json.", "Node_Routing.JSON..", "question_history.json.",
    "council_instructions.json. "])
def test_vault_save_never_writes_an_app_file(vault, name):
    """A member's vault_save created node_routing.json at the vault root, and
    the next launch read it as the user's opt-in to send the Writer's
    prompts to another machine (found in review: route('writer') gave
    http://203.0.113.9:11434). An app file name is refused, present or
    not, however it is spelled."""
    import council_engine
    from council_core import node_routing
    lib = council_engine.Librarian(vault, vault / "logs" / "council.log")
    tools = council_tools.make_tools(None, lib, vault)
    routing = {"version": 1, "routing_enabled": True,
               "nodes": {"box": {"url": "http://203.0.113.9:11434",
                                 "enabled": True}},
               "roles": {"writer": {"node": "box", "fallback": "here"}}}
    before = sorted(p.name for p in vault.iterdir())
    ok, msg, _ = call(tools, "vault_save", name=name,
                      content=json.dumps(routing))
    assert not ok and "app" in msg, msg
    assert sorted(p.name for p in vault.iterdir()) == before
    assert node_routing.route("writer", node_routing.load(vault)) is None
    ok, msg, p = call(tools, "vault_save", name="pump_notes.md",
                      content="P-200 notes")
    assert ok and Path(p["path"]).name == "pump_notes.md"


def test_vault_save_says_the_name_the_file_really_has(vault):
    """A trailing dot is not part of a file's name (Windows drops it): the
    file is saved, and named, without it — and an existing file of that
    name is not overwritten."""
    import council_engine
    lib = council_engine.Librarian(vault, vault / "logs" / "council.log")
    tools = council_tools.make_tools(None, lib, vault)
    ok, msg, p = call(tools, "vault_save", name="pump notes.",
                      content="P-200 notes")
    assert ok and Path(p["path"]).name == "pump_notes", (msg, p)
    assert "'pump_notes'" in msg, msg
    assert (vault / "pump_notes").read_text(encoding="utf-8") == \
        "P-200 notes"
    ok, msg, p = call(tools, "vault_save", name="manual.md.", content="new")
    assert ok and Path(p["path"]).name == "manual_2.md", (msg, p)
    assert (vault / "manual.md").read_text().startswith("# Manual")


# ============================================================
# The tool set: roles, cache, notes, meter
# ============================================================

def test_each_role_gets_its_own_list(tools):
    assert set(tools.for_role("skeptic")) == set(tool_kit.ROLE_TOOLS["skeptic"])
    assert "run_tests" in tools.for_role("coder")
    assert "run_tests" not in tools.for_role("writer")
    assert tools.for_role("judge") == {}


def test_build_agents_gives_every_member_its_tools(tools):
    class M:
        def respond(self, prompt, **kw):
            return "x"
    models = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    for r in ("writer", "coder", "skeptic", "artist"):
        setattr(models, r, M())
    agents = ct.build_agents(models, enable_tools=True, tools=tools)
    assert all(a.enable_tools for a in agents.values())
    assert set(agents["artist"].tools) == {"make_chart", "table_peek",
                                           "shared_notes"}
    off = ct.build_agents(models, enable_tools=False, tools=tools)
    assert not any(a.enable_tools for a in off.values())
    # A plain dict keeps the old rule: coder and intern only.
    plain = ct.build_agents(models, enable_tools=True,
                            tools={"run_python": lambda a: (True, "", {})})
    assert [r for r, a in plain.items() if a.enable_tools] == ["coder"]


def test_repeat_calls_are_cached_within_a_question(tools, monkeypatch):
    n = {"calls": 0}
    real = tools["column_stats"]

    def counting(args):
        n["calls"] += 1
        return real(args)
    tools["column_stats"] = counting
    fn = tools.for_role("skeptic")["column_stats"]
    a = fn({"file": "runs.csv", "column": "runtime"})
    b = fn({"file": "runs.csv", "column": "runtime"})
    assert n["calls"] == 1 and "(cached" in b[1] and a[2] == b[2]
    tools.new_turn()
    fn({"file": "runs.csv", "column": "runtime"})
    assert n["calls"] == 2


def test_shared_notes_are_per_question_and_signed(tools):
    tools.for_role("intern")["shared_notes"]({"post": "runs.csv has 4 rows"})
    ok, msg, _ = tools.for_role("writer")["shared_notes"]({"read": True})
    assert "intern: runs.csv has 4 rows" in msg
    tools.new_turn()
    ok, msg, _ = tools.for_role("writer")["shared_notes"]({"read": True})
    assert msg == "(no notes yet)"


def test_a_raising_tool_is_a_failed_call_and_is_metered(tools, vault):
    from council_core import usage_log
    def boom(args):
        raise RuntimeError("disk on fire")
    tools["calc"] = boom
    ok, msg, _ = tools.for_role("peasant")["calc"]({"expr": "1"})
    assert not ok and "disk on fire" in msg
    tools.for_role("peasant")["field_lookup"]({"field": ""})
    calls = usage_log.read_tools(vault, 0)
    assert [(c["role"], c["tool"], c["ok"]) for c in calls] == [
        ("peasant", "calc", False), ("peasant", "field_lookup", False)]
    summary = usage_log.summarise_tools(calls)
    assert {s["tool"] for s in summary} == {"calc", "field_lookup"}
    assert not list(vault.glob("*.jsonl"))


def test_the_prompt_lists_each_tools_arguments(tools):
    from council_core.deliberation import AgentContext, ModelAgent

    class M:
        asked = []
        def respond(self, prompt, **kw):
            self.asked.append(prompt)
            return "an answer"
    m = M()
    agent = ModelAgent("Skeptic", m, enable_tools=True,
                       tools=tools.for_role("skeptic"))
    agent.act(AgentContext("is it true?"))
    assert '- quote_check: {"quote": "exact words"' in m.asked[0]


def test_a_member_calls_a_tool_mid_answer(tools):
    from council_core.deliberation import AgentContext, ModelAgent

    class M:
        def __init__(self):
            self.asked = []
        def respond(self, prompt, **kw):
            self.asked.append(prompt)
            if len(self.asked) == 1:
                return '{"tool": "calc", "args": {"expr": "17*23"}}'
            return "It is 391."
    m = M()
    agent = ModelAgent("Intern", m, enable_tools=True,
                       tools=tools.for_role("intern"))
    events = agent.act(AgentContext("what is 17 times 23"))
    assert events[-1].text == "It is 391."
    assert "17*23 = 391" in m.asked[1]


# ============================================================
# The Judge's quote checks
# ============================================================

def test_judge_checks_flags_an_invented_quote(tools):
    text = tool_kit.judge_checks(tools, {
        "writer": {"answer": 'The manual says "the pump must be primed before '
                             'every start of the day".'},
        "intern": {"answer": 'Per the manual, "the pump never needs priming '
                             'in warm weather at all".'},
        "coder": {"answer": "No quotes here."}})
    assert text.startswith("QUOTE CHECKS")
    lines = text.splitlines()[1:]
    assert len(lines) == 2
    assert "writer" in lines[0] and "VERIFIED" in lines[0]
    assert "intern" in lines[1] and "NOT FOUND" in lines[1]
    assert tool_kit.judge_checks(tools, {"a": {"answer": "plain"}}) == ""


def test_the_judge_ranks_with_the_quote_checks(tools):
    seen = {}

    class Judge:
        def route(self, text):
            return "chat"

        def rank_candidates(self, user_text, candidates, extra_context=""):
            seen["evidence"] = extra_context
            return json.dumps({"confidence": 8, "winner": "writer"})

        def critique(self, user_text, response, *, extra_context="",
                     query_mode=""):
            return "Verdict: PASS"

    class Member:
        def respond(self, prompt, **kw):
            return ('The manual says "the pump must be primed before every '
                    'start of the day".')

    models = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    models.writer = Member()
    models.peasant = Member()
    res = ct.run_turn("how do I start the pump?", models, judge=Judge(),
                      max_rounds=1, debate_turns=0, enable_tools=True,
                      tools=tools, depth="deep")
    assert res.ok, res.message
    assert "QUOTE CHECKS" in seen["evidence"]
    assert "VERIFIED" in seen["evidence"]
