"""
council_core.llm_bench — the local-model benchmark, tested WITHOUT a model.

Every path a real run takes is driven here by ScriptedBackend, a fake model
that returns canned replies. A real model only ever runs from the CLI
(`python -m council_core.llm_bench`), never from pytest.

What has to be true for the benchmark's numbers to mean anything:

  * the cases are well-formed — every kind is a palette kind, every port name
    is one the Designer could really emit, every handler is on_<widget name>;
  * every hidden code test is SATISFIABLE (a reference handler passes it) and
    DISCRIMINATING (a plausible wrong handler fails it);
  * each gate reports its own category — a syntax error is "syntax", not
    "test_failure" — because the categories are the baseline's diagnosis;
  * the stand-in ports behave like the generated app's (an entry with type
    float reads "abc" as None; a label stores text; write-only kinds refuse
    get()).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import bench_ports as bp  # noqa: E402
from council_core import llm_bench as lb  # noqa: E402

GUI = lb.load_gui_cases()
CODE = {c["id"]: c for c in lb.load_code_cases()}


# ============================================================
# The case files
# ============================================================

def test_the_gui_suite_is_twelve_cases_four_per_tier():
    assert len(GUI) == 12
    for tier in lb.TIERS:
        assert sum(c["tier"] == tier for c in GUI) == 4, tier
    assert len({c["id"] for c in GUI}) == 12


@pytest.mark.parametrize("case", GUI, ids=lambda c: c["id"])
def test_every_gui_expectation_names_real_kinds_and_props(case):
    """A typo here fails every model for a reason it never saw."""
    from gui_shapes import PALETTE
    exp = case["expect"]
    kinds = list(exp.get("kinds_all", []))
    for group in exp.get("kinds_any", []):
        kinds += group
    kinds += list(exp.get("count_at_least", {})) + list(
        exp.get("count_at_most", {}))
    for k in kinds:
        assert k in PALETTE and k != "generic", k
    for rule in exp.get("props_any", []):
        assert rule["prop"] in PALETTE[rule["kind"]]["prop_schema"], rule
    known = {"kinds_all", "kinds_any", "count_at_least", "count_at_most",
             "labels_all", "labels_any", "props_any", "notebook_tabs",
             "min_shapes", "max_shapes", "ports_min"}
    assert set(exp) <= known, set(exp) - known


def test_the_code_suite_is_the_eight_asked_for():
    assert sorted(CODE) == [f"K{i}" for i in range(1, 9)]
    assert {c["tier"] for c in CODE.values()} <= set(lb.TIERS)


@pytest.mark.parametrize("cid", sorted(CODE))
def test_code_case_ports_are_ones_the_designer_could_emit(cid):
    """Port names pass gui_ports' own validator, kinds are ones the stand-in
    ports implement, and the handler is the name gui_spec gives a button —
    on_<btn_label> — so a handler written here drops into a real app."""
    import gui_ports
    import gui_spec
    case = CODE[cid]
    names = [p["name"] for p in case["ports"]]
    assert len(names) == len(set(names))
    for i, p in enumerate(case["ports"]):
        ok, why = gui_ports.validate_port_name(p["name"], names[:i])
        assert ok, (p["name"], why)
        assert p["kind"] in lb.PORT_KINDS and p["kind"] in bp.API, p["kind"]
        if p["kind"] != "button":
            assert p["type"] in gui_ports.caps(p["kind"]).types, p
    buttons = [p for p in case["ports"] if p["kind"] == "button"]
    assert len(buttons) == 1
    b = buttons[0]
    assert b["handler"] == case["handler"] == "on_" + gui_spec.widget_name(
        "button", b["label"], [])
    assert b["name"] == gui_ports.default_port_name("button", b["label"])


def test_the_baseline_prompt_stays_a_simple_one():
    """The baseline is "the best simple prompt in 30 lines". The template
    itself must leave room for the port list inside that."""
    template_lines = lb.BASELINE_CODE_PROMPT.count("\n") + 1
    assert template_lines <= 26, template_lines
    for case in CODE.values():
        prompt = lb.baseline_code_prompt(case)
        assert case["handler"] in prompt and case["task"] in prompt
        for p in case["ports"]:
            assert f"- {p['name']}:" in prompt
        assert "assert" not in prompt          # the hidden test stays hidden
        assert prompt.count("\n") + 1 <= 32


# ============================================================
# Reference handlers: every hidden test is satisfiable...
# ============================================================

REFERENCE = {
    "K1": '''
def on_btn_add(self, *args) -> None:
    a = self.ports.first_number.get()
    b = self.ports.second_number.get()
    if a is None or b is None:
        self.ports.result.set("Invalid input")
        return
    self.ports.result.set(f"{a + b:g}")
''',
    "K2": '''
def on_btn_filter(self, *args) -> None:
    if not hasattr(self, "_all_fruits"):
        self._all_fruits = self.ports.fruits.items()
    q = (self.ports.search.get() or "").lower()
    shown = [f for f in self._all_fruits if q in f.lower()]
    self.ports.fruits.set(shown)
    self.ports.count.set(f"{len(shown)} items")
''',
    "K3": '''
def on_btn_load(self, *args) -> None:
    import csv
    path = self.ports.csv_file.get()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    except Exception as exc:
        self.ports.table.set([])
        self.report_error("Load CSV", exc)
        return
    data = rows[1:]
    self.ports.table.set(data)
    self.ports.status.set(f"Loaded {len(data)} rows")
''',
    "K4": '''
def on_btn_analyze(self, *args) -> None:
    from image_stats import image_pixel_stats
    result = image_pixel_stats(self.ports.image_path.get())
    if result.get("error"):
        self.clear_ports("brightness", "size")
        self.report_error("Analyze", result["error"])
        return
    self.ports.brightness.set(f"{result['brightness']:.1f}")
    self.ports.size.set(f"{result['width']} x {result['height']}")
''',
    "K5": '''
def on_btn_check(self, *args) -> None:
    text = (self.ports.email.get() or "").strip()
    ok = text.count("@") == 1
    if ok:
        local, domain = text.split("@")
        ok = (bool(local) and "." in domain and not domain.startswith(".")
              and not domain.endswith("."))
    self.ports.message.set("Valid email" if ok else "Invalid email")
''',
    "K6": '''
def on_btn_start_stop(self, *args) -> None:
    import time
    if getattr(self, "_started", None) is None:
        self._started = time.monotonic()
        self.ports.state.set("Running")
    else:
        took = time.monotonic() - self._started
        self._started = None
        self.ports.state.set("Stopped")
        self.ports.elapsed.set(f"{took:.1f} s")
''',
    "K7": '''
def on_btn_save(self, *args) -> None:
    import json
    age = self.ports.age.get()
    if age is None:
        self.ports.status.set("Age must be a whole number")
        return
    data = {"name": self.ports.name.get(), "age": int(age),
            "subscribed": bool(self.ports.subscribed.get())}
    with open(self.ports.output_file.get(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    self.ports.status.set("Saved")
''',
    "K8": '''
def on_btn_convert(self, *args) -> None:
    mm = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
    v = self.ports.amount.get()
    if v is None:
        self.ports.result.set("Enter a number")
        return
    a, b = self.ports.from_unit.get(), self.ports.to_unit.get()
    self.ports.result.set(f"{v * mm[a] / mm[b]:.3f} {b}")
''',
}


def fenced(code: str) -> str:
    return "Here is the method:\n```python\n" + code.strip("\n") + "\n```\n"


@pytest.mark.parametrize("cid", sorted(CODE))
def test_a_reference_handler_passes_every_gate(cid):
    chk = lb.check_code(fenced(REFERENCE[cid]), CODE[cid])
    assert chk.ok, (chk.category, chk.detail, chk.faults)
    assert chk.category == "ok" and chk.stage == "test"


# ...and discriminating: a plausible WRONG handler fails it.
WRONG = {
    # sums strings / never says Invalid input
    "K1": REFERENCE["K1"].replace('self.ports.result.set("Invalid input")',
                                  'self.ports.result.set("0")'),
    # filters the CURRENT list, so 'AN' after 'ap' finds nothing
    "K2": '''
def on_btn_filter(self, *args) -> None:
    q = (self.ports.search.get() or "").lower()
    shown = [f for f in self.ports.fruits.items() if q in f.lower()]
    self.ports.fruits.set(shown)
    self.ports.count.set(f"{len(shown)} items")
''',
    # naive split breaks the quoted comma
    "K3": '''
def on_btn_load(self, *args) -> None:
    path = self.ports.csv_file.get()
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception as exc:
        self.ports.table.set([])
        self.report_error("Load CSV", exc)
        return
    data = [ln.split(",") for ln in lines[1:]]
    self.ports.table.set(data)
    self.ports.status.set(f"Loaded {len(data)} rows")
''',
    # never reports the failure
    "K4": REFERENCE["K4"].replace(
        'self.report_error("Analyze", result["error"])', "pass"),
    # accepts a domain without a dot
    "K5": REFERENCE["K5"].replace('and "." in domain', ""),
    # elapsed accumulates across timings
    "K6": '''
def on_btn_start_stop(self, *args) -> None:
    import time
    if not hasattr(self, "_t0"):
        self._t0 = time.monotonic()
    if getattr(self, "_running", False):
        self._running = False
        self.ports.state.set("Stopped")
        self.ports.elapsed.set(f"{time.monotonic() - self._t0:.1f} s")
    else:
        self._running = True
        self.ports.state.set("Running")
''',
    # writes the age as text
    "K7": REFERENCE["K7"].replace('"age": int(age)', '"age": str(age)'),
    # inches treated as 2.54 mm
    "K8": REFERENCE["K8"].replace('"in": 25.4', '"in": 2.54'),
}


@pytest.mark.parametrize("cid", sorted(CODE))
def test_a_plausible_wrong_handler_fails_the_hidden_test(cid):
    chk = lb.check_code(fenced(WRONG[cid]), CODE[cid])
    assert not chk.ok
    assert chk.category == "test_failure", (chk.category, chk.detail)
    assert chk.detail.startswith(("assert:", "raised:")), chk.detail


# ============================================================
# Each gate reports its own category
# ============================================================

K1 = CODE["K1"]


def test_no_code_at_all_is_no_code():
    chk = lb.check_code("I cannot help with that.", K1)
    assert (chk.ok, chk.category, chk.stage) == (False, "no_code", "extract")


def test_a_syntax_error_is_syntax():
    bad = REFERENCE["K1"].replace("return\n", "return(\n")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "syntax" and chk.stage == "parse"


def test_a_reply_cut_off_mid_fence_is_still_parsed_and_reported():
    reply = "```python\ndef on_btn_add(self, *args) -> None:\n    a = (1 +"
    assert lb.extract_code(reply, "on_btn_add").startswith("def on_btn_add")
    assert lb.check_code(reply, K1).category == "syntax"


def test_the_wrong_method_name_is_missing_handler():
    chk = lb.check_code(fenced(REFERENCE["K1"].replace("on_btn_add",
                                                       "on_add")), K1)
    assert chk.category == "missing_handler"


def test_a_denied_import_is_forbidden_import():
    bad = REFERENCE["K1"].replace("    a = self", "    import subprocess\n"
                                  "    a = self")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "forbidden_import" and chk.stage == "policy"
    assert any("subprocess" in f for f in chk.faults)


def test_a_denied_call_is_policy():
    bad = REFERENCE["K1"].replace("    a = self", "    import os\n"
                                  "    os.system('echo hi')\n    a = self")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "policy", (chk.category, chk.faults)


def test_a_council_module_in_a_standalone_app_is_forbidden_import():
    """image_stats is allowed only where the case links it (K4)."""
    bad = REFERENCE["K1"].replace("    a = self", "    import image_stats\n"
                                  "    a = self")
    assert lb.check_code(fenced(bad), K1).category == "forbidden_import"
    assert lb.check_code(fenced(REFERENCE["K4"]), CODE["K4"]).ok


def test_an_invented_port_is_wrong_port():
    bad = REFERENCE["K1"].replace("self.ports.result", "self.ports.sum_label")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "wrong_port" and chk.stage == "ports"
    assert "sum_label" in chk.detail


def test_a_method_the_port_kind_lacks_is_wrong_port():
    bad = REFERENCE["K1"].replace("self.ports.result.set(f",
                                  "self.ports.result.setText(f")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "wrong_port" and "setText" in chk.detail


def test_a_widget_reached_around_the_ports_is_wrong_port():
    bad = REFERENCE["K1"].replace("self.ports.result.set(f",
                                  "self.result_label.config(text=f")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "wrong_port" and "result_label" in chk.detail


def test_state_the_handler_stores_on_self_is_not_a_wrong_port():
    chk = lb.check_code(fenced(REFERENCE["K2"]), CODE["K2"])
    assert chk.ok, chk.detail


def test_a_wrong_answer_is_test_failure_with_the_tests_message():
    bad = REFERENCE["K1"].replace("a + b", "a - b")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "test_failure"
    assert "should show 6.5" in chk.detail


def test_a_handler_that_raises_is_test_failure_naming_handlers_py():
    bad = REFERENCE["K1"].replace("    a = self", "    1 / 0\n    a = self")
    chk = lb.check_code(fenced(bad), K1)
    assert chk.category == "test_failure"
    assert "ZeroDivisionError" in chk.detail and "handlers.py" in chk.detail


def test_a_handler_that_never_returns_is_timeout():
    bad = REFERENCE["K1"].replace("    a = self", "    while True:\n"
                                  "        pass\n    a = self")
    case = dict(K1)
    verdict, detail = lb.run_hidden_test(
        lb.static_check(lb.extract_code(fenced(bad), "on_btn_add"),
                        case).handlers_src, case, timeout=3)
    assert verdict == "timeout", detail


# ============================================================
# Assembling handlers.py from what a model writes
# ============================================================

def test_a_whole_class_is_accepted_and_renamed_to_handlermixin():
    code = ("import math\n\nclass MyHandlers:\n    FACTOR = 1\n\n"
            + "\n".join("    " + ln for ln in
                        REFERENCE["K1"].strip("\n").splitlines()))
    src, cat, _ = lb.assemble_handlers(code, "on_btn_add")
    assert cat == "ok" and "class HandlerMixin:" in src
    assert "    FACTOR = 1" in src and "import math" in src
    assert lb.check_code(fenced(code), K1).ok


def test_demo_code_around_the_method_is_dropped():
    code = (REFERENCE["K1"] + "\n\nif __name__ == '__main__':\n"
            "    app = App()\n    app.on_btn_add()\nprint('done')\n")
    src, cat, _ = lb.assemble_handlers(code, "on_btn_add")
    assert cat == "ok" and "__main__" not in src and "print(" not in src


def test_an_indented_method_body_is_dedented():
    code = "\n".join("    " + ln for ln in REFERENCE["K1"].splitlines())
    assert lb.check_code(fenced(code), K1).ok


def test_the_block_that_defines_the_handler_wins_over_a_longer_one():
    reply = ("```python\n" + "# usage\n" * 40 + "app.on_btn_add()\n```\n"
             + fenced(REFERENCE["K1"]))
    assert lb.extract_code(reply, "on_btn_add").lstrip().startswith(
        "def on_btn_add")


# ============================================================
# The stand-in ports behave like the generated app's
# ============================================================

def ui(*rows):
    return bp.FakeUi(list(rows))


def test_an_entry_reads_typed_and_blank_is_none_not_zero():
    app = ui({"name": "n", "kind": "entry", "type": "float"})
    for raw, want in (("2.5", 2.5), ("1e3", 1000.0), ("", None),
                      ("abc", None)):
        app.ports.n.value = raw
        assert app.ports.n.get() == want
    app = ui({"name": "k", "kind": "entry", "type": "int"})
    app.ports.k.value = "3.0"
    assert app.ports.k.get() == 3


def test_write_only_kinds_refuse_get_like_the_real_proxy_ports():
    app = ui({"name": "log", "kind": "log_pane", "type": "str"},
             {"name": "go", "kind": "button", "type": "event"})
    with pytest.raises(AttributeError):
        app.ports.log.get()
    with pytest.raises(AttributeError):
        app.ports.go.set(1)
    app.ports.log.set("one")
    app.ports.log.set("two")
    app.ports.log.clear()                      # a log is never wiped
    assert app.ports.log.lines == ["one", "two"]


def test_list_and_table_ports():
    app = ui({"name": "l", "kind": "listbox", "type": "str",
              "items": ["a", "b"]},
             {"name": "t", "kind": "treeview", "type": "rows"})
    assert app.ports.l.items() == ["a", "b"] and app.ports.l.get() == []
    app.ports.t.set([[1, "x"], ("2", None)])
    assert app.ports.t.rows() == [("1", "x"), ("2", "")]
    with pytest.raises(AttributeError):
        app.ports.l.rows()
    app.ports.t.clear()
    assert app.ports.t.rows() == []


def test_a_label_stores_text_and_clear_blanks_it():
    app = ui({"name": "r", "kind": "label", "type": "str"})
    app.ports.r.set(6.5)
    assert app.ports.r.value == "6.5" and app.ports.r.get() == "6.5"
    app.clear_ports("r", "no_such_port")       # never raises
    assert app.ports.r.value == ""


def test_the_api_table_matches_the_generated_ports_runtime():
    """Every method the stand-ins allow is one the generated app's Ports
    runtime defines, so a handler passing here does not fail in the app on
    a method that exists only in the fake."""
    import gui_emit_qt
    runtime = gui_emit_qt.PORTS_RUNTIME
    for kind, methods in bp.API.items():
        for m in methods:
            if m in ("name", "type", "direction", "widget"):
                continue
            assert f"def {m}(" in runtime, (kind, m)


def test_num_and_norm():
    assert bp.num("Sum: 6.50") == 6.5 and bp.num("1,000.5 mm") == 1000.5
    assert bp.num("none") is None
    assert bp.norm("  Valid   Email ") == "valid email"


# ============================================================
# The GUI suite, on canned wireframes
# ============================================================

def wireframe(*rows, title="App"):
    return json.dumps({"window": {"title": title}, "shapes": list(rows)})


def row(kind, label, x, y, w, h, **props):
    r = {"kind": kind, "label": label, "x": x, "y": y, "w": w, "h": h}
    if props:
        r["props"] = props
    return r


TODO_OK = wireframe(
    row("entry", "New task", 16, 16, 560, 32),
    row("button", "Add", 592, 16, 120, 32),
    row("listbox", "Tasks", 16, 64, 696, 400),
    row("button", "Remove", 16, 480, 120, 32),
    row("button", "Clear all", 152, 480, 120, 32), title="To-do")
S4 = next(c for c in GUI if c["id"] == "S4")


def test_a_valid_wireframe_that_meets_the_criteria_passes():
    be = lb.ScriptedBackend([TODO_OK])
    r = lb.run_gui_case(S4, be)
    assert r["passed"], r
    assert (r["model_calls"], r["repair_rounds"], r["category"]) == (1, 0, "ok")
    assert r["prompt_tokens"] > 0 and r["output_tokens"] > 0
    # The describe call is made exactly as the Designer makes it.
    assert "REQUEST\n" + S4["text"] in be.prompts[0]


def test_a_valid_wireframe_missing_what_was_asked_is_missing_required():
    no_remove = wireframe(
        row("entry", "New task", 16, 16, 560, 32),
        row("button", "Add", 592, 16, 120, 32),
        row("listbox", "Tasks", 16, 64, 696, 400),
        row("button", "Clear all", 152, 480, 120, 32),
        row("button", "Help", 16, 480, 120, 32))
    r = lb.run_gui_case(S4, lb.ScriptedBackend([no_remove]))
    assert not r["passed"] and r["category"] == "missing_required"
    assert "remove" in r["detail"]


def test_too_many_widgets_is_count_bounds():
    many = json.loads(TODO_OK)
    # Eight small labels in a row beside the bottom buttons: 13 shapes in
    # all, and S4 allows at most 10.
    many["shapes"] += [row("label", f"Note {i}", 296 + i * 48, 480, 40, 24)
                       for i in range(8)]
    r = lb.run_gui_case(S4, lb.ScriptedBackend([json.dumps(many)]))
    assert r["describe_ok"] and r["category"] == "count_bounds", r


def test_prose_three_times_is_invalid_json_after_three_calls():
    r = lb.run_gui_case(S4, lb.ScriptedBackend(["Sure! I'd love to."] * 3))
    assert (r["passed"], r["category"]) == (False, "invalid_json")
    assert r["model_calls"] == 3 and r["repair_rounds"] == 2


def test_a_reply_cut_off_three_times_is_truncated():
    cut = TODO_OK[:120]
    r = lb.run_gui_case(S4, lb.ScriptedBackend([cut] * 3))
    assert r["category"] == "truncated"


def test_a_hallucinated_kind_is_schema_kind():
    bad = wireframe(row("text_input", "Task", 16, 16, 300, 32),
                    row("button", "Add", 330, 16, 100, 32))
    r = lb.run_gui_case(S4, lb.ScriptedBackend([bad] * 3))
    assert r["category"] == "schema_kind" and "text_input" in r["detail"]


def test_overlapping_siblings_are_overlap():
    """Two widgets drawn on the same spot: the one overlap gui_snap's
    deterministic repairs cannot pull apart (a partial overlap it can)."""
    bad = wireframe(row("button", "Add", 16, 16, 200, 40),
                    row("button", "Remove", 16, 16, 200, 40))
    r = lb.run_gui_case(S4, lb.ScriptedBackend([bad] * 3))
    assert r["category"] == "overlap", r


def test_a_model_that_raises_is_model_error_and_is_still_counted():
    r = lb.run_gui_case(S4, lb.ScriptedBackend([RuntimeError("CUDA OOM")]))
    assert r["category"] == "model_error" and r["model_calls"] == 1


def test_a_passing_wireframe_is_generated_in_a_scratch_vault(tmp_path):
    """With a vault, the wireframe is saved and Generated through
    designer_project — the Generate button's own code and policy gate."""
    r = lb.run_gui_case(S4, lb.ScriptedBackend([TODO_OK]), vault=tmp_path)
    assert r["passed"], r
    assert r["generate"]["ok"], r["generate"]
    projects = [p for p in tmp_path.rglob("main.py")]
    assert projects, "nothing was generated"


def test_a_repair_round_is_counted():
    r = lb.run_gui_case(S4, lb.ScriptedBackend(["no json here", TODO_OK]))
    assert r["passed"] and r["model_calls"] == 2 and r["repair_rounds"] == 1


# ============================================================
# The run and the report
# ============================================================

def test_run_writes_both_suites_and_a_summary_per_pass():
    # Both GUI passes run before the code passes.
    be = lb.ScriptedBackend([TODO_OK, TODO_OK, fenced(REFERENCE["K1"]),
                             "no code"])
    rep = lb.run(("gui", "code"), be, only="S4,K1", passes=2)
    assert [len(p) for p in rep["gui"]["passes"]] == [1, 1]
    assert [s["passed"] for s in rep["code"]["summary"]] == [1, 0]
    assert rep["code"]["summary"][1]["categories"] == {"no_code": 1}
    assert len(rep["meta"]["calls"]) == 4
    table = lb.format_table(rep)
    assert "GUI pass 1" in table and "CODE pass 2" in table
    json.dumps(rep, default=str)               # the report serialises


def test_a_recorded_run_replays_to_the_same_verdicts_without_a_model():
    """Every reply is kept per case, so a run can be re-graded offline."""
    be = lb.ScriptedBackend(["not json", TODO_OK,
                             fenced(REFERENCE["K1"]), fenced(WRONG["K8"])])
    rep = lb.run(("gui", "code"), be, only="S4,K1,K8")
    assert rep["gui"]["passes"][0][0]["replies"] == ["not json", TODO_OK]
    again = lb.replay(json.loads(json.dumps(rep, default=str)))
    for suite in ("gui", "code"):
        old = [(r["id"], r["passed"], r["category"])
               for r in rep[suite]["passes"][0]]
        new = [(r["id"], r["passed"], r["category"])
               for r in again[suite]["passes"][0]]
        assert old == new
        assert all("recorded" in r for r in again[suite]["passes"][0])
    # A pipeline that would need a reply nobody recorded says so.
    short = json.loads(json.dumps(rep, default=str))
    short["gui"]["passes"][0][0]["replies"] = ["not json"]
    r = lb.replay(short)["gui"]["passes"][0][0]
    assert r["category"] == "model_error" and "ran out" in r["detail"]


def test_the_harness_never_loads_a_model_on_import():
    """Importing the bench imports no engine and no llama_cpp — a test
    collection must never start a model."""
    import ast
    src = (ROOT / "council_core" / "llm_bench.py").read_text(encoding="utf-8")
    top = [n for n in ast.parse(src).body
           if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name.split(".")[0] for n in top for a in n.names} | {
        (n.module or "").split(".")[0] for n in top
        if isinstance(n, ast.ImportFrom)}
    assert not names & {"council_engine", "llama_cpp", "gui_describe",
                        "run_describe_prompts"}


def test_bench_ports_is_stdlib_only():
    """It is copied next to the handler and run on its own."""
    import ast
    src = (ROOT / "council_core" / "bench_ports.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom):
            assert (n.module or "").split(".")[0] in sys.stdlib_module_names \
                or n.module == "__future__", n.module
        elif isinstance(n, ast.Import):
            for a in n.names:
                # `handlers` is the module under test, copied beside it.
                assert a.name.split(".")[0] in sys.stdlib_module_names \
                    or a.name == "handlers", a.name
