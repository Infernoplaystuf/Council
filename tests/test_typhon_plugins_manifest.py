"""
A Typhon plugin's manifest, plugin.json, read as data (plugin design 3.1,
3.2, 3.11; stage P1 row 2): council_core.plugin_manifest.

  * a manifest is read without importing a line of the plugin;
  * every field of schema 1 is checked, and anything else is refused with
    the field and the reason — an unknown field, a duplicate key, NaN, a
    file too big, not UTF-8, not one object;
  * the rules that set built-in plugins apart: `typhon.` ids, `record_gate`
    and `frame_stage`, file names; the core's own run files are no plugin's;
  * the API version rule: 0.x names this exact 0.x; from 1.0 the same major
    and a minor no later than ours;
  * the requirements a plugin puts on the interpreter, and their versions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import plugin_manifest as pm  # noqa: E402


def feature(**over):
    doc = {"schema": 1, "id": "typhon.example", "short": "ex",
           "name": "Example", "version": "1.2.3", "api": pm.API_VERSION,
           "kind": "feature", "scope": "rig", "entry": "plugin:Example"}
    doc.update(over)
    return doc


def third(**over):
    doc = feature(id="acme.thermal", short="therm", author="Acme",
                  description="A thermal camera's tools.", licence="MIT")
    doc.update(over)
    return doc


def refused(doc, builtin, field_name, words):
    with pytest.raises(pm.ManifestError) as caught:
        pm.check(doc, builtin=builtin)
    assert caught.value.field == field_name, (caught.value.field, caught.value)
    assert words in str(caught.value), str(caught.value)


# ======================================================================
# Reading
# ======================================================================
def test_a_good_manifest_is_read_into_plain_data(tmp_path):
    (tmp_path / pm.MANIFEST_FILE).write_text(json.dumps(feature(
        needs=["frames", "software_trigger|hardware_trigger"],
        python=["numpy>=1.24"], contributes=["panel", "record_gate"],
        files=["_capture_triggers.csv"],
        uses=[{"id": "typhon.calibration", "required": False}],
        network="none", budget_ms=3)), encoding="utf-8")
    m = pm.read(tmp_path, builtin=True)
    assert (m.id, m.short, m.kind, m.scope) == ("typhon.example", "ex",
                                                "feature", "rig")
    assert (m.module, m.attribute) == ("plugin", "Example")
    assert m.needs == ("frames", "software_trigger|hardware_trigger")
    assert m.uses == (pm.Use("typhon.calibration", False),)
    assert m.budget_ms == 3.0 and m.builtin and m.folder == tmp_path
    again = pm.check(m.as_dict(), builtin=True)
    assert again == m


def test_reading_a_manifest_imports_nothing_of_the_plugin(tmp_path):
    """Typhon lists plugins before any of their code runs (design 3.1)."""
    folder = tmp_path / "acme_boom"
    folder.mkdir()
    (folder / "plugin.py").write_text(
        "raise SystemExit('a plugin was imported to read its manifest')\n",
        encoding="utf-8")
    (folder / pm.MANIFEST_FILE).write_text(json.dumps(third(
        id="acme.boom", short="boom")), encoding="utf-8")
    before = set(sys.modules)
    m = pm.read(folder, builtin=False)
    assert m.id == "acme.boom"
    assert not {n for n in set(sys.modules) - before if "boom" in n
                or n == "plugin"}


@pytest.mark.parametrize("raw, words", [
    (b"", "not valid JSON"),
    (b"[1, 2]", "not one JSON object"),
    (b'{"schema": 1, "schema": 1}', "appears twice"),
    (b'{"budget_ms": NaN}', "not a number JSON allows"),
    (b'{"budget_ms": Infinity}', "not a number JSON allows"),
    ("{}".encode("utf-16"), "not UTF-8"),
])
def test_a_file_that_is_not_one_clean_json_object_is_refused(
        tmp_path, raw, words):
    (tmp_path / pm.MANIFEST_FILE).write_bytes(raw)
    with pytest.raises(pm.ManifestError, match=words):
        pm.read(tmp_path, builtin=True)


def test_a_huge_or_missing_manifest_is_refused(tmp_path):
    with pytest.raises(pm.ManifestError, match="no plugin.json"):
        pm.read(tmp_path, builtin=True)
    (tmp_path / pm.MANIFEST_FILE).write_text(
        " " * (pm.MAX_MANIFEST_BYTES + 1), encoding="utf-8")
    with pytest.raises(pm.ManifestError, match="over the"):
        pm.read(tmp_path, builtin=True)


def test_a_byte_order_mark_is_fine(tmp_path):
    (tmp_path / pm.MANIFEST_FILE).write_text(json.dumps(feature()),
                                            encoding="utf-8-sig")
    assert pm.read(tmp_path, builtin=True).id == "typhon.example"


# ======================================================================
# The fields
# ======================================================================
def test_an_unknown_field_is_refused_not_ignored():
    """A misspelt "contributes" would otherwise quietly attach nothing."""
    refused(feature(contribute=["panel"]), True, "contribute",
            "schema 1 does not have")


@pytest.mark.parametrize("name", pm.REQUIRED)
def test_every_required_field_is_required(name):
    doc = feature()
    del doc[name]
    refused(doc, True, name, f"no {name!r}")


@pytest.mark.parametrize("name", pm.THIRD_PARTY_REQUIRED)
def test_a_third_party_plugin_says_who_wrote_it_and_its_licence(name):
    doc = third()
    del doc[name]
    refused(doc, False, name, f"no {name!r}")
    refused(third(**{name: "  "}), False, name, "empty")
    assert pm.check(feature(), builtin=True).author == ""


@pytest.mark.parametrize("over, field_name, words", [
    ({"schema": 2}, "schema", "reads schema 1"),
    ({"schema": True}, "schema", "reads schema 1"),
    ({"id": "Typhon.Example"}, "id", "reverse-domain"),
    ({"id": "example"}, "id", "reverse-domain"),
    ({"id": "acme.thermal"}, "id", "starts 'typhon.'"),
    ({"short": "x"}, "short", "2-12"),
    ({"short": "Thermal"}, "short", "2-12"),
    ({"short": "abcdefghijklm"}, "short", "2-12"),
    ({"name": " "}, "name", "empty"),
    ({"name": 3}, "name", "not text"),
    ({"version": "1.2"}, "version", "major.minor.patch"),
    ({"version": "01.2.3"}, "version", "major.minor.patch"),
    ({"api": "1"}, "api", "plugin API version"),
    ({"kind": "widget"}, "kind", "not one of"),
    ({"scope": "window"}, "scope", "camera or rig"),
    ({"entry": "plugin"}, "entry", "module:object"),
    ({"entry": ".plugin:X"}, "entry", "module:object"),
    ({"entry": "../x:Y"}, "entry", "module:object"),
    ({"needs": "frames"}, "needs", "list of text"),
    ({"needs": ["Frames"]}, "needs", "capability name"),
    ({"needs": ["frames|"]}, "needs", "capability name"),
    ({"python": ["numpy >= 1.24; os"]}, "python", "import name"),
    ({"python": ["scikit-learn"]}, "python", "import name"),
    ({"contributes": ["menu"]}, "contributes", "not a place"),
    ({"contributes": ["overlay"]}, "contributes", "not built yet"),
    ({"contributes": ["review_source"]}, "contributes", "camera plugins"),
    ({"contributes": ["panel", "panel"]}, "contributes", "twice"),
    ({"files": ["tracks.csv"]}, "files", "run-file ending"),
    ({"files": ["_Tracks.csv"]}, "files", "run-file ending"),
    ({"files": ["_frames.csv"]}, "files", "core writes itself"),
    ({"files": ["_camera.json"]}, "files", "core writes itself"),
    ({"files": ["_run_end.json"]}, "files", "core writes itself"),
    ({"files": ["_events.raw"]}, "files", "core writes itself"),
    ({"files": ["_x.csv", "_x.csv"]}, "files", "twice"),
    ({"uses": [{"id": "acme.x", "required": "yes"}]}, "uses", "each entry"),
    ({"uses": [{"id": "acme.x", "why": "no"}]}, "uses", "each entry"),
    ({"network": "lan"}, "network", "not one of"),
    ({"budget_ms": 0}, "budget_ms", "above 0"),
    ({"budget_ms": True}, "budget_ms", "above 0"),
    ({"budget_ms": 5000}, "budget_ms", "at most"),
])
def test_each_field_is_checked(over, field_name, words):
    refused(feature(**over), True, field_name, words)


def test_scope_belongs_to_features():
    refused({k: v for k, v in feature().items() if k != "scope"}, True,
            "scope", "says its scope")
    refused(feature(kind="camera", scope="camera"), True, "scope",
            "only be rig")
    camera = {k: v for k, v in feature(kind="camera").items()
              if k != "scope"}
    assert pm.check(camera, builtin=True).scope == "rig"
    assert pm.check(feature(kind="camera", contributes=["review_source"]),
                    builtin=True).contributes == ("review_source",)


def test_the_recording_path_is_for_built_in_plugins_only():
    """The record gate and the frame stage sit inside the data-safety rules
    (design 3.11)."""
    for point in ("record_gate", "frame_stage"):
        assert pm.check(feature(contributes=[point]), builtin=True)
        refused(third(contributes=[point]), False, "contributes",
                "built-in plugins only")
    assert pm.check(third(contributes=["panel", "toolbar", "status",
                                       "run_hooks", "analyse"]),
                    builtin=False)


def test_a_third_party_plugins_run_files_carry_its_short_name():
    assert pm.check(third(files=["_therm_tracks.csv"]), builtin=False)
    refused(third(files=["_tracks.csv"]), False, "files",
            "<run>_therm_<what>")
    # A built-in keeps the name its design fixed (design 3.9).
    assert pm.check(feature(files=["_capture_triggers.csv"]), builtin=True)


def test_built_in_and_third_party_ids_are_kept_apart():
    refused(third(id="typhon.thermal"), False, "id", "kept for the plugins")
    refused(feature(id="acme.thermal"), True, "id", "starts 'typhon.'")


def test_long_texts_are_refused():
    refused(third(name="x" * (pm.MAX_TEXT + 1)), False, "name", "over the")
    refused(third(description="x" * (pm.MAX_DESCRIPTION + 1)), False,
            "description", "over the")
    refused(feature(needs=["frames"] * (pm.MAX_LIST + 1)), True, "needs",
            "over the")


# ======================================================================
# The API version (design 3.2)
# ======================================================================
@pytest.mark.parametrize("wanted, have, ok", [
    ("0.1", "0.1", True),
    ("0.1", "0.2", False),       # before 1.0 every change is allowed
    ("0.2", "0.1", False),
    ("1.0", "1.0", True),
    ("1.0", "1.3", True),        # an addition raises the minor
    ("1.2", "1.3", True),
    ("1.4", "1.3", False),       # needs a later minor
    ("2.0", "1.9", False),       # another major
    ("1.0", "2.0", False),
    ("x", "1.0", False),
])
def test_the_api_version_rule(wanted, have, ok):
    said = pm.api_problem(wanted, have)
    assert (said == "") is ok, said
    if not ok and wanted != "x":
        assert wanted in said and have in said, "both numbers named"


def test_this_typhons_api_is_0_x_until_p5_and_matches_the_package():
    import typhon_plugin_api as api
    assert pm.API_VERSION == api.API_VERSION
    assert pm.API_VERSION.startswith("0.")


# ======================================================================
# Requirements on the interpreter
# ======================================================================
@pytest.mark.parametrize("text, name, specs", [
    ("numpy", "numpy", []),
    ("numpy>=1.24", "numpy", [(">=", "1.24")]),
    ("pypylon >= 4, < 5", "pypylon", [(">=", "4"), ("<", "5")]),
    ("PIL==12.*", "PIL", [("==", "12.*")]),
])
def test_requirements_are_parsed(text, name, specs):
    assert pm.requirement_parts(text) == (name, specs)


@pytest.mark.parametrize("version, specs, ok", [
    ("1.24.0", [(">=", "1.24")], True),
    ("1.23.9", [(">=", "1.24")], False),
    ("2.3.5", [(">=", "1.24"), ("<", "3")], True),
    ("3.0", [("<", "3")], False),
    ("1.24.0rc1", [(">=", "1.24")], True),
    ("12.0.0", [("==", "12.*")], True),
    ("11.3.0", [("==", "12.*")], False),
    ("1.4.2", [("~=", "1.4")], True),
    ("2.0", [("~=", "1.4")], False),
    ("1.0", [("!=", "1.0.0")], False),
    ("1.0", [("==", "1")], True),
])
def test_versions_are_compared_by_their_release_numbers(version, specs, ok):
    assert pm.version_ok(version, specs) is ok
