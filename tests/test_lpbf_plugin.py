"""
The LPBF / Dyndrite plugin (council_plugins/lpbf, id council.lpbf) and the
token support it needs from Council's MCP client:

  * a server's token is read by Council from an environment variable or the
    Windows Credential Manager and sent as a header (HTTP) or an
    environment variable (stdio); a plain token in a saved header is
    refused; a missing token says where Council looked, never the value;
  * the plugin is built in, off until ticked, and gets the "mcp" service;
  * units convert to base units and a wrong unit is refused;
  * every design stays inside its ranges; runs carry specimen IDs, LED and
    VED, and a seed gives the same order;
  * the deterministic checks block out-of-limit plans and only warn on VED;
  * the model's JSON becomes a spec (aliases, units), unknown names dropped;
  * approval is bound to the checked plan, re-checks, needs a name, and the
    audit log cannot be edited; only an approved plan exports a manifest;
  * results join on specimen ID with stats and correlations;
  * Dyndrite tools get rules (read / change / blocked); asking calls read
    tools only; a change tool runs once and only after approval;
  * the tab builds offscreen and runs a plan from check to manifest.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import app_plugins as ap  # noqa: E402
from council_core import docs_servers as ds  # noqa: E402
from council_core import plugin_services as ps  # noqa: E402
from council_core import secret_source as ss  # noqa: E402

from council_plugins.lpbf.engine import doe  # noqa: E402
from council_plugins.lpbf.engine import dyndrite as dy  # noqa: E402
from council_plugins.lpbf.engine import limits as lm  # noqa: E402
from council_plugins.lpbf.engine import plan as pl  # noqa: E402
from council_plugins.lpbf.engine import spec as sp  # noqa: E402
from council_plugins.lpbf.engine.store import Store  # noqa: E402

FAKE = ROOT / "tools" / "fake_dyndrite_mcp_server.py"
_spec = importlib.util.spec_from_file_location("fake_dyndrite", FAKE)
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)

PROFILE = lm.EXAMPLE_PROFILE


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    home = tmp_path / "app"
    monkeypatch.setenv("COUNCIL_APP_DIR", str(home))
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("DYNDRITE_TOKEN", raising=False)
    yield home
    ds.close_all()


class Ctx:
    """A stand-in plugin context: data folder, JSON, services, log."""

    def __init__(self, root: Path, services=None):
        self.root = root
        self.services = services or {}
        self.logged = []

    def data_folder(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def save_json(self, name, data):
        ap.write_json_atomic(self.root / name, data)

    def load_json(self, name, default=None):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def service(self, name):
        return self.services[name]

    def log(self, text):
        self.logged.append(text)


def grid_spec(**kw) -> sp.ExperimentSpec:
    base = dict(title="P x v", profile=PROFILE["name"], design="full_factorial",
                variables=[sp.Variable("laser_power", 150, 350, 5),
                           sp.Variable("scan_speed", 600, 1400, 5)],
                fixed=[sp.Fixed("hatch_spacing", 0.1),
                       sp.Fixed("layer_thickness", 0.03)])
    base.update(kw)
    return sp.ExperimentSpec(**base)


# ======================================================================
# Tokens: Council holds them, nothing saved or shown
# ======================================================================
def test_a_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DYNDRITE_TOKEN", "abc123")
    assert ss.read("env:DYNDRITE_TOKEN") == "abc123"


def test_a_missing_token_says_where_council_looked_not_a_value():
    with pytest.raises(ss.SecretMissing) as e:
        ss.read("env:DYNDRITE_TOKEN")
    assert "DYNDRITE_TOKEN" in str(e.value)
    with pytest.raises(ss.SecretMissing) as e:
        ss.read("credential:council/none-here")
    assert "cmdkey /generic:council/none-here" in str(e.value)


def test_the_credential_manager_is_read_through_the_reader(monkeypatch):
    monkeypatch.setattr(ss, "credential_reader",
                        lambda target: "tok-9" if target == "council/dyndrite"
                        else None)
    assert ss.read("credential:council/dyndrite") == "tok-9"


def test_a_plain_token_in_a_saved_header_is_refused():
    spec = ds.ServerSpec(name="D", transport="http",
                         url="http://127.0.0.1:9/mcp",
                         headers={"Authorization": "Bearer pasted-token"})
    assert "plain text" in ds.validate(spec)
    spec.headers = {"Authorization": "Bearer ${DYNDRITE_TOKEN}"}
    assert ds.validate(spec) == ""
    assert ss.valid_source("token:abc")


def test_a_saved_server_keeps_where_the_token_is_never_the_token(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DYNDRITE_TOKEN", "never-on-disk")
    spec = ds.ServerSpec(name="D", transport="http",
                         url="http://127.0.0.1:9/mcp",
                         token_source="env:DYNDRITE_TOKEN")
    path = ds.save([spec], tmp_path / "servers.json")
    text = path.read_text(encoding="utf-8")
    assert "env:DYNDRITE_TOKEN" in text and "never-on-disk" not in text
    assert ds.load(path)[0].token_source == "env:DYNDRITE_TOKEN"
    assert "environment variable DYNDRITE_TOKEN" in spec.describe()


@pytest.fixture
def http_fake():
    httpd = fake.make_http("secret-123")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()


def http_settings(url: str, **kw):
    s = dict(dy.DEFAULT_SETTINGS, url=url)
    s.update(kw)
    return s


def test_http_sends_the_token_as_a_bearer_header(http_fake, monkeypatch):
    _, url = http_fake
    monkeypatch.setenv("DYNDRITE_TOKEN", "secret-123")
    report = ps.Mcp().check(http_settings(url))
    assert report["ok"], report["message"]
    assert [t["name"] for t in report["tools"]] == [t["name"] for t in fake.TOOLS]


def test_http_without_the_token_is_refused_by_the_server(http_fake, monkeypatch):
    _, url = http_fake
    monkeypatch.setenv("DYNDRITE_TOKEN", "wrong")
    report = ps.Mcp().check(http_settings(url))
    assert not report["ok"] and "401" in report["message"]
    assert "wrong" not in report["message"]
    monkeypatch.delenv("DYNDRITE_TOKEN")
    report = ps.Mcp().check(http_settings(url))
    assert not report["ok"] and "DYNDRITE_TOKEN is not set" in report["message"]


def test_a_remote_server_is_refused_unless_allowed():
    s = http_settings("https://dyndrite.example.com/mcp")
    assert "not this computer" in ps.Mcp().problem(s)
    assert ps.Mcp().problem(dict(s, allow_remote=True)) == ""


def test_stdio_hands_the_token_to_the_server_process(monkeypatch):
    monkeypatch.setenv("DYNDRITE_TOKEN", "secret-123")
    s = dict(dy.DEFAULT_SETTINGS, transport="stdio", command=sys.executable,
             args=[str(FAKE), "--token", "secret-123"], url="",
             name="Dyndrite stdio")
    mcp = ps.Mcp()
    report = mcp.check(s)
    assert report["ok"], report["message"]
    got = mcp.call(s, "search_docs", {"query": "hatch spacing"})
    assert "set_hatch_spacing" in got["text"] and not got["is_error"]
    mcp.release(s)


def test_stdio_needs_the_variable_name_for_the_token():
    s = dict(dy.DEFAULT_SETTINGS, transport="stdio", command="python",
             token_env="")
    assert "environment variable" in ps.Mcp().problem(s)


# ======================================================================
# The plugin is built in, off, and asks for the mcp service
# ======================================================================
def _lpbf():
    return next(f for f in ap.discover() if f.id == "council.lpbf")


def test_the_plugin_is_built_in_off_and_can_turn_on():
    found = _lpbf()
    assert found.builtin and found.can_turn_on, found.problem
    assert set(found.manifest.needs) == {"models", "mcp"}
    assert "council.lpbf" not in ap.enabled_ids()


def test_a_context_gives_the_mcp_service():
    ctx = ap.PluginContext(manifest=_lpbf().manifest)
    assert isinstance(ctx.service("mcp"), ps.Mcp)
    with pytest.raises(PermissionError):
        ctx.service("sources")


# ======================================================================
# Units, designs, runs
# ======================================================================
def test_units_convert_to_base_units_and_wrong_ones_are_refused():
    assert lm.to_base(1.2, "m/s", "scan_speed") == pytest.approx(1200)
    assert lm.to_base(30, "µm", "layer_thickness") == pytest.approx(0.03)
    assert lm.to_base(0.3, "kW", "laser_power") == pytest.approx(300)
    with pytest.raises(lm.UnitError):
        lm.to_base(30, "W", "layer_thickness")
    assert lm.parameter_key("Hatch distance") == "hatch_spacing"


@pytest.mark.parametrize("design,k,n", [
    ("full_factorial", 2, 25), ("fractional_factorial", 5, 16),
    ("latin_hypercube", 3, 12), ("central_composite", 3, 14),
    ("box_behnken", 3, 12)])
def test_every_design_stays_inside_its_ranges(design, k, n):
    pts = doe.design_points(design, k, levels=[5] * k, runs=12)
    assert len(pts) == n
    assert pts.min() >= -1 and pts.max() <= 1


def test_a_fractional_factorial_is_balanced_and_orthogonal():
    pts = doe.fractional_factorial(6)
    assert pts.shape == (16, 6)
    assert (pts.sum(axis=0) == 0).all()
    assert (pts.T @ pts == 16 * __import__("numpy").eye(6)).all()


def test_a_latin_hypercube_uses_each_slice_once_and_repeats_with_its_seed():
    pts = doe.latin_hypercube(2, 10, seed=4)
    bins = ((pts + 1) / 2 * 10).astype(int)
    for j in range(2):
        assert sorted(bins[:, j]) == list(range(10))
    assert (doe.latin_hypercube(2, 10, seed=4) == pts).all()


def test_runs_carry_specimen_ids_led_ved_and_a_seeded_order():
    s = grid_spec(replicates=2)
    runs = pl.runs(s, "EXP-T")
    assert len(runs) == 50
    assert runs[0]["specimen_id"] == "EXP-T-S001"
    assert len({r["specimen_id"] for r in runs}) == 50
    r = next(x for x in runs if x["laser_power"] == 350 and x["scan_speed"] == 600)
    assert r["VED_J_per_mm3"] == pytest.approx(350 / (600 * 0.1 * 0.03), rel=1e-4)
    assert [x["std_order"] for x in pl.runs(s, "EXP-T")] == \
        [x["std_order"] for x in runs]


# ======================================================================
# Checks
# ======================================================================
def test_a_plan_inside_the_limits_passes_with_only_a_ved_warning():
    s = grid_spec()
    issues = pl.check(s, PROFILE, pl.runs(s, "E"))
    assert not pl.blocked(issues)
    assert [i.where for i in issues] == ["VED"]


def test_out_of_limit_values_block():
    s = grid_spec(variables=[sp.Variable("laser_power", 150, 450, 3),
                             sp.Variable("scan_speed", 600, 1400, 3)])
    issues = pl.check(s, PROFILE, pl.runs(s, "E"))
    assert pl.blocked(issues)
    assert any("450 W is outside" in i.message for i in issues)


def test_a_parameter_without_a_limit_blocks_and_too_many_specimens_block():
    s = grid_spec(fixed=[sp.Fixed("contour_power", 100)])
    assert any(i.where == "contour_power" and i.level == pl.BLOCK
               for i in pl.check(s, PROFILE))
    s = grid_spec(replicates=3)            # 75 > 60
    assert any("more than the 60" in i.message for i in pl.check(s, PROFILE))


def test_no_profile_blocks_everything():
    assert pl.blocked(pl.check(grid_spec(), None))


# ======================================================================
# The model proposes; the plugin decides
# ======================================================================
def test_the_models_json_becomes_a_spec_in_base_units():
    data = {"title": "25 specimens", "design": "full_factorial",
            "variables": [
                {"parameter": "power", "low": 350, "high": 150, "unit": "W",
                 "levels": 5},
                {"parameter": "scan speed", "low": 0.6, "high": 1.4,
                 "unit": "m/s", "levels": 5},
                {"parameter": "plasma flux", "low": 1, "high": 2, "unit": ""}],
            "fixed": [{"parameter": "hatch", "value": 100, "unit": "um"},
                      {"parameter": "layer", "value": 30, "unit": "µm"}]}
    spec, notes = sp.from_model_json(data, PROFILE)
    assert [(v.key, v.low, v.high) for v in spec.variables] == \
        [("laser_power", 150, 350), ("scan_speed", 600, 1400)]
    assert {f.key: f.value for f in spec.fixed} == \
        pytest.approx({"hatch_spacing": 0.1, "layer_thickness": 0.03})
    assert any("plasma flux" in n for n in notes)
    assert spec.run_count() == 25


def test_from_request_sends_the_limits_and_a_schema():
    seen = {}

    def chat(messages, *, max_tokens=800, schema=None):
        seen["messages"], seen["schema"] = messages, schema
        return json.dumps({"title": "t", "design": "latin_hypercube",
                           "variables": [{"parameter": "laser_power",
                                          "low": 200, "high": 300,
                                          "unit": "W"}],
                           "fixed": [], "runs": 12})
    spec, _ = sp.from_request("12 random runs of power", PROFILE, chat)
    assert spec.design == "latin_hypercube" and spec.runs == 12
    assert "100 to 400 W" in seen["messages"][0]["content"]
    assert seen["schema"] is sp.REQUEST_SCHEMA


# ======================================================================
# Store: approval bound to the checked plan, audit append-only
# ======================================================================
def check_fn(spec_d, profile, runs):
    return pl.check(sp.ExperimentSpec.from_dict(spec_d), profile, runs)


def saved(tmp_path, spec=None):
    store = Store(tmp_path / "data")
    spec = spec or grid_spec()
    eid = store.next_id()
    runs = pl.runs(spec, eid)
    h = store.save_draft(eid, spec.to_dict(), runs, PROFILE)
    return store, eid, h


def test_approval_needs_a_name_and_the_hash_that_was_checked(tmp_path):
    store, eid, h = saved(tmp_path)
    assert "name" in store.approve(eid, h, who="", check=check_fn)
    assert "changed" in store.approve(eid, "0" * 16, who="Andy", check=check_fn)
    assert store.approve(eid, h, who="Andy", note="ok",
                         current_profile=PROFILE, check=check_fn) == ""
    assert store.get(eid)["status"] == "approved"
    assert "already approved" in store.approve(eid, h, who="Andy")
    with pytest.raises(ValueError):
        store.save_draft(eid, grid_spec().to_dict(), [], PROFILE)


def test_changed_limits_or_a_blocking_check_refuse_approval(tmp_path):
    store, eid, h = saved(tmp_path)
    tighter = json.loads(json.dumps(PROFILE))
    tighter["limits"]["laser_power"]["max"] = 300
    assert "limits changed" in store.approve(eid, h, who="A",
                                             current_profile=tighter,
                                             check=check_fn)
    bad = grid_spec(variables=[sp.Variable("laser_power", 150, 450, 2),
                               sp.Variable("scan_speed", 600, 1400, 2)])
    store2, eid2, h2 = saved(tmp_path / "b", bad)
    assert "blocks" in store2.approve(eid2, h2, who="A", check=check_fn)
    assert store2.get(eid2)["status"] == "draft"


def test_the_audit_log_cannot_be_edited_or_deleted(tmp_path):
    import sqlite3
    store, eid, h = saved(tmp_path)
    store.approve(eid, h, who="Andy", check=check_fn)
    actions = [a["action"] for a in store.audit(eid)]
    assert actions == ["saved draft", "approved"]
    db = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE audit SET who='x'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM audit")
    db.close()


def test_results_join_on_specimen_id_with_stats(tmp_path):
    import pandas as pd
    runs = pl.runs(grid_spec(), "E")
    res = pd.DataFrame({"Specimen ID": [r["specimen_id"] for r in runs[:20]]
                        + ["E-S999"],
                        "porosity_pct": [0.1 + r["VED_J_per_mm3"] / 1000
                                         for r in runs[:20]] + [9]})
    j = pl.join_results(runs, res)
    assert j["matched"] == 20 and j["unmatched_ids"] == ["E-S999"]
    assert len(j["missing_specimens"]) == 5 and j["responses"] == ["porosity_pct"]
    corr = pl.correlations(j["table"], ["laser_power", "scan_speed",
                                        "hatch_spacing"], ["porosity_pct"])
    assert corr.loc["laser_power", "porosity_pct"] > 0
    assert corr.loc["scan_speed", "porosity_pct"] < 0
    assert pd.isna(corr.loc["hatch_spacing", "porosity_pct"])   # held constant
    with pytest.raises(ValueError):
        pl.join_results(runs, pd.DataFrame({"x": [1]}))


# ======================================================================
# Dyndrite rules, asking, requests
# ======================================================================
def test_tools_get_rules_from_annotations_then_names_unknown_is_change():
    rules = {t["name"]: dy.guess_rule(t) for t in fake.TOOLS}
    assert rules == {"search_docs": "read", "get_parameter_syntax": "read",
                     "create_build": "change", "run_python": "change"}
    assert dy.guess_rule({"name": "frobnicate"}) == "change"
    assert dy.rule_for({"name": "search_docs"}, {"search_docs": "blocked"}) \
        == "blocked"


def test_arguments_are_checked_against_the_input_schema():
    tool = fake.TOOLS[1]
    assert "needs 'parameter'" in dy.argument_problem(tool, {})
    assert "should be string" in dy.argument_problem(tool, {"parameter": 3})
    assert dy.argument_problem(tool, {"parameter": "hatch"}) == ""


def test_asking_calls_read_tools_only_and_answers_from_them():
    replies = iter([
        {"action": "call", "tool": "run_python", "arguments": {"code": "x"}},
        {"action": "call", "tool": "get_parameter_syntax",
         "arguments": {"parameter": "hatch"}},
        {"action": "answer", "answer": "Use recipe.set_hatch_spacing(mm=0.1)."},
    ])
    called = []

    def chat(messages, *, max_tokens=700, schema=None):
        assert "run_python" not in schema["properties"]["tool"]["enum"]
        return json.dumps(next(replies))

    def call(name, args):
        called.append(name)
        return fake.Server().tool(name, args) and {
            "text": fake.DOCS["hatch"], "is_error": False}
    ans = dy.ask("How do I set hatch?", fake.TOOLS, {}, chat, call)
    assert called == ["get_parameter_syntax"]
    assert "not one of the read tools" in ans.steps[0].error
    assert "set_hatch_spacing" in ans.answer


def test_asking_with_no_read_tools_says_how_to_fix_it():
    ans = dy.ask("q", fake.TOOLS, {"search_docs": "blocked",
                                   "get_parameter_syntax": "change"},
                 lambda *a, **k: "{}", lambda n, a: {})
    assert ans.answer == "" and "read" in ans.note


def test_a_change_request_runs_once_and_only_after_approval(tmp_path):
    store = Store(tmp_path)
    n = store.request_tool("create_build", {"name": "B1"}, reason="DOE 1",
                           who="Andy")
    assert store.claim_approved(n) is None
    assert "name" in store.decide_tool(n, True, who="")
    assert store.decide_tool(n, True, who="Lead") == ""
    first = store.claim_approved(n)
    assert first["tool"] == "create_build"
    assert store.claim_approved(n) is None              # a second click
    store.finish_tool(n, {"text": "done", "is_error": False})
    assert store.tool_requests()[0]["status"] == "done"
    assert [a["action"] for a in store.audit()] == \
        ["tool requested", "tool approved", "tool done"]


# ======================================================================
# The tab
# ======================================================================
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def pump(qapp, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


class FakeModels:
    def available(self):
        return [("granite4.2:8b", "granite4.2:8b")]

    def chat(self, model):
        def chat(messages, *, max_tokens=800, schema=None):
            return json.dumps({"title": "P x v", "design": "full_factorial",
                               "variables": [
                                   {"parameter": "laser_power", "low": 150,
                                    "high": 350, "unit": "W", "levels": 5},
                                   {"parameter": "scan_speed", "low": 600,
                                    "high": 1400, "unit": "mm/s",
                                    "levels": 5}],
                               "fixed": [
                                   {"parameter": "hatch_spacing", "value": 100,
                                    "unit": "um"},
                                   {"parameter": "layer_thickness",
                                    "value": 30, "unit": "um"}]})
        return chat


def test_the_tab_builds_through_plugin_tabs(qapp, monkeypatch):
    from council_qt import plugin_tabs
    monkeypatch.setattr(ps.Models, "available", lambda self: [])
    page = plugin_tabs.build_page(_lpbf())
    assert type(page).__name__ == "LpbfTab"
    assert page.pages.count() == 5


def test_the_tab_drafts_checks_approves_and_exports(qapp, tmp_path):
    from council_plugins.lpbf import tab as tab_mod
    ctx = Ctx(tmp_path / "data", {"models": FakeModels(), "mcp": ps.Mcp()})
    tab = tab_mod.build_tab(ctx)
    assert pump(qapp, lambda: tab.model.count() == 2)
    tab.model.setCurrentIndex(1)
    tab.request.setPlainText("25 specimens varying power and speed")
    tab.draft_with_model()
    assert pump(qapp, lambda: '"laser_power"' in tab.spec_edit.toPlainText())
    assert not tab.approve_btn.isEnabled()
    tab.check_plan()
    assert tab.runs_table.rowCount() == 25
    tab.save_draft()
    assert tab.approve_btn.isEnabled() and not tab.export_btn.isEnabled()
    tab.engineer.setText("Andy")
    tab.decide(True)
    assert tab.store.get(tab.current_id)["status"] == "approved"
    assert tab.export_btn.isEnabled()
    text = tab.manifest_text(tab.current_id)
    assert "# approved by: Andy" in text
    assert text.count(f"{tab.current_id}-S0") == 25


def test_the_tab_refuses_to_approve_a_blocked_plan(qapp, tmp_path):
    from council_plugins.lpbf import tab as tab_mod
    ctx = Ctx(tmp_path / "data", {"models": FakeModels(), "mcp": ps.Mcp()})
    tab = tab_mod.build_tab(ctx)
    bad = grid_spec(variables=[sp.Variable("laser_power", 150, 900, 3),
                               sp.Variable("scan_speed", 600, 1400, 3)])
    tab.spec_edit.setPlainText(bad.to_json())
    tab.check_plan()
    tab.save_draft()
    assert not tab.approve_btn.isEnabled()
    assert "Blocked" in tab.report.toPlainText()


def test_the_dyndrite_page_lists_tools_and_saves_no_token(qapp, tmp_path,
                                                          http_fake,
                                                          monkeypatch):
    from council_plugins.lpbf import tab as tab_mod
    _, url = http_fake
    monkeypatch.setenv("DYNDRITE_TOKEN", "secret-123")
    ctx = Ctx(tmp_path / "data", {"models": FakeModels(), "mcp": ps.Mcp()})
    tab = tab_mod.build_tab(ctx)
    tab.dy_url.setText(url)
    tab.check_dyndrite()
    assert pump(qapp, lambda: tab.tool_table.rowCount() == 4), tab.dy_msg.text()
    assert [tab.req_tool.itemText(i) for i in range(tab.req_tool.count())] == \
        ["create_build", "run_python"]
    saved_text = (tmp_path / "data" / dy.SETTINGS_FILE).read_text()
    assert "secret-123" not in saved_text and "env:DYNDRITE_TOKEN" in saved_text
