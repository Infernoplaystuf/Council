"""
The Quant Duel wired into the Council's node system (council_core/quant_duel.py,
the 'quant' service role in node_routing, the Council map, Machines & roles,
and the Quant Duel tab).

Scripted runners stand in for the duel's processes, except in the end-to-end
test, which runs a real Quant Duel checkout (found next to this repository or
via QUANT_DUEL_ROOT; skipped when there is none) against a fake Ollama
registered as a Council machine.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import council_map as cm  # noqa: E402
from council_core import node_routing as nr  # noqa: E402
from council_core import quant_duel as qd  # noqa: E402


@pytest.fixture(autouse=True)
def clean_routing():
    nr.reset_cooldowns()
    nr.set_current(nr.Routing())
    yield
    nr.reset_cooldowns()
    nr.invalidate()


def _checkout(tmp_path) -> Path:
    """Enough of a duel checkout for problem() — runners never run it."""
    root = tmp_path / "quant-duel"
    (root / "quant_duel").mkdir(parents=True, exist_ok=True)
    (root / "quant_duel" / "cli.py").write_text("")
    (root / "config.yaml").write_text("")
    return root


def _settings(tmp_path, **kw) -> qd.Settings:
    return qd.Settings(enabled=True, root=str(_checkout(tmp_path)),
                       python=sys.executable, **kw)


def _routing(fallback="here", enabled=True, url="http://192.168.1.50:11434"):
    return nr.Routing(True, {"pi-a": nr.Node("pi-a", url, enabled, 1)},
                      {"quant": nr.Binding("pi-a", fallback)})


class Script:
    """A runner: answers by the command's first duel argument."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, cmd, env, timeout, cwd):
        i = cmd.index("quant_duel.cli") + 1
        args = cmd[i:]
        while args and args[0] in ("--root", "--node"):
            args = args[2:]
        node = cmd[cmd.index("--node") + 1] if "--node" in cmd else None
        self.calls.append({"node": node, "args": args, "env": env})
        ans = self.answers.get(args[0], (0, ""))
        return ans(node, args, env) if callable(ans) else ans


# ============================================================
# Settings
# ============================================================

def test_off_by_default_and_saved_atomically(tmp_path):
    assert qd.load(tmp_path) == qd.Settings()
    assert not qd.Settings().enabled
    s = _settings(tmp_path, schedule=True)
    qd.save(tmp_path, s)
    assert qd.load(tmp_path) == s
    assert not list(tmp_path.glob("*.tmp"))


def test_problem_explains_what_is_missing(tmp_path):
    assert "Choose" in qd.problem(qd.Settings())
    assert "not a Quant Duel checkout" in qd.problem(
        qd.Settings(root=str(tmp_path)))
    assert qd.problem(_settings(tmp_path)) == ""
    bad = _settings(tmp_path / "x")
    bad.python = str(tmp_path / "nope" / "python")
    assert "does not exist" in qd.problem(bad)


def test_the_checkouts_own_venv_is_used(tmp_path):
    root = _checkout(tmp_path)
    s = qd.Settings(root=str(root))
    assert qd.python_for(s) == sys.executable
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    assert qd.python_for(s) == str(py)


# ============================================================
# Where the LLM work goes
# ============================================================

def test_unbound_means_this_pc_and_the_duels_own_config():
    ep = qd.endpoint(nr.Routing())
    assert ep == qd.Endpoint("this PC")
    env = qd.job_env(ep, {"QUANT_DUEL_LLM_URL": "stale"})
    assert "QUANT_DUEL_LLM_URL" not in env
    off = _routing()
    off.routing_enabled = False
    assert qd.endpoint(off).url is None
    assert qd.endpoint(_routing(enabled=False)).url is None


def test_a_bound_machine_gets_the_llm_work():
    ep = qd.endpoint(_routing())
    assert ep.machine == "pi-a"
    assert ep.url == "http://192.168.1.50:11434/v1"
    assert ep.gate == "http://192.168.1.50:11434"
    env = qd.job_env(ep, {})
    assert env["QUANT_DUEL_LLM_URL"] == ep.url
    assert env["QUANT_DUEL_LLM_ALLOW_REMOTE"] == "1"


def test_a_resting_machine_falls_back_or_skips():
    nr.mark_failed("http://192.168.1.50:11434")
    ep = qd.endpoint(_routing("here"))
    assert ep.url is None and "resting" in ep.machine
    ep = qd.endpoint(_routing("fail"))
    assert ep.skip and "resting" in ep.skip


def test_llm_state_reads_the_last_qd_line():
    assert qd.llm_state("x\nQD-LLM: ok ok=2 failed=0\n") == "ok"
    assert qd.llm_state("QD-LLM: failed ok=0 failed=1 last_error='x'") == \
        "failed"
    assert qd.llm_state("no llm here") is None


# ============================================================
# Running jobs
# ============================================================

def test_an_llm_job_holds_the_machines_slot_and_rests_it_on_failure(tmp_path):
    routing = _routing()
    held = {}

    def tune(node, args, env):
        # while the job runs, the machine's only slot is taken
        key = nr.normalise_url("http://192.168.1.50:11434")
        held["free"] = nr._gates[key][1]._value
        held["url"] = env.get("QUANT_DUEL_LLM_URL")
        return 0, "tune 2026-10-09 (llm): LLM_FAILED\nQD-LLM: failed ok=0 " \
                  "failed=1"
    r = qd.run(_settings(tmp_path), "A", ["tune"], llm=True,
               runner=Script({"tune": tune}), routing=routing)
    assert held == {"free": 0, "url": "http://192.168.1.50:11434/v1"}
    assert r.llm == "failed" and r.machine == "pi-a"
    assert nr.cooling("http://192.168.1.50:11434") > 0     # resting now
    # the next LLM job falls back to this PC while it rests
    r = qd.run(_settings(tmp_path), "A", ["report"], llm=True,
               runner=Script({"report": (0, "QD-LLM: ok ok=1 failed=0")}),
               routing=routing)
    assert "this PC" in r.machine
    assert nr.cooling("http://192.168.1.50:11434") > 0     # not cleared here


def test_success_clears_a_rest_and_non_llm_jobs_are_never_routed(tmp_path):
    routing = _routing()
    nr.mark_failed("http://192.168.1.50:11434")
    nr.reset_cooldowns()
    script = Script({"report": (0, "QD-LLM: ok ok=1 failed=0"),
                     "daily": (0, "done")})
    qd.run(_settings(tmp_path), "B", ["report"], llm=True, runner=script,
           routing=routing)
    assert nr.cooling("http://192.168.1.50:11434") == 0
    qd.run(_settings(tmp_path), "B", ["daily"], llm=False, runner=script,
           routing=routing)
    assert "QUANT_DUEL_LLM_URL" not in script.calls[-1]["env"]
    assert script.calls[-1]["node"] == "B"


def test_a_job_is_not_started_when_the_settings_are_unusable(tmp_path):
    script = Script()
    r = qd.run(qd.Settings(enabled=True), "A", ["daily"], runner=script)
    assert r.skipped and not script.calls


def test_tick_runs_what_is_due_and_splits_the_news_poll(tmp_path):
    plans = {
        "A": [{"job": "news-poll", "args": ["news-poll"], "llm": True},
              {"job": "daily", "args": ["daily", "--start", "2026-10-05"],
               "llm": False},
              {"job": "report", "args": ["report"], "llm": True}],
        "B": [{"job": "daily", "args": ["daily"], "llm": False},
              {"job": "report", "args": ["report"], "llm": True}]}
    script = Script({
        "run-due": lambda n, a, e: (0, "noise\n" + json.dumps(plans[n])),
        "daily": lambda n, a, e: (0 if n == "A" else 1, "x")})
    lines = []
    out = qd.tick(_settings(tmp_path), script, routing=nr.Routing(),
                  log=lines.append)
    ran = [(c["node"], " ".join(c["args"])) for c in script.calls
           if c["args"][0] != "run-due"]
    assert ran == [("A", "news-poll --no-score"), ("A", "news-score"),
                   ("A", "daily --start 2026-10-05"), ("A", "report"),
                   ("B", "daily")]                 # B's daily failed: no report
    assert len(out) == 5 and any("rc=1" in line for line in lines)


def test_tick_does_nothing_when_off(tmp_path):
    script = Script()
    s = _settings(tmp_path)
    s.enabled = False
    assert qd.tick(s, script) == [] and not script.calls


def test_status_and_rows(tmp_path):
    st = {"node": "A", "news_enabled": True, "last_run": "2026-10-09",
          "books": {"live": {"equity": 102654.6, "day_pct": 0.01,
                             "total_pct": 2.65},
                    "control": {"equity": 102654.6, "day_pct": 0.01,
                                "total_pct": 2.65},
                    "spy": {"equity": 97969.4, "day_pct": 0.02,
                            "total_pct": -2.03}},
          "tuner_last": {"day": "2026-10-09", "status": "rejected"},
          "news": {"headlines": 120, "scored": 90, "pending": 30},
          "due": ["report"]}
    script = Script({"status": (0, json.dumps(st))})
    got = qd.status(_settings(tmp_path), "A", script)
    rows = qd.status_rows({"A": got, "B": RuntimeError("no ledger")})
    assert rows[0] == ("A (news)", "2026-10-09", "102,655 (+2.65%)",
                       "102,655 (+2.65%)", "97,969 (-2.03%)",
                       "2026-10-09 rejected", "120 / 90 scored", "report")
    assert rows[1][-1].startswith("error")
    with pytest.raises(qd.QuantDuelError, match="no JSON"):
        qd.status(_settings(tmp_path), "A", Script({"status": (0, "?")}))


# ============================================================
# The map and Machines & roles
# ============================================================

def test_the_map_shows_where_the_duels_llm_work_runs():
    m = cm.live_overlay(cm.static_map(), None, [], routing=_routing())
    e = [x for x in m.edges if x.src == cm.QUANT]
    assert len(e) == 1 and e[0].dst.startswith("machine:")
    assert e[0].status == "live" and "pi-a" in e[0].data
    m = cm.live_overlay(cm.static_map(), None, [], routing=nr.Routing(),
                        quant=True)
    e = [x for x in m.edges if x.src == cm.QUANT]
    assert [x.dst for x in e] == [cm.THIS_PC]
    m = cm.live_overlay(cm.static_map(), None, [], routing=nr.Routing(),
                        quant=False)
    assert cm.QUANT not in m.nodes


def test_machines_and_roles_keeps_the_quant_binding(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.widgets.machine_routing import (MachineRoutingDialog,
                                                    ROUTABLE_ROLES)
    QApplication.instance() or QApplication([])
    vault = tmp_path / "vault"
    vault.mkdir()
    nr.save(vault, _routing("fail"))
    d = MachineRoutingDialog(vault_dir=vault)
    try:
        row = list(ROUTABLE_ROLES).index("quant")
        assert d.roles.item(row, 0).text() == "quant"
        assert d.roles.cellWidget(row, 1).currentText() == "pi-a"
        assert d.save()
        assert nr.load(vault).roles["quant"] == nr.Binding("pi-a", "fail")
    finally:
        d.close()
        d.deleteLater()


# ============================================================
# The tab
# ============================================================

@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait(qapp, tab, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if not tab._busy:
            qapp.processEvents()
            return
        time.sleep(0.02)
    raise AssertionError("the tab's worker did not finish")


def test_the_tab_saves_refreshes_and_runs_due_jobs(qapp, tmp_path):
    from council_qt.tabs.quant_duel import QuantActions, QuantDuelTab
    vault = tmp_path / "vault"
    vault.mkdir()
    st = {"node": "B", "news_enabled": False, "last_run": "2026-10-09",
          "books": {}, "tuner_last": None, "news": None, "due": []}
    script = Script({
        "status": (0, json.dumps(st)),
        "run-due": (0, json.dumps([{"job": "daily", "args": ["daily"],
                                    "llm": False}])),
        "daily": (0, "daily done")})
    tab = QuantDuelTab(actions=QuantActions(vault, runner=script))
    try:
        assert not tab.timer.isActive()
        tab.enabled_box.setChecked(True)
        assert not tab.save()                       # no folder yet
        assert "Choose" in tab.status.text()
        tab.root_edit.setText(str(_checkout(tmp_path)))
        tab.python_edit.setText(sys.executable)
        tab.node_boxes["A"].setChecked(False)
        tab.schedule_box.setChecked(True)
        assert tab.save()
        assert tab.timer.isActive()
        assert qd.load(vault).nodes == ["B"]
        assert tab.refresh()
        assert not tab.refresh()                    # one command at a time
        _wait(qapp, tab)
        assert tab.table.rowCount() == 1
        assert tab.table.item(0, 0).text() == "B (no news)"
        assert tab.run_due()
        _wait(qapp, tab)
        assert "node B: daily — rc=0" in tab.log.toPlainText()
        tab.schedule_box.setChecked(False)
        assert tab.save() and not tab.timer.isActive()
    finally:
        tab.timer.stop()
        tab.close()
        tab.deleteLater()


def test_the_tab_says_where_the_llm_runs(qapp, tmp_path):
    from council_qt.tabs.quant_duel import QuantActions, QuantDuelTab
    nr.set_current(_routing())
    tab = QuantDuelTab(actions=QuantActions(tmp_path, runner=Script()))
    try:
        assert "pi-a" in tab.machine.text()
    finally:
        tab.close()
        tab.deleteLater()


# ============================================================
# End to end: a real duel checkout, LLM work on a "Pi" (a fake Ollama)
# ============================================================

def _duel_checkout():
    for cand in (os.environ.get("QUANT_DUEL_ROOT", ""),
                 str(ROOT.parent / "quant-duel")):
        if cand and (Path(cand) / "quant_duel" / "cli.py").exists():
            return Path(cand)
    return None


class _FakeOllama:
    """Answers /v1/chat/completions like Ollama, scoring every headline."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])))
                outer.requests.append(body)
                items = json.loads(body["messages"][-1]["content"])["items"]
                content = json.dumps({"scores": [{"id": i["id"], "s": 0.5}
                                                 for i in items]})
                payload = json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_end_to_end_news_scoring_runs_on_the_bound_machine(tmp_path):
    src = _duel_checkout()
    if src is None:
        pytest.skip("no Quant Duel checkout next to this repository")
    root = tmp_path / "quant-duel"
    root.mkdir()
    shutil.copytree(src / "quant_duel", root / "quant_duel",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(src / "config.yaml", root / "config.yaml")
    shutil.copytree(src / "nodes", root / "nodes")
    # one pending headline on node A
    subprocess.run([sys.executable, "-c", (
        "import sys, datetime as dt; sys.path.insert(0, sys.argv[1]);"
        "from quant_duel.news.feeds import Item;"
        "from quant_duel.news.store import NewsStore;"
        "s = NewsStore(sys.argv[1] + '/data/A/news.sqlite');"
        "now = dt.datetime.now(dt.timezone.utc);"
        "s.add('feed', [Item('Apple soars', 'x', now.strftime("
        "'%a, %d %b %Y %H:%M:%S +0000'))], {'Apple soars': ['AAPL']}, now)"),
        str(root)], check=True)
    pi = _FakeOllama()
    try:
        routing = nr.Routing(True, {"pi-test": nr.Node("pi-test", pi.url,
                                                        True, 1)},
                             {"quant": nr.Binding("pi-test", "fail")})
        s = qd.Settings(enabled=True, root=str(root), python=sys.executable)
        r = qd.run(s, "A", ["news-score"], llm=True, routing=routing)
        assert r.rc == 0, r.output
        assert r.llm == "ok" and r.machine == "pi-test"
        assert "1 scored" in r.output
        assert len(pi.requests) == 1                 # the Pi did the work
        assert nr.cooling(pi.url) == 0
        st = qd.status(s, "A")
        assert st["news"]["scored"] == 1
        jobs = qd.due(s, "B")
        assert isinstance(jobs, list)
    finally:
        pi.close()
