"""
council_core.camera_presets — named set-ups (settings + the camera's own
area), per project, per camera; JSON written atomically; a damaged file never
overwritten quietly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import camera_presets as cp
from council_core import cameras
from council_core.cameras import Roi

EVK_A = cp.Identity("prophesee", "EVK4", "00051234", "event")
EVK_B = cp.Identity("prophesee", "EVK4", "00059999", "event")
BASLER = cp.Identity("basler", "boA5320-150cm", "40012345", "frame")

BIRD_BATH = {"bias.bias_diff_on": 40, "erc.enabled": True,
             "trail.type": "STC_CUT_TRAIL", "window_ms": 20.0}


@pytest.fixture
def store(tmp_path):
    return cp.PresetStore(cp.presets_path(tmp_path))


def test_a_saved_preset_reads_back_with_its_area_in_sensor_pixels(store):
    preset, replaced, moved = store.save(EVK_A, "Bird bath", BIRD_BATH,
                                         Roi(512, 300, 160, 120), note="pm")
    assert not replaced and moved is None
    back = store.get(EVK_A, "bird BATH")
    assert back.settings == BIRD_BATH
    assert back.roi == Roi(512, 300, 160, 120)
    assert back.note == "pm" and back.own and back.created
    assert "area 512, 300, 160, 120" in back.line()


def test_the_file_is_plain_json_beside_the_app(store, tmp_path):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    assert store.path == tmp_path / "camera_presets.json"
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["format"] == cp.FORMAT
    entry = doc["cameras"][EVK_A.key]
    assert entry["model"] == "EVK4" and entry["serial"] == "00051234"
    assert entry["presets"]["Bird bath"]["roi"] is None
    assert [p.name for p in tmp_path.iterdir()] == ["camera_presets.json"], \
        "a temporary file was left behind"


def test_saving_a_name_again_replaces_it_and_keeps_when_it_was_made(store):
    first, _, _ = store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    second, replaced, _ = store.save(EVK_A, "bird bath", {"window_ms": 5.0},
                                     None)
    assert replaced and second.created == first.created
    assert [p.name for p in store.presets(EVK_A)] == ["bird bath"]
    assert store.get(EVK_A, "Bird bath").settings == {"window_ms": 5.0}


def test_presets_are_kept_per_camera(store):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    store.save(BASLER, "Bench", {"Gain": 3.0}, Roi(0, 0, 640, 480))
    assert [p.name for p in store.presets(BASLER)] == ["Bench"]
    assert [p.name for p in store.presets(EVK_A)] == ["Bird bath"]


def test_another_unit_of_the_same_model_is_offered_them_marked_as_such(store):
    """A replacement EVK4 starts from its predecessor's set-up."""
    store.save(EVK_A, "Bird bath", BIRD_BATH, Roi(512, 300, 160, 120))
    store.save(EVK_B, "Night", {"bias.bias_fo": -10}, None)
    listed = store.presets(EVK_B)
    assert [(p.name, p.own) for p in listed] == [("Night", True),
                                                 ("Bird bath", False)]
    assert "saved on EVK4 (00051234)" in listed[1].line()
    assert store.get(EVK_B, "bird bath").roi == Roi(512, 300, 160, 120)


def test_a_cameras_own_preset_hides_a_borrowed_one_of_the_same_name(store):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    store.save(EVK_B, "BIRD BATH", {"window_ms": 1.0}, None)
    listed = store.presets(EVK_B)
    assert len(listed) == 1 and listed[0].own


def test_a_borrowed_preset_is_not_renamed_or_deleted_from_here(store):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    with pytest.raises(cp.PresetError, match="saved on EVK4 \\(00051234\\)"):
        store.delete(EVK_B, "Bird bath")
    with pytest.raises(cp.PresetError, match="saved on"):
        store.rename(EVK_B, "Bird bath", "Mine")
    assert store.get(EVK_A, "Bird bath")


def test_rename_and_delete(store):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    store.save(EVK_A, "Feeder", {}, None)
    renamed = store.rename(EVK_A, "bird bath", "Bath (west)")
    assert renamed.name == "Bath (west)" and renamed.settings == BIRD_BATH
    with pytest.raises(cp.PresetError, match="already a preset"):
        store.rename(EVK_A, "Feeder", "bath (WEST)")
    store.delete(EVK_A, "feeder")
    assert [p.name for p in store.presets(EVK_A)] == ["Bath (west)"]
    with pytest.raises(cp.PresetError, match="no preset called"):
        store.delete(EVK_A, "Feeder")


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "bell\x07"])
def test_a_name_must_be_a_name(store, name):
    with pytest.raises(cp.PresetError):
        store.save(EVK_A, name, {}, None)


def test_whitespace_in_a_name_is_tidied_not_refused(store):
    preset, _, _ = store.save(EVK_A, "  bird \t bath ", {}, None)
    assert preset.name == "bird bath"


def test_only_plain_values_are_saved(store):
    with pytest.raises(cp.PresetError, match="cannot be saved"):
        store.save(EVK_A, "Odd", {"window_ms": float("nan")}, None)
    with pytest.raises(cp.PresetError, match="cannot be saved"):
        store.save(EVK_A, "Odd", {"x": [1, 2]}, None)
    assert not store.path.exists()


# ======================================================================
# A damaged file
# ======================================================================
@pytest.mark.parametrize("text", [
    "{not json", "", "[1, 2]", '{"format": 1}', '{"cameras": {}}',
    '{"format": 99, "cameras": {}}',
])
def test_a_file_that_is_not_ours_is_refused_and_left_alone(store, text):
    store.path.write_text(text, encoding="utf-8")
    with pytest.raises(cp.PresetFileError):
        store.presets(EVK_A)
    with pytest.raises(cp.PresetFileError):
        store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    assert store.path.read_text(encoding="utf-8") == text, "overwritten"


def test_repairing_moves_the_damaged_file_aside_and_says_where(store):
    store.path.write_text("{not json", encoding="utf-8")
    preset, _, moved = store.save(EVK_A, "Bird bath", BIRD_BATH, None,
                                  repair=True)
    assert moved is not None and moved.name.startswith(
        "camera_presets.damaged-")
    assert moved.read_text(encoding="utf-8") == "{not json"
    assert store.get(EVK_A, "Bird bath").settings == BIRD_BATH


def test_one_bad_preset_is_skipped_named_and_kept_on_rewrite(store):
    store.save(EVK_A, "Good", BIRD_BATH, None)
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    bad = {"settings": {"x": {"nested": 1}}, "roi": [1, 2, 3]}
    doc["cameras"][EVK_A.key]["presets"]["Bad"] = bad
    doc["cameras"]["other|thing|1"] = "garbage"
    store.path.write_text(json.dumps(doc), encoding="utf-8")
    assert [p.name for p in store.presets(EVK_A)] == ["Good"]
    assert any(line.startswith("Bad:") for line in store.problems)
    store.save(EVK_A, "Another", {}, None)
    after = json.loads(store.path.read_text(encoding="utf-8"))
    assert after["cameras"][EVK_A.key]["presets"]["Bad"] == bad
    assert after["cameras"]["other|thing|1"] == "garbage"


@pytest.mark.parametrize("roi", [[1, 2, 3], [0, 0, 0, 10], [-1, 0, 5, 5],
                                 [0, 0, 5.5, 5], "0,0,5,5"])
def test_an_area_that_is_not_one_is_a_problem_not_a_crash(store, roi):
    doc = {"format": 1, "cameras": {EVK_A.key: dict(
        EVK_A.as_dict(), presets={"Odd": {"settings": {}, "roi": roi}})}}
    store.path.write_text(json.dumps(doc), encoding="utf-8")
    assert store.presets(EVK_A) == []
    assert store.problems


def test_a_failed_write_leaves_the_old_file(store, monkeypatch):
    store.save(EVK_A, "Bird bath", BIRD_BATH, None)
    before = store.path.read_text(encoding="utf-8")

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.save(EVK_A, "Feeder", {}, None)
    assert store.path.read_text(encoding="utf-8") == before


# ======================================================================
# A preset and a camera
# ======================================================================
def simulated(kind):
    backend = cameras.SyntheticBackend()
    return backend.open(next(c for c in backend.discover() if c.kind == kind))


def test_a_camera_is_captured_and_put_back(store):
    dev = simulated("frame")
    dev.apply_settings({"Gain": 6.0, "PixelFormat": "Mono12"},
                       Roi(100, 50, 200, 100))
    settings, roi = cp.capture(dev)
    store.save(cp.Identity.of(dev.info), "Bench", settings, roi)

    dev.apply_settings({"Gain": 0.0, "PixelFormat": "Mono8"},
                       Roi(0, 0, 640, 480))
    done = cp.apply(dev, store.get(cp.Identity.of(dev.info), "Bench"))
    assert done.ok, done.summary()
    assert dev.state["Gain"] == 6.0 and dev.state["PixelFormat"] == "Mono12"
    assert dev.roi() == Roi(100, 50, 200, 100)


def test_a_preset_without_an_area_leaves_the_area_alone(store):
    dev = simulated("event")
    dev.set_roi(Roi(64, 64, 128, 128))
    settings, _ = cp.capture(dev, include_roi=False)
    preset, _, _ = store.save(cp.Identity.of(dev.info), "Biases only",
                              settings, None)
    dev.set_roi(Roi(0, 0, 320, 240))
    cp.apply(dev, preset)
    assert dev.roi() == Roi(0, 0, 320, 240)


def test_the_identity_comes_from_what_the_scan_found():
    info = cameras.CameraInfo("prophesee", "00051234", "EVK4", "00051234",
                              "Prophesee", "event")
    assert cp.Identity.of(info) == EVK_A
