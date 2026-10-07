"""
The Apothecary: the engine fixes in apothecary_engine.py, the pure helpers in
council_core.apothecary, and the Qt tab and dialogs.

Written against docs/qt_migration/remaining_tabs_requirements.md
§apothecary; the defect numbers below are that document's table rows. No test
opens a real SSH connection — paramiko is replaced by a fake that behaves like
the channel the defects depend on.
"""
from __future__ import annotations

import threading
import time

import pytest

import apothecary_engine as ae
from council_core import apothecary as core


@pytest.fixture
def apoth(tmp_path):
    a = core.Apothecary(registry_path=str(tmp_path / "node_registry.json"))
    yield a
    a.monitor.stop()


def _node(name="pi-01", **kw):
    kw.setdefault("host", "10.0.0.5")
    return core.NodeEntry(name=name, **kw)


# ============================================================
# council_core.apothecary
# ============================================================

@pytest.mark.parametrize("model, ram", [
    ("Pi 5 (16GB)", 16), ("Pi 5 (16GB) + AI HAT+", 16),
    ("Pi 5 (8GB) + AI HAT+", 8), ("Pi 4 (4GB)", 4), ("Other / 2GB", 0),
    ("", 0),
])
def test_ram_gb_reads_every_combobox_value(model, ram):
    """Defect 6: the Tk expression raised ValueError on both AI HAT+ models."""
    assert core.ram_gb_for(model) == ram


def test_every_pi_model_parses():
    for model in core.PI_MODELS:
        core.ram_gb_for(model)                          # must not raise


def test_merge_form_keeps_what_the_form_does_not_show():
    old = _node(pi_model="Pi 4 (8GB)", model="phi4", status="online",
                installed_models=["phi4"], model_log=[{"model": "phi4"}],
                active_model="phi4", last_seen="2026-01-01", created_at="x")
    entry, previous = core.merge_form(old, {
        "name": "pi-01", "host": "10.0.0.9", "port": "22",
        "ollama_port": "11434", "pi_model": "Pi 5 (16GB) + AI HAT+",
        "has_ai_hat": True, "council_role": "heavy"})
    assert previous is None
    assert entry.host == "10.0.0.9" and entry.ram_gb == 16
    assert entry.ai_hat_tops == core.AI_HAT_TOPS
    assert (entry.model, entry.status, entry.active_model, entry.created_at) \
        == ("phi4", "online", "phi4", "x")
    assert entry.installed_models == ["phi4"]
    assert entry.installed_models is not old.installed_models


def test_merge_form_reports_a_rename():
    entry, previous = core.merge_form(_node("old"), {"name": "new",
                                                     "host": "h"})
    assert (entry.name, previous) == ("new", "old")


@pytest.mark.parametrize("form, msg", [
    ({"name": "", "host": "h"}, "required"),
    ({"name": "a", "host": "h", "port": "twenty"}, "whole numbers"),
])
def test_merge_form_refuses_bad_input(form, msg):
    with pytest.raises(core.FormError, match=msg):
        core.merge_form(None, form)


def test_discover_keeps_the_existing_record():
    """Defect 12: re-discovering replaced the record with 8 of 22 fields."""
    old = _node(pi_model="Pi 5 (8GB)", council_role="fast",
                model_log=[{"model": "m"}], installed_models=["m"])
    entry = core.merge_discovered(old, "pi-01", "10.0.0.7", "pi", "pw")
    assert entry.host == "10.0.0.7" and entry.status == "online"
    assert (entry.pi_model, entry.council_role) == ("Pi 5 (8GB)", "fast")
    assert entry.model_log == [{"model": "m"}]


def test_row_label_is_display_only():
    label = core.row_label(_node("living room pi", status="online"))
    assert label.startswith("● living room pi")


@pytest.mark.parametrize("msg, error, tag", [
    ("✓ ok", False, "ok"), ("✗ bad", False, "err"), ("⚠ hm", False, "warn"),
    ("plain", True, "err"), ("plain", False, "info"), ("✓ ok", True, "ok"),
])
def test_log_tag_is_the_tk_rule(msg, error, tag):
    assert core.log_tag(msg, error) == tag


def test_wizard_tag_colours_headers():
    assert core.wizard_tag("\n[ Check Os ]") == "hdr"
    assert core.wizard_tag("=====") == "hdr"
    assert core.wizard_tag("✓ x", error=True) == "err"


def test_patch_launch_bat_replaces_inserts_or_leaves_alone():
    url = "http://10.0.0.5:11434"
    assert core.patch_launch_bat("set COUNCIL_PI_HOSTS=old\r\nx", url) == \
        f"set COUNCIL_PI_HOSTS={url}\r\nx"
    assert core.patch_launch_bat("set OLLAMA_MAX_LOADED_MODELS=2", url) == \
        f"set COUNCIL_PI_HOSTS={url}\nset OLLAMA_MAX_LOADED_MODELS=2"
    assert core.patch_launch_bat("echo hi", url) == "echo hi"


def test_status_data_exists_without_tkinter():
    assert ae.STATUS_ICON["online"] == "●"
    assert set(core.STATUS_TOKEN) == set(ae.STATUS_COLOR)


# ============================================================
# apothecary_engine fixes
# ============================================================

class FakeChannel:
    """A channel whose command has ALREADY exited, with output still
    buffered in 4 KB pieces — the shape defect 10 needs."""

    def __init__(self, out: bytes, rc: int = 0):
        self._out = [out[i:i + 4096] for i in range(0, len(out), 4096)]
        self._rc = rc

    def settimeout(self, _t): pass
    def exec_command(self, _cmd): pass
    def recv_ready(self): return bool(self._out)
    def recv(self, _n): return self._out.pop(0)
    def recv_stderr_ready(self): return False
    def exit_status_ready(self): return True
    def recv_exit_status(self): return self._rc


class FakeParamiko:
    def __init__(self, out: bytes):
        self.out = out

    class AutoAddPolicy:
        pass

    def SSHClient(self):
        out = self.out

        class Transport:
            def is_active(self): return True
            def open_session(self, timeout=None): return FakeChannel(out)

        class Client:
            def set_missing_host_key_policy(self, _p): pass
            def connect(self, **_kw): pass
            def get_transport(self): return Transport()
            def close(self): pass

        return Client()


@pytest.fixture
def fake_ssh(monkeypatch):
    def install(out: bytes):
        monkeypatch.setattr(ae, "paramiko", FakeParamiko(out))
        monkeypatch.setattr(ae, "_PARAMIKO_OK", True)
    return install


def test_run_ssh_drains_past_the_first_chunk(apoth, fake_ssh):
    """Defect 10: one 4096-byte read, then break — the rest was dropped."""
    fake_ssh(b"x" * 10_000)
    rc, out, _err = apoth.engine.run_ssh(_node(password="pw"), "cat big",
                                         None, timeout_s=5)
    assert rc == 0 and len(out) == 10_000


def test_refresh_installed_models_is_saved(apoth, fake_ssh):
    """Defects 1 and 2: the monitor called a method it does not have, and the
    Model Inventory called one Apothecary did not have."""
    apoth.registry.upsert(_node(password="pw", installed_models=["old"]))
    fake_ssh(b"NAME ID SIZE\nphi4:latest a 1GB\nqwen2.5:3b b 2GB\n")
    node = apoth._get("pi-01")
    assert apoth.refresh_installed_models(node) == ["phi4:latest",
                                                    "qwen2.5:3b"]
    assert apoth._get("pi-01").installed_models == ["phi4:latest",
                                                    "qwen2.5:3b"]


def test_a_failed_refresh_raises_instead_of_returning_stale(apoth,
                                                            monkeypatch):
    monkeypatch.setattr(ae, "_PARAMIKO_OK", False)
    with pytest.raises(RuntimeError, match="Paramiko"):
        apoth.refresh_installed_models(_node(installed_models=["old"]))
    # the monitor's own lenient path is unchanged
    assert apoth.monitor.refresh_installed_models(
        _node(installed_models=["old"])) == ["old"]


def test_log_model_call_is_recorded(apoth):
    """Defect 3: a silent no-op, so the call log was always empty."""
    apoth.registry.upsert(_node())
    apoth.monitor.log_model_call("pi-01", "phi4", "intern")
    node = apoth._get("pi-01")
    assert node.active_model == "phi4"
    assert node.model_log[-1]["role"] == "intern"


def test_a_rename_does_not_leave_a_duplicate(apoth):
    """Defect 13."""
    apoth.save_node(_node("old"))
    entry, previous = core.merge_form(apoth._get("old"),
                                      {"name": "new", "host": "h"})
    apoth.save_node(entry, previous)
    assert [n.name for n in apoth.list_nodes()] == ["new"]


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the Apothecary tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.apothecary import (ApothecaryActions,  # noqa: E402
                                        ApothecaryTab, build_apothecary)
from council_qt.tabs.apothecary_dialogs import (  # noqa: E402
    DiscoverDialog, InventoryDialog, NodeDialog, RegisterDialog)
from council_qt.tabs.pi_setup_dialog import PiSetupDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


class FakeActions(ApothecaryActions):
    """Real registry, fake SSH. Records which thread each SSH call ran on."""

    def __init__(self, vault_dir):
        super().__init__(vault_dir)
        self.threads = []
        self.models = ["phi4"]
        self.refresh_error = None

    def _seen(self, what):
        self.threads.append((what, threading.current_thread().name))

    def test(self, name, password):
        self._seen("test")
        return True, "SSH OK"

    def check_ollama(self, name, password):
        self._seen("ollama")
        return False, "Ollama not responding (rc=7)"

    def run(self, name, cmd, password, timeout_s=30):
        self._seen("run")
        return 0, "hello\n", ""

    def discover(self, hostname, progress):
        self._seen("discover")
        progress("  ✓ Found: 10.0.0.8  (via mDNS)")
        return "10.0.0.8"

    def confirm_ip(self, hostname, username, password, ip, progress):
        progress("  ✓ Pi reports IP: 10.0.0.9")
        return "10.0.0.9"

    def refresh_models(self, node):
        self._seen("refresh")
        if self.refresh_error:
            raise self.refresh_error
        return list(self.models)


class Window:
    """A stand-in with the one method the mirror uses."""

    class Council:
        def __init__(self):
            self.lines = []

        def append(self, who, text, kind):
            self.lines.append((who, text, kind))

    def __init__(self):
        self.council = self.Council()
        self.bridge = None

    def tab(self, title):
        return self.council if title == "⚖ Council" else None


@pytest.fixture
def make_tab(qapp, tmp_path):
    made = []

    def make(nodes=(), answers=None, window=None):
        actions = FakeActions(tmp_path)
        for node in nodes:
            actions.apoth.registry.upsert(node)
        answers = answers if answers is not None else {}
        view = ApothecaryTab(
            window=window, actions=actions,
            ask_string=lambda *a, **k: answers.get("string", ""),
            ask_yes_no=lambda *a, **k: answers.get("yes", False))
        made.append(view)
        return view

    yield make
    for view in made:
        for t in list(threading.enumerate()):
            if t.name.startswith("apoth-"):
                t.join(3)
        qapp.processEvents()
        view.actions.monitor.stop()
        # The monitor's callback holds the tab: break that cycle so the tab
        # is not left for a garbage collection on some worker thread.
        view.actions.monitor.status_cb = None
        view.deleteLater()
    qapp.processEvents()


def _select(tab, name):
    tab.refresh(select=name)
    assert tab.selected_name() == name


def test_the_factory_takes_a_window(qapp, monkeypatch, tmp_path):
    from council_core import paths
    monkeypatch.setattr(paths, "vault_dir", lambda: tmp_path)
    view = build_apothecary(None)
    assert isinstance(view, ApothecaryTab)
    view.deleteLater()
    qapp.processEvents()


def test_the_tab_is_advanced_only():
    from council_qt.tabs import ADVANCED_REGISTRY, REGISTRY
    assert any(f is build_apothecary for _t, f, _e in ADVANCED_REGISTRY)
    assert not any(f is build_apothecary for _t, f, _e in REGISTRY)


def test_a_name_with_spaces_selects_the_right_node(make_tab):
    """Defect 7: Tk took token 1 of the row text, so this resolved to
    "living"."""
    tab = make_tab([_node("living room pi"), _node("living")])
    _select(tab, "living room pi")
    assert tab.selected_node().name == "living room pi"
    assert "living room pi" in tab.detail.text()


def test_a_health_change_keeps_the_selection(make_tab):
    """Defect 5: every status change cleared the list and the selection."""
    tab = make_tab([_node("a"), _node("b")])
    _select(tab, "b")
    tab._status_changed("a", "offline", "SSH unreachable")
    assert tab.selected_name() == "b"
    assert "[a] SSH unreachable" in tab.log.toPlainText()


def test_health_changes_reach_the_council_transcript(make_tab):
    """Defect 11: the Tk console never posted to its ui_queue."""
    window = Window()
    tab = make_tab([_node("a")], window=window)
    tab._status_changed("a", "online", "SSH ✓  Ollama ✓")
    assert window.council.lines == [("Apothecary", "● [a] SSH ✓  Ollama ✓",
                                     "final")]


def test_the_monitor_callback_hops_to_the_gui_thread(qapp, make_tab):
    tab = make_tab([_node("a")])
    t = threading.Thread(target=tab.actions.monitor.status_cb,
                         args=("a", "degraded", "Ollama ✗"))
    t.start()
    t.join(3)
    pump(qapp, lambda: "[a] Ollama" in tab.log.toPlainText())


@pytest.mark.parametrize("slot, what", [
    ("on_test_ssh", "test"), ("on_check_ollama", "ollama"),
    ("on_restart_ollama", "run"),
])
def test_ssh_actions_run_off_the_gui_thread(qapp, make_tab, slot, what):
    """Defect 9: four of these froze the window for up to 30 s."""
    tab = make_tab([_node("a")])
    _select(tab, "a")
    getattr(tab, slot)()
    pump(qapp, lambda: tab.actions.threads and not tab._busy)
    assert tab.actions.threads[0][0] == what
    assert tab.actions.threads[0][1].startswith("apoth-")


def test_run_command_shows_its_output(qapp, make_tab):
    tab = make_tab([_node("a")], answers={"string": "echo hello"})
    _select(tab, "a")
    tab.on_run_command()
    pump(qapp, lambda: "hello" in tab.log.toPlainText())
    assert "rc=0" in tab.log.toPlainText()


def test_an_action_without_a_selection_says_so(make_tab):
    tab = make_tab([_node("a")])
    tab.nodes.setCurrentItem(None)
    tab.on_test_ssh()
    assert "Select a node first" in tab.log.toPlainText()
    assert tab.actions.threads == []


def test_delete_asks_first(make_tab):
    tab = make_tab([_node("a")], answers={"yes": False})
    _select(tab, "a")
    tab.on_delete()
    assert tab.actions.node("a") is not None


def test_the_teardown_stops_the_monitor(qapp, tmp_path):
    actions = FakeActions(tmp_path)
    tab = ApothecaryTab(actions=actions)
    monitor = actions.monitor
    assert monitor.status_cb is not None
    # Delete THIS widget now. Flushing every deferred delete in the process
    # would also destroy widgets earlier test files left pending — some of
    # which crash when destroyed (see the foundation harness).
    import shiboken6
    shiboken6.delete(tab)
    qapp.processEvents()
    assert monitor._stop.is_set() and monitor.status_cb is None


# -- dialogs -------------------------------------------------------------

def test_discover_then_wizard_opens_for_the_saved_node(qapp, make_tab, monkeypatch,
                                                       tmp_path):
    """Defect 4: the wizard opened on "whatever is selected", which the save's
    list refresh had just cleared. Defect 8: progress arrives through the
    GUI thread (the fake posts from the worker). The wizard it opens is now
    "Set up a Pi", filled in for the saved node (review, 2026-10-07)."""
    monkeypatch.setenv("COUNCIL_PI_STATE_DIR", str(tmp_path / "pi_state"))
    tab = make_tab(answers={"yes": True})
    tab.on_discover()
    dlg = tab.dialog
    assert isinstance(dlg, DiscoverDialog)
    dlg.save_as.setText("kitchen pi")
    dlg.on_find()
    pump(qapp, lambda: dlg.save_btn.isEnabled())
    assert "Pi reports IP: 10.0.0.9" in dlg.log.toPlainText()
    assert tab.actions.threads[0][1] == "apoth-discover"
    dlg.on_save()
    assert tab.actions.node("kitchen pi").host == "10.0.0.9"
    dlg = tab.dialog
    assert isinstance(dlg, PiSetupDialog)
    assert dlg.pages.currentWidget() is dlg.page_existing
    assert (dlg.ex_host.text(), dlg.ex_name.text()) == ("10.0.0.9", "kitchen pi")
    dlg.done(0)
    dlg.deleteLater()


def test_rediscovering_keeps_hardware_metadata(qapp, make_tab):
    tab = make_tab([_node("pi-01", pi_model="Pi 5 (8GB)",
                          council_role="fast")])
    tab.actions.save_discovered("pi-01", "10.0.0.9", "pi", "pw")
    node = tab.actions.node("pi-01")
    assert node.host == "10.0.0.9" and node.pi_model == "Pi 5 (8GB)"


def test_the_node_dialog_saves_an_ai_hat_model(make_tab):
    """Defect 6 through the UI: Save did nothing for these two models."""
    tab = make_tab()
    tab.on_add()
    dlg = tab.dialog
    assert isinstance(dlg, NodeDialog)
    dlg.name.setText("hat pi")
    dlg.host.setText("10.0.0.3")
    dlg.pi_model.setCurrentText("Pi 5 (16GB) + AI HAT+")
    assert dlg.ai_hat.isChecked() and dlg.role.currentText() == "heavy"
    assert "Recommended models" in dlg.hint.text()
    dlg.on_accept()
    node = tab.actions.node("hat pi")
    assert node.ram_gb == 16 and node.has_ai_hat
    assert tab.selected_name() == "hat pi"


def test_the_node_dialog_renames(make_tab):
    tab = make_tab([_node("old")])
    _select(tab, "old")
    tab.on_edit()
    tab.dialog.name.setText("new")
    tab.dialog.on_accept()
    assert [n.name for n in tab.actions.list_nodes()] == ["new"]
    assert "renamed from 'old'" in tab.log.toPlainText()


def test_the_node_dialog_shows_form_errors(make_tab):
    tab = make_tab()
    tab.on_add()
    tab.dialog.on_accept()
    assert "required" in tab.dialog.error.text()
    assert tab.actions.list_nodes() == []


def test_inventory_refresh(qapp, make_tab):
    tab = make_tab([_node("a")])
    _select(tab, "a")
    tab.on_inventory()
    dlg = tab.dialog
    assert isinstance(dlg, InventoryDialog)
    dlg.on_refresh()
    pump(qapp, lambda: not dlg._busy)
    assert "phi4" in dlg.installed.toPlainText()
    assert "1 model(s) found" in tab.log.toPlainText()


def test_inventory_refresh_failure_is_not_a_success(qapp, make_tab):
    tab = make_tab([_node("a", installed_models=["stale"])])
    tab.actions.refresh_error = RuntimeError("no route to host")
    _select(tab, "a")
    tab.on_inventory()
    tab.dialog.on_refresh()
    pump(qapp, lambda: not tab.dialog._busy)
    assert "could not refresh models: no route to host" in \
        tab.log.toPlainText()
    assert "found" not in tab.log.toPlainText()


def test_the_setup_button_opens_set_up_a_pi_for_the_selected_node(qapp, make_tab,
                                                                   monkeypatch, tmp_path):
    # The old Setup Pi Wizard ran 'curl ... install.sh | sh', opened Ollama
    # to the LAN and pulled qwen2.5:3b (not US-origin) with no click of its
    # own - also on a Pi "Set up a Pi" had made (review, 2026-10-07).
    monkeypatch.setenv("COUNCIL_PI_STATE_DIR", str(tmp_path / "pi_state"))
    tab = make_tab([_node("a", username="council")])
    _select(tab, "a")
    tab.on_wizard()
    dlg = tab.dialog
    assert isinstance(dlg, PiSetupDialog) and dlg.pages.currentWidget() is dlg.page_existing
    assert (dlg.ex_host.text(), dlg.ex_user.text(), dlg.ex_name.text()) == (
        "10.0.0.5", "council", "a")
    assert dlg.ex_pass.text() == ""             # typed by the user, used once
    assert tab.actions.threads == []            # nothing ran on the Pi
    dlg.done(0)
    dlg.deleteLater()


class _RecordingEngine:
    def __init__(self):
        self.commands = []

    def run_task_sequence(self, node, steps, pw, progress_cb):
        self.commands += [s.cmd for s in steps]
        return True, ""


def test_no_wizard_path_installs_ollama_or_downloads_a_model(tmp_path):
    # Whatever still reaches ApothecaryEngine.provision_pi (the Tk console's
    # wizard): no 'curl | sh', no Ollama on 0.0.0.0, no 'ollama pull'.
    every = " ".join(s.cmd for steps in ae.PROVISION_TASKS.values() for s in steps)
    for bad in ("install.sh", "ollama pull", "0.0.0.0", "11434"):
        assert bad not in every
    a = core.Apothecary(registry_path=str(tmp_path / "node_registry.json"))
    try:
        a.registry.upsert(_node("a", auth_method="key"))
        rec = _RecordingEngine()
        a.engine = rec
        lines = []
        ok, msg = a.provision_pi("a", "qwen2.5:3b", "10.0.0.2",
                                 progress_cb=lambda m, e: lines.append(m))
        assert ok and rec.commands
        assert not any("ollama" in c for c in rec.commands)
        assert a.registry.list_nodes()[0].model != "qwen2.5:3b"
        assert "Set up a Pi" in "\n".join(lines)
    finally:
        a.monitor.stop()


def test_register_refuses_a_bat_with_no_anchor(qapp, tmp_path):
    bat = tmp_path / "launch_council.bat"
    bat.write_text("echo hi\n", encoding="utf-8")
    lines = []
    dlg = RegisterDialog("http://x:1", lambda m, e: lines.append(m), bat)
    dlg.on_write()
    assert bat.read_text() == "echo hi\n"
    assert "by hand" in dlg.status.text() and lines == []
    dlg.deleteLater()
