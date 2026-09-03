"""
Tests for gui_ports — the typed binding decisions, still pure.

The load-bearing test is test_port_caps_covers_the_catalogue_exactly. Same
discipline as gui_colors.COLOUR_CAPS: a new widget kind added to PALETTE
without an entry here would silently default to "no port", which looks like a
decision and is not one.

Run:  python -m pytest tests/test_gui_ports.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_ports as gp                           # noqa: E402
from gui_shapes import GENERIC_KIND, PALETTE     # noqa: E402


class F:
    """Duck-typed shape stand-in — proves gui_ports never depends on Shape."""

    def __init__(self, sid, kind, label="", props=None, port=None, z=0):
        self.id, self.kind = sid, kind
        self.label = label
        self.props = props or {}
        self.port = port or {}
        self.z = z


# ============================================================
# Purity
# ============================================================

def test_the_module_is_pure():
    """Same rule as gui_colors: stdlib only, no tkinter, no gui_shapes, no
    council_engine, no vault_*."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_ports.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine", "gui_shapes",
                       "gui_canvas", "gui_emit", "gui_spec"}
              or m.startswith("vault_")}
    assert not banned, f"gui_ports must stay pure; imports {banned}"


# ============================================================
# THE CROSS-FILE INVARIANT
# ============================================================

def test_port_caps_covers_the_catalogue_exactly():
    """The pinning test. See module docstring."""
    assert set(gp.PORT_CAPS) == set(PALETTE), (
        f"missing: {set(PALETTE) - set(gp.PORT_CAPS)}; "
        f"stale: {set(gp.PORT_CAPS) - set(PALETTE)}")


def test_every_portless_kind_explains_itself():
    """A missing port that says why beats one that vanishes silently."""
    for kind, cap in gp.PORT_CAPS.items():
        if not cap.types:
            assert gp.note(kind), f"{kind} offers nothing and says nothing"


def test_dirs_and_types_are_closed_and_first_is_the_default():
    for kind, cap in gp.PORT_CAPS.items():
        if not cap.types:
            continue
        # first entry is what default_port_name / build_ports pick with no override
        assert cap.types[0], f"{kind} missing a default type"
        assert cap.dirs[0], f"{kind} missing a default direction"
        assert set(cap.dirs) <= {"i", "o", "io", "e"}, (
            f"{kind} has an unknown direction: {cap.dirs}")


def test_a_button_is_an_event_not_a_bool():
    """The user's own instinct. See module docstring."""
    cap = gp.caps("button")
    assert cap.types == ("event",)
    assert cap.dirs == ("e",)
    assert cap.binder == "event"
    assert cap.tk_option == "", "ttk.Button raises on variable=; must not attach one"


# ============================================================
# Grammar
# ============================================================

def test_reserved_names_are_the_two_public_ports_helpers_only():
    """Any bigger reserved set would push honest domain words like ``get`` or
    ``value`` off the table; any smaller and Ports would collide with itself."""
    assert gp.RESERVED_PORT_NAMES == frozenset({"read", "apply"})


def test_validate_accepts_a_plain_name():
    ok, why = gp.validate_port_name("scan_folder")
    assert ok, why


@pytest.mark.parametrize("name,reason", [
    ("", "empty"), ("1st", "identifier"), ("class", "keyword"),
    ("_hidden", "'_'"), ("read", "reserved"), ("apply", "reserved"),
    ("has space", "identifier"),
])
def test_validate_rejects_each_way_a_name_can_be_wrong(name, reason):
    ok, why = gp.validate_port_name(name)
    assert not ok
    assert reason in why.lower(), why


def test_validate_refuses_a_duplicate():
    ok, _ = gp.validate_port_name("x", taken=["x"])
    assert not ok


# ============================================================
# Defaults
# ============================================================

def test_default_port_name_comes_from_the_label():
    assert gp.default_port_name("entry", "Scan Folder") == "scan_folder"
    assert gp.default_port_name("button", "Start scan!") == "start_scan"


def test_default_port_name_falls_back_to_kind_when_the_label_is_empty():
    assert gp.default_port_name("entry", "") == "entry"


def test_a_radio_group_shares_ONE_port_name_across_members():
    """The var is shared; the port that owns it must be too."""
    name = gp.default_port_name("radiobutton", "Fast", group="mode")
    assert name == "mode", f"expected the group name, got {name}"


def test_default_collides_deterministically():
    """Same shapes -> same names, always, so a re-derivation cannot silently
    change the identifier hand-written code depends on."""
    taken = []
    n1 = gp.default_port_name("entry", "Path", taken=taken); taken.append(n1)
    n2 = gp.default_port_name("entry", "Path", taken=taken); taken.append(n2)
    n3 = gp.default_port_name("entry", "Path", taken=taken); taken.append(n3)
    assert (n1, n2, n3) == ("path", "path_2", "path_3")


def test_default_rescues_a_reserved_name():
    """Somebody may honestly label a widget 'read' or 'apply'."""
    n = gp.default_port_name("entry", "read")
    ok, _ = gp.validate_port_name(n)
    assert ok, n


def test_default_rescues_a_leading_digit_and_a_keyword():
    assert gp.default_port_name("entry", "1 fish") == "n1_fish"
    assert gp.default_port_name("entry", "class") == "class_"


# ============================================================
# build_ports
# ============================================================

def test_build_ports_makes_one_spec_per_bindable_widget():
    shapes = [
        F("a", "entry", label="Scan Folder"),
        F("b", "checkbutton", label="Dry run"),
        F("c", "button", label="Start scan"),
        F("d", "frame", label="outer"),          # no port
        F("e", "separator"),                     # no port
    ]
    ports = gp.build_ports(shapes)
    names = [p.name for p in ports]
    assert names == ["scan_folder", "dry_run", "start_scan"]
    kinds = {p.name: p.kind for p in ports}
    assert kinds == {"scan_folder": "entry",
                     "dry_run": "checkbutton",
                     "start_scan": "button"}


def test_a_radio_group_becomes_one_port_shared_by_its_members():
    """One port per (parent, group). The choices list collects every value in
    emit order."""
    shapes = [
        F("outer", "frame", z=0),
        F("r1", "radiobutton", label="Fast",
          props={"group": "mode", "value": "fast"}, z=1),
        F("r2", "radiobutton", label="Thorough",
          props={"group": "mode", "value": "thorough"}, z=2),
    ]
    parents = {"r1": "outer", "r2": "outer"}
    ports = gp.build_ports(shapes, parents=parents)
    modes = [p for p in ports if p.kind == "radiobutton"]
    assert len(modes) == 1, [p.name for p in modes]
    assert modes[0].name == "mode"
    assert modes[0].choices == ("fast", "thorough")
    assert set(modes[0].shape_ids) == {"r1", "r2"}


def test_two_radio_groups_in_different_containers_stay_separate():
    """A parent id is part of the group key on purpose — a 'size' group in a
    filter panel and a 'size' group in a chart panel are not one group."""
    shapes = [
        F("f1", "frame", z=0), F("f2", "frame", z=1),
        F("a", "radiobutton", label="S",
          props={"group": "size", "value": "s"}, z=2),
        F("b", "radiobutton", label="M",
          props={"group": "size", "value": "m"}, z=3),
        F("c", "radiobutton", label="1x",
          props={"group": "size", "value": "1"}, z=4),
        F("d", "radiobutton", label="2x",
          props={"group": "size", "value": "2"}, z=5),
    ]
    parents = {"a": "f1", "b": "f1", "c": "f2", "d": "f2"}
    ports = [p for p in gp.build_ports(shapes, parents=parents)
             if p.kind == "radiobutton"]
    names = sorted(p.name for p in ports)
    assert names == ["size", "size_2"], f"expected two groups, got {names}"


def test_a_registered_port_name_wins_over_a_new_label():
    """Retyping the label of a widget hand-written code depends on must NOT
    silently rename the port. Rename is a deliberate action (§3 in the spec)."""
    shapes = [F("a", "entry", label="Scan Folder")]
    registry = {"a": "scan_dir"}                # old name still in use
    ports = gp.build_ports(shapes, registry=registry)
    assert ports[0].name == "scan_dir"


def test_a_port_off_flag_suppresses_the_port_entirely():
    """The Label caption rule needs an escape valve — labels used as captions
    do not need a port, and .port['off']=True says so."""
    shapes = [F("a", "label", label="Files:", port={"off": True})]
    assert gp.build_ports(shapes) == []


def test_a_port_override_can_specify_type_and_direction_from_the_allowed_set():
    """The user picks from the closed list; the emitter cannot then produce
    a StringVar for a scale whose type ought to be float."""
    shapes = [F("a", "entry", label="Age",
                port={"name": "age", "type": "int", "dir": "io"})]
    p = gp.build_ports(shapes)[0]
    assert p.name == "age"
    assert (p.type, p.direction) == ("int", "io")


def test_registry_for_keys_by_shape_id_or_group_key():
    shapes = [
        F("f", "frame", z=0),
        F("r1", "radiobutton", label="A", props={"group": "g"}, z=1),
        F("r2", "radiobutton", label="B", props={"group": "g"}, z=2),
        F("e", "entry", label="Name", z=3),
    ]
    parents = {"r1": "f", "r2": "f"}
    ports = gp.build_ports(shapes, parents=parents)
    reg = gp.registry_for(ports, parents=parents)
    assert "e" in reg and reg["e"] == "name"
    # Radio group's key includes its parent
    group_keys = [k for k in reg if k.startswith("group:")]
    assert len(group_keys) == 1
    assert reg[group_keys[0]] == "g"


def test_a_port_on_a_kind_with_no_caps_is_dropped_here_and_caught_by_validate():
    """gui_spec.validate is the layer that yells; build_ports simply refuses
    to invent a spec that would then fail to emit."""
    shapes = [F("f", "frame", label="outer",
                port={"name": "wrong", "type": "str"})]
    assert gp.build_ports(shapes) == []
