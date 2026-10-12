"""
tests.offline_guard - tests must stay offline.

The Council is offline by design: localhost only, nothing leaves the PC unless
the user opts in. Its tests must be too. A run of the whole suite with a
socket probe on 2026-10-09 found smoke_test's model-downloader check making
real HTTPS connections to Hugging Face (CloudFront, port 443) - under pytest
and as a script - and tests of the Models, Nodes and roles UI asking the
user's REAL Ollama (127.0.0.1:11434 and ::1) for its models, so their results
depended on what was installed on the machine. Those tests now stub what they
reached for (tests/fake_ollama.py, or the function that lists the models).

This keeps it that way. For the whole run

  * a connection - or a UDP datagram - to anything but loopback,
  * a connection to port 11434, the real Ollama's, on any address,
  * looking up a name other than localhost or this PC's own (a DNS query)

raise

    EgressRefused: tests must stay offline - a connection to 1.2.3.4:443
    would leave this PC; only loopback may be reached (see tests/README.md,
    'Offline')

IN THIS PROCESS (patched now) and IN EVERY PYTHON CHILD a test starts
(tests/_offline/ goes on PYTHONPATH right after tests/_no_tk/, and its
sitecustomize installs the same refusal at the child's startup) - the same
design as tests/no_tk_guard.py. tests/_offline/council_offline.py has the
details: what exactly is refused, and what is not covered.

EgressRefused is a ConnectionRefusedError, so the code under test takes its
"no network" / "Ollama is not running" path, as on a PC with the cable out.
That cannot hide a test that reaches out: every refusal - in this process or
a child - is written to a report file, and the autouse `offline_test` fixture
FAILS the test it happened in (at teardown; one that happened between tests
fails the next test's setup, naming the test that was running).

A fake server on loopback (tests/fake_ollama.py's FakeOllama, any
http.server on 127.0.0.1 port 0) works as before: it is on a port of its own.
A test that really must connect to port 11434 - one that puts its own fake
there - opts in with `@pytest.mark.allow_ollama_port` or the
`allow_ollama_port` fixture (registered in pytest.ini); it still may not
leave the machine.

COUNCIL_OFFLINE_LOG=<file> records every refusal there too (pid, test, what,
where), so a whole run can show the guard fired nowhere but in its own tests
(tests/test_offline_guard.py, and the test_inner.py of the pytest it runs).

Imported by tests/conftest.py and inferno_local/tests/conftest.py, and by
tests/smoke_test.py when it runs as a script (which loads no conftest).
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

#: The folder put on every child's PYTHONPATH (after tests/_no_tk).
HOOK_DIR = Path(__file__).resolve().parent / "_offline"

#: no_tk_guard's hook folder: it stays first on PYTHONPATH (its sitecustomize
#: runs the next one - this guard's).
_NO_TK_DIR = Path(__file__).resolve().parent / "_no_tk"


def _core():
    """tests/_offline/council_offline.py, loaded by path: the folder itself
    is NOT put on this process's sys.path - its sitecustomize is for
    children."""
    name = "council_offline"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, HOOK_DIR / "council_offline.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_CORE = _core()
MESSAGE: str = _CORE.MESSAGE
EgressRefused = _CORE.EgressRefused
LookupRefused = _CORE.LookupRefused
OLLAMA_PORT: int = _CORE.OLLAMA_PORT
REPORT_ENV: str = _CORE.REPORT_ENV
LOG_ENV: str = _CORE.LOG_ENV
ALLOW_OLLAMA_ENV: str = _CORE.ALLOW_OLLAMA_ENV

#: One refusal: (pid, the test that was running, what was refused, where).
Refusal = Tuple[str, str, str, str]

_state = {"seen": 0}


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _ensure_report() -> None:
    """The report every refusal of this session is written to - children's
    too, since they inherit $COUNCIL_OFFLINE_REPORT. A pytest started by a
    test shares its parent's (and reads only what comes after it)."""
    path = os.environ.get(REPORT_ENV)
    if path and os.path.isfile(path):
        if "path" not in _state:
            _state["path"] = path
            _state["seen"] = os.path.getsize(path)
    else:
        fd, path = tempfile.mkstemp(prefix="council_offline_", suffix=".tsv")
        os.close(fd)
        os.environ[REPORT_ENV] = path
        _state["path"], _state["seen"] = path, 0
        atexit.register(_remove, path)
    _CORE.REPORT_PATH = _state["path"]


def install() -> None:
    """Refuse egress here and in every python child started from now."""
    _CORE.install()
    hook = str(HOOK_DIR)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
             if p and p != hook]
    at = 1 if parts and parts[0] == str(_NO_TK_DIR) else 0
    parts.insert(at, hook)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    _ensure_report()


def installed() -> bool:
    """Whether this process refuses egress, and its children will too."""
    parts = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    return (_CORE.installed() and str(HOOK_DIR) in parts
            and bool(os.environ.get(REPORT_ENV)))


def take_refusals() -> List[Refusal]:
    """Every refusal recorded since the last call - in this process or any
    child - and mark them read. A test of the guard calls this to take the
    refusals it caused on purpose; the `offline_test` fixture calls it to
    fail a test that caused any other."""
    path = _state.get("path") or os.environ.get(REPORT_ENV)
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(_state["seen"])
            data = f.read()
    except OSError:
        return []
    end = data.rfind(b"\n") + 1                # a child may be mid-line
    _state["seen"] += end
    out: List[Refusal] = []
    for line in data[:end].decode("utf-8", "replace").splitlines():
        cells = line.split("\t")
        if len(cells) == 4:
            out.append(tuple(cells))           # type: ignore[arg-type]
    return out


def describe(refusals: List[Refusal]) -> str:
    """The refusals, one per line, for a failure message."""
    return "\n".join(f"  pid {pid}, during {test}: {what}\n      asked by {where}"
                     for pid, test, what, where in refusals)


try:
    import pytest
except ImportError:                      # script mode (smoke_test's main())
    pytest = None


# At IMPORT, i.e. when conftest is loaded: before any test module is
# collected, so a connection made at import or collection time is refused.
install()


if pytest is not None:
    @pytest.fixture(scope="session", autouse=True)
    def offline_network():
        """The guard, for the whole session (it is installed on import; this
        asserts it is still in force when the first test starts, and that
        nothing was refused after the last test)."""
        install()
        assert installed()
        yield
        late = take_refusals()
        if late:
            pytest.fail(f"{MESSAGE} - refused after the last test:\n"
                        f"{describe(late)}", pytrace=False)

    @pytest.fixture(autouse=True)
    def offline_test(request):
        """Fails a test that tried to go online - in this process or in a
        program it started - even when the code under test caught the
        refusal. A refusal made before its own setup (a worker an earlier
        test left running, an import at collection, a module- or
        session-scoped fixture) fails this test's setup."""
        stray = take_refusals()
        if stray:
            pytest.fail(f"{MESSAGE} - refused before this test's own setup "
                        f"(at collection, by a worker an earlier test left "
                        f"running, or in a module- or session-scoped "
                        f"fixture):\n{describe(stray)}", pytrace=False)
        opted = request.node.get_closest_marker("allow_ollama_port")
        before = os.environ.get(ALLOW_OLLAMA_ENV)
        if opted is not None:
            os.environ[ALLOW_OLLAMA_ENV] = "1"
        try:
            yield
        finally:
            if opted is not None:
                if before is None:
                    os.environ.pop(ALLOW_OLLAMA_ENV, None)
                else:
                    os.environ[ALLOW_OLLAMA_ENV] = before
        refused = take_refusals()
        if refused:
            pytest.fail(f"{MESSAGE} - this test (or a program it started) "
                        f"was refused {len(refused)} time(s):\n"
                        f"{describe(refused)}", pytrace=False)

    @pytest.fixture
    def allow_ollama_port():
        """Opt in to connecting to port 11434 for one test (and the python
        children it starts). Only for a test that puts its OWN fake server
        there: on a developer PC that port is the real Ollama's. Loopback
        only, as ever."""
        before = os.environ.get(ALLOW_OLLAMA_ENV)
        os.environ[ALLOW_OLLAMA_ENV] = "1"
        try:
            yield
        finally:
            if before is None:
                os.environ.pop(ALLOW_OLLAMA_ENV, None)
            else:
                os.environ[ALLOW_OLLAMA_ENV] = before
