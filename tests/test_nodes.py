"""
The Nodes tab: which Ollama hosts are up, and what they are running.

FOUR SILENT DEFECTS, AND THE ONE THAT MATTERS MOST
The Tk auto-refresh is re-armed only inside the branch that handles a
successful result, and its probe worker has no error handling at all. One host
answering `/api/ps` with a JSON array instead of an object raises in a daemon
thread; the result never arrives, the timer is never re-armed, and the tab
reads "Probing…" until the app is restarted.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import nodes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Status:
    """A stand-in with the real NodeStatus's fields, verified against
    council_engine.py:1833."""
    host: str
    reachable: bool
    active_models: int = 0
    installed_models: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    active_model_names: List[str] = field(default_factory=list)


class Dispatcher:
    def __init__(self, statuses=None, hosts=None, boom=None):
        self.hosts = hosts or ["http://a:11434"]
        self._statuses = statuses if statuses is not None else [
            Status("http://a:11434", True, 1, ["m1", "m2"], 9.4, ["qwen"])]
        self._boom = boom
        self.invalidated = 0

    def invalidate(self, host=None):
        self.invalidated += 1

    def probe_all(self):
        if self._boom:
            raise self._boom
        return list(self._statuses)


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "nodes.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


# ============================================================
# Rows
# ============================================================

def test_a_reachable_host_shows_its_latency():
    row = nodes.to_row(Status("h", True, 1, ["m"], 12.7, ["qwen"]))
    assert row.up
    assert row.status == nodes.UP
    assert row.latency == "13 ms"


def test_an_unreachable_host_has_no_latency_and_no_models():
    """A host that is DOWN is different from one that is up and idle.
    Collapsing both to "none" makes an unreachable node look merely quiet."""
    row = nodes.to_row(Status("h", False))
    assert not row.up
    assert row.status == nodes.DOWN
    assert row.latency == nodes.NOT_APPLICABLE
    assert row.active == nodes.NOT_APPLICABLE


def test_a_reachable_host_running_nothing_says_none():
    row = nodes.to_row(Status("h", True, 0, ["m"], 3.2, []))
    assert row.active == "none"
    assert row.active != nodes.NOT_APPLICABLE


def test_a_long_model_list_is_truncated_with_a_count():
    """A Pi with thirty models pinned would push every other column off the
    screen."""
    row = nodes.to_row(Status("h", True, 0,
                              [f"m{i}" for i in range(9)], 1.0, []))
    assert row.installed.count(",") == nodes.INSTALLED_SHOWN - 1
    assert "(+3 more)" in row.installed


def test_a_short_model_list_is_not_annotated():
    row = nodes.to_row(Status("h", True, 0, ["a", "b"], 1.0, []))
    assert row.installed == "a, b"
    assert "more" not in row.installed


def test_a_host_with_no_models_shows_a_dash():
    assert nodes.to_row(Status("h", True, 0, [], 1.0, [])).installed == \
        nodes.NOT_APPLICABLE


def test_a_status_missing_its_optional_fields_does_not_raise():
    """NodeStatus's list fields have default_factory, but a probe that fails
    partway can hand back something thinner."""
    class Thin:
        host = "h"
        reachable = True

    row = nodes.to_row(Thin())
    assert row.host == "h"


# ============================================================
# Host parsing
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("a, b", ["a", "b"]),
    (" a ,, b ", ["a", "b"]),
    ("", []),
    (None, []),
    ("solo", ["solo"]),
])
def test_the_host_box_is_split_and_trimmed(raw, expected):
    assert nodes.parse_hosts(raw) == expected


def test_an_empty_box_means_no_extra_hosts_not_no_hosts():
    """A user who clears the field is removing the Pis, not switching the app
    off — the dispatcher keeps its default, which is localhost."""
    assert nodes.parse_hosts("") == []


# ============================================================
# Probing
# ============================================================

def test_a_probe_returns_a_row_per_host():
    result = nodes.probe(Dispatcher())
    assert result.ok
    assert len(result.rows) == 1


def test_a_probe_that_raises_is_reported_rather_than_lost():
    """THE DEFECT. The Tk worker has no guard, so this kills the refresh loop
    permanently and leaves "Probing…" on screen for the rest of the session."""
    result = nodes.probe(Dispatcher(boom=AttributeError("'list' has no get")))
    assert not result.ok
    assert "AttributeError" in result.problem


def test_a_normal_refresh_does_not_invalidate_the_cache():
    """The 15s poll is allowed to use the 10s cache; that is what it is for."""
    dispatcher = Dispatcher()
    nodes.probe(dispatcher)
    assert dispatcher.invalidated == 0


def test_refresh_now_invalidates_first():
    """`invalidate()` exists in council_engine and has ZERO callers. Kill a
    node, press Refresh Now inside ten seconds, and every row still reads "up"
    with its old latency under a brand-new timestamp."""
    dispatcher = Dispatcher()
    nodes.probe(dispatcher, force=True)
    assert dispatcher.invalidated == 1


def test_a_dispatcher_without_invalidate_still_probes():
    """Older than this code. A stale row beats a dead Refresh button."""
    class Old:
        hosts = ["h"]

        def probe_all(self):
            return [Status("h", True)]

    assert nodes.probe(Old(), force=True).ok


def test_no_dispatcher_at_all_is_reported():
    assert not nodes.probe(None).ok


def test_a_result_carries_the_hosts_it_probed():
    result = nodes.probe(Dispatcher(hosts=["h1", "h2"]))
    assert result.hosts == ("h1", "h2")


def test_a_result_from_a_replaced_host_list_is_stale():
    """Two probes can be in flight — the 15s one and the one Apply starts —
    and the slower can land last, repainting the table with the host list the
    user just replaced."""
    dispatcher = Dispatcher(hosts=["new"])
    assert nodes.is_stale(nodes.ProbeResult(hosts=("old",)), dispatcher)
    assert not nodes.is_stale(nodes.ProbeResult(hosts=("new",)), dispatcher)


def test_the_host_list_is_said_to_be_temporary():
    """It is read from COUNCIL_PI_HOSTS at startup and written nowhere. Add
    two nodes, use them all session, restart: gone, with nothing said."""
    assert "COUNCIL_PI_HOSTS" in nodes.hosts_are_temporary()


# ============================================================
# Rebuilding
# ============================================================

def test_a_failed_rebuild_hands_back_no_dispatcher(monkeypatch, tmp_path):
    """The Tk code assigns self.dispatcher BEFORE building the council, with
    no try — so a failure leaves a new dispatcher wired to the old
    personalities, and the two disagree about which hosts exist for the rest
    of the session."""
    import council_engine

    from council_core import council_turn

    monkeypatch.setattr(council_engine, "build_dispatcher",
                        lambda extra_hosts=None: Dispatcher())
    monkeypatch.setattr(council_turn, "load_personalities",
                        lambda *a, **k: (None, "no judge model is pinned"))
    result = nodes.rebuild("http://pi:11434", tmp_path)
    assert not result.ok
    assert result.dispatcher is None
    assert "judge" in result.message


def test_a_dispatcher_that_cannot_be_built_is_reported(monkeypatch, tmp_path):
    import council_engine

    def _boom(extra_hosts=None):
        raise OSError("no network")

    monkeypatch.setattr(council_engine, "build_dispatcher", _boom)
    result = nodes.rebuild("h", tmp_path)
    assert not result.ok
    assert result.dispatcher is None


def test_a_good_rebuild_hands_back_both_halves(monkeypatch, tmp_path):
    import council_engine

    from council_core import council_turn

    built = Dispatcher()
    monkeypatch.setattr(council_engine, "build_dispatcher",
                        lambda extra_hosts=None: built)
    monkeypatch.setattr(council_turn, "load_personalities",
                        lambda *a, **k: (object(), ""))
    result = nodes.rebuild("http://pi:11434", tmp_path)
    assert result.ok
    assert result.dispatcher is built
    assert result.personalities is not None
    assert "http://pi:11434" in result.message


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the Nodes tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.nodes import NodesActions, NodesTab, build_nodes  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def drive(qapp, tab, seconds=8.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy, "still probing"


@pytest.fixture
def make_tab(qapp):
    made = []

    def build(dispatcher=None, auto_refresh=False):
        tab = NodesTab(actions=NodesActions(dispatcher=dispatcher
                                            or Dispatcher()),
                       auto_refresh=auto_refresh)
        made.append(tab)
        return tab

    yield build
    import threading
    deadline = time.time() + 5.0
    while any(t.name.startswith("nodes-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    for tab in made:
        tab._timer.stop()
        tab.deleteLater()
    qapp.processEvents()


def test_the_factory_takes_a_window(qapp):
    tab = build_nodes(None)
    assert isinstance(tab, NodesTab)
    tab._timer.stop()
    tab.deleteLater()


def test_the_table_fills_from_a_probe(make_tab, qapp):
    tab = make_tab(Dispatcher(statuses=[
        Status("http://a:11434", True, 1, ["m1"], 9.4, ["qwen"]),
        Status("http://b:11434", False)]))
    tab.refresh(force=True)
    drive(qapp, tab)
    assert tab.table.rowCount() == 2
    assert tab.table.item(0, 0).text() == "http://a:11434"
    assert tab.table.item(1, 1).text() == nodes.DOWN


def test_the_status_line_counts_what_is_up(make_tab, qapp):
    tab = make_tab(Dispatcher(statuses=[Status("a", True), Status("b", False)]))
    tab.refresh(force=True)
    drive(qapp, tab)
    assert "2 host(s)" in tab.status.text()
    assert "1 up" in tab.status.text()


def test_up_and_down_are_coloured_from_the_theme(make_tab, qapp):
    """Tk hardcodes #a6e3a1 / #f38ba8 in the tab."""
    from council_qt import theme
    tab = make_tab(Dispatcher(statuses=[Status("a", True), Status("b", False)]))
    tab.refresh(force=True)
    drive(qapp, tab)
    tokens = theme.tokens("dark")
    assert tab.table.item(0, 1).foreground().color().name() == \
        tokens["success"].lower()
    assert tab.table.item(1, 1).foreground().color().name() == \
        tokens["error"].lower()


def test_the_host_box_starts_from_the_dispatcher(make_tab):
    tab = make_tab(Dispatcher(hosts=["http://a:11434", "http://b:11434"]))
    assert tab.hosts_box.text() == "http://a:11434, http://b:11434"


def test_a_probe_runs_off_the_gui_thread(make_tab, qapp):
    import threading
    tab = make_tab()
    seen = []
    real = tab.actions.probe
    tab.actions.probe = lambda **kw: (seen.append(
        threading.current_thread().name), real(**kw))[1]
    tab.refresh(force=True)
    drive(qapp, tab)
    assert seen and seen[0] != "MainThread"


def test_a_failing_probe_does_not_stop_the_refresh(make_tab, qapp):
    """THE DEFECT, at the view level. The Tk re-arm lives only on the success
    path, so one bad host leaves the tab reading "Probing…" forever."""
    tab = make_tab(Dispatcher(boom=AttributeError("bad host")),
                   auto_refresh=True)
    drive(qapp, tab)
    assert tab._timer.isActive(), "the refresh timer stopped"
    assert not tab._busy, "the busy flag was never released"
    assert "Could not probe" in tab.status.text()


def test_the_timer_is_repeating_rather_than_re_armed():
    """A re-arm is a path that can be missed. A repeating timer started once
    on the GUI thread has no such path — and cannot be started from a worker,
    where QTimer.start() silently does nothing."""
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "nodes.py").read_text(
        encoding="utf-8")
    assert "setSingleShot(True)" not in source
    assert "_timer.start(" not in code_of(source, "_done")


def test_the_timer_belongs_to_the_tab(make_tab):
    """A QTimer parented to the widget dies with it. The Tk after-id lives on
    the application object and must be cancelled by hand, which is why closing
    the tab never stops the polling."""
    tab = make_tab()
    assert tab._timer.parent() is tab


def test_a_stale_result_does_not_repaint_the_table(make_tab, qapp):
    tab = make_tab(Dispatcher(statuses=[Status("a", True)]))
    tab.refresh(force=True)
    drive(qapp, tab)
    before = tab.table.rowCount()
    stale = nodes.ProbeResult(
        rows=[nodes.to_row(Status("gone", True)) for _ in range(5)],
        hosts=("a-different-list",))
    tab._show(stale)
    assert tab.table.rowCount() == before


def test_a_second_refresh_while_one_runs_is_skipped(make_tab, qapp):
    import threading
    release = threading.Event()
    calls = []
    tab = make_tab()

    def slow(**_kw):
        calls.append(1)
        release.wait(3.0)
        return nodes.ProbeResult(hosts=tuple(tab.actions.dispatcher.hosts))

    tab.actions.probe = slow
    tab.refresh(force=True)
    for _ in range(200):
        qapp.processEvents()
        if tab._busy:
            break
    tab.refresh(force=True)
    release.set()
    drive(qapp, tab)
    assert len(calls) == 1


def test_applying_a_host_list_rebuilds_on_a_worker(make_tab, qapp):
    import threading
    tab = make_tab()
    seen = []

    def rebuild(raw):
        seen.append(threading.current_thread().name)
        return nodes.RebuildResult(False, "nope")

    tab.actions.rebuild = rebuild
    tab.hosts_box.setText("http://pi:11434")
    tab.on_apply()
    drive(qapp, tab)
    assert seen and seen[0] != "MainThread"


def test_a_failed_apply_does_not_swap_the_dispatcher(qapp, monkeypatch,
                                                     tmp_path):
    import council_engine

    from council_core import council_turn

    original = Dispatcher()
    actions = NodesActions(dispatcher=original, vault_dir=tmp_path)
    monkeypatch.setattr(council_engine, "build_dispatcher",
                        lambda extra_hosts=None: Dispatcher(hosts=["new"]))
    monkeypatch.setattr(council_turn, "load_personalities",
                        lambda *a, **k: (None, "no judge"))
    result = actions.rebuild("http://new:11434")
    assert not result.ok
    assert actions.dispatcher is original


def test_a_good_apply_swaps_the_dispatcher(qapp, monkeypatch, tmp_path):
    import council_engine

    from council_core import council_turn

    replacement = Dispatcher(hosts=["new"])
    actions = NodesActions(dispatcher=Dispatcher(), vault_dir=tmp_path)
    monkeypatch.setattr(council_engine, "build_dispatcher",
                        lambda extra_hosts=None: replacement)
    monkeypatch.setattr(council_turn, "load_personalities",
                        lambda *a, **k: (object(), ""))
    assert actions.rebuild("http://new:11434").ok
    assert actions.dispatcher is replacement


def test_the_tab_says_the_host_list_will_not_survive_a_restart(make_tab):
    tab = make_tab()
    assert "COUNCIL_PI_HOSTS" in tab.note.text()


def test_no_worker_touches_a_widget_directly():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "nodes.py").read_text(
        encoding="utf-8")
    for name in ("refresh", "on_apply"):
        body = code_of(source, name)
        assert "_to_ui" in body
        inner = body.split("def work", 1)[1]
        inner = inner.split("def show", 1)[0]
        for forbidden in ("self.table.", "self.status.setText",
                          "self.apply_btn"):
            assert forbidden not in inner, f"{name}'s worker touches a widget"
