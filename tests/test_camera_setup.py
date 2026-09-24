"""
Tests for council_core.camera_setup — the guides, the live checks, and the
saved choice behind the capture apps' setup wizard.

The fakes model what the REAL SDKs reported when these checks were run
against them: the pylon runtime inside the pypylon pip wheel offers "USB,
GigE" (plus "Camera Emulation" under PYLON_CAMEMU) and no CoaXPress layer at
all, and a Metavision install with nothing plugged in lists no devices.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import camera_setup as cs


# ======================================================================
# Fakes shaped like the measured SDKs
# ======================================================================
class FakeTl:
    def __init__(self, name, kind, cls):
        self._name, self._kind, self._cls = name, kind, cls

    def GetFriendlyName(self):
        return self._name

    def GetTLType(self):
        return self._kind

    def GetDeviceClass(self):
        return self._cls

    def GetFullName(self):
        return f"{self._name}/{self._cls}"


class FakeInfo:
    def __init__(self, serial, model):
        self._serial, self._model = serial, model

    def GetSerialNumber(self):
        return self._serial

    def GetModelName(self):
        return self._model

    def GetVendorName(self):
        return "Basler"

    def GetFullName(self):
        return f"Basler {self._model} {self._serial}"


#: What pip-installed pylon 12.3 actually offered, measured.
PIP_WHEEL_TLS = [FakeTl("USB", "U3V", "BaslerUsb"),
                 FakeTl("GigE", "GEV", "BaslerGigE")]
CXP_TL = FakeTl("CoaXPress", "CXP", "BaslerGenTlCxp")


class FakePylon:
    def __init__(self, tls=None, devices=(), tl_error=None, enum_error=None):
        self._tls = PIP_WHEEL_TLS if tls is None else tls
        self._devices = list(devices)
        self._tl_error, self._enum_error = tl_error, enum_error
        outer = self

        class _Factory:
            @staticmethod
            def GetInstance():
                return outer

        self.TlFactory = _Factory

    def GetPylonVersionString(self):
        return "12.3.0.1362"

    def EnumerateTls(self):
        if self._tl_error:
            raise self._tl_error
        return self._tls

    def EnumerateDevices(self):
        if self._enum_error:
            raise self._enum_error
        return self._devices


class FakeHal:
    class DeviceDiscovery:
        serials = []

        @classmethod
        def list(cls):
            return cls.serials


def fake_hal(serials=()):
    hal = FakeHal()
    hal.DeviceDiscovery = type("DD", (), {
        "list": staticmethod(lambda: list(serials))})
    return hal


def by_label(readiness, label):
    return next(c for c in readiness.checks if c.label == label)


# ======================================================================
# Guides
# ======================================================================
def test_there_is_a_guide_for_every_choice():
    for choice in cs.CHOICES:
        g = cs.guide(choice, python="C:/py/python.exe")
        assert g.steps, f"no steps for {choice}"
        assert g.title and g.summary


def test_an_unknown_camera_is_refused():
    with pytest.raises(ValueError):
        cs.guide("hasselblad")


def test_the_basler_guide_names_coaxpress():
    """The step the pip wheel cannot do for you."""
    text = " ".join(s.text for s in cs.guide("basler").steps)
    assert "CoaXPress" in text and "CXP" in text


def test_the_install_command_targets_the_python_the_app_runs_under():
    """`pip install` in a terminal installs into the TERMINAL's Python.

    The package is then "installed" and still missing from the app.
    """
    python = r"C:\envs\pylon\python.exe"
    commands = [s.command for s in cs.guide("basler", python=python).steps
                if s.command]
    assert commands == [f'"{python}" -m pip install pypylon']


def test_every_link_is_https_and_goes_to_the_vendor():
    for choice, host in (("basler", "baslerweb.com"),
                         ("prophesee", "docs.prophesee.ai")):
        urls = [s.url for s in cs.guide(choice).steps if s.url]
        assert urls, f"no links for {choice}"
        for url in urls:
            assert url.startswith("https://") and host in url, url


def test_the_prophesee_guide_names_the_running_python_version():
    """The bindings import under exactly one Python version."""
    version = ".".join(str(v) for v in sys.version_info[:2])
    text = " ".join(s.text for s in cs.guide("prophesee").steps)
    assert f"Python {version}" in text


def test_the_evk4_guide_carries_the_usb_driver_step():
    """Windows has no driver for an EVK4, and without one the camera is not
    found — silently, exactly as if unplugged. Building OpenEB does not
    install it."""
    steps = cs.guide("prophesee").steps
    driver = next(s for s in steps if "wdi-simple" in s.text)
    assert "administrator" in driver.text
    assert driver.url.endswith("windows_openeb.html")
    # All three product IDs, as Prophesee's page gives them.
    for pid in ("0x00f4", "0x00f5", "0x00f3"):
        assert f"-v 0x04b4 -p {pid}" in driver.command
    assert driver.command.count("\n") == 2, "one command per line"


def test_the_evk4_guide_says_the_installer_needs_an_account():
    """The first thing a user hits on the installer route."""
    assert "account" in cs.guide("prophesee").steps[0].text


@pytest.mark.parametrize("version", cs.SDK_PYTHONS)
def test_a_supported_python_is_said_to_be_supported(version):
    text = " ".join(s.text for s in cs.guide("prophesee",
                                             version=version).steps)
    assert f"Python {version}" in text and "this one is supported" in text


def test_an_unsupported_python_is_called_out(version="3.13"):
    """The bindings will not import under it, installed or not."""
    text = " ".join(s.text for s in cs.guide("prophesee",
                                             version=version).steps)
    assert "NOT SUPPORTED" in text


def test_the_basler_guide_mentions_the_emulator_for_trying_it_without_hardware():
    assert "PYLON_CAMEMU" in cs.guide("basler").note


# ======================================================================
# Basler checks
# ======================================================================
def test_no_pypylon_is_a_failure_that_says_which_step_fixes_it():
    import council_core.cameras as cameras

    class Missing(cameras.BaslerBackend):
        def sdk(self):
            raise cameras.CameraError("pypylon is not installed")

    original = cameras.BaslerBackend
    cameras.BaslerBackend = Missing
    try:
        r = cs.check("basler")
    finally:
        cameras.BaslerBackend = original
    assert not r.ready
    assert r.checks[0].ok is False
    assert "step 3" in r.checks[0].fix
    assert "step 3" in r.summary()


def test_the_pip_wheel_alone_is_flagged_for_coaxpress():
    """The measured case: USB and GigE layers, no CoaXPress.

    A WARNING, not a failure — the same pylon runs a USB camera perfectly.
    """
    r = cs.check("basler", basler_sdk=FakePylon())
    cxp = by_label(r, "CoaXPress support")
    assert cxp.ok is None
    assert "USB, GigE" in cxp.detail
    assert "boA5320" in cxp.fix
    assert r.ready, "a warning must not block"


def test_a_coaxpress_layer_is_recognised():
    r = cs.check("basler", basler_sdk=FakePylon(tls=PIP_WHEEL_TLS + [CXP_TL]))
    assert by_label(r, "CoaXPress support").ok is True


def test_a_pylon_that_will_not_list_its_layers_is_a_warning_not_a_crash():
    r = cs.check("basler",
                 basler_sdk=FakePylon(tl_error=RuntimeError("no factory")))
    assert by_label(r, "CoaXPress support").ok is None


def test_a_found_camera_makes_it_ready():
    pylon = FakePylon(devices=[FakeInfo("40012345", "boA5320-150cm")])
    r = cs.check("basler", basler_sdk=pylon)
    found = by_label(r, cs.CAMERAS_FOUND)
    assert found.ok is True and "boA5320-150cm" in found.detail
    assert r.found_camera
    assert "Ready" in r.summary()


def test_an_emulated_camera_is_labelled_as_one():
    """PYLON_CAMEMU must never pass for a real camera."""
    pylon = FakePylon(devices=[FakeInfo("0815-0000", "Emulation")])
    r = cs.check("basler", basler_sdk=pylon)
    assert "(emulated)" in by_label(r, cs.CAMERAS_FOUND).detail


def test_no_camera_yet_is_a_warning_while_setting_up():
    """Unplugged is the normal state mid-setup; the software may be fine."""
    r = cs.check("basler", basler_sdk=FakePylon())
    found = by_label(r, cs.CAMERAS_FOUND)
    assert found.ok is None
    assert "Connect the camera" in r.summary()


def test_an_enumeration_that_throws_is_a_failure():
    r = cs.check("basler",
                 basler_sdk=FakePylon(enum_error=RuntimeError("TL crashed")))
    assert by_label(r, cs.CAMERAS_FOUND).ok is False
    assert not r.ready


# ======================================================================
# Prophesee checks
# ======================================================================
def test_no_metavision_names_the_python_version_as_a_possible_cause():
    """Installed-but-wrong-Python looks identical to not installed."""
    import council_core.cameras as cameras

    class Missing(cameras.EvkBackend):
        def sdk(self):
            raise cameras.CameraError("No module named 'metavision_hal'")

    original = cameras.EvkBackend
    cameras.EvkBackend = Missing
    try:
        r = cs.check("prophesee")
    finally:
        cameras.EvkBackend = original
    version = ".".join(str(v) for v in sys.version_info[:2])
    assert r.checks[0].ok is False
    assert version in r.checks[0].fix


def test_metavision_with_nothing_plugged_in():
    """Measured against a real OpenEB build: list() is [] with no camera."""
    r = cs.check("prophesee", evk_sdk=fake_hal())
    assert r.checks[0].ok is True
    found = by_label(r, cs.CAMERAS_FOUND)
    assert found.ok is None
    assert "USB 3" in found.fix
    # The SDK cannot tell "no driver" from "no camera"; the advice must — and
    # must SAY the driver is the likely cause. Checking only for the word
    # "driver" passed with that sentence removed, because the next one still
    # mentions a driver in passing.
    assert "USB driver is the likely cause" in found.fix
    assert "step 4" in found.fix


def test_an_evk4_that_enumerates_is_found():
    r = cs.check("prophesee", evk_sdk=fake_hal(["00050423"]))
    assert r.found_camera


def test_checking_an_unknown_camera_is_refused():
    with pytest.raises(ValueError):
        cs.check("hasselblad")


# ======================================================================
# The saved choice
# ======================================================================
def test_a_saved_choice_reads_back(tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "prophesee")
    assert cs.load_choice(path) == "prophesee"


def test_no_file_means_ask(tmp_path):
    assert cs.load_choice(cs.setup_path(tmp_path)) is None


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps(["basler"]),
    json.dumps({"format": 99, "camera": "basler"}),
    json.dumps({"format": cs.FORMAT, "camera": "hasselblad"}),
])
def test_anything_untrustworthy_means_ask_again(tmp_path, content):
    """Not an error that stops the app starting: just ask."""
    path = cs.setup_path(tmp_path)
    path.write_text(content, encoding="utf-8")
    assert cs.load_choice(path) is None


def test_an_unknown_camera_is_never_saved(tmp_path):
    with pytest.raises(ValueError):
        cs.save_choice(cs.setup_path(tmp_path), "hasselblad")
    assert not cs.setup_path(tmp_path).exists()


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "basler")
    assert sorted(p.name for p in tmp_path.iterdir()) == [cs.SETUP_FILE]


def test_saving_again_replaces_the_answer(tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "basler")
    cs.save_choice(path, "prophesee")
    assert cs.load_choice(path) == "prophesee"
