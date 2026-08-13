"""
Code-aware chunking and signature extraction.

The point of the module is that a retrieved chunk of source is a WHOLE callable
with its imports, instead of an 800-character window that lands mid-function.
These assert that directly, and the same AST walk's second output — the
signature catalogue that makes a call a fill-in-the-blanks template.

Run:  python -m pytest tests/test_code_chunks.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import code_chunks as cc  # noqa: E402

SRC = '''"""Module docstring: AcmeCAD helpers."""
import acmecad as acme
from acmecad.units import mm

DEFAULT_SIZE = 0.5


def prepare(path, heal=True):
    """Load and repair."""
    s = acme.Session()
    return s, s.load(path)


@staticmethod
def helper(x: int = 3) -> str:
    """Decorated."""
    return str(x)


class Mesher:
    """Meshes things."""

    def __init__(self, session, element_size: float = DEFAULT_SIZE):
        self.session = session

    def run(self, part, *parts, quadratic: bool = True, **opts) -> bool:
        """Mesh it."""
        return True

    def _private(self):
        pass
'''


def by_name(chunks, name):
    return next((c for c in chunks if c.get("name") == name), None)


# ============================================================
# Chunk boundaries
# ============================================================

def test_python_splits_on_definitions_not_characters():
    chunks = cc.chunk_source(SRC, "ex.py", ".py")
    names = [c["name"] for c in chunks]
    assert "<module>" in names
    assert "prepare" in names and "helper" in names and "Mesher" in names
    kinds = {c["name"]: c["kind"] for c in chunks}
    assert kinds["prepare"] == "function" and kinds["Mesher"] == "class"


def test_every_code_chunk_carries_the_imports():
    """A function retrieved alone does not say Session came from
    `import acmecad as acme` — and for a docs assistant that IS half the
    answer."""
    for c in cc.chunk_source(SRC, "ex.py", ".py"):
        assert "import acmecad as acme" in c["text"], c["name"]


def test_a_decorated_function_keeps_its_decorator():
    """ast puts a decorated function's lineno on the `def`, so slicing from
    there silently drops @staticmethod — and a method that lost its decorator
    reads as a different thing."""
    c = by_name(cc.chunk_source(SRC, "ex.py", ".py"), "helper")
    assert "@staticmethod" in c["text"]


def test_the_module_preamble_is_its_own_chunk():
    """Module constants and the install line live here; dropping it loses
    DEFAULT_SIZE and the module docstring."""
    c = by_name(cc.chunk_source(SRC, "ex.py", ".py"), "<module>")
    assert "DEFAULT_SIZE = 0.5" in c["text"]
    assert "Module docstring" in c["text"]


def test_a_large_class_splits_per_method_keeping_the_class_line():
    # NB: EVERY body line needs the indent. The first version of this fixture
    # indented only the first, so the source did not parse and split_python
    # correctly returned None — the test was wrong, not the chunker.
    big = "class Big:\n" + "".join(
        f"    def m{i}(self):\n" + f"        x = {i}\n" * 40 for i in range(6))
    chunks = cc.split_python(big, "big.py")
    method_chunks = [c for c in chunks if c.kind == "method"]
    assert method_chunks, "a huge class must not be one chunk"
    for c in method_chunks:
        assert "class Big:" in c.text, (
            "a retrieved method must still say what it belongs to")


def test_an_oversized_function_splits_on_line_boundaries():
    huge = "def f():\n" + "".join(f"    line_{i} = {i}\n" for i in range(900))
    chunks = cc.split_python(huge, "huge.py")
    fn = [c for c in chunks if c.name == "f"]
    assert len(fn) > 1, "a 900-line function cannot be one chunk"
    assert all(len(c.text) <= cc.MAX_CHUNK_CHARS + 200 for c in fn)
    assert any("continued from" in c.text for c in fn[1:]), (
        "a retrieved tail must not be an anonymous block")
    for c in fn:
        assert not c.text.endswith("line_1"), "never split mid-line"


def test_unparseable_python_declines_so_the_caller_falls_back():
    """One broken example script must not cost the whole corpus."""
    assert cc.chunk_source("def broken(:\n  pass", "b.py", ".py") is None
    assert cc.split_python("def broken(:", "b.py") is None


def test_prose_is_declined_so_it_keeps_window_chunking():
    assert cc.chunk_source("# Title\n\nSome docs.\n", "readme.md", ".md") is None


def test_braced_languages_get_a_heuristic_not_a_window():
    js = ("function alpha() {\n  return 1;\n}\n\n"
          "function beta(x) {\n  return x * 2;\n}\n")
    got = cc.chunk_source(js, "x.js", ".js")
    assert got and len(got) >= 2, "should split at the blank line before beta"


# ============================================================
# Signatures — the mad-libs catalogue
# ============================================================

def test_signatures_capture_annotations_and_defaults():
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    run = sigs["Mesher.run"]
    names = [p.name for p in run.params]
    assert names[:2] == ["self", "part"]
    kinds = {p.name: p.kind for p in run.params}
    assert kinds["parts"] == "vararg" and kinds["opts"] == "kwarg"
    assert kinds["quadratic"] == "kwonly"
    q = next(p for p in run.params if p.name == "quadratic")
    assert q.annotation == "bool" and q.default == "True" and not q.required
    assert next(p for p in run.params if p.name == "part").required
    assert run.returns == "bool" and run.doc == "Mesh it."


def test_defaults_bind_to_the_tail_of_the_positional_list():
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    prep = sigs["prepare"]
    assert next(p for p in prep.params if p.name == "path").required
    heal = next(p for p in prep.params if p.name == "heal")
    assert heal.default == "True" and not heal.required


def test_a_class_carries_its_constructor_signature():
    """A class's call signature IS __init__ — that is what a user needs to
    instantiate it."""
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    m = sigs["Mesher"]
    assert m.kind == "class"
    assert [p.name for p in m.params][:2] == ["self", "session"]
    assert next(p for p in m.params if p.name == "element_size").default \
        == "DEFAULT_SIZE"


def test_render_call_fills_only_the_blanks():
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    prep = sigs["prepare"]
    assert prep.render_call({"path": "a.step"}) == "prepare(path='a.step')"
    # A missing REQUIRED argument is a visible placeholder, not an omission —
    # an incomplete call must look incomplete, not raise TypeError later.
    assert "<" in prep.render_call({})
    assert prep.render_call({"path": "x"}, receiver="mod") == "mod.prepare(path='x')"


def test_self_is_never_offered_as_an_argument():
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    spec = sigs["Mesher.run"].to_tool_spec()
    assert "self" not in [p["name"] for p in spec["parameters"]]
    assert "self" not in sigs["Mesher.run"].render_call({})


def test_tool_spec_shape():
    sigs = {s.qualname: s for s in cc.extract_signatures(SRC, "ex.py")}
    spec = sigs["prepare"].to_tool_spec()
    assert spec["name"] == "prepare"
    assert spec["description"] == "Load and repair."
    assert "ex.py:" in spec["source"]
    path = next(p for p in spec["parameters"] if p["name"] == "path")
    assert path["required"] and "default" not in path
    heal = next(p for p in spec["parameters"] if p["name"] == "heal")
    assert not heal["required"] and heal["default"] == "True"


def test_catalogue_excludes_private_callables(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(SRC, encoding="utf-8")
    names = {s["name"] for s in cc.build_catalogue([f])}
    assert "prepare" in names and "Mesher_run" in names
    assert not any("_private" in n for n in names), (
        "a leading underscore is not the API a user is meant to call")


def test_signature_extraction_survives_broken_source():
    assert cc.extract_signatures("def f(:", "b.py") == []


# ============================================================
# vault_rag integration
# ============================================================

def test_vault_rag_routes_python_to_code_chunks_and_prose_to_windows():
    import vault_rag as vr
    code = vr._chunk_for(SRC, "ex.py", ".py")
    assert any(c.get("kind") == "function" for c in code), (
        "vault_rag is not using the code-aware path")

    prose = vr._chunk_for("# Docs\n\n" + ("word " * 900), "d.md", ".md")
    assert len(prose) > 1 and all("kind" not in c for c in prose), (
        "prose must keep the overlapping window chunker")


def test_a_chunker_failure_never_costs_the_file(monkeypatch):
    """A bug in the chunker must degrade to window chunking, not drop the
    document."""
    import vault_rag as vr
    import code_chunks as _cc
    monkeypatch.setattr(_cc, "chunk_source",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    got = vr._chunk_for(SRC, "ex.py", ".py")
    assert got, "the file was lost when the chunker raised"
