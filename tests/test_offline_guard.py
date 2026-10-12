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
through the guard's own check function - on this PC 127.0.0.1:11434 is the
user's real Ollama - with one exception, the pytest of its own that
test_a_cached_refusal_does_not_let_the_next_test_pass runs: there an audit
hook stops any connection to that port that gets past the guard, at the C
call, before it is made.

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
    """The guard's own checks are asked FIRST, so one that stopped refusing
    fails this test before any lookup is made. huggingface.co and
    raspberrypi.local go to the check alone: through the socket functions,
    a broken check would have sent a real DNS query and an mDNS multicast on
    the LAN (review, 2026-10-12). The lookups themselves use reserved names
    (.invalid never resolves) and TEST-NET / loopback addresses."""
    _require_guard()
    for name in ("huggingface.co", "raspberrypi.local",
                 "localhost.council-test.invalid", "council-test.invalid"):
        with pytest.raises(guard.LookupRefused, match="would ask DNS"):
            guard._CORE.check_name(name)
    for address in ("192.0.2.1", "127.0.0.2"):
        with pytest.raises(guard.LookupRefused, match="would ask DNS"):
            guard._CORE.check_reverse(address)
    _taken(6)
    # The checks refuse; now the functions the code under test calls.
    with pytest.raises(socket.gaierror, match="would ask DNS"):
        socket.getaddrinfo("localhost.council-test.invalid", 443)
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyname("council-test.invalid")
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyname_ex("council-test.invalid")
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(guard.LookupRefused):
        socket.gethostbyaddr("127.0.0.2")      # loopback, but it went out
    _taken(5)


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


class _StopAtWSASend:
    """asyncio.windows_events' _overlapped, for one test: what reaches
    WSASend / WSASendTo - i.e. got past the guard - is recorded and stopped
    there, so a broken guard fails the test without a datagram leaving."""

    def __init__(self, real):
        self._real = real
        self.reached = []

    def Overlapped(self, *args):
        stop = self

        class Ov:
            def __init__(self, ov):
                self._ov = ov

            def WSASend(self, handle, buf, flags):
                stop.reached.append(bytes(buf))
                raise OSError("stopped at WSASend: past the guard")

            def WSASendTo(self, handle, buf, flags, addr):
                stop.reached.append(bytes(buf))
                raise OSError("stopped at WSASendTo: past the guard")

            def __getattr__(self, name):
                return getattr(self._ov, name)

        return Ov(self._real.Overlapped(*args))

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def proactor_send(monkeypatch):
    """The proactor's sends, checked and stopped short of the network."""
    if sys.platform != "win32":
        pytest.skip("the proactor event loop is Windows'")
    from asyncio import windows_events
    for name in ("connect", "send", "sendto"):
        assert getattr(getattr(windows_events.IocpProactor, name),
                       "_council_offline", False), \
            f"IocpProactor.{name} is not the guard's"
    stop = _StopAtWSASend(windows_events._overlapped)
    monkeypatch.setattr(windows_events, "_overlapped", stop)
    return stop


def test_asyncio_udp_to_a_connected_peer_is_refused(proactor_send):
    """A datagram endpoint with a remote_addr connects its socket (no
    packet) and then sends with WSASend - not sendto - to that peer. It
    went out neither refused nor recorded (review, 2026-10-12)."""
    _require_guard()
    errors = []

    class Protocol(asyncio.DatagramProtocol):
        def error_received(self, exc):
            errors.append(exc)

    async def go():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            Protocol, remote_addr=("192.0.2.1", 9))
        try:
            transport.sendto(b"x")
            for _ in range(50):
                if errors:
                    break
                await asyncio.sleep(0.01)
        finally:
            transport.close()

    asyncio.run(go())
    assert proactor_send.reached == [], "the datagram got past the guard"
    assert errors and isinstance(errors[0], guard.EgressRefused), errors
    (_pid, _test, what, _where), = _taken(1)
    assert what == "a UDP datagram to 192.0.2.1:9"


def test_asyncio_sock_sendall_on_a_connected_udp_socket_is_refused(
        proactor_send):
    _require_guard()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        try:
            s.connect(("192.0.2.1", 9))                      # no packet
        except guard.EgressRefused:
            raise
        except OSError:
            pytest.skip("no route off this PC to check against")

        async def go():
            await asyncio.get_running_loop().sock_sendall(s, b"x")

        with pytest.raises(guard.EgressRefused, match="a UDP datagram"):
            asyncio.run(go())
    finally:
        s.close()
    assert proactor_send.reached == [], "the datagram got past the guard"
    _taken(1)


def test_asyncio_udp_on_loopback_still_works():
    _require_guard()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(5)

    async def go():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=rx.getsockname())
        try:
            transport.sendto(b"ping")
            await asyncio.sleep(0.05)
        finally:
            transport.close()

    try:
        asyncio.run(go())
        assert rx.recvfrom(16)[0] == b"ping"
    finally:
        rx.close()
    _taken(0)


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


INNER_CHILDREN_TESTS = textwrap.dedent('''
    import os, subprocess, sys

    def test_starts_children():
        bare = dict(os.environ)
        bare.pop("PYTHONPATH")
        for flags, env in [(["-I"], None), (["-E"], None), (["-S"], None),
                           ([], bare), ([], None),
                           (["-u", "-X", "utf8"], None)]:
            subprocess.run([sys.executable] + flags + ["-c", "pass"],
                           env=env, check=True, timeout=60)
''')


def test_a_python_child_out_of_the_guards_reach_is_listed(tmp_path):
    """A child started with -I, -E or -S, or without PYTHONPATH, runs with
    no guard - and nothing said so; a probe that rides on PYTHONPATH cannot
    see it either (review, 2026-10-12). With COUNCIL_OFFLINE_LOG set, each
    one is listed there - not a refusal, so it fails nothing - naming the
    line that started it, through tests/desktop_guard.py's wrapper of
    Popen, as in the suite. Run in a pytest of its own, which sets the log
    from the start; the children only run `pass`."""
    _require_guard()
    log = tmp_path / "offline.log"
    code, out = _inner_pytest(
        tmp_path, "test_inner_children.py", INNER_CHILDREN_TESTS,
        "from tests.desktop_guard import desktop_openers  # noqa\n",
        {guard.LOG_ENV: str(log)})
    assert code == 0, out
    assert "1 passed" in out, out
    lines = [line.split("\t") for line in
             log.read_text(encoding="utf-8").splitlines()]
    whats = [what.split(":")[0] for _pid, _test, what, _where in lines]
    assert whats == [
        "a python child without the guard (started with -I)",
        "a python child without the guard (started with -E)",
        "a python child without the guard (started with -S)",
        "a python child without the guard (its PYTHONPATH does not have "
        "tests/_offline)"], lines
    pids = {pid for pid, _test, _what, _where in lines}
    assert len(pids) == 1 and str(os.getpid()) not in pids  # the inner pytest
    for _pid, test, _what, where in lines:
        assert "test_inner_children.py::test_starts_children" in test
        assert "test_inner_children.py" in where, where   # not the wrapper
    _taken(0)                                       # nothing was refused


@pytest.mark.parametrize("argv,why", [
    (["python", "-I", "x.py"], "started with -I"),
    (["python", "-W", "ignore", "-sE", "-m", "x"], "started with -E"),
    (["pythonw.exe", "-Xutf8", "-S", "x.py"], "started with -S"),
    (["python", "-c", "print('-I -E -S')"], None),     # the code, not flags
    (["python", "x.py", "-I"], None),                  # the script's own
    (["git", "status"], None),                         # not python
    ([r"C:\Program Files\Python 3\python.exe", "-I", "x.py"],
     "started with -I"),
])
def test_how_a_child_is_told_unguarded(argv, why):
    """Popen's audit event carries the command LINE on Windows; it is split
    back the way the C runtime splits it."""
    core = guard._CORE
    env = {"PYTHONPATH": str(guard.HOOK_DIR)}
    assert core.unguarded_python_child(None, argv, env) == why
    line = subprocess.list2cmdline(argv)
    assert core._split_command_line(line) == argv
    assert core.unguarded_python_child(None, line, env) == why
    if why is None and core._is_python(argv[0]):
        assert core.unguarded_python_child(None, line, {}) == \
            "its PYTHONPATH does not have tests/_offline"


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


def _inner_pytest(tmp_path, name, tests, conftest_extra="",
                  env_extra=None):
    """Run `tests` in a pytest of its own, with this guard as its conftest
    (and its own report, not ours)."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n" + conftest_extra
        + "from tests.offline_guard import (allow_ollama_port,  # noqa\n"
        "    offline_network, offline_test)\n", encoding="utf-8")
    (tmp_path / name).write_text(tests, encoding="utf-8")
    env = dict(os.environ)
    env.pop(guard.REPORT_ENV, None)            # its own report, not ours
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("COUNCIL_OLLAMA_HOST", None)
    env.update(env_extra or {})
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path / name)],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
        env=env)
    return r.returncode, r.stdout + r.stderr


def test_a_test_that_tried_fails_even_if_it_swallowed_the_refusal(tmp_path):
    """Run three tests in a pytest of their own, with this guard as their
    conftest: the two that tried to reach 192.0.2.1 - one in its own process,
    one in a child - error with the guard's message; the one that stayed on
    loopback passes."""
    _require_guard()
    code, out = _inner_pytest(tmp_path, "test_inner.py", INNER_TESTS)
    assert code == 1, out
    # pytest counts a test whose call passed and whose teardown failed in
    # both columns: "3 passed, 2 errors" - and the run fails.
    assert "3 passed, 2 errors" in out, out
    assert "ERROR at teardown of test_swallowed_here" in out
    assert "ERROR at teardown of test_swallowed_in_a_child" in out
    assert out.count("tests must stay offline - this test (or a program it "
                     "started) was refused 1 time(s)") == 2, out
    _taken(0)


INNER_CACHED_TESTS = textwrap.dedent('''
    from council_core import local_models

    def test_asks_for_the_models():
        local_models.ollama_tags()

    def test_asks_again_while_the_refusal_is_cached():
        local_models.ollama_tags()

    def test_asks_whether_it_is_up_while_the_refusal_is_cached():
        local_models.ollama_reachable()
''')

#: In that pytest, a connection to port 11434 that got past the guard is
#: stopped at the C call (an audit hook runs before the connect), so a
#: broken guard fails the test without reaching the real Ollama.
STOP_PAST_THE_GUARD = textwrap.dedent('''
    def _stop(event, args):
        if (event == "socket.connect" and isinstance(args[1], tuple)
                and len(args[1]) >= 2 and args[1][1] == 11434):
            raise RuntimeError("past the guard: stopped at connect")
    sys.addaudithook(_stop)
''')


def test_a_cached_refusal_does_not_let_the_next_test_pass(tmp_path):
    """council_core.local_models caches what Ollama answered - a refused
    call too, as "not running" - for 5 to 60 s. Three tests in a row that
    each ask the real Ollama's port: only the first was refused; the other
    two read the cached "not running", asked nothing, and passed (review,
    2026-10-12). Each test now starts with that cache empty, so all three
    error."""
    _require_guard()
    code, out = _inner_pytest(tmp_path, "test_inner_cached.py",
                              INNER_CACHED_TESTS, STOP_PAST_THE_GUARD)
    assert code == 1, out
    assert "past the guard" not in out, out
    assert "3 passed, 3 errors" in out, out
    for name in ("test_asks_for_the_models",
                 "test_asks_again_while_the_refusal_is_cached",
                 "test_asks_whether_it_is_up_while_the_refusal_is_cached"):
        assert f"ERROR at teardown of {name}" in out, out
    assert out.count("a connection to 127.0.0.1:11434") == 3, out
    _taken(0)
