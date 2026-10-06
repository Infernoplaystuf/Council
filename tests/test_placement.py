"""
The weekly placement review: the usage meter, the report, the controller's
proposal and what is allowed to happen to it.

The rules pinned here are product rules: only US-origin models; nothing is
applied without the user; the Council never installs or removes a model
itself; a role cannot be sent to another machine yet; no credential from
node_registry.json reaches the report.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import model_slots as ms  # noqa: E402
from council_core import placement as pl  # noqa: E402
from council_core import usage_log  # noqa: E402

NOW = time.mktime((2026, 10, 6, 12, 0, 0, 0, 0, -1))


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "node_registry.json").write_text(json.dumps({"nodes": [{
        "name": "pi-kitchen", "host": "192.168.1.50", "ollama_port": 11434,
        "password": "hunter2-secret", "username": "pi",
        "pi_model": "Pi 5 (16GB)", "ram_gb": 16, "status": "online",
        "installed_models": ["llama3.2:3b", "gemma3:1b"],
        "model_log": [{"ts": "x", "model": "llama3.2:3b", "role": "intern"}],
    }]}), encoding="utf-8")
    return v


def _slots():
    return ms.SlotConfig(
        slots={"main": ms.Slot("main", "ollama:gpt-oss:20b"),
               "fast": ms.Slot("fast", "ollama:llama3.2:3b")},
        roles={"peasant": "fast", "intern": "fast"})


STATUSES = [
    NS(host="http://localhost:11434", reachable=True,
       installed_models=["gpt-oss:20b", "llama3.2:3b", "llama3.1:8b"],
       active_model_names=["gpt-oss:20b"]),
    NS(host="http://192.168.1.50:11434", reachable=True,
       installed_models=["llama3.2:3b", "gemma3:1b"], active_model_names=[]),
]


def _calls(vault, n=3):
    for i in range(n):
        usage_log.record(vault, {"role": "intern", "model": "llama3.2:3b",
                                 "backend": "ollama",
                                 "host": "http://localhost:11434",
                                 "gen_tokens": 300, "seconds": 20.0,
                                 "gen_tok_s": 15.0, "wait_s": 4.0,
                                 "prompt": "NEVER STORED"},
                         now=NOW - 3600 * (i + 1))
    usage_log.record(vault, {"role": "judge", "model": "gpt-oss:20b",
                             "backend": "gguf", "gen_tokens": 100,
                             "seconds": 5.0}, now=NOW - 60)
    # Older than a week: not in the report.
    usage_log.record(vault, {"role": "writer", "model": "old", "seconds": 1},
                     now=NOW - 30 * 86400)


# ---- the meter -------------------------------------------------------------

def test_the_meter_keeps_numbers_never_text(vault):
    _calls(vault)
    calls = usage_log.read(vault, NOW - 7 * 86400, NOW)
    assert len(calls) == 4
    raw = "".join(p.read_text() for p in
                  usage_log.usage_dir(vault).glob("calls-*.jsonl"))
    assert "NEVER STORED" not in raw
    assert calls[-1]["host"] == usage_log.LOCAL_HOST      # gguf → local


def test_a_damaged_line_is_skipped(vault):
    _calls(vault, 1)
    path = next(usage_log.usage_dir(vault).glob("calls-*.jsonl"))
    with path.open("a") as fh:
        fh.write("{not json\n")
    assert len(usage_log.read(vault, NOW - 7 * 86400, NOW)) == 2


def test_summary_groups_by_role_model_and_machine(vault):
    _calls(vault)
    rows = usage_log.summarise(usage_log.read(vault, NOW - 7 * 86400, NOW))
    intern = next(r for r in rows if r["role"] == "intern")
    assert (intern["calls"], intern["gen_tokens"], intern["seconds"]) == \
        (3, 900, 60.0)
    assert intern["median_tok_s"] == 15.0 and intern["median_wait_s"] == 4.0
    assert rows[0]["role"] == "intern"                     # busiest first


def test_the_engine_reports_every_call_to_the_meter(vault):
    import council_engine
    assert usage_log.install(vault)
    try:
        council_engine._record_stats("coder", {"backend": "ollama",
                                               "model": "llama3.1:8b",
                                               "host": "http://localhost:11434",
                                               "seconds": 2.0})
    finally:
        usage_log.uninstall()
    calls = usage_log.read(vault, time.time() - 60)
    assert [c["role"] for c in calls] == ["coder"]


def test_a_failing_listener_never_fails_the_call():
    import council_engine

    def boom(_stats):
        raise RuntimeError("meter broke")
    council_engine.add_stats_listener(boom)
    try:
        council_engine._record_stats("judge", {"seconds": 1})
    finally:
        council_engine.remove_stats_listener(boom)
    assert council_engine.last_call_stats("judge")["seconds"] == 1


# ---- the report ------------------------------------------------------------

def test_report_has_usage_machines_and_roles_and_no_credentials(vault):
    _calls(vault)
    r = pl.build_report(vault, slots=_slots(), statuses=STATUSES, now=NOW)
    text = r.text()
    assert "hunter2" not in text and "password" not in text.lower()
    assert [m.name for m in r.machines] == [pl.THIS_PC, "pi-kitchen"]
    assert "Pi 5 (16GB), 16 GB RAM" in text
    assert "intern: llama3.2:3b" in text and "judge: gpt-oss:20b" in text
    assert "old" not in [u["model"] for u in r.usage]
    assert {u["host"] for u in r.usage} == {pl.THIS_PC}
    assert r.controller_role == "judge"


def test_probe_matches_machines_by_exact_host():
    """192.168.1.5 is not 192.168.1.50."""
    reg = [pl.Machine("pi-a", "192.168.1.50:11434")]
    st = [NS(host="http://192.168.1.5:11434", reachable=True,
             installed_models=["phi3"], active_model_names=[])]
    import council_core.placement as mod
    orig = mod._registry_machines
    mod._registry_machines = lambda v: [pl.Machine(m.name, m.host)
                                        for m in reg]
    try:
        r = pl.build_report(Path("."), statuses=st)
    finally:
        mod._registry_machines = orig
    names = [m.name for m in r.machines]
    assert "pi-a" in names and "192.168.1.5:11434" in names


def test_a_controller_role_overrides_the_judge():
    cfg = _slots()
    cfg.roles["controller"] = "fast"
    assert pl.controller_role(cfg) == "controller"


# ---- the controller and the rules ------------------------------------------

def _report(vault):
    return pl.build_report(vault, slots=_slots(), statuses=STATUSES, now=NOW)


def test_check_sorts_every_change(vault):
    proposal = {"rearrange": True, "summary": "intern waits on the judge",
                "role_changes": [
                    {"role": "intern", "model": "llama3.1:8b",
                     "machine": "This PC", "reason": "4 s waits"},
                    {"role": "peasant", "model": "gemma3:1b",
                     "machine": "pi-kitchen", "reason": "idle Pi"},
                    {"role": "writer", "model": "qwen2.5:7b",
                     "machine": "This PC", "reason": "x"},
                    {"role": "coder", "model": "phi4:14b",
                     "machine": "This PC", "reason": "x"},
                    {"role": "wizard", "model": "llama3.1:8b",
                     "machine": "This PC", "reason": "x"},
                    {"role": "intern", "model": "llama3.1:8b",
                     "machine": "mars", "reason": "x"},
                    # Same model, other machine: still advice.
                    {"role": "intern", "model": "llama3.2:3b",
                     "machine": "pi-kitchen", "reason": "x"},
                    # Same model, this PC: nothing to change.
                    {"role": "intern", "model": "llama3.2:3b",
                     "machine": "This PC", "reason": "x"}],
                "install": [
                    {"machine": "pi-kitchen", "model": "llama3.2:1b",
                     "reason": "small"},
                    {"machine": "pi-kitchen", "model": "deepseek-r1:8b",
                     "reason": "x"},
                    {"machine": "pi-kitchen", "model": "x; rm -rf /",
                     "reason": "x"}],
                "remove": [
                    {"machine": "This PC", "model": "gpt-oss:20b",
                     "reason": "x"},
                    {"machine": "pi-kitchen", "model": "gemma3:1b",
                     "reason": "unused"}]}
    c = pl.check(proposal, _report(vault))
    assert [(x.role, x.model) for x in c.apply_now] == [("intern",
                                                        "llama3.1:8b")]
    assert [(x.role, x.machine) for x in c.advice] == [
        ("peasant", "pi-kitchen"), ("intern", "pi-kitchen")]
    assert [x.command for x in c.commands] == ["ollama pull llama3.2:1b",
                                               "ollama rm gemma3:1b"]
    why = " | ".join(x.why_not for x in c.rejected)
    for reason in ("US-origin", "does not have phi4:14b", "no role 'wizard'",
                   "no machine 'mars'", "not a valid model name",
                   "intern already uses llama3.2:3b",
                   "a role on this PC still uses it"):
        assert reason in why
    assert "Rejected:" in c.text() and "never installs" in c.text()


def test_parse_proposal_finds_json_in_prose():
    p = pl.parse_proposal('Sure!\n{"rearrange": false, "summary": "fine", '
                          '"role_changes": [], "install": [], "remove": []}')
    assert p["summary"] == "fine" and p["role_changes"] == []
    with pytest.raises(ValueError):
        pl.parse_proposal("no json here")


def test_run_review_asks_the_judge_and_applies_nothing(vault):
    asked = {}

    def chat(messages, **kw):
        asked.update(kw, prompt=messages[-1]["content"])
        return json.dumps({"rearrange": True, "summary": "s",
                           "role_changes": [{"role": "intern",
                                             "model": "llama3.1:8b",
                                             "machine": "This PC",
                                             "reason": "r"}],
                           "install": [], "remove": []})
    _calls(vault)
    rid, report, checked, error = pl.run_review(
        vault, slots=_slots(), statuses=STATUSES, chat=chat, now=NOW)
    assert not error and asked["role"] == "judge"
    assert asked["json_schema"] is pl.PROPOSAL_SCHEMA
    assert "USAGE" in asked["prompt"]
    assert len(checked.apply_now) == 1
    assert not (vault / ms.FILE_NAME).exists()            # nothing applied
    last = pl.last_review(vault)
    assert last["id"] == rid
    assert pl.checked_from_dict(last["checked"]).apply_now[0].model == \
        "llama3.1:8b"


def test_a_failed_controller_is_logged_not_raised(vault):
    def chat(messages, **kw):
        raise ConnectionError("model not loaded")
    rid, _r, checked, error = pl.run_review(vault, slots=_slots(), chat=chat,
                                            now=NOW)
    assert checked is None and "model not loaded" in error
    assert pl.last_review(vault)["error"] == error


def test_apply_writes_the_role_and_reuses_a_slot(vault):
    ms.save(vault, _slots())
    done = pl.apply_role_changes(vault, [
        pl.Change("role", role="intern", model="llama3.1:8b",
                  machine=pl.THIS_PC),
        pl.Change("role", role="writer", model="llama3.2:3b",
                  machine=pl.THIS_PC),
        pl.Change("role", role="peasant", model="gemma3:1b",
                  machine="pi-kitchen")])              # advice: never applied
    cfg = ms.load(vault)
    assert cfg.slots[cfg.roles["intern"]].path == "ollama:llama3.1:8b"
    assert cfg.roles["writer"] == "fast"               # existing slot reused
    assert cfg.roles["peasant"] == "fast"              # unchanged
    assert len(done) == 2


def test_due_is_weekly_and_applying_does_not_reset_it(vault):
    assert pl.due(vault, now=NOW)
    report = pl.build_report(vault, now=NOW)
    rid = pl.log_review(vault, report, None, None, now=NOW)
    assert not pl.due(vault, now=NOW + 6 * 86400)
    pl.log_applied(vault, rid, ["x"], now=NOW + 6.5 * 86400)
    assert pl.last_review(vault)["id"] == rid
    assert pl.due(vault, now=NOW + 7 * 86400)


def test_the_usage_folder_is_kept_out_of_vault_searches():
    import conversation_logger
    assert usage_log.DIR_NAME in conversation_logger.PROTECTED_SUBDIRS


# ---- the Qt window and the scheduler ---------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(qapp, cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _review(vault):
    def chat(messages, **kw):
        return json.dumps({"rearrange": True, "summary": "s",
                           "role_changes": [{"role": "intern",
                                             "model": "llama3.1:8b",
                                             "machine": "This PC",
                                             "reason": "r"}],
                           "install": [{"machine": "pi-kitchen",
                                        "model": "llama3.2:1b",
                                        "reason": "small"}],
                           "remove": []})
    return pl.run_review(vault, slots=_slots(), statuses=STATUSES, chat=chat,
                         now=time.time())


def test_dialog_shows_the_review_and_applies_only_what_is_ticked(qapp, vault):
    from PySide6.QtCore import Qt
    from council_qt.widgets.placement_review import PlacementReviewDialog
    ms.save(vault, _slots())
    _review(vault)
    d = PlacementReviewDialog(vault_dir=vault)
    try:
        assert d.apply_list.count() == 1
        assert "ollama pull llama3.2:1b" in d.commands_text()
        d.apply()                                  # nothing ticked
        assert ms.load(vault).roles["intern"] == "fast"
        d.apply_list.item(0).setCheckState(Qt.CheckState.Checked)
        d.apply()
        cfg = ms.load(vault)
        assert cfg.slots[cfg.roles["intern"]].path == "ollama:llama3.1:8b"
        assert "Applied" in d.status.text()
        log = pl.reviews_path(vault).read_text()
        assert '"kind": "applied"' in log
    finally:
        d.close()
        d.deleteLater()


def test_scheduler_runs_only_when_due(qapp, vault):
    from PySide6.QtWidgets import QMainWindow
    from council_qt.widgets.placement_review import PlacementScheduler
    runs = []

    class Win(QMainWindow):
        status = ""

        def set_status(self, text):
            self.status = text

    win = Win()
    sched = PlacementScheduler(win, vault,
                               runner=lambda v: runs.append(v) or _review(v),
                               first_ms=10_000_000, every_ms=10_000_000)
    try:
        assert sched.check()
        assert _pump(qapp, lambda: not sched._busy and win.status)
        assert "proposes changes" in win.status
        assert not sched.check()                   # reviewed: not due
        assert len(runs) == 1
    finally:
        win.close()
        win.deleteLater()


# ---- routed machines: a move can be applied ---------------------------------

def _routing(vault, enabled=True):
    from council_core import node_routing as nr
    r = nr.Routing(True, {"kitchen": nr.Node(
        "kitchen", "http://192.168.1.50:11434", enabled, 1)}, {})
    nr.save(vault, r)
    return r


def test_a_move_to_a_routed_machine_can_be_applied(vault):
    from council_core import node_routing as nr
    ms.save(vault, _slots())
    proposal = {"rearrange": True, "summary": "s", "install": [],
                "remove": [], "role_changes": [
                    {"role": "intern", "model": "llama3.2:3b",
                     "machine": "pi-kitchen", "reason": "idle Pi"}]}
    report = _report(vault)
    assert pl.check(proposal, report).advice                 # no routing
    assert pl.check(proposal, report, _routing(vault, False)).advice
    checked = pl.check(proposal, report, _routing(vault))
    assert [c.node for c in checked.apply_now] == ["kitchen"]
    assert "on pi-kitchen" in checked.text()
    done = pl.apply_role_changes(vault, checked.apply_now)
    assert done and "on pi-kitchen" in done[0]
    routing = nr.load(vault)
    assert routing.roles["intern"].node == "kitchen"
    assert routing.roles["intern"].fallback == "here"
    cfg = ms.load(vault)
    assert cfg.slots[cfg.roles["intern"]].path == "ollama:llama3.2:3b"
    # Moving it back to this PC removes the binding.
    back = pl.Change("role", role="intern", model="llama3.1:8b",
                     machine=pl.THIS_PC)
    pl.apply_role_changes(vault, [back])
    assert "intern" not in nr.load(vault).roles
