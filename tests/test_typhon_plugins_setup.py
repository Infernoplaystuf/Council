"""
Stage P2 rows 3 and 4 (docs/typhon_plugins_design.md 2.3, 2.4 item 15,
5.4, 12; docs/typhon_multicamera_design.md 2.2, M1 row 4): a "Typhon
(plugins)" project's set-up ticks camera FAMILIES — its enabled camera
plugins — and each camera plugin brings its own guide, checks and (Basler)
scan page into the wizard; camera_setup.json's old single choice is read as
the project's plugin list; cameras take their names in the rig from
camera_rig.json, and the scan says "in use as cam2".

  * camera_setup.json stays FORMAT 1: a single-choice file is a list of
    one, and a families file still reads in today's Typhon;
  * the Basler and Prophesee plugins' guides and checks ARE today's, and
    the Basler scan says which camera of the rig it found in use;
  * the host scans only the families ticked (and the simulated cameras,
    which need no set-up), offers every family in the wizard, and never
    lets a plugin's failing guide or check reach the wizard as an error;
  * the families wizard: check boxes, each ticked family's steps, checks
    and scan, saved as the project's families;
  * in a "Typhon (plugins)" project frame_camera scans, opens and sets up
    through the host — the same rows and notes as today's scan for the
    same set-up — names each camera in the rig, and marks "in use as cam2";
  * today's Typhon is unchanged: its wizard, its scan, and no plugin host.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import basler_scan as bs  # noqa: E402
from council_core import camera_rig, cameras  # noqa: E402
from council_core import camera_rig_file as rf  # noqa: E402
from council_core import camera_setup as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from council_qt.widgets import camera_wizard as cw  # noqa: E402
from tests import fake_rig  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
BUILTIN_IDS = [pid for pid, _folder in ph.BUILTIN_PLUGINS]
PYTHON = r"C:\envs\pylon\python.exe"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def home(tmp_path, monkeypatch):
    where = tmp_path / "plugin_home"
    where.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(where))
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    return where


def make_plugin_project(folder, home):
    folder.mkdir(parents=True, exist_ok=True)
    pl.make_project(folder, pl.read_base_set(home, BUILTIN_IDS), home)
    return folder


class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


@pytest.fixture
def host(home, tmp_path):
    made = ph.PluginHost(camera_rig.Rig(Template))
    made.open_project(make_plugin_project(tmp_path / "proj", home))
    yield made
    for slot in made.rig:
        if getattr(slot, "device", None) is not None:
            slot.device.close()
            slot.device = None
    made.close()


def setup_file(host):
    return cs.setup_path(host.project_dir)


# ======================================================================
# camera_setup.json: still FORMAT 1, read both ways
# ======================================================================
def test_a_single_choice_file_is_a_list_of_one(tmp_path):
    """A project set up today is set up for that one family: not asked
    again (design 2.4 item 15)."""
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "prophesee")
    assert cs.load_families(path) == ["prophesee"]


def test_families_are_format_1_and_todays_reader_reads_the_first_it_knows(
        tmp_path):
    path = cs.setup_path(tmp_path)
    assert cs.save_families(path, ["prophesee", "basler",
                                   "prophesee"]) == ["prophesee", "basler"]
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == cs.FORMAT == 1
    assert doc["cameras"] == ["prophesee", "basler"]
    assert doc["camera"] == "prophesee"
    assert cs.load_choice(path) == "prophesee", "today's Typhon reads it"
    assert cs.load_families(path) == ["prophesee", "basler"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [cs.SETUP_FILE], \
        "no temporary file is left behind"


def test_a_family_todays_reader_does_not_know_leaves_it_asking(tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_families(path, ["acme.thermal", "basler"])
    assert cs.load_choice(path) == "basler"
    cs.save_families(path, ["acme.thermal"])
    assert "camera" not in json.loads(path.read_text(encoding="utf-8"))
    assert cs.load_choice(path) is None
    assert cs.load_families(path) == ["acme.thermal"]


@pytest.mark.parametrize("content", [
    json.dumps({"format": 1, "camera": "basler", "cameras": "basler"}),
    json.dumps({"format": 1, "camera": "basler", "cameras": []}),
    json.dumps({"format": 1, "cameras": ["Basler"]}),
    json.dumps({"format": 1, "cameras": ["basler", 3]}),
    json.dumps({"format": 1, "cameras": ["basler\n"]}),
    json.dumps({"format": 99, "cameras": ["basler"]}),
    "{not json",
])
def test_families_not_worth_trusting_mean_ask_again(tmp_path, content):
    path = cs.setup_path(tmp_path)
    path.write_text(content, encoding="utf-8")
    assert cs.load_families(path) is None


@pytest.mark.parametrize("bad", [[], "basler", ["Basler"], ["basler", ""],
                                 ["a b"]])
def test_families_that_cannot_be_saved_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        cs.save_families(cs.setup_path(tmp_path), bad)
    assert list(tmp_path.iterdir()) == []


def test_families_are_said_by_their_labels():
    assert cs.families_label(["basler"]) == "Basler"
    assert cs.families_label(["basler", "prophesee"]) == \
        "Basler and Prophesee EVK4"
    fam = cs.Family("acme.thermal", "acme.thermal", "Acme", "T", "",
                    guide=lambda p: None, check=lambda: None)
    assert cs.families_label(["basler", "acme.thermal", "prophesee"],
                             [fam]) == "Basler, Acme and Prophesee EVK4"
    assert cs.families_label([]) == "not set up"


# ======================================================================
# The camera plugins' own set-up (design 2.3, 2.4 item 15)
# ======================================================================
def test_the_sdk_plugins_contribute_a_set_up_and_the_simulated_none():
    got = {name: pm.read(BUILTIN / name, builtin=True).contributes
           for name in ("basler", "prophesee", "simulated")}
    assert "setup" in got["basler"] and "setup" in got["prophesee"]
    assert "setup" not in got["simulated"]
    with pytest.raises(pm.ManifestError, match="camera plugins only"):
        pm.check({"schema": 1, "id": "typhon.xx", "short": "xx", "name": "X",
                  "version": "1.0.0", "api": pm.API_VERSION,
                  "kind": "feature", "scope": "rig", "entry": "p:P",
                  "contributes": ["setup"]}, builtin=True)


def test_each_plugins_guide_is_todays_guide(host):
    for pid, choice in (("typhon.basler", "basler"),
                        ("typhon.prophesee", "prophesee")):
        plugin = host.instance(pid)
        assert isinstance(plugin, api.CameraSetup), pid
        assert plugin.setup_guide(PYTHON) == cs.guide(choice, python=PYTHON)
        assert plugin.setup_label == cs.label(choice)
    assert not isinstance(host.instance("typhon.simulated"), api.CameraSetup)
    assert api.Guide is cs.Guide and api.Readiness is cs.Readiness
    assert api.Check is cs.Check and api.Step is cs.Step


def test_each_plugins_checks_are_todays_checks(host):
    from tests.test_camera_setup import FakeInfo, FakePylon, fake_hal
    pylon = FakePylon(devices=[FakeInfo("40012345", "boA5320-150cm")])
    basler = host.instance("typhon.basler")
    basler.sdk = pylon
    assert basler.setup_checks() == cs.check("basler", basler_sdk=pylon)
    hal = fake_hal(["00050423"])
    evk = host.instance("typhon.prophesee")
    evk.sdk = hal
    assert evk.setup_checks() == cs.check("prophesee", evk_sdk=hal)
    assert evk.setup_checks().found_camera


def test_without_its_sdk_a_plugins_checks_say_what_to_install(
        host, monkeypatch):
    without_camera_sdks(monkeypatch)
    for pid, words in (("typhon.basler", "step 3"),
                       ("typhon.prophesee", "step 1")):
        r = host.instance(pid).setup_checks()
        assert r.checks[0].ok is False and words in r.checks[0].fix, pid


def basler_report(monkeypatch, plugin, serials=("40012345", "40099999")):
    """The real scan, over test_basler_scan's fake pylon (Windows faked too:
    no pnputil, no registry)."""
    from tests.test_basler_scan import FakeInfo, FakePylon, FakeTl
    real = bs.scan
    monkeypatch.setattr(bs, "scan", lambda *a, **kw: real(
        *a, pnp=lambda args: None, gentl_changed=lambda: None, **kw))
    plugin.scan_sdk = (FakePylon({"BaslerUsb": FakeTl(
        [FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A1920-160ucBAS",
                  SerialNumber=s) for s in serials], access=1)}), object())


def test_the_basler_scan_says_which_camera_of_the_rig_is_in_use(
        host, monkeypatch):
    plugin = host.instance("typhon.basler")
    basler_report(monkeypatch, plugin)
    report = plugin.setup_scan({"40012345": "cam2"})
    held = next(c for c in report.cameras if c.key == "40012345")
    line = next(c for c in held.checks if c.label == "In use by Typhon")
    assert line.detail.startswith("in use as cam2")
    free = next(c for c in report.cameras if c.key == "40099999")
    assert not any(c.label == "In use by Typhon" for c in free.checks)
    opened = plugin.scan_sdk[0].opened
    assert "40099999" in opened, "a free camera is opened and tried"
    assert "40012345" not in opened, \
        "a camera the app holds is never opened again"


# ======================================================================
# The host: the set-up read as the project's plugin list
# ======================================================================
CAMERAS = ["typhon.basler", "typhon.prophesee", "typhon.simulated"]


def test_never_set_up_every_camera_plugin_is_scanned(host):
    assert host.setup_choice() is None
    assert host.camera_plugins() == CAMERAS


@pytest.mark.parametrize("ticked, scanned", [
    (["basler"], ["typhon.basler", "typhon.simulated"]),
    (["prophesee"], ["typhon.prophesee", "typhon.simulated"]),
    (["prophesee", "basler"], CAMERAS),
    (["acme.thermal"], ["typhon.simulated"]),
])
def test_only_the_families_ticked_are_scanned(host, ticked, scanned):
    cs.save_families(setup_file(host), ticked)
    assert host.setup_choice() == ticked
    assert host.camera_plugins() == scanned, "the simulated cameras always"


def test_a_project_set_up_today_scans_as_today(host, monkeypatch):
    """camera_setup.json's single choice read as the project's plugin list:
    the same cameras and the same notes as today's scan of that choice
    (frame_camera._backends_for)."""
    without_camera_sdks(monkeypatch)
    for choice in cs.CHOICES:
        cs.save_choice(setup_file(host), choice)
        through = host.discover_cameras()
        today = cameras.discover(frame_camera._backends_for(choice))
        assert through.cameras == today.cameras, choice
        assert through.notes == today.notes, choice


def test_every_family_is_offered_ticked_or_not(host):
    cs.save_families(setup_file(host), ["prophesee"])
    families = host.camera_families()
    assert [f.key for f in families] == ["basler", "prophesee"]
    assert [f.plugin for f in families] == ["typhon.basler",
                                            "typhon.prophesee"]
    basler, evk = families
    assert basler.label == "Basler" and evk.label == "Prophesee EVK4"
    assert basler.title == cs.guide("basler").title
    assert basler.summary == cs.guide("basler").summary
    assert basler.guide(PYTHON) == cs.guide("basler", python=PYTHON)
    assert basler.scan is not None and evk.scan is None
    assert basler.scan_label == "Scan Basler cameras"
    assert "test grab" in basler.scan_tip


def test_a_family_switched_off_in_the_project_is_not_offered(host):
    pl.switch_off(host.project_dir, "typhon.prophesee")
    host.open_project(host.project_dir)
    assert [f.key for f in host.camera_families()] == ["basler"]
    assert "typhon.prophesee" not in host.camera_plugins()


def test_a_plugins_failing_guide_or_check_never_reaches_the_wizard(host):
    plugin = host.instance("typhon.prophesee")

    def boom(*_a):
        raise RuntimeError("the guide exploded")

    plugin.setup_guide = boom
    plugin.setup_checks = boom
    evk = next(f for f in host.camera_families() if f.key == "prophesee")
    assert "could not give its set-up steps" in evk.guide(PYTHON).steps[0].text
    assert "the guide exploded" in evk.guide(PYTHON).steps[0].text
    r = evk.check()
    assert r.checks[0].ok is False and "the guide exploded" in \
        r.checks[0].detail
    assert host.state("typhon.prophesee")[0] == ph.ACTIVE, \
        "a built-in camera plugin stays on"
    log = (host.project_dir / ph.ERRORS_LOG).read_text(encoding="utf-8")
    assert "the guide exploded" in log


def test_a_third_party_camera_family_is_offered_by_its_backend(host):
    """A camera plugin from someone else with a set-up (made in this process:
    third-party loading comes at P5) is a family by its dotted backend."""
    class Thermal:
        backend = "acme.thermal"
        setup_label = "Acme thermal"

        def available(self):
            return True, ""

        def discover(self):
            return []

        def setup_guide(self, python):
            return api.Guide("acme.thermal", "Acme thermal camera",
                             "For Acme's T100.", (api.Step(
                                 "Install it", command=f'"{python}" -m pip '
                                                       f'install acme'),))

        def setup_checks(self):
            return api.Readiness("acme.thermal",
                                 [api.Check("acme installed", True)])

    manifest = pm.check({
        "schema": 1, "id": "acme.thermal", "short": "acme", "name": "Acme",
        "version": "1.0.0", "api": pm.API_VERSION, "kind": "camera",
        "entry": "plugin:Thermal", "contributes": ["setup"],
        "author": "Acme", "description": "Thermal cameras",
        "licence": "MIT"}, builtin=False)
    host.adopt(manifest, Thermal)
    acme = next(f for f in host.camera_families() if f.key == "acme.thermal")
    assert acme.label == "Acme thermal" and acme.scan is None
    assert acme.check().ready
    cs.save_families(setup_file(host), ["acme.thermal"])
    assert host.camera_plugins() == ["typhon.simulated", "acme.thermal"]


# ======================================================================
# The families wizard (council_qt.widgets.camera_wizard)
# ======================================================================
def readiness(key, *checks):
    return cs.Readiness(key, list(checks))


def family(key, *, check=None, scan=None, label=None):
    choice = key if key in cs.CHOICES else None
    g = cs.guide(choice) if choice else cs.Guide(
        key, f"{key} camera", f"For {key}.",
        (cs.Step("Install it", command=f"install {key}"),))
    return cs.Family(
        key=key, plugin=f"typhon.{key}", label=label or cs.label(key),
        title=g.title, summary=g.summary,
        guide=(lambda python, c=choice, g=g: cs.guide(c, python=python)
               if c else g),
        check=check or (lambda k=key: readiness(
            k, cs.Check(f"{k} installed", True, "yes"))),
        scan=scan, scan_label=f"Scan {key} cameras" if scan else "")


def no_cameras(held):
    return bs.ScanReport(pylon_version="test")


def settle(wizard, seconds=5.0):
    deadline = time.monotonic() + seconds
    while wizard.check.readiness is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert wizard.check.readiness is not None, "the checks never came back"


def settle_scan(wizard, key, seconds=5.0):
    deadline = time.monotonic() + seconds
    while key not in wizard.check.scan_reports and \
            time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert key in wizard.check.scan_reports, "the scan never came back"


def families_wizard(qapp, *keys, current=None, families=None, held=None):
    w = cw.CameraFamiliesWizard(
        families or [family("basler", scan=no_cameras), family("prophesee")],
        python=PYTHON, current=current, held=held)
    w.restart()
    for key in keys:
        w.choose.buttons[key].setChecked(True)
    return w


def test_every_family_is_a_check_box_and_one_must_be_ticked(qapp):
    w = families_wizard(qapp)
    boxes = w.choose.buttons
    assert list(boxes) == ["basler", "prophesee"]
    assert all(type(b).__name__ == "QCheckBox" for b in boxes.values())
    assert w.chosen() == [] and not w.choose.isComplete()
    boxes["prophesee"].setChecked(True)
    boxes["basler"].setChecked(True)
    assert w.chosen() == ["basler", "prophesee"], "in the order offered"
    assert w.choice() == "basler" and w.choose.isComplete()
    assert "simulated cameras are always listed" in \
        w.choose.simulated_note.text()


def test_the_families_ticked_before_are_ticked_again(qapp):
    w = families_wizard(qapp, current=["prophesee", "acme.gone"])
    assert w.chosen() == ["prophesee"]


def test_one_family_reads_exactly_as_todays_wizard(qapp):
    """The pages of a one-family set-up are today's."""
    w = families_wizard(qapp, "basler")
    w.next()
    today = cw.CameraSetupWizard(python=PYTHON, checker=lambda c: None,
                                 scanner=lambda: no_cameras({}))
    today.restart()
    today.choose.buttons["basler"].setChecked(True)
    today.next()
    assert w.install.steps.text() == today.install.steps.text()
    assert w.install.title() == today.install.title()
    assert w.install.note.text() == today.install.note.text()
    assert w.install.copy_btn.isVisibleTo(w.install)
    w.install.copy_commands()
    assert QGuiApplication.clipboard().text() == \
        f'"{PYTHON}" -m pip install pypylon'


def test_several_families_each_get_their_steps_and_a_copy_button(qapp):
    w = families_wizard(qapp, "basler", "prophesee")
    w.next()
    text = w.install.steps.text()
    assert "CoaXPress" in text and "Metavision" in text
    assert text.index(cs.guide("basler").title) < \
        text.index(cs.guide("prophesee").title)
    assert not w.install.copy_btn.isVisibleTo(w.install)
    assert sorted(w.install.copy_buttons) == ["basler", "prophesee"]
    w.install.copy_buttons["prophesee"].click()
    assert QGuiApplication.clipboard().text().splitlines() == \
        list(cs.WDI_COMMANDS), "an EVK4's commands, one per line, alone"
    w.install.copy_buttons["basler"].click()
    assert QGuiApplication.clipboard().text() == \
        f'"{PYTHON}" -m pip install pypylon'


def test_every_step_of_several_families_can_be_scrolled_to(qapp):
    """Drawn at the wizard's size, a Basler's and an EVK4's steps are longer
    than the page: before they scrolled, the EVK4's driver commands were
    below the bottom edge and out of reach (seen in an offscreen render)."""
    if qapp.platformName() != "offscreen":
        pytest.skip("drawn only on the offscreen platform, never on a screen")
    w = families_wizard(qapp, "basler", "prophesee")
    w.resize(760, 640)
    w.show()                       # offscreen: drawn in memory only
    try:
        w.next()
        for _ in range(20):
            QApplication.processEvents()
            time.sleep(0.01)
        area = w.install.steps_area
        bar = area.verticalScrollBar()
        assert "wdi-simple" in w.install.steps.text()
        assert bar.maximum() > 0, "longer than the page, so it scrolls"
        assert w.install.steps.height() <= \
            area.viewport().height() + bar.maximum(), "every line reachable"
    finally:
        w.hide()
        w.deleteLater()


def test_going_back_and_unticking_replaces_the_steps(qapp):
    w = families_wizard(qapp, "basler", "prophesee")
    w.next()
    w.back()
    w.choose.buttons["basler"].setChecked(False)
    w.next()
    text = w.install.steps.text()
    assert "Metavision" in text and "CoaXPress" not in text
    assert w.install.copy_buttons == {}


def test_each_familys_checks_run_off_the_ui_thread(qapp):
    seen = {}

    def check_for(key):
        def check():
            seen[key] = threading.current_thread().name
            return readiness(key, cs.Check(f"{key} SDK", None, "maybe",
                                           f"fix {key}"))
        return check

    w = families_wizard(qapp, "basler", "prophesee", families=[
        family("basler", check=check_for("basler")),
        family("prophesee", check=check_for("prophesee"))])
    w.next()
    w.next()
    settle(w)
    assert set(seen) == {"basler", "prophesee"}
    assert threading.main_thread().name not in seen.values()
    assert set(w.check.readiness) == {"basler", "prophesee"}
    text = w.check.results.text()
    assert "fix basler" in text and "fix prophesee" in text
    assert "⚠" in text and "Check" in text
    summary = w.check.summary.text()
    assert "Basler:" in summary and "Prophesee EVK4:" in summary
    assert w.check.isComplete()


def test_a_familys_check_that_crashes_is_reported_not_raised(qapp):
    def boom():
        raise RuntimeError("the SDK exploded")

    w = families_wizard(qapp, "prophesee", families=[
        family("basler"), family("prophesee", check=boom)])
    w.next()
    w.next()
    settle(w)
    assert w.check.readiness["prophesee"].checks[0].ok is False
    assert "the SDK exploded" in w.check.results.text()


def test_only_a_family_with_a_scan_page_scans_and_it_is_told_what_is_held(
        qapp):
    seen = {}

    def scanner(held):
        seen["held"] = dict(held)
        seen["thread"] = threading.current_thread().name
        report = bs.ScanReport(pylon_version="12.3")
        cam = bs.CameraReport("40012345", "boA5320-150cm", "boost",
                              "40012345", "CoaXPress", bs.CXP_CLASS, "free")
        cam.checks = [bs.Check(bs.PASS, "In use by Typhon",
                               f"in use as {held['40012345']} — connected")]
        report.cameras = [cam]
        return report

    w = families_wizard(qapp, "basler", "prophesee", families=[
        family("basler", scan=scanner), family("prophesee")],
        held=lambda: {"40012345": "cam2"})
    w.next()
    w.next()
    settle_scan(w, "basler")
    assert list(w.check.scan_views) == ["basler"]
    assert seen["held"] == {"40012345": "cam2"}
    assert seen["thread"] != threading.main_thread().name
    assert "in use as cam2" in w.check.scan_views["basler"].toPlainText()
    assert w.check.scan_buttons["basler"].text() == "Scan basler cameras"
    w.check.scan_buttons["basler"].click()
    settle_scan(w, "basler")


def test_a_scan_that_crashes_is_reported_not_raised(qapp):
    def scanner(held):
        raise RuntimeError("pylon exploded")

    w = families_wizard(qapp, "basler", families=[
        family("basler", scan=scanner)])
    w.next()
    w.next()
    settle_scan(w, "basler")
    assert "pylon exploded" in w.check.scan_views["basler"].toPlainText()
    assert w.check.scan_buttons["basler"].isEnabled()


def test_a_check_from_an_earlier_visit_is_never_shown_on_this_one(qapp):
    release = threading.Event()

    def slow():
        release.wait(5)
        return readiness("basler", cs.Check("OLD BASLER RESULT", True))

    w = families_wizard(qapp, "basler", families=[
        family("basler", check=slow), family("prophesee")])
    w.next()
    w.next()
    w.back()
    w.back()
    w.choose.buttons["basler"].setChecked(False)
    w.choose.buttons["prophesee"].setChecked(True)
    w.next()
    w.next()
    settle(w)
    release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    assert set(w.check.readiness) == {"prophesee"}
    assert "OLD BASLER RESULT" not in w.check.results.text()


def test_finishing_saves_the_families_ticked(qapp, tmp_path):
    path = cs.setup_path(tmp_path)

    def finish(wizard):
        wizard.choose.buttons["prophesee"].setChecked(True)
        wizard.choose.buttons["basler"].setChecked(True)
        return 1

    got = cw.run_families_wizard(None, path, [family("basler"),
                                              family("prophesee")],
                                 execute=finish)
    assert got == ["basler", "prophesee"]
    assert cs.load_families(path) == ["basler", "prophesee"]
    assert cs.load_choice(path) == "basler", "today's reader"


def test_cancelling_saves_nothing(qapp, tmp_path):
    path = cs.setup_path(tmp_path)

    def cancel(wizard):
        wizard.choose.buttons["basler"].setChecked(True)
        return 0

    assert cw.run_families_wizard(None, path, [family("basler")],
                                  execute=cancel) is None
    assert not path.exists()


def test_a_project_set_up_today_starts_from_its_one_choice(qapp, tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "prophesee")
    seen = {}

    def peek(wizard):
        seen["ticked"] = wizard.chosen()
        return 0

    cw.run_families_wizard(None, path, [family("basler"),
                                        family("prophesee")], execute=peek)
    assert seen["ticked"] == ["prophesee"]
    assert cs.load_choice(path) == "prophesee", "cancelled: unchanged"


def test_a_family_ticked_before_and_not_offered_now_stays_ticked(
        qapp, tmp_path):
    """Its camera plugin is switched off in the project's list, so the
    wizard has no check box for it: the answer it never asked about is not
    lost, and the family is scanned again once its plugin is back on."""
    path = cs.setup_path(tmp_path)
    cs.save_families(path, ["acme.thermal", "prophesee"])

    def basler_only(wizard):
        assert list(wizard.choose.buttons) == ["basler", "prophesee"]
        assert wizard.chosen() == ["prophesee"]
        wizard.choose.buttons["prophesee"].setChecked(False)
        wizard.choose.buttons["basler"].setChecked(True)
        return 1

    got = cw.run_families_wizard(None, path, [family("basler"),
                                              family("prophesee")],
                                 execute=basler_only)
    assert got == ["basler"], "what was ticked in the wizard"
    assert cs.load_families(path) == ["basler", "acme.thermal"]
    assert cs.load_choice(path) == "basler", "today's reader"


# ======================================================================
# frame_camera in a "Typhon (plugins)" project
# ======================================================================
@pytest.fixture
def plugin_project(tmp_path, home, monkeypatch):
    """A "Typhon (plugins)" project attached to frame_camera, with neither
    camera SDK importable; the rig's host, slots and set-up path are put
    back afterwards."""
    without_camera_sdks(monkeypatch)
    project = make_plugin_project(tmp_path / "project", home)
    frame_camera.disconnect()
    rig = frame_camera._RIG
    first = rig.slots[0]
    kept = (rig._plugins, rig.setup_path, rig.found, first.name)
    rig._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(project)
    frame_camera._LIVE.found = None
    yield project
    frame_camera.disconnect()
    rig.select(first.name)
    for slot in list(rig)[1:]:
        if getattr(slot, "device", None) is not None:
            slot.device.close()
            slot.clear()
        rig.remove_slot(slot.name)
    if rig._plugins is not None:
        rig._plugins.close()
    rig._plugins, rig.setup_path, rig.found = kept[:3]
    if first.name != kept[3]:
        rig.rename_slot(first.name, kept[3])


@pytest.fixture
def classic_project(tmp_path):
    folder = tmp_path / "classic"
    folder.mkdir()
    return folder


def scan_in(project):
    frame_camera._LIVE.setup_path = cs.setup_path(project)
    return frame_camera.list_cameras()


def test_the_plugin_list_file_is_named_as_the_lists_name_it():
    assert frame_camera._PLUGIN_LIST_FILE == pl.PROJECT_FILE


@pytest.mark.parametrize("choice", [None, "basler", "prophesee"])
def test_its_scan_is_todays_scan_for_the_same_set_up(
        plugin_project, classic_project, choice):
    for project in (plugin_project, classic_project):
        if choice:
            cs.save_choice(cs.setup_path(project), choice)
    today = scan_in(classic_project)
    assert frame_camera._RIG._plugins is None, "today's Typhon: no host"
    through = scan_in(plugin_project)
    host = frame_camera._RIG._plugins
    assert host is not None and host.project_dir == plugin_project
    assert through == today


def test_a_project_set_up_today_opens_unchanged(plugin_project):
    """Not asked its set-up questions again; scans that one family."""
    cs.save_choice(cs.setup_path(plugin_project), "basler")
    assert not frame_camera._needs_setup()
    assert frame_camera.current_families() == ["basler"]
    notes = " ".join(frame_camera.list_cameras()["notes"]).lower()
    assert "pypylon" in notes and "metavision" not in notes


def test_a_project_never_set_up_is_asked_and_searches_everything(
        plugin_project):
    assert frame_camera._needs_setup()
    notes = " ".join(frame_camera.list_cameras()["notes"]).lower()
    assert "pypylon" in notes and "metavision" in notes


def test_a_project_with_no_family_to_tick_is_not_asked(plugin_project):
    """Every camera plugin with a set-up switched off: only the simulated
    cameras are left, which need none, so the first-run question would
    have nothing to tick, and is not asked at every start."""
    for pid in ("typhon.basler", "typhon.prophesee"):
        pl.switch_off(plugin_project, pid)
    assert not frame_camera._needs_setup()
    assert frame_camera._RIG.plugins.family_plugins() == []
    assert not cs.setup_path(plugin_project).exists(), "nothing written"
    rows = frame_camera.list_cameras()["rows"]
    assert rows and all("Simulated" in r for r in rows)
    pl.switch_on(plugin_project, "typhon.basler")
    frame_camera._RIG.plugins.open_project(plugin_project)
    assert frame_camera._needs_setup(), "a family to tick: asked"


def test_several_families_are_all_scanned(plugin_project):
    cs.save_families(cs.setup_path(plugin_project), ["prophesee", "basler"])
    assert not frame_camera._needs_setup()
    listed = frame_camera.list_cameras()
    notes = " ".join(listed["notes"]).lower()
    assert "pypylon" in notes and "metavision" in notes
    assert any("Simulated" in r for r in listed["rows"])


def test_today_typhon_reads_a_families_file_and_never_makes_the_host(
        classic_project):
    rig = frame_camera._RIG
    kept = rig._plugins
    rig._plugins = None
    try:
        cs.save_families(cs.setup_path(classic_project),
                         ["prophesee", "basler"])
        frame_camera._LIVE.setup_path = cs.setup_path(classic_project)
        assert frame_camera.current_choice() == "prophesee"
        assert not frame_camera._needs_setup()
        frame_camera.list_cameras()
        assert rig._plugins is None
    finally:
        frame_camera._LIVE.setup_path = None
        rig._plugins = kept


def connect_sim(kind="frame"):
    rows = frame_camera.list_cameras()["rows"]
    return frame_camera.connect(simulated_row(rows, kind))


def test_connect_opens_through_the_camera_plugin_and_names_the_camera(
        plugin_project):
    said = connect_sim("frame")
    assert said["summary"].startswith("Connected")
    assert "Note" not in said["summary"]
    slot = frame_camera._RIG.active
    assert type(slot.device) is cameras.SyntheticDevice
    assert slot.confirmed is not None and slot.confirmed[0] is slot.device, \
        "the plugin confirmed what the camera can do onto its slot"
    assert "software_trigger" in slot.capabilities
    store = rf.RigFile(rf.rig_path(plugin_project))
    assert store.name_of(slot.info) == "cam1" == slot.name


def test_the_same_camera_gets_the_same_name_next_time(plugin_project):
    connect_sim("frame")
    frame_camera.disconnect()
    connect_sim("event")
    assert frame_camera._RIG.active.name == "cam2", \
        "cam1 is the frame camera's"
    frame_camera.disconnect()
    connect_sim("frame")
    assert frame_camera._RIG.active.name == "cam1"
    names = {c.name: c.key for c in
             rf.RigFile(rf.rig_path(plugin_project)).cameras()}
    assert names == {"cam1": "sim-frame", "cam2": "sim-event"}


def test_a_camera_is_renamed_in_the_rig_and_keeps_it_next_time(
        plugin_project):
    connect_sim("frame")
    host = frame_camera._RIG.plugins
    store = rf.RigFile(rf.rig_path(plugin_project))
    store.save_rig_preset("Bright", {"cam1": "Daylight"})
    assert host.rename_camera("cam1", "left") == "left"
    assert frame_camera._RIG.active.name == "left"
    assert store.name_of(frame_camera._RIG.active.info) == "left"
    assert store.rig_presets() == {"Bright": {"left": "Daylight"}}, \
        "its rig presets follow it"
    frame_camera.disconnect()
    connect_sim("event")
    assert frame_camera._RIG.active.name == "cam1", "a new camera: cam1"
    frame_camera.disconnect()
    connect_sim("frame")
    assert frame_camera._RIG.active.name == "left", "the same name next time"


def test_a_rename_that_cannot_be_is_refused_and_changes_nothing(
        plugin_project):
    connect_sim("frame")
    frame_camera.disconnect()
    connect_sim("event")                              # cam2 in the rig file
    host = frame_camera._RIG.plugins
    path = rf.rig_path(plugin_project)
    before = path.read_bytes()
    for bad, words in (("Left Cam", "cannot name a camera"),
                       ("cam1", "another camera of this rig")):
        with pytest.raises(rf.RigError, match=words):
            host.rename_camera("cam2", bad)
        assert frame_camera._RIG.active.name == "cam2"
        assert path.read_bytes() == before
    frame_camera._RIG.add_slot("right")
    with pytest.raises(rf.RigError, match="already named 'right'"):
        host.rename_camera("cam2", "right")
    with pytest.raises(rf.RigError, match="no camera open"):
        host.rename_camera("right", "far")
    path.write_text("{damaged", encoding="utf-8")
    with pytest.raises(rf.RigFileError):
        host.rename_camera("cam2", "near")
    assert frame_camera._RIG.active.name == "cam2"
    assert path.read_text(encoding="utf-8") == "{damaged", "left as it is"
    with pytest.raises(KeyError):
        host.rename_camera("nobody", "near")


def test_a_damaged_rig_file_is_said_left_alone_and_never_in_the_way(
        plugin_project):
    path = rf.rig_path(plugin_project)
    path.write_text("{damaged", encoding="utf-8")
    said = connect_sim("frame")["summary"]
    assert said.startswith("Connected") and "keeps the name cam1" in said
    assert frame_camera._RIG.active.device is not None
    assert path.read_text(encoding="utf-8") == "{damaged"


def test_a_camera_another_slot_holds_is_in_use_as_its_name(plugin_project):
    """With a second camera slot (multi-camera), the scan marks a camera the
    window has open "in use as cam2", and Connect refuses it in another
    slot before anything opens."""
    rig = frame_camera._RIG
    rows = frame_camera.list_cameras()["rows"]
    assert not any("in use" in r for r in rows)
    second = rig.add_slot("cam2")
    host = rig.plugins
    event = next(i for i in rig.found.cameras if i.kind == "event")
    second.device = host.open_camera(event, camera="cam2")
    second.info = event
    listed = frame_camera.list_cameras()["rows"]
    marked = [r for r in listed if "in use as" in r]
    assert len(marked) == 1 and marked[0].endswith("· in use as cam2")
    assert "Simulated event camera" in marked[0]
    with pytest.raises(RuntimeError, match="in use as cam2"):
        frame_camera.connect(marked[0])
    assert rig.active.device is None, "nothing was opened"
    connect_sim("frame")
    rows = frame_camera.list_cameras()["rows"]
    assert any(r.endswith("frame · in use as cam1") for r in rows)


def test_a_one_camera_window_s_rows_read_as_they_always_have(plugin_project):
    before = frame_camera.list_cameras()["rows"]
    connect_sim("frame")
    assert frame_camera.list_cameras()["rows"] == before


def test_the_scan_is_told_about_every_open_camera_and_its_name(
        plugin_project):
    rig = frame_camera._RIG
    connect_sim("frame")
    second = rig.add_slot("cam2")
    event = next(i for i in rig.found.cameras if i.kind == "event")
    second.device = rig.plugins.open_camera(event, camera="cam2")
    second.info = event
    frame_camera._tell_the_scan_what_is_open()
    assert cw.held_keys() == ["sim-frame", "sim-event"]
    assert cw.held_names() == {"sim-frame": "cam1", "sim-event": "cam2"}


def test_setup_runs_the_families_wizard_and_says_what_was_ticked(
        plugin_project, monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    seen = {}

    def fake(parent, path, families, **kw):
        seen["families"] = [f.key for f in families]
        seen["held"] = kw.get("held")
        return cs.save_families(path, ["basler", "prophesee"])

    monkeypatch.setattr(cw, "run_families_wizard", fake)
    monkeypatch.setattr(cw, "run_wizard", lambda *a, **k: pytest.fail(
        "today's single-choice wizard in a plugins project"))
    out = frame_camera.setup()
    assert seen["families"] == ["basler", "prophesee"]
    assert seen["held"] is frame_camera._held_names
    assert out["summary"].startswith(
        "Set up for Basler and Prophesee EVK4.")
    assert frame_camera.current_families() == ["basler", "prophesee"]
    assert out["rows"]


def test_a_cancelled_families_setup_says_nothing_changed(plugin_project,
                                                          monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    monkeypatch.setattr(cw, "run_families_wizard", lambda *a, **k: None)
    assert "nothing changed" in frame_camera.setup()["summary"]
    assert not cs.setup_path(plugin_project).exists()


def test_todays_typhon_still_gets_todays_wizard(classic_project, monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    frame_camera._LIVE.setup_path = cs.setup_path(classic_project)
    monkeypatch.setattr(cw, "run_families_wizard", lambda *a, **k:
                        pytest.fail("the families wizard in today's Typhon"))
    monkeypatch.setattr(cw, "run_wizard", lambda parent, p, **kw: (
        cs.save_choice(p, "basler") or "basler"))
    try:
        assert "Set up for Basler." in frame_camera.setup()["summary"]
    finally:
        frame_camera._LIVE.setup_path = None


NO_QT_PLUGINS_APP = """
import os, sys
sys.path.insert(0, {repo!r})
os.environ.pop("COUNCIL_NO_DIALOGS", None)
import frame_camera
frame_camera._LIVE.setup_path = {setup!r}
print("SETUP", frame_camera.setup()["summary"], flush=True)
print("HOST", frame_camera._RIG._plugins is not None, flush=True)
"""


def test_a_plugins_project_with_no_qt_application_says_so_instead_of_dying(
        tmp_path, home):
    project = make_plugin_project(tmp_path / "project", home)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env.pop("COUNCIL_NO_DIALOGS", None)
    env[pl.HOME_ENV] = str(home)
    done = subprocess.run(
        [sys.executable, "-c", NO_QT_PLUGINS_APP.format(
            repo=str(ROOT), setup=str(cs.setup_path(project)))],
        env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.stdout, done.stderr)
    lines = dict(line.split(" ", 1) for line in done.stdout.splitlines()
                 if " " in line)
    assert "Qt" in lines["SETUP"] and lines["HOST"] == "True"
    assert not cs.setup_path(project).exists()


# ======================================================================
# The built "Typhon (plugins)" app: the first-run wizard
# ======================================================================
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import run_example_gui as rex
    vault = tmp_path_factory.mktemp("p2b_vault")
    return rex.build("typhon_plugins", project="typhon_plugins_setup",
                     vault_dir=vault, target="qt")


@pytest.fixture
def forget_generated():
    kept = list(sys.path)
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = None
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def construct(pdir):
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    return generated.App()


def first_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(frame_camera, "_first_run",
                        lambda app: seen.append(app))
    return seen


def pump(condition, seconds):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def test_the_built_app_has_a_plugin_list_and_asks_until_set_up(
        qapp, built, monkeypatch, forget_generated):
    assert (built / frame_camera._PLUGIN_LIST_FILE).is_file()
    cs.setup_path(built).unlink(missing_ok=True)
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    seen = first_runs(monkeypatch)
    ui = construct(built)
    pump(lambda: seen, 3.0)
    assert seen == [ui]
    ui.close()


def test_the_built_app_set_up_today_is_not_asked_again(
        qapp, built, monkeypatch, forget_generated):
    cs.save_choice(cs.setup_path(built), "basler")
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    seen = first_runs(monkeypatch)
    ui = construct(built)
    pump(lambda: seen, 0.3)
    assert seen == []
    ui.close()
