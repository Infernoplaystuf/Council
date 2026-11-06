"""
camera_rig.json — the name each of a project's cameras goes by in the rig,
and its rig presets (multi-camera design 2.2 and 2.5, M1 row 4; plugin
design stage P2 row 3): council_core.camera_rig_file, and the rig's
rename_slot and holding.

  * cam1, cam2, ... by default; the same camera gets the same name next
    time, known by its backend and serial (an EVK4's model is read only
    once it is open), or by its key with no serial;
  * names are safe in a folder name; renaming follows into rig presets;
  * a rig preset says which preset goes on which camera; plan() says what
    putting it on the open cameras would do;
  * ADDITIVE: what the module does not know is kept when it rewrites;
  * NEVER LOST QUIETLY: JSON only, written whole under a temporary name of
    its own and renamed, under an OS lock; a damaged or newer file is
    refused and left byte for byte; reading never writes.
"""
from __future__ import annotations

import ast
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import camera_rig  # noqa: E402
from council_core import camera_rig_file as rf  # noqa: E402
from council_core.cameras import CameraInfo  # noqa: E402

BASLER = CameraInfo("basler", "40012345", "boA5320-150cm", "40012345",
                    "Basler", "frame")
#: An EVK4 as discovery lists it (no sensor read yet), then as it is once
#: open (cameras.identify reads the sensor: the model changes).
EVK_FOUND = CameraInfo("prophesee", "00051234", "", "00051234", "", "event")
EVK_OPEN = CameraInfo("prophesee", "00051234", "IMX636", "00051234",
                      "Prophesee", "event")
SIM = CameraInfo("simulated", "sim-frame", "Simulated frame camera", "SIM-1",
                 "simulated", "frame")
NO_SERIAL = CameraInfo("acme.thermal", "usb:3-1", "T100", "", "Acme", "frame")


@pytest.fixture
def rig_file(tmp_path):
    return rf.RigFile(rf.rig_path(tmp_path))


def doc_of(store):
    return json.loads(store.path.read_text(encoding="utf-8"))


# ======================================================================
# Names
# ======================================================================
def test_reading_a_project_with_no_rig_file_writes_nothing(rig_file, tmp_path):
    assert rig_file.cameras() == []
    assert rig_file.name_of(BASLER) is None
    assert rig_file.rig_presets() == {}
    assert list(tmp_path.iterdir()) == []


def test_cameras_are_named_cam1_cam2_in_turn(rig_file):
    assert rig_file.assign(BASLER) == "cam1"
    assert rig_file.assign(EVK_FOUND) == "cam2"
    assert [c.name for c in rig_file.cameras()] == ["cam1", "cam2"]
    entry = doc_of(rig_file)["cameras"]["cam1"]
    assert {k: entry[k] for k in ("backend", "key", "serial", "model",
                                  "kind")} == {
        "backend": "basler", "key": "40012345", "serial": "40012345",
        "model": "boA5320-150cm", "kind": "frame"}
    assert entry["named"]


def test_the_same_camera_gets_the_same_name_next_time(rig_file):
    rig_file.assign(SIM)
    assert rig_file.assign(BASLER) == "cam2"
    again = rf.RigFile(rig_file.path)
    assert again.assign(BASLER) == "cam2"
    assert again.name_of(BASLER) == "cam2"
    assert len(again.cameras()) == 2


def test_an_evk4_is_the_same_camera_before_and_after_its_sensor_is_read(
        rig_file):
    """Known by its serial: discovery cannot read the sensor, the open
    camera can (cameras.identify), and it is one camera."""
    assert rig_file.assign(EVK_FOUND) == "cam1"
    assert rig_file.assign(EVK_OPEN) == "cam1"
    assert rig_file.name_of(EVK_FOUND) == rig_file.name_of(EVK_OPEN) == "cam1"
    assert doc_of(rig_file)["cameras"]["cam1"]["model"] == "IMX636", \
        "the fuller description is kept"


def test_a_camera_with_no_serial_is_known_by_its_key(rig_file):
    assert rig_file.assign(NO_SERIAL) == "cam1"
    moved = CameraInfo("acme.thermal", "usb:4-2", "T100", "", "Acme", "frame")
    assert rig_file.name_of(moved) is None, "another port, another key"
    assert rig_file.name_of(NO_SERIAL) == "cam1"


def test_the_same_serial_on_another_backend_is_another_camera(rig_file):
    rig_file.assign(BASLER)
    other = CameraInfo("acme.thermal", "x", "T", BASLER.serial, "", "frame")
    assert rig_file.name_of(other) is None
    assert rig_file.assign(other) == "cam2"


def test_a_new_camera_takes_the_name_offered_when_it_is_free(rig_file):
    assert rig_file.assign(BASLER, prefer="cam3") == "cam3"
    assert rig_file.assign(SIM, prefer="cam3") == "cam1", \
        "cam3 is the Basler's"
    assert rig_file.assign(EVK_OPEN, taken=["cam2"]) == "cam4", \
        "cam1 and cam3 are named, cam2 is in use in the rig"


def test_a_known_camera_keeps_its_name_whatever_is_offered(rig_file):
    rig_file.assign(BASLER, prefer="cam2")
    assert rig_file.assign(BASLER, prefer="cam1") == "cam2"


def test_rename_follows_into_rig_presets_and_refuses_clashes(rig_file):
    rig_file.assign(BASLER)
    rig_file.assign(EVK_OPEN)
    rig_file.save_rig_preset("Bath", {"cam1": "Bird bath", "cam2": "Low"})
    rig_file.rename("cam1", "top-cam")
    assert rig_file.name_of(BASLER) == "top-cam"
    assert rig_file.rig_presets() == {"Bath": {"top-cam": "Bird bath",
                                               "cam2": "Low"}}
    assert [c.name for c in rig_file.cameras()] == ["top-cam", "cam2"], \
        "the file's order is kept"
    with pytest.raises(rf.RigError, match="already named"):
        rig_file.rename("top-cam", "cam2")
    with pytest.raises(rf.RigError, match="cannot name a camera"):
        rig_file.rename("top-cam", "Top Cam")
    with pytest.raises(rf.RigError, match="no camera"):
        rig_file.rename("cam9", "cam8")


@pytest.mark.parametrize("bad", ["", "Cam1", "cam 1", "-cam", "a" * 17,
                                 "cam_1", "cam/1", "..", "cam1\n"])
def test_a_name_is_safe_in_a_folder_name(rig_file, bad):
    with pytest.raises(rf.RigError):
        rig_file.assign(BASLER, prefer=bad)
    assert not rig_file.path.exists()


def test_forget_frees_the_name_and_presets_list_it_as_missing(rig_file):
    rig_file.assign(BASLER)
    rig_file.save_rig_preset("Bath", {"cam1": "Bird bath"})
    rig_file.forget("cam1")
    assert rig_file.name_of(BASLER) is None
    assert rig_file.assign(SIM) == "cam1"
    assert rig_file.plan("Bath", []).missing == ["cam1"]


# ======================================================================
# Rig presets
# ======================================================================
def test_a_rig_preset_says_which_preset_goes_on_which_camera(rig_file):
    saved = rig_file.save_rig_preset("  Bird   bath ",
                                     {"cam1": "Bird bath", "cam2": " Low  "})
    assert saved == "Bird bath"
    assert rig_file.rig_presets() == {"Bird bath": {"cam1": "Bird bath",
                                                    "cam2": "Low"}}
    with pytest.raises(rf.RigError, match="already called"):
        rig_file.save_rig_preset("bird BATH", {"cam1": "x"})
    assert rig_file.save_rig_preset("bird BATH", {"cam1": "x"},
                                    replace=True) == "Bird bath"
    assert rig_file.rig_presets() == {"Bird bath": {"cam1": "x"}}


@pytest.mark.parametrize("name, cameras, words", [
    ("", {"cam1": "a"}, "give a rig preset a name"),
    ("x" * 61, {"cam1": "a"}, "at most 60"),
    ("P", {}, "at least one camera"),
    ("P", {"Cam 1": "a"}, "cannot name a camera"),
    ("P", {"cam1": "  "}, "give a preset a name"),
])
def test_a_rig_preset_is_checked(rig_file, name, cameras, words):
    with pytest.raises(rf.RigError, match=words):
        rig_file.save_rig_preset(name, cameras)
    assert not rig_file.path.exists()


def test_plan_says_what_putting_a_rig_preset_on_would_do(rig_file):
    rig_file.save_rig_preset("Bath", {"cam1": "Bird bath", "cam3": "Wide"})
    plan = rig_file.plan("bath", ["cam1", "cam2"])
    assert plan == rf.RigPlan(preset="Bath", apply={"cam1": "Bird bath"},
                              missing=["cam3"], untouched=["cam2"])
    with pytest.raises(rf.RigError, match="no rig preset"):
        rig_file.plan("Nothing", ["cam1"])


def test_rig_presets_rename_and_delete(rig_file):
    rig_file.save_rig_preset("A", {"cam1": "a"})
    rig_file.save_rig_preset("B", {"cam1": "b"})
    assert rig_file.rename_rig_preset("a", "Alpha") == "Alpha"
    assert list(rig_file.rig_presets()) == ["Alpha", "B"]
    with pytest.raises(rf.RigError, match="already called"):
        rig_file.rename_rig_preset("Alpha", "b")
    assert rig_file.delete_rig_preset("ALPHA") == "Alpha"
    assert list(rig_file.rig_presets()) == ["B"]
    with pytest.raises(rf.RigError, match="no rig preset"):
        rig_file.delete_rig_preset("Alpha")


# ======================================================================
# Additive, and never lost quietly
# ======================================================================
def test_what_it_does_not_know_is_kept_when_it_rewrites(rig_file):
    """A later Typhon's or a plugin's block (the triggering plugin's rig-wide
    set-up, plugin design 3.6), a field of a camera's entry and of a rig
    preset: all kept as they were."""
    rig_file.path.write_text(json.dumps({
        "format": rf.FORMAT_NAME, "format_version": 1,
        "typhon.trigger": {"source": "Timer", "hz": 20},
        "cameras": {"cam1": {"backend": "basler", "key": "40012345",
                             "serial": "40012345", "role": "leader",
                             "tile": [1, 2]}},
        "rig_presets": {"Bath": {"cameras": {"cam1": "Bird bath"},
                                 "note": "the garden"}},
    }), encoding="utf-8")
    assert rig_file.assign(BASLER) == "cam1"        # model filled in
    assert rig_file.assign(SIM) == "cam2"
    rig_file.save_rig_preset("Bath", {"cam1": "Dusk"}, replace=True)
    doc = doc_of(rig_file)
    assert doc["typhon.trigger"] == {"source": "Timer", "hz": 20}
    assert doc["cameras"]["cam1"]["role"] == "leader"
    assert doc["cameras"]["cam1"]["tile"] == [1, 2]
    assert doc["cameras"]["cam1"]["model"] == "boA5320-150cm"
    assert doc["rig_presets"]["Bath"]["note"] == "the garden"
    assert doc["rig_presets"]["Bath"]["cameras"] == {"cam1": "Dusk"}


def test_nothing_is_written_when_nothing_changed(rig_file):
    rig_file.assign(BASLER)
    before = rig_file.path.stat().st_mtime_ns
    text = rig_file.path.read_bytes()
    rig_file.assign(BASLER)
    rig_file.rename("cam1", "cam1")
    assert rig_file.path.read_bytes() == text
    assert rig_file.path.stat().st_mtime_ns == before


@pytest.mark.parametrize("content, words", [
    ("{not json", "damaged"),
    ("[]", "not a camera rig file"),
    (json.dumps({"format": "typhon-camera-presets"}), "not a camera rig"),
    (json.dumps({"format": rf.FORMAT_NAME}), "no format_version"),
    (json.dumps({"format": rf.FORMAT_NAME, "format_version": 1,
                 "cameras": {"Cam 1": {"backend": "b", "key": "k"}}}),
     "not a camera name"),
    (json.dumps({"format": rf.FORMAT_NAME, "format_version": 1,
                 "cameras": {"cam1": {"backend": "b"}}}), "damaged"),
    (json.dumps({"format": rf.FORMAT_NAME, "format_version": 1,
                 "rig_presets": {"P": {"cameras": {"cam1": 3}}}}),
     "damaged"),
    ('{"format": "typhon-camera-rig", "format_version": NaN}', "damaged"),
])
def test_a_file_it_cannot_trust_is_refused_and_left_as_it_is(
        rig_file, content, words):
    rig_file.path.write_text(content, encoding="utf-8")
    before = rig_file.path.read_bytes()
    with pytest.raises(rf.RigFileError, match=words):
        rig_file.assign(BASLER)
    with pytest.raises(rf.RigFileError):
        rig_file.save_rig_preset("P", {"cam1": "a"})
    with pytest.raises(rf.RigFileError):
        rig_file.name_of(BASLER)
    assert rig_file.path.read_bytes() == before
    assert sorted(p.name for p in rig_file.path.parent.iterdir()
                  if not p.name.startswith(".camera_rig.json.lock")) == \
        [rf.RIG_FILE], "no temporary file is left behind"


def test_a_file_a_newer_typhon_wrote_is_left_as_it_is(rig_file):
    rig_file.path.write_text(json.dumps({
        "format": rf.FORMAT_NAME, "format_version": 2, "cameras": {}}),
        encoding="utf-8")
    before = rig_file.path.read_bytes()
    with pytest.raises(rf.RigFileError) as caught:
        rig_file.assign(BASLER)
    assert caught.value.newer and "newer Typhon" in str(caught.value)
    assert rig_file.path.read_bytes() == before


def test_a_file_written_with_a_byte_order_mark_is_read(rig_file):
    """Notepad's "UTF-8 with BOM" is the same JSON."""
    rig_file.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({
        "format": rf.FORMAT_NAME, "format_version": 1,
        "cameras": {"cam7": {"backend": "basler", "key": "40012345",
                             "serial": "40012345"}}}).encode("utf-8"))
    assert rig_file.name_of(BASLER) == "cam7"


def test_written_whole_with_no_temporary_file_left(rig_file, tmp_path):
    rig_file.assign(BASLER)
    rig_file.save_rig_preset("P", {"cam1": "a"})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [".camera_rig.json.lock", rf.RIG_FILE]
    assert doc_of(rig_file)["format_version"] == rf.FORMAT_VERSION


def test_two_windows_naming_at_once_lose_nothing(tmp_path):
    """Every change re-reads the file under the lock: eight writers naming
    one camera each, at the same moment, leave eight names."""
    infos = [CameraInfo("simulated", f"sim-frame-{i}", "Sim", f"SIM-{i}",
                        "simulated", "frame") for i in range(1, 9)]
    got, errors = [], []
    start = threading.Barrier(len(infos))

    def name(info):
        try:
            start.wait()
            got.append(rf.RigFile(rf.rig_path(tmp_path)).assign(info))
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=name, args=(i,)) for i in infos]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert sorted(got) == sorted(f"cam{i}" for i in range(1, 9))
    store = rf.RigFile(rf.rig_path(tmp_path))
    assert {c.serial for c in store.cameras()} == {i.serial for i in infos}


def test_json_only_no_pickle_and_no_qt():
    tree = ast.parse((ROOT / "council_core" / "camera_rig_file.py")
                     .read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"pickle", "marshal", "shelve", "PySide6",
                           "tkinter", "frame_camera", "council_qt"}


# ======================================================================
# The rig: renaming a slot, and which slot holds a camera
# ======================================================================
class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


def test_a_slot_takes_its_cameras_name():
    rig = camera_rig.Rig(Template)
    slot = rig.active
    rig.rename_slot("cam1", "top-cam")
    assert slot.name == "top-cam" and rig.names() == ["top-cam"]
    assert rig.slot("top-cam") is slot and slot.id == 1
    rig.add_slot()
    assert rig.names() == ["top-cam", "cam1"]
    with pytest.raises(ValueError, match="already named"):
        rig.rename_slot("cam1", "top-cam")
    with pytest.raises(ValueError, match="cannot name a camera"):
        rig.rename_slot("cam1", "Top")
    with pytest.raises(KeyError):
        rig.rename_slot("cam9", "cam8")
    assert rig.rename_slot("cam1", "cam1").name == "cam1"


def test_the_rig_says_which_slot_holds_a_camera():
    rig = camera_rig.Rig(Template)
    second = rig.add_slot()
    assert rig.holding(BASLER) is None
    second.device, second.info = object(), EVK_OPEN
    assert rig.holding(EVK_FOUND) is second, "by backend and key"
    assert rig.holding(BASLER) is None
    other = CameraInfo("basler", EVK_OPEN.key, "", "", "", "frame")
    assert rig.holding(other) is None, "the same key on another backend"
    second.device = None
    assert rig.holding(EVK_FOUND) is None, "a slot with no camera holds none"
