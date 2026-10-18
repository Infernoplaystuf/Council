"""
gui_policy.py — what a generated GUI project is allowed to do.

Pure stdlib, importable from both the designer and the generated app, so the
same rule is enforced wherever code can be run (the nx_policy pattern).

THIS IS A DIFFERENT, DELIBERATELY MORE PERMISSIVE POLICY THAN THE ANALYST'S
--------------------------------------------------------------------------
vault_analyst.validate_generated_code guards a READ-ONLY data sandbox: it must
forbid writing files at all. A generated application is not that. It legitimately
opens files the user picked, saves output, and imports modules. Reusing the
analyst validator here would reject every real app; reusing its STRUCTURE is
correct, and so is its hard-won lesson:

    CHECK EVERY ast.Attribute NODE, CALLED OR NOT.

A blocklist is only as strong as its least-checked syntactic form. `os.system(x)`
is an obvious call, but `fn = os.system` then `fn(x)` is the same capability
through a name binding, and a validator that only inspects ast.Call sees
nothing. That exact hole was demonstrated against this repo's analyst sandbox —
a bound method escaped it and deleted a real file — which is why it is closed
here from the start rather than after an incident.

WHAT THIS CANNOT DO, STATED PLAINLY
-----------------------------------
Spec 8 asks that a project not write "outside the project directory or an
explicitly user-chosen path". Statically, that is undecidable: open(p, "w")
where p is computed at runtime cannot be resolved by reading the source. So this
module denies the calls whose PURPOSE is destruction (rmtree, unlink, remove,
rmdir) and the modules that escape the process entirely, and it does NOT claim
to prove where a write lands. Claiming an enforcement that does not exist would
be worse than the gap: it would make a reviewer stop looking.

For the same reason: a name assembled at runtime (getattr(os, "sys" + "tem"))
cannot be read statically either. Every SPELLED form is checked — attribute,
`from os import system`, `from os import *`, getattr with a literal, and the
namespace dicts (__dict__, vars(), globals()) a literal key would reach it
through — but this is a gate against generated code going wrong, not a
sandbox against code written to escape it.
"""
from __future__ import annotations

import ast
import builtins as _builtins
import keyword
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

# The Council's own source directory. A `requires` naming a module here is
# Council code, not a package — see check_requires.
APP_ROOT = Path(__file__).resolve().parent

MODES = ("linked", "standalone")

# Modules a "linked" project may import beyond the stdlib (spec 8).
# council_engine is ABSENT ON PURPOSE: importing it from a generated app would
# construct a SECOND GGUF singleton in a second process — two models, two VRAM
# allocations, and an inference lock that no longer serialises anything.
LINKED_MODULES = frozenset({
    "image_stats", "image_index", "plot_registry", "plots_pane", "graph_data",
    "vault_analyst", "data_index", "df_cache", "stats_cache", "provenance",
    "frame_timing", "frame_roi", "frame_classes", "frame_camera",
    "gui_settings",
})

# Third-party packages both modes may use.
THIRD_PARTY = frozenset({"pandas", "numpy", "matplotlib", "PIL", "pillow"})

# ---- the Qt target -----------------------------------------------------
# PySide6 IS NOT ADMITTED BY ROOT NAME, and that distinction is the whole
# point. Every other rule in this module keys on the root of a dotted import
# (_check_import sees "PySide6", never "PySide6.QtNetwork"), so putting
# "PySide6" on an allowlist would admit the entire binding in one step —
# measured against this gate before the rule below existed:
#
#     requires: PySide6   ->   QProcess, QtNetwork, QDesktopServices,
#                              QSettings, QPluginLoader, QtQml and QtSql
#                              all passed unchallenged.
#
# QtNetwork, QtQml/QtQuick, QtSql and QtDBus all ship inside
# PySide6-Essentials, so they are present on any machine that can run a Qt
# project. A generated app needs exactly three modules.
TOOLKIT_ROOTS = frozenset({"PySide6"})
QT_SUBMODULES_OK = frozenset({"QtCore", "QtGui", "QtWidgets"})

# A second Qt binding cannot share a process with PySide6 — it loads its own
# copy of the Qt libraries and the app dies on import. These are refused by
# name so a declaration cannot smuggle one in.
FOREIGN_QT = frozenset({"PyQt5", "PyQt6", "PySide2", "qtpy"})

# Names denied wherever they are spelled, for the same reason DENIED_ATTRS
# exists: a capability reached through an attribute is the same capability.
# Each of these escapes the process, reaches the network, writes the registry,
# loads native code, or deletes a tree.
DENIED_QT_NAMES = frozenset({
    "QProcess", "QProcessEnvironment",          # a shell by another name
    "QLibrary", "QPluginLoader",                # loads arbitrary native code
    "QDesktopServices",                         # openUrl launches a browser
    "QSettings",                                # writes the Windows registry
    "QNetworkAccessManager", "QNetworkRequest", "QTcpSocket", "QUdpSocket",
    "QSslSocket", "QLocalSocket", "QWebSocket",
    "QSharedMemory",                            # cross-process memory
    "removeRecursively",                        # QDir's spelling of rmtree
    "QQmlEngine", "QJSEngine",                  # evaluate arbitrary JS
    "QSqlDatabase", "QSqlQuery",
    "QUiLoader",                                # loads UI from a file
})

# The generated project's own modules.
# "main" is the entry point since the launch.py -> main.py rename, and the
# launch.py shim left in older projects is literally `from main import main`.
# Without it here that shim — generated code — failed the project's own gate,
# so every older project reported "policy REFUSED" on Generate while running
# fine, which teaches a user to ignore the one message that matters.
PROJECT_MODULES = frozenset({"ui", "app", "handlers", "launch", "main",
                             "widgets", "main_ui"})

# ---- why a name is refused, in words a user can act on -----------------
# Every refusal says which of these it is. A bare "not permitted" is the
# message people learn to work around; a reason is the one they can judge.
WHY_PROGRAM = "it starts another program or Python process, outside the app"
WHY_NETWORK = ("it reaches the network, and nothing leaves this PC unless the "
               "user opts in")
WHY_ASYNCIO = ("its subprocess and connection calls start programs and reach "
               "the network")
WHY_DESKTOP = "it opens a browser or another program on the desktop"
WHY_EXIT = ("it ends the app at once, with no file closed and no recording "
            "finished; return, or close the window, instead")
WHY_NATIVE = "it calls native code directly, past every rule here"
WHY_OBJECTS = "it turns data back into live objects, which runs code"
WHY_CODE = ("it runs text as code, or imports a module by a name built at "
            "runtime")
WHY_MODULE_TABLE = ("it hands back any loaded module by a string key, past "
                    "the import rules; import what the app uses instead")
WHY_ALL_OBJECTS = ("it hands back every live object, loaded modules "
                   "included, past the import rules")
WHY_REGISTRY = "it writes the Windows registry"
WHY_SIGNAL = "it signals or ends a process — this app's or another's"
WHY_ENV = "it changes the environment the programs the app starts inherit"
WHY_DELETE = "it deletes files or folders"
WHY_INTERPRETER = "it reaches the interpreter's own state, past every rule here"

# Denied in BOTH modes, by ROOT name — `import asyncio.subprocess` and
# `from xmlrpc.client import ServerProxy` are both their root. Each escapes
# the process, reaches the network, executes arbitrary text, or deserialises
# into live objects.
#
# The second half was measured passing this gate on 2026-10-15 (the Typhon
# plugin research found the first few; the rest are their relatives): asyncio
# started a child Python and served on a socket, socketserver + xmlrpc
# answered a call, and each was admitted as "stdlib" because the allowlist is
# sys.stdlib_module_names. The private halves (_socket, _ssl, _winapi,
# _posixsubprocess, _overlapped, _ctypes, _imp) are the same capability under
# a name nobody thinks to deny.
DENIED_MODULE_REASONS = {
    # starts programs
    "subprocess": WHY_PROGRAM, "pty": WHY_PROGRAM,
    "multiprocessing": WHY_PROGRAM, "_multiprocessing": WHY_PROGRAM,
    "_winapi": WHY_PROGRAM, "_posixsubprocess": WHY_PROGRAM,
    "pipes": WHY_PROGRAM,                    # Template runs /bin/sh
    "distutils": WHY_PROGRAM,                # distutils.spawn, ccompiler
    "ensurepip": WHY_PROGRAM,                # runs pip, which downloads
    "venv": WHY_PROGRAM,                     # runs ensurepip
    "idlelib": WHY_PROGRAM,                  # its shell is a subprocess + rpc
    # the network
    "socket": WHY_NETWORK, "_socket": WHY_NETWORK,
    "ssl": WHY_NETWORK, "_ssl": WHY_NETWORK,
    "requests": WHY_NETWORK, "urllib": WHY_NETWORK, "urllib2": WHY_NETWORK,
    "urllib3": WHY_NETWORK, "http": WHY_NETWORK,
    "ftplib": WHY_NETWORK, "telnetlib": WHY_NETWORK, "smtplib": WHY_NETWORK,
    "poplib": WHY_NETWORK, "imaplib": WHY_NETWORK, "nntplib": WHY_NETWORK,
    "smtpd": WHY_NETWORK, "asyncore": WHY_NETWORK, "asynchat": WHY_NETWORK,
    "socketserver": WHY_NETWORK, "xmlrpc": WHY_NETWORK,
    "wsgiref": WHY_NETWORK,                  # simple_server is http.server
    "_overlapped": WHY_NETWORK,              # asyncio's Windows connect/send
    "asyncio": WHY_ASYNCIO, "_asyncio": WHY_ASYNCIO,
    # the desktop
    "webbrowser": WHY_DESKTOP,
    "antigravity": WHY_DESKTOP,              # importing it opens a browser
    # native code
    "ctypes": WHY_NATIVE, "_ctypes": WHY_NATIVE, "cffi": WHY_NATIVE,
    # data into objects
    "pickle": WHY_OBJECTS, "cPickle": WHY_OBJECTS, "marshal": WHY_OBJECTS,
    "shelve": WHY_OBJECTS, "dill": WHY_OBJECTS,
    # text into code, and imports by a computed name
    "importlib": WHY_CODE, "imp": WHY_CODE, "_imp": WHY_CODE,
    "_frozen_importlib": WHY_CODE, "_frozen_importlib_external": WHY_CODE,
    "runpy": WHY_CODE, "code": WHY_CODE, "codeop": WHY_CODE,
    "pkgutil": WHY_CODE,                     # resolve_name('subprocess:Popen')
    "pydoc": WHY_CODE,                       # locate('subprocess.Popen')
    "zipimport": WHY_CODE, "site": WHY_CODE,  # addsitedir runs .pth lines
    "cProfile": WHY_CODE, "profile": WHY_CODE, "trace": WHY_CODE,
    "pdb": WHY_CODE, "bdb": WHY_CODE, "doctest": WHY_CODE,
    "rlcompleter": WHY_CODE,
    # Not stdlib on 3.11 (so refused anyway), named so a newer Python that
    # ships them cannot admit them: each runs a string in a subinterpreter.
    "_xxsubinterpreters": WHY_CODE, "_interpreters": WHY_CODE,
    # `builtins.exec(...)` is exec — and `compile` cannot be denied as an
    # attribute, because re.compile is everywhere.
    "builtins": WHY_CODE,
    # the registry — QSettings is denied for the same reason
    "winreg": WHY_REGISTRY, "_winreg": WHY_REGISTRY,
}
DENIED_MODULES = frozenset(DENIED_MODULE_REASONS)

# Denied by their FULL dotted name, under a root that stays permitted:
# concurrent.futures' ThreadPoolExecutor is ordinary, its process half is a
# second Python (measured: a ProcessPoolExecutor worker wrote a file the gate
# never saw). Matched on the module path and on any name imported from it.
DENIED_SUBMODULES = {
    "concurrent.futures.process": WHY_PROGRAM,
    "concurrent.interpreters": WHY_CODE,     # 3.14+
}

# Attribute names denied only on ONE module, because the same word is
# ordinary on everything else: Job().abort() is a method, os.abort() ends the
# process; threading.Timer is a thread, timeit.Timer('...') runs a string.
# The receiver is resolved through the file's own imports (`import os as o`,
# `from logging import config`) and matched on its last component, so
# shutil.os.abort is os.abort too. A module bound to a plain variable is not
# followed — that is a name assembled at runtime, which no static gate reads.
DENIED_MODULE_ATTRS = {
    "os": {"abort": WHY_EXIT},
    "nt": {"abort": WHY_EXIT},
    "posix": {"abort": WHY_EXIT},
    "signal": {"alarm": WHY_EXIT,            # SIGALRM's default ends it
               "setitimer": WHY_EXIT},
    "logging.config": {"listen": WHY_NETWORK,       # a socket server for config
                       # socketserver is re-exported INTO logging.config, so
                       # the TCP server and handler listen() builds are
                       # reachable directly (logging.config.ThreadingTCPServer
                       # IS socketserver.ThreadingTCPServer).
                       "ThreadingTCPServer": WHY_NETWORK,
                       "StreamRequestHandler": WHY_NETWORK},
    "timeit": {"timeit": WHY_CODE, "repeat": WHY_CODE, "Timer": WHY_CODE,
               "main": WHY_CODE},
    # every live object, loaded modules included — sys.modules without the
    # name; gc.collect() stays ordinary
    "gc": {"get_objects": WHY_ALL_OBJECTS, "get_referents": WHY_ALL_OBJECTS,
           "get_referrers": WHY_ALL_OBJECTS},
}

# Builtins that turn data into code, and the namespace dicts that hand any
# denied name back through a string key: vars(os)["system"],
# globals()["__builtins__"]["eval"]. Generated code uses none of them.
DENIED_BUILTINS = frozenset({"eval", "exec", "compile", "__import__",
                             "__builtins__", "globals", "vars", "locals"})

# `from X import *` pulls every public name in — `from os import *` is
# `system` with no attribute left to check. Tkinter is the one library whose
# documentation teaches star imports, and it exports nothing on the deny list.
STAR_IMPORT_OK = frozenset({"tkinter"})


def _why(reason: str, *names: str) -> dict:
    return {n: reason for n in names}


# A DENIED module's own name, reached as an attribute: refused only where
# the receiver is, or may be, a module (_Receivers says which). A module that
# imported a denied one hands it over with no import the gate can see —
# logging.handlers.socket, frame_camera.ctypes, np.ctypeslib — and several
# allowed stdlib modules keep one bound as a live attribute: codecs.builtins
# IS builtins (so codecs.builtins.eval is eval), enum aliases it `bltns`,
# cgitb.pydoc is pydoc and unittest.mock.pkgutil is pkgutil (locate /
# resolve_name load any callable from a dotted string), mimetypes._winreg is
# winreg, xml.sax.saxutils.urllib and email.utils.urllib are the urllib
# package (saxutils' own code calls urllib.request.urlopen); sys.modules,
# meta_path and path_hooks are `import` with no import statement. The route
# is closed at the module name, one step before the terminal .eval /
# .locate / .CreateKeyEx, which cannot be denied directly: re.compile,
# QApplication.exec() and DataFrame.eval are honest.
#
# ON AN ORDINARY OBJECT THE SAME WORD IS ONLY A NAME. These were refused on
# every receiver (2026-10-15), so `self.socket` holding a camera
# connection, `self.modules` a plugin list and `self.ssl` a checkbox failed
# the gate with "it reaches the network" about an attribute that reaches
# nothing. Now they pass on self, cls and anything else the file shows is
# not a module, and are refused on everything that is or may be one.
MODULE_ROUTE_REASONS = {
    **_why(WHY_PROGRAM,
           "subprocess", "_winapi", "_posixsubprocess", "multiprocessing"),
    **_why(WHY_NETWORK,
           "socket", "_socket", "ssl", "socketserver", "xmlrpc",
           "urllib", "urllib2", "urllib3"),
    **_why(WHY_ASYNCIO, "asyncio"),
    **_why(WHY_DESKTOP, "webbrowser"),
    **_why(WHY_NATIVE, "ctypes", "_ctypes", "ctypeslib"),
    **_why(WHY_MODULE_TABLE,
           "modules", "meta_path", "path_hooks", "path_importer_cache"),
    **_why(WHY_CODE, "importlib", "builtins", "bltns", "pydoc", "pkgutil"),
    **_why(WHY_REGISTRY, "winreg", "_winreg"),
}
MODULE_ROUTES = frozenset(MODULE_ROUTE_REASONS)

# Attribute names the gate refuses: everywhere, except MODULE_ROUTES, which
# are refused only on a receiver that may be a module. os and sys are
# PERMITTED — a real application needs os.path and sys.argv — so the
# dangerous surface is denied by name instead of by module.
#
# Every name here — module routes included — also stops being a port name
# (gui_ports renames a "Remove" button's port remove_button), so a word a
# user would type as a label belongs in DENIED_MODULE_ATTRS instead, keyed
# on its module.
DENIED_ATTR_REASONS = {
    # process / shell escape — os.spawn*/exec*/posix_spawn* in every
    # spelling: the first few were denied and their siblings (spawnvp,
    # execvpe, posix_spawn ...) passed, which is no denial at all.
    **_why(WHY_PROGRAM,
           "system", "popen", "spawn", "spawnl", "spawnle", "spawnlp",
           "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
           "execl", "execle", "execlp", "execlpe", "execv", "execve",
           "execvp", "execvpe", "posix_spawn", "posix_spawnp",
           "fork", "forkpty",
           # subprocess's and multiprocessing's entry points, by any
           # receiver (the modules themselves are MODULE_ROUTES)
           "Popen", "getoutput", "getstatusoutput",
           # a ProcessPoolExecutor's worker is a second Python
           "ProcessPoolExecutor",
           # asyncio's subprocess calls, on a loop from anywhere
           "create_subprocess_exec", "create_subprocess_shell",
           "subprocess_exec", "subprocess_shell"),
    **_why(WHY_SIGNAL, "kill", "killpg"),
    **_why(WHY_ENV, "putenv"),
    # ends the process with nothing cleaned up
    **_why(WHY_EXIT,
           "_exit", "raise_signal", "pthread_kill",
           # faulthandler's crash helpers
           "_sigsegv", "_sigabrt", "_sigfpe", "_read_null", "_fatal_error",
           "_fatal_error_c_thread", "_stack_overflow"),
    # the network, by the calls themselves
    **_why(WHY_NETWORK,
           "open_connection", "start_server", "create_connection",
           "create_server", "create_datagram_endpoint",
           "open_unix_connection", "start_unix_server",
           "create_unix_connection", "create_unix_server", "sock_connect",
           "connect_accepted_socket",
           # logging.handlers' network handlers (its file handlers are fine)
           "SocketHandler", "DatagramHandler", "SysLogHandler",
           "HTTPHandler", "SMTPHandler"),
    # the desktop: os.startfile opens any file with its program
    **_why(WHY_DESKTOP, "startfile"),
    # native code by another door: `import ctypes` is denied, but numpy hands
    # it over (np.ctypeslib.ctypes.windll...), as does any module that
    # imported it (frame_camera.ctypes) — its loaders, by any receiver
    **_why(WHY_NATIVE,
           "windll", "cdll", "oledll",
           "pydll", "pythonapi", "WinDLL", "CDLL", "OleDLL", "PyDLL",
           "LoadLibrary", "add_dll_directory"),
    # destruction
    **_why(WHY_DELETE, "rmtree", "unlink", "remove", "removedirs", "rmdir"),
    # introspection escapes that reach the interpreter's own state
    **_why(WHY_INTERPRETER,
           "__subclasses__", "__bases__", "__mro__", "__globals__",
           "__code__", "__closure__", "__builtins__", "__import__",
           "__reduce__", "__reduce_ex__", "__getattribute__", "__dict__",
           # a frame's namespaces are globals()/vars() by another name:
           # sys._getframe().f_builtins['eval'] is eval
           "f_globals", "f_locals", "f_builtins"),
    # loaders
    **_why(WHY_CODE, "load_module", "import_module", "exec_module"),
    **_why(WHY_OBJECTS, "loads", "load"),
    # a denied module's own name: on a module only (see MODULE_ROUTES)
    **MODULE_ROUTE_REASONS,
}
DENIED_ATTRS = frozenset(DENIED_ATTR_REASONS)

# Calls that look an attribute up by a STRING: getattr(os, 'system') is
# os.system. operator.attrgetter('system')(os) and methodcaller('system',
# cmd)(os) are the same lookup spelled through operator, and
# inspect.getattr_static through inspect; each literal is checked like
# getattr's (attrgetter's dotted 'path.system' part by part).
LOOKUP_CALLS = frozenset({"getattr", "attrgetter", "methodcaller",
                          "getattr_static"})

# `loads`/`load` are denied above because pickle.loads is the risk, but json
# and PIL use the same spelling harmlessly. These receivers are exempted so a
# normal app is not rejected for reading its own settings file.
SAFE_LOAD_RECEIVERS = frozenset({"json", "yaml", "tomllib", "toml", "Image",
                                 "np", "numpy", "plt", "pd", "pandas"})


def _stdlib_names() -> Set[str]:
    """The stdlib module names for the running interpreter.

    sys.stdlib_module_names (3.10+) is authoritative and version-accurate,
    which a hand-maintained list never stays."""
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    return {  # pragma: no cover - only on <3.10, below this app's floor
        "abc", "argparse", "ast", "base64", "collections", "csv", "dataclasses",
        "datetime", "enum", "functools", "io", "itertools", "json", "logging",
        "math", "os", "pathlib", "random", "re", "shutil", "statistics",
        "string", "sys", "tempfile", "textwrap", "threading", "time", "tkinter",
        "traceback", "typing", "uuid", "warnings",
    }


def _roots(names: Sequence[str]) -> Set[str]:
    return {str(n).strip().split(".")[0] for n in names if str(n).strip()}


def allowed_modules(mode: str, extra: Sequence[str] = (),
                    toolkit: str = "tk") -> Set[str]:
    """Every root module name importable in ``mode``.

    ``extra`` is the project's declared `requires` — a camera app's SDK — plus,
    from validate_dir, the project's own modules. It widens the allowlist for
    THAT project only, so an import nobody declared is still refused, and it
    can never re-admit a denied module, nor an app module into a standalone
    project: those subtractions happen last."""
    base = _stdlib_names() | set(THIRD_PARTY) | set(PROJECT_MODULES)
    if (toolkit or "tk").strip().lower() in ("qt", "pyside6"):
        # The ROOT only. Which submodules may follow it is _check_qt's job,
        # and a Qt project does not have to declare its own toolkit in
        # `requires` — `requires` is for what the APP needs, not the frame
        # the designer generated it into.
        base |= set(TOOLKIT_ROOTS)
    if mode == "linked":
        base |= set(LINKED_MODULES)
    # Council code is never admitted this way (check_requires says why).
    base |= {r for r in _roots(extra)
             if r in LINKED_MODULES or r in PROJECT_MODULES
             or not is_council_module(r)}
    if mode != "linked":
        base -= set(LINKED_MODULES)
    return base - set(DENIED_MODULES)


def as_requires(value) -> List[str]:
    """A `requires` value as a clean list, whatever shape it arrived in.

    A hand-edited or model-written gspec may say "requires": "numpy" where a
    list was meant; iterating that string gave ['n', 'u', 'm', 'p', 'y'] and
    main.py imported each letter."""
    if value is None:
        return []
    if isinstance(value, str):
        return parse_requires(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def parse_requires(text) -> List[str]:
    """'pypylon, numpy PIL' -> ['pypylon', 'numpy', 'PIL'].

    Commas or whitespace separate; order is kept and repeats dropped, so what
    the user typed is what the gspec stores. Validity is check_requires' job."""
    out: List[str] = []
    for part in str(text or "").replace(",", " ").split():
        if part not in out:
            out.append(part)
    return out


def is_council_module(root: str) -> bool:
    """Whether ``root`` is a module of the Council itself (a .py file or a
    package beside this one)."""
    return ((APP_ROOT / f"{root}.py").is_file()
            or (APP_ROOT / root / "__init__.py").is_file())


def check_requires(names: Sequence[str], mode: str = "linked") -> List[str]:
    """Problems with a project's declared `requires`, one message each.

    Declaring a package is how a project widens its own allowlist, so the
    declaration itself is gated: a denied module (subprocess, socket, pickle,
    ...) cannot be declared into it, and neither can Council code. `requires`
    is for PACKAGES. The Council modules an app may reach are LINKED_MODULES,
    and only in linked mode: council_agents, vault_rag and the rest import
    council_engine at load, so declaring one would build the second GGUF
    singleton the council_engine ban exists to prevent."""
    errs: List[str] = []
    seen: Set[str] = set()
    for raw in as_requires(names):
        name = str(raw).strip()
        if not name:
            continue
        parts = name.split(".")
        if not all(p.isidentifier() and not keyword.iskeyword(p)
                   for p in parts):
            errs.append(f"requires {name!r} is not an importable module name "
                        f"(one package per entry, by its IMPORT name — PIL, "
                        f"not Pillow; no 'as')")
            continue
        root = parts[0]
        sub = _denied_submodule(name)
        if root in DENIED_MODULES:
            errs.append(f"requires {name!r}: {root!r} is never permitted in a "
                        f"generated app, declared or not — "
                        f"{DENIED_MODULE_REASONS[root]}")
        elif sub:
            errs.append(f"requires {name!r}: {sub[0]!r} is never permitted in "
                        f"a generated app, declared or not — {sub[1]}")
        elif root in FOREIGN_QT:
            errs.append(f"requires {name!r}: the Qt target is PySide6, and two "
                        f"Qt bindings cannot share one process")
        elif root in TOOLKIT_ROOTS:
            # Declaring the toolkit would be a way to ask for the whole binding
            # on a Tk project. A Qt project gets PySide6 from its TARGET, and
            # only QtCore/QtGui/QtWidgets even then.
            errs.append(f"requires {name!r}: the toolkit is not declared here — "
                        f"a Qt project gets PySide6 from its emit target, and "
                        f"only {', '.join(sorted(QT_SUBMODULES_OK))} with it")
        elif root == "council_engine":
            errs.append("requires 'council_engine': it would build a second "
                        "GGUF singleton inside the app")
        elif root in LINKED_MODULES:
            if mode != "linked":
                errs.append(f"requires {name!r}: {root!r} is Council code, so "
                            f"a standalone project cannot use it — switch the "
                            f"project to linked mode")
        elif root not in PROJECT_MODULES and is_council_module(root):
            errs.append(f"requires {name!r}: {root!r} is part of the Council, "
                        f"not a package. An app may use only the linked "
                        f"modules: {', '.join(sorted(LINKED_MODULES))}")
        if name in seen:
            errs.append(f"requires {name!r} is listed twice")
        seen.add(name)
    return errs


def validate(code: str, mode: str = "linked",
             extra_modules: Sequence[str] = (),
             toolkit: str = "tk") -> Tuple[bool, List[str]]:
    """(ok, errors) for one source file.

    Returns EVERY fault, like gui_spec.validate — a user fixing generated or
    hand-written code should see the whole list, not one per run.

    ``extra_modules`` is the project's declared `requires`."""
    errs: List[str] = []
    if mode not in MODES:
        return False, [f"unknown import mode {mode!r}; expected one of {MODES}"]
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return False, [f"does not parse: line {exc.lineno}: {exc.msg}"]

    allowed = allowed_modules(mode, extra_modules, toolkit)
    aliases = _module_aliases(tree)
    receivers = _Receivers(tree)

    for node in ast.walk(tree):
        # ---- imports ----
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                _check_import(root, node, mode, allowed, errs)
                if root not in DENIED_MODULES:
                    _check_submodule(a.name, node, errs)
                _check_qt(a.name, node, errs)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and not node.level:     # relative: inside the project
                _check_import(root, node, mode, allowed, errs)
                if root not in DENIED_MODULES:
                    _check_submodule(node.module or "", node, errs)
                _check_qt(node.module or "", node, errs)
            # `from os import system` binds the denied attribute to a plain
            # name, and a plain name is never checked again — so the IMPORTED
            # NAMES are checked here, relative imports included.
            line = getattr(node, "lineno", 0)
            only_here = ({} if node.level else
                         _module_attr_rule(node.module or ""))
            for a in node.names:
                if (root and not node.level and root not in DENIED_MODULES
                        and a.name != "*"
                        and not _denied_submodule(node.module or "")):
                    # `from concurrent.futures import process` — a submodule
                    # arriving as a NAME, which the module path never shows.
                    _check_submodule(f"{node.module}.{a.name}", node, errs)
                if a.name in only_here:
                    errs.append(f"line {line}: importing {a.name!r} from "
                                f"{node.module} is not permitted in a "
                                f"generated app — {only_here[a.name]}")
                    continue
                if a.name == "*":
                    if node.level or root in STAR_IMPORT_OK:
                        continue
                    errs.append(f"line {line}: `from {node.module} import *` "
                                f"is not permitted in a generated app — "
                                f"import the names it uses")
                elif ((node.module or "") in TOOLKIT_ROOTS
                      and not node.level
                      and a.name not in QT_SUBMODULES_OK):
                    # `from PySide6 import QtNetwork` — the submodule arrives as
                    # an imported NAME, not as part of the module path, so the
                    # dotted-name rule never sees it.
                    errs.append(
                        f"line {line}: {node.module}.{a.name} is not permitted "
                        f"in a generated app — only "
                        f"{', '.join(sorted(QT_SUBMODULES_OK))} are")
                elif a.name in DENIED_QT_NAMES:
                    errs.append(f"line {line}: importing {a.name!r} from "
                                f"{node.module or '.'} is not permitted in a "
                                f"generated app — it escapes the process, "
                                f"reaches the network, or loads native code")
                elif a.name in DENIED_BUILTINS or (
                        a.name in DENIED_ATTRS
                        and not (a.name in ("load", "loads")
                                 and root in SAFE_LOAD_RECEIVERS)):
                    errs.append(f"line {line}: importing {a.name!r} from "
                                f"{node.module or '.'} is not permitted in a "
                                f"generated app — {_reason(a.name)}")

        # ---- attribute access: CALLED OR NOT ----
        elif isinstance(node, ast.Attribute):
            if node.attr in DENIED_QT_NAMES:
                recv = _receiver_name(node.value)
                where = f"{recv}." if recv else ""
                errs.append(
                    f"line {getattr(node, 'lineno', 0)}: {where}{node.attr} is "
                    f"not permitted in a generated app")
            elif node.attr in DENIED_ATTRS:
                recv = _receiver_name(node.value)
                if node.attr in ("load", "loads") and recv in SAFE_LOAD_RECEIVERS:
                    continue
                route = node.attr in MODULE_ROUTES
                if route and not receivers.may_be_module(node.value):
                    continue       # self.socket: a name on an ordinary object
                where = f"{recv}." if recv else ""
                errs.append(
                    f"line {getattr(node, 'lineno', 0)}: {where}{node.attr} is "
                    f"not permitted in a generated app — "
                    f"{_reason(node.attr)}"
                    + (_route_note(node.attr) if route else ""))
            else:
                why = _module_attr_rule(
                    _dotted(node.value, aliases)).get(node.attr)
                if why:
                    recv = _receiver_name(node.value)
                    errs.append(
                        f"line {getattr(node, 'lineno', 0)}: {recv}."
                        f"{node.attr} is not permitted in a generated app — "
                        f"{why}")

        # ---- name-level ----
        elif isinstance(node, ast.Name):
            if node.id in DENIED_QT_NAMES:
                # `from PySide6.QtCore import QProcess` is caught above, but a
                # name bound any other way is the same capability.
                errs.append(f"line {getattr(node, 'lineno', 0)}: "
                            f"{node.id} is not permitted in a generated app")
            elif node.id in DENIED_BUILTINS:
                call = "" if node.id.startswith("__b") else "()"
                errs.append(f"line {getattr(node, 'lineno', 0)}: "
                            f"{node.id}{call} is not permitted")

        # ---- getattr(x, "system") — the string-indirection bypass ----
        elif isinstance(node, ast.Call):
            _check_lookup(node, aliases, errs, receivers)

    return (not errs), errs


def _route_note(name: str) -> str:
    """What a refused module route says after its reason: where it was
    reached, and that the same word on an ordinary object is fine."""
    return (f" (reached through what is, or may be, a module; a plain "
            f"attribute such as self.{name} is fine)")


def _reason(name: str) -> str:
    """Why ``name`` (an attribute, imported name or builtin) is refused."""
    if name in DENIED_ATTR_REASONS:
        return DENIED_ATTR_REASONS[name]
    if name in DENIED_BUILTINS:
        return WHY_CODE
    return "it escapes the process, reaches the network, or loads native code"


def _denied_submodule(full: str):
    """(the denied dotted name, why) when ``full`` is or is under one."""
    for name, why in DENIED_SUBMODULES.items():
        if full == name or full.startswith(name + "."):
            return name, why
    return None


def _check_submodule(full: str, node: ast.AST, errs: List[str]) -> None:
    hit = _denied_submodule(full)
    if hit:
        errs.append(f"line {getattr(node, 'lineno', 0)}: importing "
                    f"{hit[0]!r} is not permitted in a generated app — "
                    f"{hit[1]}")


def _module_aliases(tree: ast.AST) -> dict:
    """Local name -> the dotted module path it was imported as, from every
    import in the file: `import os as o` gives o -> os, `from logging import
    config` gives config -> logging.config. What an imported name really is
    (module or function) is not known statically and does not need to be:
    it is only ever compared against DENIED_MODULE_ATTRS' module names."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    out[root] = root
        elif isinstance(node, ast.ImportFrom) and not node.level \
                and node.module:
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict) -> str:
    """The module path an attribute's receiver names, or "" when it is not
    a plain dotted name (a call, a subscript)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value, aliases)
        return f"{head}.{node.attr}" if head else ""
    return ""


def _module_attr_rule(path: str) -> dict:
    """DENIED_MODULE_ATTRS' entry for a receiver path: the module itself, or
    a path that ENDS in it (shutil.os is os)."""
    for name, rule in DENIED_MODULE_ATTRS.items():
        if path == name or path.endswith("." + name):
            return rule
    return {}


# Names a file may use without binding them: the builtins, and the globals
# every module has. Any OTHER name a file uses and never binds came from a
# star import (or was injected), and may be anything — a module included.
_ALWAYS_THERE = frozenset(dir(_builtins)) | frozenset({
    "__name__", "__file__", "__doc__", "__spec__", "__loader__",
    "__package__", "__annotations__", "__path__", "__cached__"})

# Pattern captures (3.10+): `case m:` binds the subject, which may be a module.
_MATCH_CAPTURES = tuple(getattr(ast, n) for n in
                        ("MatchAs", "MatchStar", "MatchMapping")
                        if hasattr(ast, n))


class _FuncInfo:
    """One function's shape, for the parameter rule below."""
    __slots__ = ("name", "positional", "params", "first", "static",
                 "is_method")

    def __init__(self, name, positional, params, first, static, is_method):
        self.name = name                 # a def's name, a lambda's bound name
        self.positional = positional     # ordered positional parameter names
        self.params = params             # every parameter name (frozenset)
        self.first = first               # self/cls, or "" when there is none
        self.static = static
        self.is_method = is_method


class _Receivers:
    """Which receivers in one file are, or may be, a module — the receivers
    a MODULE_ROUTE is refused on.

    Read from the whole file at once, flow-insensitive but SCOPE-AWARE where
    scope decides it (a default and an annotation are read in the ENCLOSING
    scope, not the function's own), and erring towards "may be". A receiver
    may be a module when it is:

      * a name an import binds (`import codecs`, `from xml.sax import
        saxutils` — an imported name may be a module), or any attribute
        chain rooted at one (logging.handlers, np.ctypeslib);
      * a name — or ANY attribute of that name, on anything — that the file
        assigns, at least once, a value that may be or hold a module:
        `m = codecs`, `self.mod = codecs`, a class body's `mod = codecs`
        (read later as self.mod), `setattr(self, 'mod', codecs)`, a walrus,
        a default — so m.builtins and self.mod.builtins are refused as
        codecs.builtins is;
      * a value the gate cannot see: a call's result (inspect.getmodule(f),
        getattr(codecs, name)), a subscript, an awaited or yielded value,
        and every name bound by iterating an iterable that may carry a
        module, by unpacking one, by `with ... as` or by a match pattern.
        A CALL TO A CLASS THE FILE DEFINES is an ordinary instance, not a
        value the gate cannot see — `opts = Opts()`, `self.cam = Cam()` —
        so a plain attribute of it keeps the word as a name;
      * a parameter a call in the file hands a module (f(codecs), the
        default m=codecs, b.put(codecs) binding self.held = m), or EVERY
        non-first parameter of a function whose name is used anywhere but
        as the thing a call calls — stored, aliased, decorated, passed on —
        because then the gate cannot see how it is called (a function only
        ever called in plain sight, or never referenced, is read exactly:
        `def f(opts): return opts.ssl` is ordinary);
      * `self` in a class whose instances answer an attribute with a value
        the gate cannot pin to a name: a @dataclass / NamedTuple /
        SimpleNamespace (the constructor sets any field, H(codecs)), or a
        class that defines __getattr__ / __getattribute__; and the name a
        property or cached_property returns, from its return values;
      * a name the file uses but never binds, other than a builtin: it came
        from a star import and may be anything.

    Everything else is an ordinary object, where the same word is only a
    name: self and cls and their attributes (outside the class shapes
    above), an instance of a class the file defines, a literal, and a name
    only ever assigned values like those.

    Not followed, stated plainly: a module handed in AS self
    (Cls.method(codecs)); an attribute set through a name built at runtime
    (setattr(obj, name, codecs)); and a function reached only by a string
    (getattr(self, 'f')(codecs)) — its name is not written anywhere, so the
    gate cannot see that a module reaches its parameters."""

    def __init__(self, tree: ast.AST) -> None:
        self.tainted: Set[str] = set()      # names that may hold a module
        self.bound: Set[str] = set()        # every name the file binds
        self._classes: Set[str] = set()     # ClassDef names the file defines
        self._scopes: Dict[int, Tuple[ast.AST, ...]] = {}
        self._info: Dict[int, _FuncInfo] = {}
        self._by_name: Dict[str, List[int]] = {}
        self._module_self: Set[int] = set()  # funcs whose first param may be one
        self._tainted_params: Dict[int, Set[str]] = {}
        self._moduleish: Dict[int, bool] = {}
        self._class_init: Dict[str, int] = {}   # class name -> its __init__ id
        flat: List[Tuple[Set[str], object]] = []
        param_pairs: List[Tuple[int, str, ast.AST]] = []
        calls: List[ast.Call] = []
        uses: List[Tuple[str, int]] = []    # (name, id) of every Load name/attr
        call_funcs: Set[int] = set()        # id of every Call.func node
        lambda_names: List[Tuple[str, int]] = []
        # Iterative: a generated file can hold an expression thousands of
        # nodes deep (a long string concatenation), past the recursion limit.
        stack: List[Tuple[ast.AST, Tuple[ast.AST, ...], Any]] = [
            (tree, (), None)]
        while stack:
            node, funcs, cls = stack.pop()
            inner = funcs
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
                inner = funcs + (node,)
                self._function(node, cls, flat, param_pairs)
                self._push_function(node, funcs, inner, stack)
                continue
            if isinstance(node, ast.ClassDef):
                self.bound.add(node.name)
                self._classes.add(node.name)
                for child in ast.iter_child_nodes(node):
                    stack.append((child, funcs, node))
                continue
            if isinstance(node, ast.Name):
                self._scopes[id(node)] = funcs
                if isinstance(node.ctx, ast.Load):
                    uses.append((node.id, id(node)))
                else:
                    self.bound.add(node.id)
            elif isinstance(node, ast.Attribute):
                if isinstance(node.ctx, ast.Load):
                    uses.append((node.attr, id(node)))
                self._binding(node, flat, lambda_names)
            else:
                self._binding(node, flat, lambda_names)
            if isinstance(node, ast.Call):
                call_funcs.add(id(node.func))
                calls.append(node)
            for child in ast.iter_child_nodes(node):
                stack.append((child, inner, None))

        # A lambda bound to a name is called by that name: g = lambda m: ...
        for name, lid in lambda_names:
            self._by_name.setdefault(name, []).append(lid)
            if self._info[lid].name is None:
                self._info[lid].name = name
        # A function whose name is used anywhere but as what a call calls is
        # out of the gate's sight (stored, aliased, decorated, passed on):
        # every non-first parameter of it may be a module. (Attribute uses
        # share this name space, erring only towards "may be".)
        leaked = {name for name, nid in uses if nid not in call_funcs}
        for fid, info in self._info.items():
            if info.name in leaked:
                for p in info.params:
                    if p != info.first:
                        self._tainted_params[fid].add(p)
        # A call in plain sight hands its arguments to a function's
        # parameters: map them, so f(codecs) taints f's m.
        for call in calls:
            for fid, pname, value in self._map_call(call, call_funcs):
                param_pairs.append((fid, pname, value))

        # To a fixpoint, by a WORKLIST rather than re-walking every pair each
        # pass: a name assigned from a tainted one is tainted too. Each pair
        # is indexed by the names its value reads; tainting a name revisits
        # only the pairs that read it, so a long `x0 = x1 ... xn = codecs`
        # chain resolves in one sweep down it, not one pass per link.
        entries: List[Tuple[str, object, object]] = (
            [("flat", frozenset(ns), v) for ns, v in flat]
            + [("param", (fid, pn), v) for fid, pn, v in param_pairs])
        index: Dict[str, List[int]] = {}
        for i, (_kind, _sink, value) in enumerate(entries):
            if value is None:
                continue
            for node in ast.walk(value):
                if isinstance(node, ast.Name):
                    index.setdefault(node.id, []).append(i)
                elif isinstance(node, ast.Attribute):
                    index.setdefault(node.attr, []).append(i)
        done = [False] * len(entries)
        queue: List[str] = [p for tp in self._tainted_params.values()
                            for p in tp]   # leaked parameters, already set

        def fire(i: int) -> None:
            kind, sink, value = entries[i]
            if done[i] or (value is not None and not self._carries(value)):
                return
            done[i] = True
            if kind == "flat":
                for name in sink:
                    if name not in self.tainted:
                        self.tainted.add(name)
                        queue.append(name)
            else:
                fid, pname = sink
                tp = self._tainted_params[fid]
                if pname not in tp:
                    tp.add(pname)
                    queue.append(pname)

        for i in range(len(entries)):       # the pairs that fire on an
            fire(i)                          # import or a value in plain sight
        while queue:                         # then down the chains they start
            for i in index.get(queue.pop(), ()):
                if not done[i]:
                    fire(i)

    # -- reading the file ---------------------------------------------------
    def _push_function(self, node, funcs, inner, stack) -> None:
        """A function's decorators, defaults and annotations are read in the
        ENCLOSING scope (``funcs``); only its body is its own (``inner``). A
        default `e=self.builtins.eval` on an inner method reads the OUTER
        self, so it must not be given the inner method's scope — where self
        is that method's first parameter and so looks ordinary."""
        args = node.args
        for value in list(args.defaults) + list(args.kw_defaults):
            if value is not None:
                stack.append((value, funcs, None))
        for a in (args.posonlyargs + args.args + args.kwonlyargs
                  + [x for x in (args.vararg, args.kwarg) if x is not None]):
            if a.annotation is not None:
                stack.append((a.annotation, funcs, None))
        if isinstance(node, ast.Lambda):
            stack.append((node.body, inner, None))
            return
        for deco in node.decorator_list:
            stack.append((deco, funcs, None))
        if node.returns is not None:
            stack.append((node.returns, funcs, None))
        for stmt in node.body:
            stack.append((stmt, inner, None))

    def _function(self, node: ast.AST, cls, flat, param_pairs) -> None:
        args = node.args
        positional = [a.arg for a in args.posonlyargs + args.args]
        names = set(positional) | {a.arg for a in args.kwonlyargs}
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                names.add(extra.arg)
        lam = isinstance(node, ast.Lambda)
        static = (not lam and any(_decorator_name(d) == "staticmethod"
                                  for d in node.decorator_list))
        is_method = cls is not None and not lam and not static
        first = positional[0] if (is_method and positional) else ""
        info = _FuncInfo(None if lam else node.name, positional,
                         frozenset(names), first, static, is_method)
        self._info[id(node)] = info
        self._tainted_params[id(node)] = set()
        self.bound |= names
        if not lam:
            self.bound.add(node.name)
            self._by_name.setdefault(node.name, []).append(id(node))
        # self is a module in a class whose instances answer any attribute
        # with a value the gate cannot pin to a name.
        if is_method and positional and self._is_moduleish(cls):
            self._module_self.add(id(node))
        # Building the instance runs __init__: A(codecs) hands codecs to it.
        if is_method and node.name == "__init__":
            self._class_init[cls.name] = id(node)
        # A property/cached_property names an attribute: taint that name
        # from what it returns (self.mod.builtins, with mod a @property that
        # returns codecs).
        if (cls is not None and not lam
                and any(_decorator_name(d) in ("property", "cached_property")
                        for d in node.decorator_list)):
            for ret in _returns_in(node):
                if ret.value is not None:
                    flat.append(({node.name}, ret.value))
        # A default is assigned to its parameter, in THIS function's scope.
        with_defaults = positional[len(positional) - len(args.defaults):] \
            if args.defaults else []
        for name, value in zip(with_defaults, args.defaults):
            param_pairs.append((id(node), name, value))
        for a, value in zip(args.kwonlyargs, args.kw_defaults):
            if value is not None:
                param_pairs.append((id(node), a.arg, value))

    def _is_moduleish(self, cls: ast.ClassDef) -> bool:
        cached = self._moduleish.get(id(cls))
        if cached is not None:
            return cached
        result = False
        for deco in cls.decorator_list:
            if _decorator_name(deco) == "dataclass":
                result = True
        for base in cls.bases:
            name = (base.attr if isinstance(base, ast.Attribute)
                    else base.id if isinstance(base, ast.Name) else "")
            if name in ("NamedTuple", "SimpleNamespace"):
                result = True
        for stmt in cls.body:
            if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and stmt.name in ("__getattr__", "__getattribute__")):
                result = True
        self._moduleish[id(cls)] = result
        return result

    def _map_call(self, call: ast.Call, call_funcs: Set[int]):
        """Which parameters of which functions this call hands an argument:
        f(x) to f's parameters, obj.meth(x) to meth's after self, A.f(x) to
        a staticmethod's from the first. Only calls whose target the file
        names in plain sight — the laundering routes it cannot see taint
        every non-first parameter already (``leaked``)."""
        fn = call.func
        targets: List[Tuple[_FuncInfo, int, int]] = []   # info, id, skip
        if isinstance(fn, ast.Name):
            for fid in self._by_name.get(fn.id, ()):
                info = self._info[fid]
                if not info.is_method:          # a plain function or lambda
                    targets.append((info, fid, 0))
            init = self._class_init.get(fn.id)  # A(codecs) -> A.__init__
            if init is not None:
                targets.append((self._info[init], init, 1))
        elif isinstance(fn, ast.Attribute):
            on_class = (isinstance(fn.value, ast.Name)
                        and fn.value.id in self._classes)
            for fid in self._by_name.get(fn.attr, ()):
                info = self._info[fid]
                if not info.is_method and not info.static:
                    continue
                if info.static:
                    targets.append((info, fid, 0))   # A.f(x) -> f's own
                elif not on_class:
                    targets.append((info, fid, 1))   # obj.meth(x) -> after self
                # A.meth(x) on a non-static method hands x AS self: a limit.
        out = []
        for info, fid, skip in targets:
            params = info.positional[skip:]
            for value, name in zip(call.args, params):
                if isinstance(value, ast.Starred):
                    break                    # *args: positions unknown
                out.append((fid, name, value))
            for kw in call.keywords:
                if kw.arg and kw.arg in info.params:
                    out.append((fid, kw.arg, kw.value))
        return out

    def _binding(self, node: ast.AST, pairs, lambda_names) -> None:
        if isinstance(node, ast.Import):
            for a in node.names:
                self._imported(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    self._imported(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                self._assigned(target, node.value, pairs)
                if (isinstance(node.value, ast.Lambda)
                        and isinstance(target, ast.Name)):
                    lambda_names.append((target.id, id(node.value)))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if node.value is not None:
                self._assigned(node.target, node.value, pairs)
                if (isinstance(node.value, ast.Lambda)
                        and isinstance(node.target, ast.Name)):
                    lambda_names.append((node.target.id, id(node.value)))
        elif isinstance(node, ast.NamedExpr):
            self._assigned(node.target, node.value, pairs)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            # Only an iterable that may carry a module taints what it yields:
            # `for m in (codecs,)` does, `for o in self.items` does not.
            pairs.append((_target_names(node.target), node.iter))
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                pairs.append((_target_names(node.optional_vars),
                              node.context_expr))
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                self.bound.add(node.name)
        elif isinstance(node, ast.Call):
            named = _setattr_literal(node)
            if named is not None:
                pairs.append(({named[0]}, named[1]))
        elif _MATCH_CAPTURES and isinstance(node, _MATCH_CAPTURES):
            name = getattr(node, "name", None) or getattr(node, "rest", None)
            if name:
                self.bound.add(name)
                pairs.append(({name}, None))

    def _imported(self, name: str) -> None:
        self.tainted.add(name)
        self.bound.add(name)

    @staticmethod
    def _assigned(target: ast.AST, value: ast.AST, pairs) -> None:
        names = _target_names(target)
        if not names:
            return
        if (isinstance(target, (ast.Tuple, ast.List, ast.Starred))
                and not isinstance(value, (ast.Tuple, ast.List, ast.Set))):
            pairs.append((names, None))      # elements the gate cannot see
        else:
            pairs.append((names, value))

    def _carries(self, value: ast.AST) -> bool:
        """Whether ``value`` may be, or hold, a module."""
        return any(self.may_be_module(n) for n in ast.walk(value)
                   if isinstance(n, ast.expr))

    # -- the question -------------------------------------------------------
    def may_be_module(self, node: ast.AST) -> bool:
        """Whether the expression ``node`` is, or may be, a module."""
        while isinstance(node, ast.Attribute):
            if node.attr in self.tainted:
                return True
            node = node.value
        if isinstance(node, ast.Name):
            return self._name(node)
        if isinstance(node, ast.IfExp):
            return (self.may_be_module(node.body)
                    or self.may_be_module(node.orelse))
        if isinstance(node, ast.BoolOp):
            return any(self.may_be_module(v) for v in node.values)
        if isinstance(node, ast.NamedExpr):
            return self.may_be_module(node.value)
        if isinstance(node, ast.Call):
            # Building an instance of a class the file defines is an ordinary
            # object; any other call's result is a value the gate cannot see.
            fn = node.func
            if (isinstance(fn, ast.Name) and fn.id in self._classes
                    and fn.id not in self.tainted):
                return False
            return True
        return isinstance(node, (ast.Subscript, ast.Await,
                                 ast.Yield, ast.YieldFrom, ast.Starred))

    def _name(self, node: ast.Name) -> bool:
        name = node.id
        if name in self.tainted:
            return True
        for func in reversed(self._scopes.get(id(node), ())):
            info = self._info[id(func)]
            if name in info.params:
                if name == info.first:
                    return id(func) in self._module_self
                return name in self._tainted_params[id(func)]
        return name not in self.bound and name not in _ALWAYS_THERE


def _target_names(target: ast.AST) -> Set[str]:
    """The names an assignment target binds: a name, an attribute's own name
    (self.mod -> mod), and each one an unpacking binds. A subscript binds
    none — what it stores is read back through a subscript, already a
    "may be"."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        out: Set[str] = set()
        for elt in target.elts:
            out |= _target_names(elt)
        return out
    return set()


def _setattr_literal(call: ast.Call):
    """(name, value) for setattr(obj, 'name', value) and the __setattr__
    spellings of it, with the name a literal; else None."""
    fn, args = call.func, call.args
    if isinstance(fn, ast.Name) and fn.id == "setattr" and len(args) >= 3:
        name, value = args[1], args[2]
    elif isinstance(fn, ast.Attribute) and fn.attr == "__setattr__":
        if len(args) >= 3:                 # object.__setattr__(obj, 'n', v)
            name, value = args[1], args[2]
        elif len(args) == 2:               # obj.__setattr__('n', v)
            name, value = args[0], args[1]
        else:
            return None
    else:
        return None
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return name.value, value
    return None


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _returns_in(node: ast.AST) -> List[ast.Return]:
    """Every ``return`` in a function's own body — not a nested function's or
    class's. A property's returns say what reading its attribute gives."""
    out: List[ast.Return] = []
    stack = list(getattr(node, "body", []))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(n, ast.Return):
            out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _check_lookup(node: ast.Call, aliases: dict, errs: List[str],
                  receivers: "_Receivers") -> None:
    """getattr(os, 'system'), attrgetter('path.system'), methodcaller(
    'system', cmd), inspect.getattr_static(os, 'system'): a denied name by a
    string literal is the denied name. A module route by getattr is judged
    on its receiver, as the attribute is: getattr(codecs, 'builtins') is
    refused, getattr(self, 'socket', None) is not; attrgetter and
    methodcaller name no receiver, so theirs are refused."""
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else (
        fn.attr if isinstance(fn, ast.Attribute) else "")
    if name not in LOOKUP_CALLS:
        return
    recv = None
    if name in ("getattr", "getattr_static"):
        if len(node.args) < 2:
            return
        literals, recv = node.args[1:2], node.args[0]
    elif name == "methodcaller":
        literals = node.args[:1]
    else:
        literals = node.args
    for arg in literals:
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        parts = arg.value.split(".")
        for part in parts:
            if (part in MODULE_ROUTES and recv is not None and len(parts) == 1
                    and not receivers.may_be_module(recv)):
                why = None
            elif part in (DENIED_ATTRS | DENIED_BUILTINS | DENIED_QT_NAMES):
                why = _reason(part) + (_route_note(part)
                                       if part in MODULE_ROUTES else "")
            elif recv is not None:
                why = _module_attr_rule(_dotted(recv, aliases)).get(part)
            else:
                why = None
            if why:
                spelled = ("getattr(..., " if name == "getattr"
                           else f"{name}(")
                errs.append(
                    f"line {getattr(node, 'lineno', 0)}: "
                    f"{spelled}{arg.value!r}) reaches a denied attribute by "
                    f"name — {why}")
                break


def _check_import(root: str, node: ast.AST, mode: str, allowed: Set[str],
                  errs: List[str]) -> None:
    line = getattr(node, "lineno", 0)
    if root in DENIED_MODULES:
        errs.append(f"line {line}: importing {root!r} is not permitted "
                    f"in a generated app — {DENIED_MODULE_REASONS[root]}")
        return
    if root == "council_engine":
        errs.append(
            f"line {line}: council_engine must not be imported by a generated "
            f"app — it would build a second GGUF singleton in this process. "
            f"Route model access through the designer instead.")
        return
    if root in allowed:
        return
    if mode == "standalone" and root in LINKED_MODULES:
        errs.append(
            f"line {line}: {root!r} is an app module, so this project is not "
            f"standalone. Switch the project to linked mode, or remove it.")
        return
    if root not in PROJECT_MODULES and is_council_module(root):
        errs.append(f"line {line}: {root!r} is part of the Council — an app "
                    f"may use only the linked modules")
        return
    errs.append(f"line {line}: {root!r} is not on the {mode} allowlist — if "
                f"the app needs it, add it to the project's requires")


def _check_qt(full_name: str, node: ast.AST, errs: List[str]) -> None:
    """Judge a Qt import by its FULL dotted name, not its root.

    This is the one rule in this module that cannot be expressed as a root-name
    allowlist, and it exists because the root-name allowlist is exactly what
    made `requires: PySide6` open QtNetwork, QProcess and QtQml at once.

    A foreign binding (PyQt5/6, PySide2, qtpy) is refused outright: two
    bindings in one process each load their own Qt libraries and the app dies
    on import, so there is no version of this that works.
    """
    line = getattr(node, "lineno", 0)
    parts = [p for p in str(full_name or "").split(".") if p]
    if not parts:
        return
    root = parts[0]
    if root in FOREIGN_QT:
        errs.append(
            f"line {line}: {root!r} is not permitted in a generated app — the "
            f"Qt target is PySide6, and two Qt bindings cannot share one "
            f"process")
        return
    if root not in TOOLKIT_ROOTS or len(parts) < 2:
        return
    sub = parts[1]
    if sub not in QT_SUBMODULES_OK:
        errs.append(
            f"line {line}: {root}.{sub} is not permitted in a generated app — "
            f"only {', '.join(sorted(QT_SUBMODULES_OK))} are. "
            f"({sub} ships inside PySide6-Essentials, so it is importable on "
            f"any machine that can run this app; that is why it is denied by "
            f"name rather than assumed absent.)")


def _receiver_name(node: ast.AST) -> str:
    """A readable name for whatever an attribute was reached through."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver_name(node.value)}.{node.attr}".lstrip(".")
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


# Folders inside a project that hold no code the app runs.
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "env"})


def project_sources(pdir) -> list:
    """Every .py in a generated project, subfolders included.

    Not a fixed list of the files generation writes: the app can import any
    module it can reach, and a helper module the gate never read was a way
    past it — measured, a widgets.py beside main.py ran subprocess with the
    gate saying OK. Built from what EXISTS, so the launch.py shim left in
    older projects is gated too."""
    pdir = Path(pdir)
    out = []
    for p in sorted(pdir.rglob("*.py")):
        rel = p.relative_to(pdir).parts[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel):
            continue
        out.append(p)
    return out


def project_modules(pdir) -> List[str]:
    """The top-level module names a project provides itself — its own .py
    files and packages — which its code may import.

    Safe to admit because project_sources gates every one of them. A local
    file cannot re-admit a denied module or council_engine: allowed_modules
    and _check_import still subtract and refuse those by name."""
    pdir = Path(pdir)
    names = [p.stem for p in pdir.glob("*.py")]
    names += [d.name for d in pdir.iterdir()
              if d.is_dir() and (d / "__init__.py").is_file()]
    return sorted({n for n in names if n.isidentifier()})


def validate_dir(pdir, mode: str = "linked",
                 extra_modules: Sequence[str] = (),
                 toolkit: str = "tk") -> Tuple[bool, List[str]]:
    """The gate over a whole project directory. The one call Generate, Run
    and run_example_gui all make, so they cannot disagree about a project."""
    return validate_project(project_sources(pdir), mode, extra_modules,
                            local_modules=project_modules(pdir), root=pdir,
                            toolkit=toolkit)


def validate_project(paths: Sequence, mode: str = "linked",
                     extra_modules: Sequence[str] = (),
                     local_modules: Sequence[str] = (), root=None,
                     toolkit: str = "tk") -> Tuple[bool, List[str]]:
    """Validate several files, prefixing each fault with its filename (its
    path under ``root`` when given, so ui/app.py and app.py stay distinct).

    ``extra_modules`` is the project's declared `requires`; a bad declaration
    is itself a fault. ``local_modules`` are the project's own modules."""
    all_errs: List[str] = list(check_requires(extra_modules, mode))
    importable = as_requires(extra_modules) + list(local_modules)
    for p in paths:
        p = Path(p)
        try:
            name = p.relative_to(root).as_posix() if root else p.name
        except ValueError:
            name = p.name
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            all_errs.append(f"{name}: cannot read ({exc})")
            continue
        ok, errs = validate(src, mode, importable, toolkit)
        all_errs.extend(f"{name}: {e}" for e in errs)
    return (not all_errs), all_errs
