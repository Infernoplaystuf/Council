"""
The offline guard itself: a connection off loopback, a connection to the real
Ollama's port and a DNS lookup are refused - in this process and in a python
child - while a fake server on loopback still works; and a test that tried,
even one whose code swallowed the error, fails.

Every check that expects a refusal first asks whether the guard is in place
and stops there if not, so a broken guard fails these tests WITHOUT a packet
leaving the machine to find out. The addresses refused are 192.0.2.1
(TEST-NET-1, reserved for documentation: routed nowhere) and names under
.invalid, which never resolve. The real Ollama's port is only ever "reached"
through the guard's own check function, never with a socket: on this PC
127.0.0.1:11434 is the user's real Ollama.

Run:  python -m pytest tests/test_offline_guard.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import no_tk_guard  # noqa: E402
from tests import offline_guard as guard  # noqa: E402
from tests.fake_ollama import FakeOllama  # noqa: E402

#: TEST-NET-1 (RFC 5737): documentation only, never routed.
NOWHERE = ("192.0.2.1", 443)


def _require_guard():
    assert guard.installed(), "the offline guard is not in place"


def _taken(expect: int):
    """The refusals this test caused on purpose - taken, so the offline_test
    fixture does not fail the test for them."""
    got = guard.take_refusals()
    assert len(got) == expect, guard.describe(got)
    return got


def test_the_guard_is_in_force_for_the_session():
    _require_guard()
    parts = os.environ["PYTHONPATH"].split(os.pathsep)
    # The Tk guard's hook stays first (its own test checks that); this one
    # comes right after it, and is run by it.
    assert parts[0] == str(no_tk_guard.HOOK_DIR)
    assert parts[1] == str(guard.HOOK_DIR)
    assert Path(os.environ[guard.REPORT_ENV]).is_file()


def test_the_message_says_why_and_where_to_read_more():
    assert guard.MESSAGE == "tests must stay offline"
    readme = (ROOT / "tests" / "README.md").read_text(encoding="utf-8")
    assert "## Offline" in readme


# ---------------------------------------------------------------- this process

def test_a_connection_off_loopback_is_refused():
    _require_guard()
    with pytest.raises(guard.EgressRefused,
                       match=r"^tests must stay offline - a connection to "
                             r"192\.0\.2\.1:443 would leave this PC"):
        socket.create_connection(NOWHERE, timeout=1)
    (pid, test, what, where), = _taken(1)
    assert pid == str(os.getpid())
    assert "test_a_connection_off_loopback_is_refused" in test
    assert what == "a connection to 192.0.2.1:443"
    assert "test_offline_guard.py" in where       # the line that asked


def test_the_refusal_is_what_an_unplugged_network_looks_like():
    """A ConnectionRefusedError, so the code under test takes its own
    'offline' path (urllib wraps it in URLError, as it would wrap the real
    one) instead of a crash in a worker thread."""
    _require_guard()
    with pytest.raises(urllib.error.URLError) as caught:
        urllib.request.urlopen("http://192.0.2.1:8080/x", timeout=1)
    assert isinstance(caught.value.reason, ConnectionRefusedError)
    assert "tests must stay offline" in str(caught.value.reason)
    _taken(1)


def test_a_name_in_connect_is_refused_before_it_is_resolved():
    """socket.connect(("name", port)) resolves inside C - no getaddrinfo to
    catch - so the connect itself refuses anything but a loopback literal or
    localhost."""
    _require_guard()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(guard.EgressRefused):
            s.connect(("council-test.invalid", 80))
        with pytest.raises(guard.EgressRefused):
            s.connect_ex(("localhost.council-test.invalid", 80))
    finally:
        s.close()
    _taken(2)


def test_the_real_ollama_port_is_refused_even_on_loopback():
    _require_guard()
    for address in [("127.0.0.1", 11434), ("::1", 11434, 0, 0),
                    ("localhost", 11434), ("127.0.0.2", 11434)]:
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            with pytest.raises(guard.EgressRefused,
                               match="is the real Ollama's port"):
                s.connect(address)
        finally:
            s.close()
    _taken(4)


def test_a_test_can_opt_in_to_the_ollama_port(allow_ollama_port):
    """Only the guard's check is asked - a socket would reach the user's
    real Ollama here. Loopback only, still."""
    _require_guard()
    guard._CORE.check_connect(("127.0.0.1", 11434))           # no raise
    with pytest.raises(guard.EgressRefused, match="would leave this PC"):
        guard._CORE.check_connect(("192.0.2.1", 11434))
    _taken(1)


@pytest.mark.allow_ollama_port
def test_the_marker_opts_in_too():
    _require_guard()
    assert os.environ.get(guard.ALLOW_OLLAMA_ENV) == "1"
    guard._CORE.check_connect(("127.0.0.1", 11434))           # no raise


def test_the_opt_in_ends_with_the_test():
    _require_guard()
    assert guard.ALLOW_OLLAMA_ENV not in os.environ
    with pytest.raises(guard.EgressRefused):
        guard._CORE.check_connect(("127.0.0.1", 11434))
    _taken(1)


def test_a_name_lookup_is_refused_without_asking_dns():
    _require_guard()
    for name in ("huggingface.co", "localhost.council-test.invalid",
                 "raspberrypi.local"):
        with pytest.raises(socket.gaierror, match="would ask DNS"):
            socket.getaddrinfo(name, 443)
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyname("council-test.invalid")
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyaddr("127.0.0.2")      # loopback, but it went out
    _taken(6)


def test_this_pc_is_still_looked_up():
    _require_guard()
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("::1", 80)
    assert socket.getaddrinfo(None, 80)
    guard._CORE.check_reverse("127.0.0.1")                   # no raise
    guard._CORE.check_name(socket.gethostname())             # no raise
    _taken(0)


def test_a_udp_datagram_off_loopback_is_refused_but_a_route_check_is_not():
    """A UDP connect() sends nothing - council_core.apothecary.desktop_ip
    uses one to learn the LAN address - so only a datagram is refused."""
    _require_guard()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(guard.EgressRefused, match="a UDP datagram"):
            s.sendto(b"x", ("192.0.2.1", 9))
        try:
            s.connect(("192.0.2.1", 9))                      # no packet
        except guard.EgressRefused:
            raise
        except OSError:
            pytest.skip("no route off this PC to check against")
        with pytest.raises(guard.EgressRefused, match="a UDP datagram"):
            s.send(b"x")
    finally:
        s.close()
    _taken(2)


def test_udp_on_loopback_still_works():
    _require_guard()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(5)
        tx.sendto(b"ping", rx.getsockname())
        assert rx.recvfrom(16)[0] == b"ping"
    finally:
        rx.close()
        tx.close()
    _taken(0)


def test_asyncio_is_refused_too():
    """Windows' proactor event loop connects with ConnectEx, not
    socket.connect - the guard patches it separately."""
    _require_guard()

    async def go():
        await asyncio.wait_for(asyncio.open_connection(*NOWHERE), 5)

    with pytest.raises(guard.EgressRefused):
        asyncio.run(go())
    _taken(1)


def test_a_fake_server_on_loopback_works():
    _require_guard()
    with FakeOllama() as srv:
        with urllib.request.urlopen(srv.url + "/api/tags", timeout=10) as r:
            names = [m["name"] for m in json.loads(r.read())["models"]]
    assert "llama3.1:8b" in names
    _taken(0)


def test_a_fake_server_on_ipv6_loopback_works():
    _require_guard()
    if not socket.has_ipv6:
        pytest.skip("no IPv6 here")
    try:
        srv = FakeOllama(host="::1")
    except OSError:
        pytest.skip("cannot listen on ::1 here")
    with srv:
        with urllib.request.urlopen(srv.url + "/api/version", timeout=10) as r:
            assert json.loads(r.read())["version"]
    _taken(0)


def test_a_refusal_is_recorded_when_asked(tmp_path, monkeypatch):
    """COUNCIL_OFFLINE_LOG is how a whole run shows the guard never fired."""
    _require_guard()
    log = tmp_path / "offline.log"
    monkeypatch.setenv(guard.LOG_ENV, str(log))
    with pytest.raises(guard.EgressRefused):
        socket.create_connection(NOWHERE, timeout=1)
    pid, test, what, where = log.read_text(
        encoding="utf-8").strip().split("\t")
    assert pid == str(os.getpid())
    assert "test_a_refusal_is_recorded_when_asked" in test
    assert what == "a connection to 192.0.2.1:443"
    assert "test_offline_guard.py" in where
    _taken(1)


def test_install_puts_the_guard_back_in_front_of_a_later_wrapper(monkeypatch):
    """Something that wraps socket.connect after the guard (a usercustomize
    run after the hook's sitecustomize, a tracing tool) would see an
    off-loopback address first; install() puts the refusal in front again."""
    _require_guard()
    seen = []
    inner = socket.socket.connect

    def later(self, address):
        seen.append(address)
        return inner(self, address)

    monkeypatch.setattr(socket.socket, "connect", later)
    assert not guard._CORE.installed()
    guard._CORE.install()
    assert guard._CORE.installed()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(guard.EgressRefused):
            s.connect(NOWHERE)
    finally:
        s.close()
    assert seen == [], "the later wrapper ran before the refusal"
    _taken(1)


def test_a_refusal_is_reported_even_with_the_variable_gone(monkeypatch):
    """A test that clears the environment cannot hide its own refusals."""
    _require_guard()
    monkeypatch.delenv(guard.REPORT_ENV)
    with pytest.raises(guard.EgressRefused):
        socket.create_connection(NOWHERE, timeout=1)
    _taken(1)


# ---------------------------------------------------------------- a child

CHILD = textwrap.dedent('''
    import socket, sys, urllib.request
    if not getattr(socket.socket, "_council_offline", False):
        print("UNGUARDED")
        sys.exit(2)                  # stop BEFORE anything could leave
    if sys.argv[1] == "fake":
        with urllib.request.urlopen(sys.argv[2] + "/api/tags", timeout=10) as r:
            print("FAKE", r.status)
        sys.exit(0)
    socket.create_connection(("192.0.2.1", 443), timeout=1)
''')


def _child(tmp_path, *args):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, timeout=60,
                          cwd=str(tmp_path))


def test_a_python_child_is_refused_too(tmp_path):
    """A generated app, a driver or a probe runs as a child; the guard
    reaches it through PYTHONPATH - and its refusal reaches this test's
    report, so the offline_test fixture would fail the test that started
    it."""
    _require_guard()
    r = _child(tmp_path, "nowhere")
    assert "UNGUARDED" not in r.stdout, "the child has no guard"
    assert r.returncode != 0
    assert "EgressRefused" in r.stderr
    assert "tests must stay offline - a connection to 192.0.2.1:443" in r.stderr
    (pid, test, what, _where), = _taken(1)
    assert pid != str(os.getpid())
    assert "test_a_python_child_is_refused_too" in test
    assert what == "a connection to 192.0.2.1:443"


def test_a_python_child_still_reaches_a_fake_server(tmp_path):
    _require_guard()
    with FakeOllama() as srv:
        r = _child(tmp_path, "fake", srv.url)
    assert "UNGUARDED" not in r.stdout, "the child has no guard"
    assert r.returncode == 0, r.stderr
    assert "FAKE 200" in r.stdout
    _taken(0)


OTHER_SITE = textwrap.dedent('''
    import os
    os.environ["OTHER_SITE_RAN"] = str(int(os.environ.get("OTHER_SITE_RAN", "0")) + 1)
''')


@pytest.mark.parametrize("order", ["no_tk-first", "offline-first"])
def test_the_child_hooks_compose_in_either_order(tmp_path, order):
    """Both hooks run the sitecustomize that comes after them. In either
    order both guards are installed, the one they shadow runs exactly once,
    and neither runs the other in a loop."""
    other = tmp_path / "other_site"
    other.mkdir()
    (other / "sitecustomize.py").write_text(OTHER_SITE, encoding="utf-8")
    hooks = [str(no_tk_guard.HOOK_DIR), str(guard.HOOK_DIR)]
    if order == "offline-first":
        hooks.reverse()
    env = dict(os.environ)
    env.pop("OTHER_SITE_RAN", None)
    env["PYTHONPATH"] = os.pathsep.join(hooks + [str(other)])
    r = subprocess.run(
        [sys.executable, "-c",
         "import os, socket, tkinter; "
         "print(os.environ.get('OTHER_SITE_RAN'), "
         "getattr(socket.socket, '_council_offline', False), "
         "getattr(tkinter, '_council_no_tk', False))"],
        capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["1", "True", "True"]


# ------------------------------------------------- a test that tried, fails

INNER_TESTS = textwrap.dedent('''
    import socket, subprocess, sys

    def test_swallowed_here():
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=1)
        except OSError:
            pass                       # "offline? fine" - still a failure

    def test_swallowed_in_a_child():
        subprocess.run([sys.executable, "-c",
                        "import socket\\n"
                        "try:\\n"
                        "    socket.create_connection(('192.0.2.1', 443), timeout=1)\\n"
                        "except OSError:\\n"
                        "    pass\\n"], timeout=60)

    def test_loopback_is_fine():
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        socket.create_connection(srv.getsockname(), timeout=5).close()
        srv.close()
''')


def test_a_test_that_tried_fails_even_if_it_swallowed_the_refusal(tmp_path):
    """Run three tests in a pytest of their own, with this guard as their
    conftest: the two that tried to reach 192.0.2.1 - one in its own process,
    one in a child - error with the guard's message; the one that stayed on
    loopback passes."""
    _require_guard()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.offline_guard import (allow_ollama_port,  # noqa\n"
        "    offline_network, offline_test)\n", encoding="utf-8")
    (tmp_path / "test_inner.py").write_text(INNER_TESTS, encoding="utf-8")
    env = dict(os.environ)
    env.pop(guard.REPORT_ENV, None)            # its own report, not ours
    env.pop("PYTEST_ADDOPTS", None)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path / "test_inner.py")],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
        env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    # pytest counts a test whose call passed and whose teardown failed in
    # both columns: "3 passed, 2 errors" - and the run fails.
    assert "3 passed, 2 errors" in out, out
    assert "ERROR at teardown of test_swallowed_here" in out
    assert "ERROR at teardown of test_swallowed_in_a_child" in out
    assert out.count("tests must stay offline - this test (or a program it "
                     "started) was refused 1 time(s)") == 2, out
    _taken(0)
