"""
The rig and its camera slots behind frame_camera (multi-camera design 2.2,
stage M1 row 1; plugin design 5, stage P1 row 1).

frame_camera's one module-level camera, `_LIVE = _Live()`, became a Rig of
CameraSlots with `_LIVE` kept as a compatibility VIEW: per-connection names
to the active slot, app-level names to the rig. The promise is that nothing
else changes — every existing Typhon test runs unchanged (the branch gate,
tools/typhon_plugins_gate.py) — and these pin the view itself.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
from council_core import camera_rig
from tests.sim_cameras import simulated_row

#: Every field `_Live.__init__` created at qt-migration 43b317e, where the
#: branch began: 49 (42 before the trigger work, plugin design 5.3). Each
#: must still resolve; fields added later are covered by the loop over
#: whatever `_Live` declares now.
FIELDS_AT_BRANCH = (
    "device", "session", "info", "found", "folder", "run", "setup_path",
    "raw_path", "reviewer", "canvas", "popouts", "reported", "network",
    "capturing", "previewing", "preview_retry_at", "idle", "idle_reason",
    "stream_started", "rate_note", "rate_note_until", "run_note_until",
    "run_note", "shown_aoi", "job", "listeners", "settings_window",
    "picker", "tabs", "as_connected", "as_connected_triggers",
    "connect_listing", "quiet_reason", "run_waited", "box_changed",
    "camera_set", "box_seen", "hooked_boxes", "box_synced", "queued",
    "preset_in_use", "record_path", "trigger_run", "last_trigger_run",
    "armed", "shown_reduce", "capture_button", "capture_key", "disarm_note",
)

#: The app-level fields the multi-camera design names (its 2.2: the 78 test
#: uses that a view pointing only at slot 1 would have broken), plus the
#: other fields the code marks as about the app.
APP_LEVEL = {"setup_path", "reviewer", "listeners", "found", "popouts",
             "canvas", "picker", "tabs", "box_changed", "box_seen",
             "hooked_boxes", "settings_window", "capture_button",
             "capture_key"}

#: Per-connection fields the design names (2.2: device, session, job,
#: previewing "and the rest of what clear() resets") and the trigger work's.
PER_CONNECTION = {"device", "session", "job", "previewing", "capturing",
                  "info", "folder", "run", "trigger_run", "last_trigger_run",
                  "armed", "shown_reduce", "disarm_note"}


class _Mark:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<mark {self.name}>"


@pytest.fixture
def rig():
    """A rig of its own over frame_camera's _Live, so the module's state is
    never touched."""
    made = camera_rig.Rig(frame_camera._Live)
    return made, camera_rig.LiveView(made)


def declared():
    return list(vars(frame_camera._Live()))


# ======================================================================
# The view: every field
# ======================================================================
def test_the_49_fields_of_the_branch_point_are_all_still_declared():
    missing = sorted(set(FIELDS_AT_BRANCH) - set(declared()))
    assert len(FIELDS_AT_BRANCH) == 49
    assert not missing, f"_Live no longer declares {missing}"


def test_every_field_resolves_through_the_view_for_reading(rig):
    made, view = rig
    names = declared()
    assert len(names) >= 49
    for name in names:
        home = made.active if camera_rig.where(view, name) == "slot" \
            else made
        assert getattr(view, name) is getattr(home, name), name


def test_every_field_resolves_through_the_view_for_writing(rig):
    """Set through the view, the value lands where the field lives — on the
    active slot or on the rig — and nowhere else; read back it is the
    same object."""
    made, view = rig
    for name in declared():
        side = camera_rig.where(view, name)
        home, other = ((made.active, made) if side == "slot"
                       else (made, made.active))
        mark = _Mark(name)
        setattr(view, name, mark)
        assert getattr(home, name) is mark, name
        assert getattr(view, name) is mark, name
        assert getattr(other, name, None) is not mark, name
        assert name not in vars(other), (
            f"{name} was written onto the {'rig' if side == 'slot' else 'slot'}")


def test_the_modules_live_is_a_view_over_the_modules_rig():
    assert isinstance(frame_camera._LIVE, camera_rig.LiveView)
    assert camera_rig.rig_of(frame_camera._LIVE) is frame_camera._RIG
    assert isinstance(frame_camera._RIG, camera_rig.Rig)
    assert frame_camera._RIG.names() == ["cam1"]
    assert frame_camera._RIG.active is frame_camera._RIG.slots[0]
    assert (frame_camera._RIG.slot_fields | frame_camera._RIG.rig_fields
            == set(declared()))


# ======================================================================
# Which side each field is on
# ======================================================================
def test_per_connection_is_exactly_what_clear_resets():
    """The split is the code's own rule (multi-camera design 2.2): what
    `_Live.clear()` — Disconnect — resets belongs to one connection, and
    goes on the slot; the rest survives Disconnect, is about the app, and
    goes on the rig."""
    probe = frame_camera._Live()
    before = {k: object() for k in vars(probe)}
    for k, v in before.items():
        setattr(probe, k, v)
    probe.clear()
    reset = {k for k, v in vars(probe).items() if before.get(k) is not v}
    per_connection, app_level = camera_rig.split_fields(frame_camera._Live)
    assert per_connection == reset
    assert app_level == set(before) - reset
    assert not per_connection & app_level


def test_the_fields_the_design_names_are_on_the_right_side(rig):
    _made, view = rig
    for name in APP_LEVEL:
        assert camera_rig.where(view, name) == "rig", name
    for name in PER_CONNECTION:
        assert camera_rig.where(view, name) == "slot", name


def test_clear_through_the_view_resets_the_slot_and_keeps_the_app(rig):
    made, view = rig
    listeners = view.listeners
    view.setup_path = Path("setup.json")
    view.found = "scan"
    view.box_changed["exposure"] = 3
    view.run = "run_1"
    view.device = object()
    view.as_connected["Gain"] = 2.0
    view.clear()
    assert view.device is None and view.run == "" and view.as_connected == {}
    assert made.active.device is None
    assert view.setup_path == Path("setup.json") and view.found == "scan"
    assert view.listeners is listeners and view.box_changed == {"exposure": 3}


def test_a_name_never_declared_is_refused_both_ways(rig):
    _made, view = rig
    with pytest.raises(AttributeError):
        view.not_a_field = 1
    with pytest.raises(AttributeError):
        view.not_a_field                                  # noqa: B018
    assert not hasattr(view, "not_a_field")
    with pytest.raises(AttributeError):
        del view.device


def test_monkeypatch_on_the_view_sets_and_undoes(monkeypatch):
    """How the tests use _LIVE (monkeypatch.setattr(frame_camera._LIVE,
    "setup_path", ...)): set through the view, undone through it."""
    before = frame_camera._RIG.setup_path
    with monkeypatch.context() as m:
        m.setattr(frame_camera._LIVE, "setup_path", Path("elsewhere.json"))
        assert frame_camera._RIG.setup_path == Path("elsewhere.json")
    assert frame_camera._RIG.setup_path == before


# ======================================================================
# Slots
# ======================================================================
def test_per_connection_names_follow_the_active_slot(rig):
    made, view = rig
    view.run = "first"
    view.setup_path = Path("app.json")
    cam2 = made.add_slot()
    assert cam2.name == "cam2" and made.active.name == "cam1"
    made.select("cam2")
    assert view.run == "" and view.device is None
    assert view.setup_path == Path("app.json"), "app-level: the same rig"
    view.run = "second"
    made.select("cam1")
    assert view.run == "first" and cam2.run == "second"
    view.clear()
    assert view.run == "" and cam2.run == "second", "clear is one slot"


def test_slot_names_follow_the_designs_rule(rig):
    made, _view = rig
    for bad in ("Cam2", "cam 2", "-cam", "", "a" * 17, "cam_2", "cám"):
        with pytest.raises(ValueError):
            made.add_slot(bad)
    made.add_slot("bench-left")
    with pytest.raises(ValueError):
        made.add_slot("bench-left")
    assert made.names() == ["cam1", "bench-left"]


def test_a_rig_runs_at_most_four_cameras_and_names_fill_gaps(rig):
    made, _view = rig
    made.add_slot()
    made.add_slot()
    made.add_slot()
    assert made.names() == ["cam1", "cam2", "cam3", "cam4"]
    with pytest.raises(ValueError):
        made.add_slot()
    made.remove_slot("cam2")
    assert made.add_slot().name == "cam2"
    assert [s.id for s in made.slots] == [1, 3, 4, 5]


def test_removing_a_slot_never_closes_or_loses_a_camera(rig):
    made, view = rig
    with pytest.raises(ValueError):
        made.remove_slot("cam1")                  # the last one
    made.add_slot()
    with pytest.raises(ValueError):
        made.remove_slot("cam1")                  # the active one
    made.select("cam2")
    made.slot("cam1").device = object()
    with pytest.raises(ValueError):
        made.remove_slot("cam1")                  # a camera open in it
    assert made.connected() == [made.slot("cam1")]
    made.slot("cam1").clear()
    made.remove_slot("cam1")
    assert made.names() == ["cam2"]
    with pytest.raises(KeyError):
        made.select("cam1")
    del view


def test_each_slot_has_its_own_lock_and_plugin_state_and_keeps_who_it_is(rig):
    made, _view = rig
    cam2 = made.add_slot()
    cam1 = made.slot("cam1")
    assert cam1.lock is not cam2.lock
    assert cam1.plugin_state == {} and cam1.plugin_state is not cam2.plugin_state
    cam1.plugin_state["typhon.trigger"] = {"armed": True}
    cam1.role = "leader"
    cam1.run = "r"
    cam1.clear()
    assert (cam1.id, cam1.name, cam1.role) == (1, "cam1", "leader")
    assert cam1.plugin_state == {"typhon.trigger": {"armed": True}}
    assert cam1.run == ""


def test_a_template_may_not_declare_a_name_the_rig_keeps():
    class Clashing:
        def __init__(self):
            self.active = None          # the rig's own
            self.device = None

        def clear(self):
            self.device = None

    with pytest.raises(ValueError, match="active"):
        camera_rig.Rig(Clashing)

    class SlotClash:
        def __init__(self):
            self.name = ""              # the slot's own

        def clear(self):
            self.name = ""

    with pytest.raises(ValueError, match="name"):
        camera_rig.Rig(SlotClash)


def test_camera_rig_imports_no_toolkit_and_not_frame_camera():
    """council_core's rule (no Qt), and the layering: the rig is handed its
    template, it does not reach up into the linked module."""
    tree = ast.parse((ROOT / "council_core" / "camera_rig.py")
                     .read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"PySide6", "tkinter", "frame_camera", "council_qt"}
    assert imported <= {"__future__", "re", "threading", "typing"}


# ======================================================================
# frame_camera's functions over the rig, with a simulated camera
# ======================================================================
@pytest.fixture
def module_state():
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None


def test_connect_and_disconnect_act_on_the_active_slot(module_state):
    rig = frame_camera._RIG
    setup = rig.setup_path
    listed = frame_camera.list_cameras()
    assert rig.found is not None, "the scan result is the rig's"
    frame_camera.connect(simulated_row(listed["rows"], "frame"))
    slot = rig.active
    assert slot.device is not None and slot.session is not None
    assert frame_camera._LIVE.device is slot.device
    assert rig.connected() == [slot]
    assert "device" not in vars(rig), "a connection is never the rig's"
    frame_camera.disconnect()
    assert slot.device is None and slot.session is None
    assert rig.connected() == []
    assert rig.found is not None and rig.setup_path == setup
