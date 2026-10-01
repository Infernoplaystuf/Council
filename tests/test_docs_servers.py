"""
council_core.docs_servers — the list of documentation servers, and roles.

Every test writes to a temp file; the default path (the app folder) is only
ever resolved, under the session sandbox from tests/sandbox_vault.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import docs_servers as ds  # noqa: E402

FIXTURE = ROOT / "tests" / "data" / "mcp_fixture_server.py"


def fixture_spec(name="fixture", *flags) -> ds.ServerSpec:
    return ds.ServerSpec(name=name, command=sys.executable,
                         args=[str(FIXTURE), *flags], timeout=10)


# ============================================================ the file

def test_no_file_means_the_bundled_server(tmp_path):
    servers = ds.load(tmp_path)
    assert len(servers) == 1 and servers[0].bundled
    assert servers[0].command == ds.PYTHON_TOKEN
    assert not (tmp_path / ds.FILE_NAME).exists(), "loading wrote a file"


def test_the_default_lives_in_the_app_folder_not_the_vault():
    """The vault is committed to git by the Librarian; a server entry is a
    command line and may carry tokens."""
    from council_core import paths
    assert ds.config_path().parent == paths.app_dir()
    assert ds.config_path().parent != paths.vault_dir()


def test_save_and_load_round_trip(tmp_path):
    a = fixture_spec("a")
    b = ds.ServerSpec(name="b", transport="http",
                      url="http://127.0.0.1:9/mcp", packages=["numpy"])
    ds.save([a, b], tmp_path)
    again = ds.load(tmp_path)
    assert [s.to_json() for s in again] == [a.to_json(), b.to_json()]
    data = json.loads((tmp_path / ds.FILE_NAME).read_text(encoding="utf-8"))
    assert data["format"] == ds.FORMAT


def test_a_damaged_file_says_why_and_falls_back(tmp_path):
    (tmp_path / ds.FILE_NAME).write_text("{not json", encoding="utf-8")
    assert ds.load(tmp_path)[0].bundled
    assert "could not be read" in ds.problem(tmp_path)


@pytest.mark.parametrize("spec,words", [
    (ds.ServerSpec(name=""), "name"),
    (ds.ServerSpec(name="x", transport="stdio"), "command"),
    (ds.ServerSpec(name="x", transport="http", url="ftp://a"), "http://"),
    (ds.ServerSpec(name="x", transport="http",
                   url="http://docs.example.com/mcp"), "Allow remote"),
    (ds.ServerSpec(name="x", transport="carrier-pigeon", command="c"),
     "Transport"),
])
def test_validation_says_what_is_wrong(spec, words):
    assert words in ds.validate(spec)


def test_a_remote_server_is_allowed_only_when_ticked():
    spec = ds.ServerSpec(name="r", transport="http",
                         url="https://docs.example.com/mcp",
                         allow_remote=True)
    assert ds.validate(spec) == ""


def test_add_refuses_a_duplicate_and_remove_removes(tmp_path):
    ds.add(fixture_spec("one"), tmp_path)
    with pytest.raises(ValueError, match="already a server"):
        ds.add(fixture_spec("one"), tmp_path)
    assert [s.name for s in ds.load(tmp_path)] == [ds.BUNDLED_NAME, "one"]
    ds.remove("one", tmp_path)
    assert [s.name for s in ds.load(tmp_path)] == [ds.BUNDLED_NAME]


def test_placeholders_expand_at_start_time():
    command, args, _env, _cwd = ds.expand(ds.bundled_spec())
    assert command == sys.executable
    assert Path(args[0]) == ds.APP_ROOT / "tools" / "pydocs_mcp_server.py"
    assert "{cache}" not in " ".join(args)


@pytest.mark.parametrize("line,command,args", [
    ("python -m docs_server --port 0", "python", ["-m", "docs_server",
                                                  "--port", "0"]),
    ('"C:\\Program Files\\Py\\python.exe" server.py',
     "C:\\Program Files\\Py\\python.exe", ["server.py"]),
    ("", "", []),
])
def test_command_lines_split_like_a_shell(line, command, args):
    assert ds.parse_command_line(line) == (command, args)


# ============================================================ roles

def _tool(tool_name, desc="", **props):
    return {"name": tool_name, "description": desc,
            "inputSchema": {"type": "object", "properties": {
                k: {"type": v} for k, v in props.items()},
                "required": list(props)[:1]}}


def test_the_bundled_tool_names_are_recognised():
    tools = [_tool("search_docs", query="string", package="string"),
             _tool("get_doc", name="string"),
             _tool("list_packages", prefix="string")]
    r = ds.detect_roles(tools)
    assert (r.search_tool, r.search_arg, r.package_arg) == (
        "search_docs", "query", "package")
    assert (r.fetch_tool, r.fetch_arg) == ("get_doc", "name")


def test_generic_search_and_fetch_names():
    tools = [_tool("fetch", "Fetch a document by id", id="string"),
             _tool("search", "Search the docs", query="string")]
    r = ds.detect_roles(tools)
    assert (r.search_tool, r.fetch_tool, r.fetch_arg) == ("search", "fetch",
                                                         "id")


def test_unusual_argument_names_are_found_from_the_schema():
    tools = [_tool("query_docs", "Query documentation", question="string"),
             _tool("read_page", "Read one page", url="string")]
    r = ds.detect_roles(tools)
    assert (r.search_tool, r.search_arg) == ("query_docs", "question")
    assert (r.fetch_tool, r.fetch_arg) == ("read_page", "url")


def test_no_fetch_tool_falls_back_to_resources():
    r = ds.detect_roles([_tool("search_documentation", q="string")],
                        has_resources=True)
    assert r.search_tool == "search_documentation" and r.search_arg == "q"
    assert r.fetch_tool == "" and r.via_resources


def test_overrides_win_and_a_missing_override_is_ignored():
    tools = [_tool("search_docs", query="string"),
             _tool("lookup", "Look up", term="string"),
             _tool("get_doc", name="string")]
    spec = ds.ServerSpec(name="x", command="c", search_tool="lookup")
    assert ds.detect_roles(tools, spec=spec).search_tool == "lookup"
    spec = ds.ServerSpec(name="x", command="c", search_tool="gone")
    assert ds.detect_roles(tools, spec=spec).search_tool == "search_docs"


def test_a_server_with_no_search_is_not_usable():
    r = ds.detect_roles([_tool("add", "Add two numbers", a="integer")])
    assert not r.usable


# ============================================================ connecting

def test_check_server_lists_the_tools():
    report = ds.check_server(fixture_spec())
    assert report.ok, report.message
    assert report.server == "fixture 0.1"
    assert ("search_docs", "Search the docs.") in report.tools
    assert report.roles.search_tool == "search_docs"
    assert report.resources == 5
    text = "\n".join(report.lines())
    assert "Search with: search_docs(query, package)" in text


def test_check_server_reports_a_bad_command_as_a_sentence():
    report = ds.check_server(ds.ServerSpec(name="bad",
                                           command="no-such-cmd-15926"))
    assert not report.ok
    assert "was not found" in report.message


def test_check_server_refuses_an_unticked_remote_url_before_connecting():
    report = ds.check_server(ds.ServerSpec(
        name="far", transport="http", url="http://docs.example.com/mcp"))
    assert not report.ok and "Allow remote" in report.message


def test_the_pool_reuses_a_live_server_and_release_stops_it():
    spec = fixture_spec("pooled")
    try:
        c1, roles = ds.pooled(spec)
        c2, _ = ds.pooled(spec)
        assert c1 is c2 and roles.usable
        proc = c1.transport.proc
        ds.release("pooled")
        proc.wait(timeout=5)
        assert not c1.connected
        c3, _ = ds.pooled(spec)
        assert c3 is not c1, "a released server was handed out again"
    finally:
        ds.release("pooled")


def test_editing_a_servers_command_gets_a_new_process():
    a = fixture_spec("same-name")
    b = fixture_spec("same-name", "--page-size", "2")
    try:
        ca, _ = ds.pooled(a)
        cb, _ = ds.pooled(b)
        assert ca is not cb
    finally:
        ds.release("same-name")
