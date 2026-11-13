"""
council_core.pylon_env: pypylon finding a CoaXPress camera's GenTL producer,
and saying what it sees when it finds no camera (Andy, 13 November 2026: a
boA9344-30cm on a CXP grabber worked in pylon Viewer and Typhon listed none).

Temporary folders and a fake pylon only; nothing outside this process's
environment is changed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import cameras  # noqa: E402
from council_core import pylon_env as pe  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(pe.ENV, raising=False)
    monkeypatch.delenv(pe.OFF_ENV, raising=False)
    monkeypatch.setattr(pe, "_prepared", None)


def producer(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ProducerCXP.cti").write_bytes(b"")
    return folder


def test_the_machine_s_gentl_path_is_added_to_this_process(tmp_path):
    cxp = producer(tmp_path / "cxp")
    added = pe.prepare(registry=[str(cxp)], roots=[])
    assert added == [str(cxp)]
    assert os.environ[pe.ENV].split(os.pathsep) == [str(cxp)]
    assert pe.prepare(registry=[str(cxp)], roots=[]) == [str(cxp)], "once"
    pe.prepare(registry=[str(cxp)], roots=[], force=True)
    assert os.environ[pe.ENV].split(os.pathsep) == [str(cxp)], "not twice"


def test_a_producer_under_program_files_basler_is_found(tmp_path):
    cxp = producer(tmp_path / "Basler" / "pylon 8" / "Runtime" / "x64"
                   / "CXP")
    (tmp_path / "Basler" / "pylon 8" / "readme.txt").parent.mkdir(
        parents=True, exist_ok=True)
    assert pe.prepare(registry=[], roots=[tmp_path]) == [str(cxp)]
    assert str(cxp) in os.environ[pe.ENV]


def test_a_path_that_already_has_a_producer_is_left_as_it_is(
        tmp_path, monkeypatch):
    mine = producer(tmp_path / "mine")
    producer(tmp_path / "Basler" / "other")
    monkeypatch.setenv(pe.ENV, str(mine))
    assert pe.prepare(registry=[], roots=[tmp_path]) == []
    assert os.environ[pe.ENV] == str(mine)


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv(pe.OFF_ENV, "1")
    cxp = producer(tmp_path / "cxp")
    assert pe.prepare(registry=[str(cxp)], roots=[]) == []
    assert pe.ENV not in os.environ


class _Named:
    def __init__(self, name):
        self.name = name

    def GetFriendlyName(self):
        return self.name


class FakeFactory:
    def __init__(self, tls, devices):
        self.tls, self.devices = tls, devices

    def EnumerateTls(self):
        return [_Named(t) for t in self.tls]

    def EnumerateDevices(self):
        return [_Named(d) for d in self.devices]


class FakePylon:
    def __init__(self, tls, devices=()):
        factory = FakeFactory(tls, list(devices))

        class TlFactory:
            @staticmethod
            def GetInstance():
                return factory
        self.TlFactory = TlFactory


def test_the_report_names_the_transport_layers_and_a_missing_cxp_one():
    rep = pe.report(FakePylon(["USB", "GigE"]))
    assert rep["transport_layers"] == ["USB", "GigE"] and rep["devices"] == []
    said = "\n".join(pe.words(rep))
    assert "transport layers USB, GigE" in said
    assert "cameras pylon sees: 0" in said
    assert "no CoaXPress" in said and "CXP Camera Support" in said
    assert f"{pe.ENV}: not set" in said
    ok = "\n".join(pe.words(pe.report(FakePylon(
        ["USB", "CoaXPress"], ["Basler boA9344-30cm (123)"]))))
    assert "no CoaXPress" not in ok and "boA9344-30cm" in ok


def test_typhon_s_no_camera_note_says_what_pylon_sees():
    backend = cameras.BaslerBackend(FakePylon(["USB", "GigE"]))
    found = cameras.discover([backend])
    notes = "\n".join(found.notes)
    assert not found.cameras
    assert "basler: pypylon" in notes and "transport layers USB, GigE" in notes
    assert "no CoaXPress" in notes


def test_each_camera_is_opened_and_its_trigger_inputs_said():
    from tests.test_basler_trigger import FakePylon as TriggerPylon
    from tests.test_typhon_plugins_basler_lines import LevelBasler
    said = "\n".join(pe.full_lines(TriggerPylon(LevelBasler())))
    assert "Python " in said and " at " in said, "which Python"
    assert "camera: Basler" in said and "opens: sensor" in said
    assert "input lines: Line1" in said
    assert "CxpTrigger0" in said and "grabber" in said
    assert "Typhon can watch (snapshot/record): Line1" in said


def test_the_command_prompt_gets_the_camera_list(capsys, monkeypatch):
    monkeypatch.delenv(pe.QUIET_ENV, raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import frame_camera
    out = frame_camera.list_cameras()
    said = capsys.readouterr()
    # stderr, not stdout: tests and tools that read the app's printed
    # output (its last lines, as JSON) must not get these lines in it.
    assert pe.PREFIX not in said.out
    printed = said.err
    assert f"{pe.PREFIX} {out['summary']}" in printed
    assert "note:" in printed
    # Plain ASCII: a cp1252 console turned the row's middle dot into a
    # byte a UTF-8 reader of the app's output rejected.
    assert any("\u00b7" in r for r in out["rows"]), "a row has a middle dot"
    assert printed.isascii()
    monkeypatch.setenv(pe.QUIET_ENV, "1")
    frame_camera.list_cameras()
    assert pe.PREFIX not in capsys.readouterr().err, "can be silenced"


def test_the_command_prompt_says_where_the_scan_looked(capsys, monkeypatch):
    """check.bat saw the camera, the app did not (Andy, 13 November): the
    console now names the Python the scan ran in, the camera the app is
    set up for, and the GenTL path, so the two can be compared."""
    monkeypatch.delenv(pe.QUIET_ENV, raising=False)
    import frame_camera
    monkeypatch.setattr(frame_camera, "current_choice", lambda: "prophesee")
    frame_camera.list_cameras()
    err = capsys.readouterr().err
    assert "scanned with Python" in err and sys.executable in err
    assert "Basler cameras are NOT searched" in err
    assert pe.ENV in err


def test_the_wizard_scan_sets_the_gentl_path_before_pypylon(monkeypatch):
    """The set-up wizard's "Scan Basler cameras" imported pypylon without
    prepare(): run first, it left the app's later camera list blind to a
    CXP camera."""
    from council_core import basler_scan
    calls = []
    monkeypatch.setattr(pe, "prepare", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setitem(sys.modules, "pypylon", None)   # import fails fast
    basler_scan.scan(test_grab=False, pnp=lambda a: None,
                     gentl_changed=lambda: None)
    assert calls, "prepare() ran before the pypylon import"
