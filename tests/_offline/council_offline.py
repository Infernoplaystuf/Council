"""
council_offline - tests must stay offline. Nothing a test does may reach past
this PC's loopback, and nothing may reach the real Ollama's port (11434).

The Council is offline by design; its tests must be too. A probe of the whole
suite on 2026-10-09, and again on 2026-10-12, found a test making real HTTPS
connections to Hugging Face, and tests asking the user's REAL Ollama on
127.0.0.1:11434 / ::1 for its models - so their results depended on what was
installed on the machine.

tests/offline_guard.py loads this twice over, the way tests/no_tk_guard.py
loads tests/_no_tk/council_no_tk.py:

  * in the pytest process, where install() patches the socket module at once;
  * in every python CHILD a test starts, through sitecustomize.py beside this
    file - offline_guard puts this folder on PYTHONPATH (right after
    tests/_no_tk, whose sitecustomize runs the next one, which is this), and
    a child inherits it.

What is refused, with EgressRefused - a ConnectionRefusedError, so the code
under test sees what it would see on a PC with no network, and its own error
handling runs:

  * a TCP connect (socket.connect / connect_ex, and asyncio's Windows
    proactor, which connects without them) to anything but a loopback
    address or "localhost";
  * a UDP datagram (sendto / sendmsg, or send on a UDP socket connected
    elsewhere - and the same through asyncio's Windows proactor, which
    sends with WSASend / WSASendTo, e.g. a create_datagram_endpoint with a
    remote_addr) to anything but loopback. A UDP connect() alone sends nothing
    - the Council uses one to learn which LAN address a route would use - so
    that is still allowed;
  * a connect to port 11434 - the real Ollama's - on ANY address, unless the
    test opted in (COUNCIL_OFFLINE_ALLOW_OLLAMA_PORT=1, which the
    allow_ollama_port marker and fixture set). A fake server listens on a
    port of its own (tests/fake_ollama.py) and needs no opt-in;
  * looking up a NAME (getaddrinfo, gethostbyname[_ex]) other than
    "localhost" or this PC's own host name, and a reverse lookup
    (gethostbyaddr, getnameinfo) of anything but 127.0.0.1 or ::1: either
    would send a DNS (or mDNS / LLMNR) query off the machine. Those raise
    LookupRefused, which is an EgressRefused AND a socket.gaierror.

Every refusal is recorded - pid, the test that was running (a child inherits
PYTEST_CURRENT_TEST), what was refused, and the first frame outside the
standard library and site-packages that asked - in the file
$COUNCIL_OFFLINE_REPORT names (offline_guard reads it to fail the test that
did it, even where the code under test swallowed the error), and in
$COUNCIL_OFFLINE_LOG when that is set (how a whole run shows the guard fired
nowhere but in its own tests).

NOT COVERED: a child started with -I, -E or -S, or with an environment that
drops PYTHONPATH; a program that is not Python (curl, ssh, git, ollama) - no
test starts one that goes online today; a socket made from _socket directly;
Qt's own networking (gui_policy keeps QtNetwork out of generated apps, and
the Council does not use it). The python children are not silent, though:
with $COUNCIL_OFFLINE_LOG set, every one that subprocess.Popen starts out of
the guard's reach is LISTED there ("a python child without the guard",
why, its command line, and the line that started it) - a note, not a
refusal: it fails no test. A whole run then shows which children only code
review vouches for (one review counted about 186 launches: gui_smoke, the
benches, the docs runner), instead of a probe that cannot see them either.

Standard library only, and syntax an old Python still parses: a child may be
another interpreter (a camera SDK's conda env), and a sitecustomize that fails
would break every child, not only those that go online.
"""
import os
import re
import socket
import sys

#: How every refusal starts.
MESSAGE = "tests must stay offline"

#: How the log lists a python child started out of the guard's reach.
UNGUARDED = "a python child without the guard"

#: Where every refusal is also recorded, for a whole run (optional).
LOG_ENV = "COUNCIL_OFFLINE_LOG"

#: The session's refusal report (tests/offline_guard.py creates it).
REPORT_ENV = "COUNCIL_OFFLINE_REPORT"

#: "1" while a test has opted in to the real Ollama's port.
ALLOW_OLLAMA_ENV = "COUNCIL_OFFLINE_ALLOW_OLLAMA_PORT"

#: The real Ollama's port on a developer PC.
OLLAMA_PORT = 11434

#: Where to read what to do instead.
SEE = "see tests/README.md, 'Offline'"

_MARK = "_council_offline"


class EgressRefused(ConnectionRefusedError):
    """A test (or a program a test started) tried to reach past loopback, or
    the real Ollama's port.

    A ConnectionRefusedError ON PURPOSE. no_tk_guard's refusal is a
    BaseException, so that a Tk test's `except Exception: skip` could not
    hide it; here the code under test must see what it would see with the
    network unplugged - its "Ollama is not running" or "offline" path - and
    a BaseException out of a Qt worker or a server thread would take more
    down with it than the one test. Nothing is hidden by that: every refusal
    is recorded, and offline_guard fails the test it happened in."""


class LookupRefused(EgressRefused, socket.gaierror):
    """A name lookup that would have asked DNS. A socket.gaierror too, so
    code that handles "the name did not resolve" handles this."""


#: Refusals made in THIS process, oldest first: (pid, test, what, where).
REFUSALS = []

#: This process's own report file (set by tests/offline_guard.py), used when
#: $COUNCIL_OFFLINE_REPORT is gone - a test that empties os.environ must not
#: also make its own refusals unreadable.
REPORT_PATH = None


def _ip(text):
    """The IP address `text` is a literal of, else None."""
    import ipaddress
    h = str(text).strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _local_ip(ip):
    # 0.0.0.0 / :: as a DESTINATION is this host (Linux) or an error
    # (Windows); either way nothing leaves the machine.
    return bool(ip.is_loopback or ip.is_unspecified)


def is_local_host(host):
    """localhost, or a loopback (or unspecified) IP LITERAL. Never a name
    that would be looked up: "localhost.evil.example" is not this PC."""
    if isinstance(host, bytes):
        host = host.decode("latin-1")
    h = str(host).strip("[]").lower()
    if h in ("localhost", ""):
        return True
    ip = _ip(h)
    return ip is not None and _local_ip(ip)


def _own_names():
    try:
        name = socket.gethostname()
    except Exception:                                # noqa: BLE001
        return ()
    return (name.lower(),) if name else ()


def _where():
    """The first frame, from the refusal outward, outside this file, the
    standard library and site-packages: the code that asked."""
    try:
        import sysconfig
        skip = []
        paths = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib", "purelib", "platlib"):
            p = paths.get(key)
            if p:
                skip.append(os.path.normcase(os.path.abspath(p)) + os.sep)
        me = os.path.normcase(os.path.abspath(__file__))
        f = sys._getframe(1)
        first = None
        while f is not None:
            fn = os.path.normcase(os.path.abspath(f.f_code.co_filename))
            if fn != me:
                if first is None:
                    first = f
                if not any(fn.startswith(s) for s in skip):
                    first = f
                    break
            f = f.f_back
        if first is not None:
            return "%s:%s %s" % (first.f_code.co_filename, first.f_lineno,
                                 first.f_code.co_name)
    except Exception:                                # noqa: BLE001
        pass
    return "?"


def _line(rec):
    return "\t".join(str(x).replace("\t", " ").replace("\r", " ")
                     .replace("\n", " ") for x in rec) + "\n"


def _note(what):
    """Write `what` to $COUNCIL_OFFLINE_LOG only: not a refusal, so not in
    the report, and no test fails for it."""
    path = os.environ.get(LOG_ENV)
    if not path:
        return
    rec = (os.getpid(), os.environ.get("PYTEST_CURRENT_TEST", "-"), what,
           _where())
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_line(rec))
    except Exception:                                # noqa: BLE001
        pass


def _record(what):
    rec = (os.getpid(), os.environ.get("PYTEST_CURRENT_TEST", "-"), what,
           _where())
    REFUSALS.append(rec)
    line = _line(rec)
    for env in (REPORT_ENV, LOG_ENV):
        path = os.environ.get(env)
        if not path and env == REPORT_ENV:
            path = REPORT_PATH
        if not path:
            continue
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:                            # noqa: BLE001
            pass
    return rec


def ollama_port_allowed():
    """Whether the running test opted in to the real Ollama's port."""
    return os.environ.get(ALLOW_OLLAMA_ENV, "") == "1"


def _refuse(what, why, cls=EgressRefused):
    _record(what)
    raise cls("%s - %s %s (%s)" % (MESSAGE, what, why, SEE))


def _inet(family):
    return family in (socket.AF_INET, getattr(socket, "AF_INET6", None))


def check_connect(address):
    """Refuse a TCP connect to `address` (host, port, ...) unless it stays on
    loopback and off the real Ollama's port."""
    if not isinstance(address, tuple) or len(address) < 2:
        return
    host, port = address[0], address[1]
    shown = "a connection to %s:%s" % (host, port)
    if not is_local_host(host):
        _refuse(shown, "would leave this PC; only loopback may be reached")
    if port == OLLAMA_PORT and not ollama_port_allowed():
        _refuse(shown, "is the real Ollama's port; use tests/fake_ollama.py "
                "(it listens on a port of its own), or opt in with the "
                "allow_ollama_port marker or fixture")


def check_datagram(address):
    """Refuse a UDP datagram to `address` unless it stays on loopback."""
    if not isinstance(address, tuple) or len(address) < 2:
        return
    if not is_local_host(address[0]):
        _refuse("a UDP datagram to %s:%s" % (address[0], address[1]),
                "would leave this PC; only loopback may be reached")


def check_name(host):
    """Refuse looking up a name that is not this PC's."""
    if host is None:
        return
    if isinstance(host, bytes):
        host = host.decode("latin-1")
    h = str(host).strip("[]").lower()
    if h in ("", "localhost") or _ip(h) is not None:
        return                                       # nothing is asked
    if h.rstrip(".") in _own_names():
        return                                       # answered locally
    _refuse("looking up the name %r" % (str(host),),
            "would ask DNS; use localhost or a loopback address",
            LookupRefused)


def check_reverse(host):
    """Refuse a reverse lookup of anything but 127.0.0.1 and ::1.

    Only those two: Windows answers them itself (measured 2026-10-12, at
    most 0.03 s - http.server's getfqdn asks for 127.0.0.1 each time a
    server starts), while 127.0.0.2, also loopback, went out as a query
    that took 4.97 s (tests/fake_ollama.py, _Server.server_bind)."""
    ip = _ip(host)
    if ip is None:
        check_name(host)                             # a name: looked up
        return
    if str(ip) not in ("127.0.0.1", "::1"):
        _refuse("a reverse lookup of %r" % (str(host),),
                "would ask DNS; only 127.0.0.1 and ::1 are answered here",
                LookupRefused)


def _patch_socket():
    cls = socket.socket
    real = dict((name, getattr(cls, name))
                for name in ("connect", "connect_ex", "sendto", "sendmsg",
                             "send", "sendall") if hasattr(cls, name))

    def udp(sock):
        return sock.type == socket.SOCK_DGRAM

    def peer_check(sock):
        if _inet(sock.family) and udp(sock):
            try:
                peer = sock.getpeername()
            except OSError:
                return
            check_datagram(peer)

    def connect(self, address):
        if _inet(self.family) and not udp(self):
            check_connect(address)
        return real["connect"](self, address)

    def connect_ex(self, address):
        if _inet(self.family) and not udp(self):
            check_connect(address)
        return real["connect_ex"](self, address)

    def sendto(self, data, *args):
        if _inet(self.family) and args:
            check_datagram(args[-1])
        return real["sendto"](self, data, *args)

    def send(self, *args, **kwargs):
        peer_check(self)
        return real["send"](self, *args, **kwargs)

    def sendall(self, *args, **kwargs):
        peer_check(self)
        return real["sendall"](self, *args, **kwargs)

    wrappers = {"connect": connect, "connect_ex": connect_ex,
                "sendto": sendto, "send": send, "sendall": sendall}

    if "sendmsg" in real:
        def sendmsg(self, buffers, *args):
            if _inet(self.family):
                if len(args) >= 3 and args[2] is not None:
                    check_datagram(args[2])
                else:
                    peer_check(self)
            return real["sendmsg"](self, buffers, *args)
        wrappers["sendmsg"] = sendmsg

    real_gai = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        check_name(host)
        return real_gai(host, *args, **kwargs)

    lookups = {"getaddrinfo": getaddrinfo}
    for name in ("gethostbyname", "gethostbyname_ex"):
        if hasattr(socket, name):
            def by_name(host, _real=getattr(socket, name)):
                check_name(host)
                return _real(host)
            lookups[name] = by_name

    if hasattr(socket, "gethostbyaddr"):
        real_ghba = socket.gethostbyaddr

        def gethostbyaddr(host):
            check_reverse(host)
            return real_ghba(host)
        lookups["gethostbyaddr"] = gethostbyaddr

    if hasattr(socket, "getnameinfo"):
        real_gni = socket.getnameinfo

        def getnameinfo(sockaddr, flags):
            host = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
            if _ip(host) is None:
                check_name(host)
            elif not flags & getattr(socket, "NI_NUMERICHOST", 0):
                check_reverse(host)
            return real_gni(sockaddr, flags)
        lookups["getnameinfo"] = getnameinfo

    # Only where the guard is not already outermost: something that wrapped
    # these after it (a usercustomize, a tracing tool) is wrapped in turn, so
    # the refusal comes first again; a function still the guard's own is
    # left alone rather than checked twice.
    for name, fn in wrappers.items():
        if not getattr(getattr(cls, name), _MARK, False):
            setattr(fn, _MARK, True)
            setattr(cls, name, fn)
    for name, fn in lookups.items():
        if not getattr(getattr(socket, name), _MARK, False):
            setattr(fn, _MARK, True)
            setattr(socket, name, fn)
    setattr(cls, _MARK, True)


def _udp_peer_check(conn):
    """A datagram sent on a UDP socket connected elsewhere goes to its peer:
    check that. (The proactor's send also writes to PIPES - a subprocess's
    stdin - which are not sockets and are left alone.)"""
    if not (isinstance(conn, socket.socket) and _inet(conn.family)
            and conn.type == socket.SOCK_DGRAM):
        return
    try:
        peer = conn.getpeername()
    except OSError:
        return                                       # not connected
    check_datagram(peer)


def _patch_proactor(module):
    """asyncio's Windows event loop connects and sends with ConnectEx,
    WSASend and WSASendTo, not socket.connect / send / sendto - so its
    connect, send and sendto are checked here.

    send too: a CONNECTED UDP endpoint (create_datagram_endpoint with
    remote_addr, or sock_sendall on a UDP socket connected elsewhere) sends
    its datagrams with WSASend, to the peer. Without this a datagram to
    192.0.2.1 went out neither refused nor recorded (review, 2026-10-12)."""
    proactor = getattr(module, "IocpProactor", None)
    if proactor is None or proactor.__dict__.get(_MARK):
        return
    real_connect = proactor.connect
    real_send = getattr(proactor, "send", None)
    real_sendto = getattr(proactor, "sendto", None)

    def connect(self, conn, address):
        if _inet(conn.family) and conn.type != socket.SOCK_DGRAM:
            check_connect(address)
        return real_connect(self, conn, address)
    setattr(connect, _MARK, True)
    proactor.connect = connect

    if real_send is not None:
        def send(self, conn, buf, flags=0):
            _udp_peer_check(conn)
            return real_send(self, conn, buf, flags)
        setattr(send, _MARK, True)
        proactor.send = send

    if real_sendto is not None:
        def sendto(self, conn, buf, flags=0, addr=None):
            if _inet(conn.family):
                if addr is not None:
                    check_datagram(addr)
                else:
                    _udp_peer_check(conn)
            return real_sendto(self, conn, buf, flags, addr)
        setattr(sendto, _MARK, True)
        proactor.sendto = sendto
    setattr(proactor, _MARK, True)


class _Finder(object):
    """Patches asyncio.windows_events as it is imported. Finds nothing
    itself: the spec comes from the finders after this one."""

    NAME = "asyncio.windows_events"

    def find_spec(self, name, path=None, target=None):
        if name != self.NAME:
            return None
        finders = list(sys.meta_path)
        later = finders[finders.index(self) + 1:] if self in finders else []
        spec = None
        for finder in later:
            find = getattr(finder, "find_spec", None)
            if find is not None:
                spec = find(name, path, target)
                if spec is not None:
                    break
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            _patch_proactor(module)

        spec.loader.exec_module = exec_and_patch
        return spec


_PYTHON = re.compile(r"^pythonw?(\d+(\.\d+)*)?(\.exe)?$", re.IGNORECASE)


def _split_command_line(line):
    """A Windows command line as the C runtime splits it into argv - the
    inverse of subprocess.list2cmdline, which is what Popen's audit event
    carries on Windows."""
    argv, cur, quoted, have, i, n = [], [], False, False, 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            j = i
            while j < n and line[j] == "\\":
                j += 1
            if j < n and line[j] == '"':
                cur.append("\\" * ((j - i) // 2))
                if (j - i) % 2:
                    cur.append('"')
                else:
                    quoted = not quoted
                i, have = j + 1, True
                continue
            cur.append(line[i:j])
            i, have = j, True
            continue
        if c == '"':
            quoted, have = not quoted, True
        elif c in " \t" and not quoted:
            if have:
                argv.append("".join(cur))
            cur, have = [], False
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        argv.append("".join(cur))
    return argv


def _is_python(program):
    if not program:
        return False
    if _PYTHON.match(os.path.basename(program)):
        return True
    try:
        return (os.path.normcase(os.path.abspath(program))
                == os.path.normcase(os.path.abspath(sys.executable)))
    except Exception:                                # noqa: BLE001
        return False


def _interpreter_flags(args):
    """The one-letter interpreter options before the script, -c or -m."""
    flags = set()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or not arg.startswith("-") or arg == "-":
            break
        if arg.startswith("--"):
            if arg == "--check-hash-based-pycs":
                i += 1
            i += 1
            continue
        letters = arg[1:]
        for k, ch in enumerate(letters):
            if ch in "cm":
                return flags                        # the rest is the program's
            if ch in "WX":
                if k == len(letters) - 1:
                    i += 1                          # its value is the next arg
                break
            flags.add(ch)
        i += 1
    return flags


def _pythonpath(env):
    if env is None:
        return os.environ.get("PYTHONPATH", "")
    for key, value in env.items():
        if os.fsdecode(key).upper() == "PYTHONPATH":
            return os.fsdecode(value)
    return ""


def unguarded_python_child(executable, args, env):
    """Why the program subprocess.Popen is about to start - (executable,
    args, env) as its audit event gives them - is a python that will run
    WITHOUT this guard; None when it is guarded, or not python."""
    if isinstance(args, (str, bytes)):
        argv = _split_command_line(os.fsdecode(args))
    else:
        argv = [os.fsdecode(a) for a in args]
    program = os.fsdecode(executable) if executable is not None else (
        argv[0] if argv else "")
    if not _is_python(program):
        return None
    off = sorted(_interpreter_flags(argv[1:]) & set("IES"))
    if off:
        return "started with " + " ".join("-" + f for f in off)
    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    on_path = [os.path.normcase(os.path.abspath(p))
               for p in _pythonpath(env).split(os.pathsep) if p]
    if here not in on_path:
        return "its PYTHONPATH does not have tests/_offline"
    return None


_WATCHING = []


def _watch_children():
    """With $COUNCIL_OFFLINE_LOG set, list there every python child that
    subprocess.Popen starts without this guard. An audit hook: it cannot be
    removed again, so it is added only when the log is asked for, once per
    process, and it returns at once for every other event. It never raises
    - an exception from an audit hook would stop the Popen."""
    if _WATCHING or not os.environ.get(LOG_ENV) \
            or not hasattr(sys, "addaudithook"):
        return

    def hook(event, args):
        if event != "subprocess.Popen":
            return
        try:
            why = unguarded_python_child(args[0], args[1], args[3])
            if why:
                shown = args[1] if isinstance(args[1], str) else " ".join(
                    os.fsdecode(a) for a in args[1])
                if len(shown) > 300:
                    shown = shown[:300] + "..."
                _note("%s (%s): %s" % (UNGUARDED, why, shown))
        except Exception:                            # noqa: BLE001
            pass

    sys.addaudithook(hook)
    _WATCHING.append(hook)


def install():
    """Refuse egress, and the real Ollama's port, in THIS process from now
    on. Idempotent; called again, it puts the guard back in front of
    anything that has wrapped the socket functions since."""
    _watch_children()
    if not installed():
        _patch_socket()
    loaded = sys.modules.get(_Finder.NAME)
    if loaded is not None:
        _patch_proactor(loaded)
    elif not any(isinstance(f, _Finder) for f in sys.meta_path):
        # Just ahead of the PathFinder that would find the module - NOT at
        # the front, where tests/_no_tk keeps its own finder and checks that
        # it is first.
        import importlib.machinery
        finders = list(sys.meta_path)
        at = (finders.index(importlib.machinery.PathFinder)
              if importlib.machinery.PathFinder in finders else len(finders))
        sys.meta_path.insert(at, _Finder())


def installed():
    """Whether the guard's wrappers are the outermost ones right now. (A
    test may wrap one for a while - inferno_local's own socket guard does -
    and puts it back; install() puts the guard in front again.)"""
    if not socket.socket.__dict__.get(_MARK):
        return False
    for name in ("connect", "connect_ex", "sendto", "send", "sendall"):
        if not getattr(getattr(socket.socket, name), _MARK, False):
            return False
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr", "getnameinfo"):
        if hasattr(socket, name) and \
                not getattr(getattr(socket, name), _MARK, False):
            return False
    return True


def run_next_sitecustomize(here):
    """Run the sitecustomize that comes AFTER this folder on sys.path, if
    there is one: being on PYTHONPATH hides any later one (an interpreter's
    own, or coverage's). Only LATER entries are searched, so two hooks that
    each run the next one cannot run each other in a loop."""
    import importlib.machinery
    import importlib.util
    mine = os.path.normcase(os.path.abspath(here))
    norm = [os.path.normcase(os.path.abspath(p)) if p else "" for p in sys.path]
    start = norm.index(mine) + 1 if mine in norm else 0
    rest = [p for p, n in zip(sys.path[start:], norm[start:])
            if p and n != mine]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
