"""
Plugins written by someone else (stage P5; docs/typhon_plugins_design.md
4.1, 4.5, 4.6, 4.7; Andy's answer to question 7: approved once):

  * a plugin's folder dragged into the plugins folder waits for approval;
    approved, it loads — as typhon_thirdparty.<id>, whatever its folder
    is called — and the approval is kept in approvals.json, so the next
    start runs it without asking, a later version included;
  * the bytes read and hashed at start-up are the bytes that run: a module
    or data file changed after start-up is not used in that session (the
    data file is refused), and no bytecode is written into the folder;
  * two folders with one id are both refused; an id with an underscore is
    refused;
  * a plugin asking for the internet waits until the user opts it in, and
    the network tripwire refuses a connection its manifest does not allow;
  * the Plugins window's review shows every file's sha256 and the lines
    worth a look; withdrawing an approval switches the plugin off;
  * a run's camera record names the code that ran, by sha256.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_loader  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.test_typhon_plugins_host import (builtins_pkg, make_host,  # noqa
                                            manifest_doc, plugin_home)

CODE = textwrap.dedent('''
    from . import helper

    class P:
        def __init__(self):
            self.ctx = None
            self.said = helper.WORD

        def activate(self, ctx):
            self.ctx = ctx

        def deactivate(self):
            self.ctx = None

        def word(self):
            return self.said

        def data(self, name):
            return self.ctx.read_data(name)

        def connect(self, host, port):
            # The event socket.connect raises for every connection (the
            # tests' offline guard stops a real one before it is made).
            import sys
            sys.audit("socket.connect", None, (host, port))
''')


def drop_in(home, folder, doc=None, code=CODE, word="first"):
    """A plugin's folder, as its author hands it over, dragged in."""
    where = pl.plugins_folder(home) / folder
    where.mkdir(parents=True)
    doc = doc or manifest_doc("acme.thermal", "therm", builtin=False)
    (where / pm.MANIFEST_FILE).write_text(json.dumps(doc), encoding="utf-8")
    (where / "plugin.py").write_text(code, encoding="utf-8")
    (where / "helper.py").write_text(f"WORD = {word!r}\n", encoding="utf-8")
    (where / "table.json").write_text('{"a": 1}', encoding="utf-8")
    return where


@pytest.fixture(autouse=True)
def forget_packages():
    yield
    for name in list(sys.modules):
        if name.startswith(plugin_loader.PACKAGE_ROOT + "."):
            plugin_loader.uninstall(".".join(name.split(".")[:2]))
    plugin_loader._NETWORK.clear()


def approved_host(builtins_pkg, home, tmp_path, pid="acme.thermal"):
    host = make_host(builtins_pkg, [])
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    host.open_project(proj, create=True)
    host.approve(pid)
    host.switch_on(pid)
    return host


def test_approved_once_it_loads_from_any_folder_name(builtins_pkg,
                                                     plugin_home, tmp_path):
    drop_in(plugin_home, "Thermal plugin v1.2 (unzipped)")
    host = make_host(builtins_pkg, [])
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    host.switch_on("acme.thermal")
    assert host.instance("acme.thermal") is None       # not approved yet
    host.approve("acme.thermal")
    made = host.instance("acme.thermal")
    assert made is not None and made.word() == "first"
    assert type(made).__module__ == "typhon_thirdparty.acme_thermal.plugin"
    kept = pl.read_approvals(plugin_home)["acme.thermal"]
    assert kept.version == "1.0.0" and "plugin.py" in kept.files
    host.close()

    # The next start: never asked again, a later version included (4.6).
    for name in list(sys.modules):
        if name.startswith("typhon_thirdparty."):
            plugin_loader.uninstall("typhon_thirdparty.acme_thermal")
    folder = pl.plugins_folder(plugin_home) / "Thermal plugin v1.2 (unzipped)"
    doc = json.loads((folder / pm.MANIFEST_FILE).read_text())
    doc["version"] = "1.1.0"
    (folder / pm.MANIFEST_FILE).write_text(json.dumps(doc))
    (folder / "helper.py").write_text("WORD = 'second'\n")
    again = make_host(builtins_pkg, [])
    again.open_project(proj)
    assert again.state("acme.thermal")[0] == ph.ON
    assert again.instance("acme.thermal").word() == "second"
    again.close()


def test_the_bytes_read_at_start_up_are_the_bytes_that_run(
        builtins_pkg, plugin_home, tmp_path):
    where = drop_in(plugin_home, "acme_thermal")
    host = make_host(builtins_pkg, [])
    host.registry                                   # start-up: read, hashed
    (where / "helper.py").write_text("WORD = 'swapped'\n", encoding="utf-8")
    (where / "table.json").write_text('{"a": 2}', encoding="utf-8")
    (where / "extra.json").write_text('{}', encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    host.registry
    # open_project re-reads the registry: start-up is the host's first
    # read, so swap after it again for the session's bytes.
    host.approve("acme.thermal")
    host.switch_on("acme.thermal")
    made = host.instance("acme.thermal")
    assert made.word() == "swapped"     # read when this registry was made
    (where / "helper.py").write_text("WORD = 'later'\n", encoding="utf-8")
    import importlib
    importlib.reload(sys.modules["typhon_thirdparty.acme_thermal.helper"])
    assert sys.modules["typhon_thirdparty.acme_thermal.helper"].WORD == \
        "swapped", "a reload compiles the held bytes, never the disk's"
    assert made.data("table.json") == b'{"a": 2}'
    (where / "table.json").write_text('{"a": 3}', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="changed since"):
        made.data("table.json")
    (where / "new.json").write_text('{}', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="next start"):
        made.data("new.json")
    with pytest.raises(FileNotFoundError, match="outside"):
        made.data("../../secret.txt")
    assert not list(where.rglob("__pycache__"))
    assert not list(where.rglob("*.pyc"))
    host.close()


def test_two_folders_with_one_id_are_both_refused(builtins_pkg, plugin_home):
    drop_in(plugin_home, "a_copy")
    drop_in(plugin_home, "b_copy")
    host = make_host(builtins_pkg, [])
    state, why = host.state("acme.thermal")
    assert state == ph.INCOMPATIBLE and "take one" in why
    refused = {f.name: w for f, w in host.registry.refused}
    assert "take one" in refused["b_copy"]


def test_an_id_with_an_underscore_is_refused(builtins_pkg, plugin_home):
    drop_in(plugin_home, "x", manifest_doc("acme.ther_mal", "therm",
                                           builtin=False))
    host = make_host(builtins_pkg, [])
    state, why = host.state("acme.ther_mal")
    assert state == ph.INCOMPATIBLE and "underscore" in why


def test_the_internet_waits_for_the_users_opt_in(builtins_pkg, plugin_home,
                                                 tmp_path):
    drop_in(plugin_home, "acme_thermal", manifest_doc(
        "acme.thermal", "therm", builtin=False, network="internet"))
    host = make_host(builtins_pkg, [])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    host.switch_on("acme.thermal")
    host.approve("acme.thermal")
    state, why = host.state("acme.thermal")
    assert state == ph.NEEDS_APPROVAL and "Allow internet" in why
    assert host.instance("acme.thermal") is None
    host.approve("acme.thermal", internet=True)
    assert host.instance("acme.thermal") is not None
    host.close()


def test_the_tripwire_refuses_what_the_manifest_does_not_allow(
        builtins_pkg, plugin_home, tmp_path):
    drop_in(plugin_home, "acme_thermal")              # network: none
    host = approved_host(builtins_pkg, plugin_home, tmp_path)
    made = host.instance("acme.thermal")
    with pytest.raises(PermissionError, match="allows only network 'none'"):
        made.connect("192.0.2.1", 9)                  # off this PC
    made.connect("127.0.0.1", 9)                      # this PC: allowed
    # Typhon's own code is never stopped by it.
    sys.audit("socket.connect", None, ("192.0.2.1", 9))
    assert any("refused a network connection" in line
               for line in host.said)
    host.close()


@pytest.mark.parametrize("network, address, ok", [
    ("none", ("127.0.0.1", 80), True),
    ("none", ("192.168.1.20", 80), False),
    ("camera-link", ("192.168.1.20", 3956), True),
    ("camera-link", ("169.254.10.2", 3956), True),
    ("camera-link", ("8.8.8.8", 53), False),
    ("camera-link", ("example.com", 80), False),
    ("internet", ("8.8.8.8", 53), True),
])
def test_what_each_network_field_allows(network, address, ok):
    assert plugin_loader.address_allowed(address, network) is ok


def test_the_review_shows_every_file_and_the_lines_worth_a_look(
        builtins_pkg, plugin_home):
    drop_in(plugin_home, "acme_thermal", code=CODE + textwrap.dedent('''
        import subprocess
        def run():
            subprocess.run(["tool"])
    '''))
    host = make_host(builtins_pkg, [])
    text = "\n".join(host.review("acme.thermal"))
    held = host.record("acme.thermal").held
    for rel, f in held.files.items():
        assert rel in text and f.sha256 in text
    assert "subprocess calls" in text and "Typhon's rights" in text
    assert any(r.label == "subprocess calls"
               for r in host.risks("acme.thermal"))


def test_withdrawing_switches_it_off(builtins_pkg, plugin_home, tmp_path):
    drop_in(plugin_home, "acme_thermal")
    host = approved_host(builtins_pkg, plugin_home, tmp_path)
    assert host.instance("acme.thermal") is not None
    host.withdraw("acme.thermal")
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL
    assert host.instance("acme.thermal") is None
    assert "acme.thermal" not in pl.read_approvals(plugin_home)
    host.close()


def test_rescan_finds_a_folder_dragged_in_later(builtins_pkg, plugin_home):
    host = make_host(builtins_pkg, [])
    assert host.record("acme.thermal") is None
    drop_in(plugin_home, "acme_thermal")
    assert host.rescan() == ["acme.thermal"]
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL


def test_a_built_in_cannot_be_approved(builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("good", manifest_doc("typhon.good", "good"))
    host = make_host(builtins_pkg, [("typhon.good", "good")])
    with pytest.raises(ValueError):
        host.approve("typhon.good")


def test_a_damaged_approvals_file_approves_nothing(builtins_pkg,
                                                   plugin_home):
    drop_in(plugin_home, "acme_thermal")
    (plugin_home / pl.APPROVALS_FILE).write_text("{oops", encoding="utf-8")
    host = make_host(builtins_pkg, [])
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL
    assert any("approvals.json" in n for n in host.notes)


# ======================================================================
# The Plugins window (council_qt.widgets.plugin_panels)
# ======================================================================
class StubStrip:
    """What PluginsDialog asks of the toolbar strip."""

    def __init__(self, host):
        self.host = host
        self.said = []

    def _name(self, pid):
        m = self.host.manifest(pid)
        return m.name if m is not None else pid

    def switch(self, pid, on):
        (self.host.switch_on if on else self.host.switch_off)(pid)
        return f"{pid} {'on' if on else 'off'}"

    def refresh(self):
        pass

    def close_window(self, pid):
        pass

    def _say(self, text):
        self.said.append(text)


def test_the_plugins_window_reviews_approves_and_withdraws(
        builtins_pkg, plugin_home, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from council_qt.widgets import plugin_panels as pp

    drop_in(plugin_home, "acme_thermal", manifest_doc(
        "acme.thermal", "therm", builtin=False, kind="camera",
        capabilities=["frames"]))
    host = make_host(builtins_pkg, [])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    strip = StubStrip(host)
    dlg = pp.PluginsDialog(strip)
    assert "acme.thermal" in dlg.boxes, "a camera plugin from someone else"
    assert not dlg.approve_buttons["acme.thermal"].isHidden()
    assert dlg.withdraw_buttons["acme.thermal"].isHidden()
    assert "needs approval" in dlg.notes["acme.thermal"].text()

    review = dlg.review("acme.thermal")
    assert "sha256" in review.text.toPlainText()
    assert review.approve_button.isEnabled()
    assert review.allow_internet.isHidden()

    dlg.approve("acme.thermal")
    assert host.state("acme.thermal")[0] == ph.ON
    assert dlg.boxes["acme.thermal"].isChecked()
    assert dlg.approve_buttons["acme.thermal"].isHidden()
    assert not dlg.withdraw_buttons["acme.thermal"].isHidden()
    assert "next camera scan" in dlg.notes["acme.thermal"].text()

    dlg.withdraw("acme.thermal")
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL

    assert dlg.open_plugins_folder() == pl.plugins_folder(plugin_home)
    drop_in(plugin_home, "acme_other", manifest_doc(
        "acme.other", "other", builtin=False))
    assert dlg.rescan() == ["acme.other"]
    assert "acme.other" in dlg.boxes
    dlg.deleteLater()
    app.processEvents()
    host.close()
