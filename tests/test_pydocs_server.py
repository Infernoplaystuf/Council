"""
tools/pydocs_mcp_server.py — documentation by reading source, served over MCP.

The index is tested in-process (fast, and lets a test prove nothing was
imported); the protocol end to end through the real client and a real
subprocess. Two tests read REAL installed packages — numpy in this env and
simplnx in the nxpython env — because an index that only works on a fixture
package is not the feature.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import mcp_client as mc  # noqa: E402

SERVER = ROOT / "tools" / "pydocs_mcp_server.py"
BENCH = ROOT / "tests" / "data" / "docsbench"

_spec = importlib.util.spec_from_file_location("pydocs_mcp_server", SERVER)
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)


@pytest.fixture(scope="module")
def gq():
    return pd.build_index(pd.Finder([str(BENCH)]), "glimmerquay")


def write_pkg(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


# ============================================================ the index

def test_reading_never_imports_the_package(gq):
    assert "glimmerquay" not in sys.modules
    assert importlib.util.find_spec("glimmerquay") is None, \
        "the benchmark package must not be importable from the test env"


def test_objects_get_their_public_names(gq):
    ledger = gq.entries["glimmerquay.ledger.Ledger"]
    assert ledger["public"] == "glimmerquay.Ledger"
    assert gq.lookup("glimmerquay.Ledger") is ledger
    assert gq.lookup("glimmerquay.ledger.Ledger") is ledger
    method = gq.lookup("glimmerquay.Ledger.append")
    assert method["kind"] == "method"
    assert "1000" in method["doc"]


def test_signatures_come_from_the_source(gq):
    e = gq.lookup("glimmerquay.encode_frame")
    assert e["sig"] == ("payload: bytes, *, checksum: str = 'fletcher16', "
                        "pad_to: int = 8")
    assert pd.signature_line(e).startswith("glimmerquay.encode_frame(")
    assert "-> bytes" in pd.signature_line(e)


def test_a_constant_keeps_its_value_and_attribute_docstring(gq):
    e = gq.lookup("glimmerquay.DEFAULT_TIMEOUT_MS")
    assert e["kind"] == "constant" and e["value"] == "2750"
    assert "milliseconds" in e["doc"]


def test_search_ranks_the_right_object_first(gq):
    for query, expected in [("ledger capacity", "glimmerquay.Ledger"),
                            ("default checksum frame",
                             "glimmerquay.encode_frame"),
                            ("millivolts unit", "glimmerquay.to_millivolts"),
                            ("encode_frame", "glimmerquay.encode_frame")]:
        top = gq.search(query, 3)[0][1]
        assert top["public"] == expected, (query, top["public"])


def test_private_names_only_when_named(gq):
    found = [e["public"] for _s, e in gq.search("units millivolts", 25)]
    assert not any("._" in p for p in found), found
    named = [e["qual"] for _s, e in gq.search("_TO_MV", 5)]
    assert "glimmerquay.units._TO_MV" in named


def test_a_page_has_signature_docstring_members_and_a_short_path(gq):
    page = pd.render_page(gq, gq.lookup("glimmerquay.Ledger"))
    assert "class glimmerquay.Ledger(capacity: int = 64" in page
    assert '"rotate" discards the oldest entry' in page
    assert "glimmerquay.Ledger.append(entry" in page
    assert "Defined in glimmerquay/ledger.py, line" in page
    assert str(BENCH) not in page, "an absolute path wastes prompt tokens"


def test_a_broken_stub_line_loses_one_declaration_not_the_file(tmp_path):
    """simplnx.pyi declares `2d: bool`; before parse_tolerant the whole
    159 KB stub — every filter — read as nothing."""
    write_pkg(tmp_path, {"stubby.pyi": """
        class Crop:
            class ValueType:
                2d: bool
                crop_x: bool
            def execute(self, data, crop_x: bool = ..., x_bound=...) -> int: ...
        def helper(a: int) -> str: ...
    """})
    idx = pd.build_index(pd.Finder([str(tmp_path)]), "stubby")
    assert not idx.errors
    assert idx.lookup("stubby.Crop.execute")["sig"].startswith("data, crop_x")
    assert idx.lookup("stubby.helper") is not None


def test_reexports_and_dunder_all_decide_public_names(tmp_path):
    write_pkg(tmp_path, {
        "pk/__init__.py": "from ._impl import *\nfrom .sub import Thing as "
                          "Widget\n",
        "pk/_impl.py": "__all__ = ['shown']\ndef shown(x):\n    'Shown "
                       "helper.'\ndef hidden(y):\n    'Secret helper.'\n",
        "pk/sub.py": "class Thing:\n    'A thing.'\n    def go(self, fast="
                     "False):\n        'Go.'\n",
    })
    idx = pd.build_index(pd.Finder([str(tmp_path)]), "pk")
    assert idx.entries["pk._impl.shown"]["public"] == "pk.shown"
    assert idx.entries["pk._impl.hidden"]["public"] == "pk._impl.hidden"
    assert idx.lookup("pk.Widget")["qual"] == "pk.sub.Thing"
    assert idx.lookup("pk.Widget.go")["sig"] == "fast=False"
    found = [e["public"] for _s, e in idx.search("secret helper", 10)]
    assert "pk.shown" in found and "pk._impl.hidden" not in found, found
    assert "pk._impl.hidden" in [e["public"] for _s, e in
                                 idx.search("hidden", 10)], \
        "a private name typed exactly is still found"


def test_a_stub_declaring_an_imported_name_is_one_object(tmp_path):
    """numpy/__init__.pyi declares `class matrix` (no docstring) while
    __init__.py imports it from numpy.matrixlib: the index had two entries
    and two pages for one object, and one question read both — [1]
    numpy.matrix, "(No docstring.)", and [4] numpy.matrixlib.defmatrix.matrix.
    The sub-package's stub importing the name back from the top (as
    numpy/matrixlib/__init__.pyi does) must not loop."""
    write_pkg(tmp_path, {
        "tw/__init__.py": "from .core import Grid, blend\n",
        "tw/__init__.pyi": "class Grid:\n    def warp(self, k: float) -> None"
                           ": ...\ndef blend(a: int) -> int: ...\n",
        "tw/core.py": "class Grid:\n    'A grid of cells.'\n    def warp(self,"
                      " k=1.0):\n        'Warp the grid by k.'\n"
                      "def blend(a):\n    'Blend a.'\n    return a\n",
        "tw/sub/__init__.py": "from ..core import *\n",
        "tw/sub/__init__.pyi": "from tw import Grid as Grid\n",
    })
    idx = pd.build_index(pd.Finder([str(tmp_path)]), "tw")
    grid = idx.lookup("tw.Grid")
    assert (grid["qual"], grid["public"], grid["doc"]) == (
        "tw.core.Grid", "tw.Grid", "A grid of cells.")
    assert idx.lookup("tw.Grid.warp")["doc"] == "Warp the grid by k."
    assert idx.lookup("tw.blend")["doc"] == "Blend a."
    assert idx.lookup("tw.sub.Grid")["qual"] == "tw.core.Grid"
    found = [e["public"] for _s, e in idx.search("grid cells warp", 10)]
    assert found.count("tw.Grid") == 1 and found.count("tw.Grid.warp") == 1, \
        found
    page = pd.render_page(idx, grid)
    assert "(No docstring.)" not in page and "A grid of cells." in page


def test_a_documented_declaration_stays(tmp_path):
    """numpy's C types (ndarray, dtype) are declared in the stub and get
    their docstrings from add_newdoc: there is no source definition to
    merge them into, and they must keep their pages."""
    write_pkg(tmp_path, {
        "cy/__init__.py": "from ._native import Arr\n",
        "cy/__init__.pyi": "class Arr:\n    def size(self) -> int: ...\n",
        "cy/_docs.py": "from numpy._core.function_base import add_newdoc\n"
                       "add_newdoc('cy', 'Arr', 'An array.')\n",
    })
    idx = pd.build_index(pd.Finder([str(tmp_path)]), "cy")
    assert idx.lookup("cy.Arr")["doc"] == "An array."
    assert idx.lookup("cy.Arr.size") is not None


def test_add_newdoc_calls_document_compiled_functions(tmp_path):
    """numpy documents its C functions this way; read as data, not run."""
    write_pkg(tmp_path, {
        "cx/__init__.py": "from .core import *\n",
        "cx/core.py": "from ._native import *\n",
        "cx/core.pyi": "def blend(a: int, b: int) -> int: ...\n"
                       "class Grid:\n    def warp(self, k: float) -> None: "
                       "...\n",
        "cx/_docs.py": "from .core import add_newdoc\n"
                       "add_newdoc('cx.core', 'blend', 'Blend two ints.')\n"
                       "add_newdoc('cx.core', 'Grid', ('warp', 'Warp it.'))\n",
    })
    idx = pd.build_index(pd.Finder([str(tmp_path)]), "cx")
    assert idx.lookup("cx.blend")["doc"] == "Blend two ints."
    assert idx.lookup("cx.Grid.warp")["doc"] == "Warp it."


def test_the_index_round_trips_through_json(gq, tmp_path):
    again = pd.PackageIndex.from_json(json.loads(json.dumps(gq.to_json())))
    assert [e["public"] for _s, e in again.search("ledger capacity", 3)] == \
        [e["public"] for _s, e in gq.search("ledger capacity", 3)]


def test_the_cache_is_json_and_is_used(tmp_path):
    lib = pd.Library(pd.Finder([str(BENCH)]), ["glimmerquay"],
                     str(tmp_path / "cache"))
    lib.index("glimmerquay")
    files = list((tmp_path / "cache").glob("glimmerquay-*.json"))
    assert len(files) == 1
    json.loads(files[0].read_text(encoding="utf-8"))
    again = pd.Library(pd.Finder([str(BENCH)]), ["glimmerquay"],
                       str(tmp_path / "cache"))
    assert again._from_cache("glimmerquay") is not None


# ============================================================ over MCP

def serve(*args, **kw) -> mc.McpClient:
    return mc.McpClient.stdio(sys.executable, [str(SERVER), *args],
                              timeout=60, **kw)


@pytest.fixture(scope="module")
def bench_client():
    c = serve("--path", str(BENCH), "--package", "glimmerquay").connect()
    yield c
    c.close()


def test_the_server_lists_three_tools_with_schemas(bench_client):
    tools = {t["name"]: t for t in bench_client.list_tools()}
    assert set(tools) == {"search_docs", "get_doc", "list_packages"}
    assert tools["search_docs"]["inputSchema"]["required"] == ["query"]
    assert "outputSchema" in tools["search_docs"]


def test_search_returns_structured_results(bench_client):
    r = bench_client.call_tool("search_docs", {"query": "ledger capacity"})
    assert not r.is_error
    names = [x["name"] for x in r.structured["results"]]
    assert names[0] == "glimmerquay.Ledger"
    assert "1. glimmerquay.Ledger [class]" in r.text


def test_get_doc_and_its_did_you_mean(bench_client):
    page = bench_client.call_tool("get_doc", {"name": "glimmerquay.Cadence"})
    assert 'anchor : {"epoch", "start"}' in page.text
    miss = bench_client.call_tool("get_doc", {"name": "Ledger.apend"})
    assert miss.is_error
    assert "glimmerquay.Ledger.append" in miss.text


def test_resources_are_the_modules_and_read_as_pages(bench_client):
    uris = [r["uri"] for r in bench_client.list_resources()]
    assert "pydoc://glimmerquay.codec" in uris
    text = mc.content_text(bench_client.read_resource(
        "pydoc://glimmerquay.units"))
    assert "GAIN_PRESETS" in text
    with pytest.raises(mc.McpError) as err:
        bench_client.read_resource("pydoc://glimmerquay.nothing_here")
    assert err.value.code == -32002


def test_an_old_client_gets_no_structured_content():
    with serve("--path", str(BENCH), "--package", "glimmerquay") as c:
        c.protocols = ("2024-11-05",)
        c.connect()
        assert c.protocol_version == "2024-11-05"
        assert all("outputSchema" not in t for t in c.list_tools())
        r = c.call_tool("search_docs", {"query": "ledger"})
        assert r.structured is None and "glimmerquay.Ledger" in r.text


def test_no_package_is_a_helpful_tool_error():
    with serve("--path", str(BENCH)).connect() as c:
        r = c.call_tool("search_docs", {"query": "ledger capacity"})
        assert r.is_error and "package=" in r.text
        named = c.call_tool("search_docs",
                            {"query": "glimmerquay ledger capacity"})
        assert not named.is_error, "a package named in the query is enough"


def test_protocol_errors_have_their_codes(bench_client):
    with pytest.raises(mc.McpError) as err:
        bench_client.request("prompts/get", {"name": "x"})
    assert err.value.code == -32601
    with pytest.raises(mc.McpError) as err:
        bench_client.call_tool("no_such_tool")
    assert err.value.code == -32602


def test_junk_input_gets_a_parse_error_and_stdout_stays_clean():
    proc = subprocess.run(
        [sys.executable, str(SERVER), "--path", str(BENCH)],
        input=b"this is not json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n",
        capture_output=True, timeout=60)
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ============================================================ real packages

def test_numpy_from_its_source():
    """numpy: 6,900+ objects, C functions documented by add_newdoc, public
    names three modules away from their definitions."""
    if importlib.util.find_spec("numpy") is None:
        pytest.skip("numpy is not installed here")
    lib = pd.Library(pd.Finder([p for p in sys.path if p]), ["numpy"])
    idx = lib.index("numpy")
    top = [e["public"] for _s, e in idx.search("solve linear system", 3)]
    assert "numpy.linalg.solve" in top, top
    assert "numpy.save" == idx.search("save array to file", 1)[0][1][
        "public"]
    arr = idx.lookup("numpy.array")
    assert arr["doc"] and "Create an array" in arr["doc"]
    assert idx.lookup("numpy.sum")["public"] == "numpy.sum"


NXPYTHON = Path.home() / "miniconda3" / "envs" / "nxpython" / "python.exe"


def test_simplnx_in_the_nxpython_env():
    """The Dream3D gap: write_script has no catalog grounding. The stub has
    every filter's execute() signature; this is that grounding."""
    if not NXPYTHON.is_file():
        pytest.skip("no nxpython env on this machine")
    lib = pd.make_library(["--python", str(NXPYTHON), "--package",
                           "simplnx"])
    idx = lib.index("simplnx")
    if idx is None:
        pytest.skip("simplnx is not installed in nxpython")
    assert len(idx.entries) > 1000
    top = [e["public"] for _s, e in idx.search("read csv file", 3)]
    assert "simplnx.ReadCSVFileFilter" in top, top
    execute = idx.lookup("simplnx.ReadCSVFileFilter.execute")
    assert "data_structure" in execute["sig"]
