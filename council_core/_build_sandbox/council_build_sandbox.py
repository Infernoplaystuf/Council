"""
The Build tab's sandbox, inside the child: code run from the Build tab
(Run, Test) cannot reach the network, and its error output arrives in order
with its print()s.

council_core.build_sandbox puts this folder first on the child's PYTHONPATH,
so its sitecustomize runs at the child's startup, before the user's code:

  * OFFLINE. An audit hook (sys.addaudithook: it cannot be removed, and it
    sees every socket call the socket module makes, from any library) refuses
    a connection, a datagram or a name lookup to anything but this PC
    (localhost, 127.x, ::1). The refusal is a ConnectionRefusedError, so the
    code takes its "no network" path, and one line on stderr says what was
    refused. A python child the code starts inherits PYTHONPATH and so the
    hook; a program that is not Python (curl.exe) is not covered.
  * ONE STREAM. With COUNCIL_SANDBOX_MERGE=1, stderr is the same pipe as
    stdout (os.dup2), so a traceback lands where it happened in the output.

Stdlib only, no Council imports: the child is the user's own Python.
"""
import os
import sys

_LOCAL = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
          "::1", "0:0:0:0:0:0:0:1", ""}
_said = set()


class EgressRefused(ConnectionRefusedError):
    pass


def is_local(host) -> bool:
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("ascii", "replace")
    if host is None:
        return True
    if isinstance(host, int):              # an IPv4 address as a number
        return (host >> 24) == 127
    host = str(host).strip().strip("[]").lower()
    if "%" in host:
        host = host.split("%", 1)[0]
    return host in _LOCAL or host.startswith("127.") or host.startswith("::ffff:127.")


def _refuse(what: str, host, port=None):
    where = f"{host}:{port}" if port is not None else f"{host}"
    if where not in _said:
        _said.add(where)
        try:
            sys.__stderr__.write(f"[Council sandbox] refused {what} {where}: code run "
                                 "from the Build tab stays offline\n")
            sys.__stderr__.flush()
        except Exception:                                # noqa: BLE001
            pass
    raise EgressRefused(f"{what} {where} refused: the Council runs this code offline")


def _hook(event, args):
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and addr and not is_local(addr[0]):
            _refuse("a connection to", addr[0], addr[1] if len(addr) > 1 else None)
    elif event == "socket.getaddrinfo":
        if args and not is_local(args[0]):
            _refuse("looking up", args[0], args[1] if len(args) > 1 else None)
    elif event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        if args and not is_local(args[0]):
            _refuse("looking up", args[0])


def install() -> None:
    if os.environ.get("COUNCIL_SANDBOX_MERGE") == "1":
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
        except Exception:                                # noqa: BLE001
            pass
    if os.environ.get("COUNCIL_SANDBOX_OFFLINE", "1") == "1":
        sys.addaudithook(_hook)


def run_next_sitecustomize(here) -> None:
    """Run the sitecustomize this folder's one shadows (the interpreter's
    own, a test guard's), searching only the entries after this folder."""
    import importlib.machinery
    import importlib.util
    mine = os.path.normcase(os.path.abspath(here))
    norm = [os.path.normcase(os.path.abspath(p)) if p else "" for p in sys.path]
    start = norm.index(mine) + 1 if mine in norm else 0
    rest = [p for p, n in zip(sys.path[start:], norm[start:]) if p and n != mine]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
