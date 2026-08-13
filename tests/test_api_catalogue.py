"""
The vault API catalogue and its two agent tools.

The load-bearing test here is test_building_the_catalogue_never_executes_vault_code.
"Turn methods into tools" has an unsafe reading — let the agent CALL them —
which requires importing arbitrary customer or vendor Python, and an import runs
the module's top level. A vendor example script can open files or spawn a
process before any function is called. The catalogue is a LOOKUP surface for
exactly that reason, and the test proves it by planting a file that writes a
marker at import time and asserting the marker never appears.

Run:  python -m pytest tests/test_api_catalogue.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_catalogue as ac  # noqa: E402

API = '''"""AcmeCAD helpers."""
import acmecad as acme

DEFAULT_SIZE = 0.5


def mesh_part(part, element_size: float = DEFAULT_SIZE,
              quadratic: bool = False) -> bool:
    """Generate a finite-element mesh. element_size is in millimetres."""
    return True


def export_stl(part, path, binary: bool = True) -> str:
    """Write the part to an STL file."""
    return path


def _internal(x):
    """Not public API."""


class Session:
    """An AcmeCAD session. Not thread-safe."""

    def __init__(self, licence_key, timeout: int = 30):
        pass

    def heal(self, part, tolerance: float = 1e-6):
        """Repair non-manifold geometry."""
'''


@pytest.fixture()
def vault(tmp_path):
    d = tmp_path / "vault" / "data_in"
    d.mkdir(parents=True)
    (d / "acme_api.py").write_text(API, encoding="utf-8")
    ac.clear_cache()
    return tmp_path / "vault"


# ============================================================
# THE SAFETY PROPERTY
# ============================================================

def test_building_the_catalogue_never_executes_vault_code(tmp_path):
    """A vault file's TOP LEVEL must never run. Importing to introspect would
    be the obvious implementation and is exactly the trap: a vendor example
    script can do anything before a single function is called."""
    v = tmp_path / "vault" / "data_in"
    v.mkdir(parents=True)
    marker = tmp_path / "SIDE_EFFECT_RAN"
    (v / "hostile.py").write_text(
        "from pathlib import Path\n"
        f"Path(r'{marker}').write_text('executed')\n\n\n"
        "def looks_innocent(a, b=2):\n"
        '    """A perfectly normal function."""\n'
        "    return a + b\n", encoding="utf-8")
    ac.clear_cache()

    cat = ac.build(tmp_path / "vault")

    assert not marker.exists(), (
        "the catalogue EXECUTED a vault file — it must only parse")
    # ...and it still catalogued the function, so safety cost nothing.
    assert cat.get("looks_innocent") is not None


def test_the_tools_do_not_import_or_exec_vault_modules():
    """Structural backstop for the same property."""
    import ast
    import inspect
    import council_gui_engine as cge
    src = inspect.getsource(cge._make_tools)
    tree = ast.parse(src.lstrip())
    fns = [f for f in ast.walk(tree)
           if isinstance(f, ast.FunctionDef) and f.name.startswith("api_")]
    assert len(fns) == 2, "expected api_search and api_signature"
    for f in fns:
        body = ast.get_source_segment(src.lstrip(), f) or ""
        for banned in ("importlib", "__import__", "exec(", "eval(",
                       "subprocess", "runpy"):
            assert banned not in body, f"{f.name} must not use {banned}"


# ============================================================
# Building
# ============================================================

def test_catalogue_indexes_public_callables(vault):
    cat = ac.build(vault)
    names = {e.name for e in cat.entries}
    assert {"mesh_part", "export_stl", "Session", "Session_heal"} <= names
    assert cat.files_scanned == 1


def test_private_callables_are_excluded(vault):
    """A leading underscore is not the API a user is meant to call."""
    assert cat_names(ac.build(vault)).isdisjoint({"_internal"})


def cat_names(cat):
    return {e.name for e in cat.entries}


def test_caches_and_rebuilds_when_the_corpus_changes(vault):
    first = ac.build(vault)
    assert ac.build(vault) is first, "an unchanged corpus must not re-parse"

    (vault / "data_in" / "more.py").write_text(
        'def brand_new(z):\n    """Added later."""\n', encoding="utf-8")
    again = ac.build(vault)
    assert again is not first
    assert again.get("brand_new") is not None


def test_generated_and_cache_directories_are_skipped(vault):
    for junk in ("__pycache__", ".backups", "site-packages", ".venv"):
        d = vault / "data_in" / junk
        d.mkdir(parents=True, exist_ok=True)
        (d / "noise.py").write_text(
            'def should_not_appear(q):\n    """junk."""\n', encoding="utf-8")
    ac.clear_cache()
    assert "should_not_appear" not in cat_names(ac.build(vault))


def test_broken_source_does_not_stop_the_catalogue(vault):
    (vault / "data_in" / "broken.py").write_text(
        "def nope(:\n", encoding="utf-8")
    ac.clear_cache()
    cat = ac.build(vault)
    assert cat.get("mesh_part") is not None, (
        "one unparseable file must not cost the rest of the corpus")


def test_an_empty_vault_is_not_an_error(tmp_path):
    ac.clear_cache()
    cat = ac.build(tmp_path / "nothing_here")
    assert len(cat) == 0 and cat.files_scanned == 0


# ============================================================
# Search + lookup
# ============================================================

def test_search_finds_by_intent(vault):
    cat = ac.build(vault)
    names = [h["name"] for h in cat.search("how do I mesh a part")]
    assert "mesh_part" in names
    assert "export_stl" in [h["name"] for h in cat.search("write an STL file")]
    assert "Session_heal" in [h["name"] for h in
                              cat.search("repair non-manifold geometry")]


def test_a_name_hit_outranks_a_docstring_hit(vault):
    """Someone asking about "export" wants export_stl, not a function whose
    docstring happens to mention exporting."""
    cat = ac.build(vault)
    assert cat.search("export")[0]["name"] == "export_stl"


def test_search_splits_snake_and_camel_case(vault):
    """A user does not know which convention the vendor used."""
    cat = ac.build(vault)
    assert "mesh_part" in [h["name"] for h in cat.search("element size")]


def test_get_is_exact_then_forgiving(vault):
    cat = ac.build(vault)
    assert cat.get("mesh_part")["name"] == "mesh_part"
    assert cat.get("Session.heal")["name"] == "Session_heal", (
        "dotted form must resolve — a model writes it that way")
    assert cat.get("definitely_not_here") is None


def test_describe_shows_the_signature_and_a_blank_template(vault):
    cat = ac.build(vault)
    text = ac.describe(cat.get("mesh_part"))
    assert "element_size: float = DEFAULT_SIZE" in text, "real default"
    assert "-> bool" in text
    assert "millimetres" in text, "the docstring is the disambiguator"
    assert "acme_api.py:" in text, "a citation the user can open"
    assert "template: mesh_part(part=<part>)" in text, (
        "only REQUIRED args appear as blanks; optionals are already answered")


def test_self_is_never_offered_on_a_method(vault):
    cat = ac.build(vault)
    spec = cat.get("Session_heal")
    assert "self" not in [p["name"] for p in spec["parameters"]]


# ============================================================
# The agent tools
# ============================================================

def tools_for(vault):
    import council_gui_engine as cge
    return cge._make_tools(None, None, vault)


def test_both_tools_are_registered(vault):
    names = set(tools_for(vault))
    assert {"api_search", "api_signature"} <= names
    assert "run_python" in names, "the existing tools must survive"


def test_api_search_returns_signatures_not_prose(vault):
    ok, text, payload = tools_for(vault)["api_search"]({"query": "mesh"})
    assert ok and "mesh_part(" in text and "template:" in text
    assert payload["matches"] and payload["indexed"] >= 4


def test_api_signature_returns_one_exact_entry(vault):
    ok, text, payload = tools_for(vault)["api_signature"]({"name": "export_stl"})
    assert ok and payload["found"]
    assert payload["spec"]["name"] == "export_stl"
    assert "binary: bool = True" in text


def test_a_near_miss_suggests_rather_than_failing(vault):
    """A model will write `mesh` for `mesh_part` as often as not; refusing an
    almost-right name helps nobody."""
    ok, text, payload = tools_for(vault)["api_signature"]({"name": "mesh"})
    assert ok and not payload["found"]
    assert "mesh_part" in text and "Did you mean" in text


def test_no_match_says_how_much_was_searched(vault):
    """A negative is only worth anything if the coverage is stated."""
    ok, text, payload = tools_for(vault)["api_search"](
        {"query": "quantum entanglement"})
    assert ok and payload["matches"] == []
    assert "indexed" in text and "file(s)" in text


def test_empty_args_are_rejected_clearly(vault):
    t = tools_for(vault)
    assert t["api_search"]({"query": "  "})[0] is False
    assert t["api_signature"]({})[0] is False


def test_k_is_clamped_not_trusted(vault):
    """A model emitting k=99999 must not turn one tool call into the whole
    catalogue pasted into the context window."""
    t = tools_for(vault)
    _ok, _text, payload = t["api_search"]({"query": "part", "k": 99999})
    assert len(payload["matches"]) <= 20
    _ok, _text, payload = t["api_search"]({"query": "part", "k": "nonsense"})
    assert payload["matches"], "a junk k must fall back, not crash"
