"""
nx_guard.py — runs a pipeline script inside the containment the app promises.

    <python> nx_guard.py <policy.json> <script.py>

workflow_runner launches every simplnx script, and every model-written one,
through this instead of `python script.py`. It runs IN the script's own
interpreter (the DREAM3D-NX env for a simplnx script), so nothing here may
import an app module beyond the pure-stdlib nx_introspect, nx_policy and
path_contain beside it.

Why a check inside the run
--------------------------
nx_policy.validate_script reads the TEXT of a model-written script. A text
check can be talked past — a name assembled at run time, a path computed from
a string, a module reached as an attribute of an allowed one — and a writer
filter's output path is a value, not a name: WriteDREAM3DFilter pointed at the
vault root, the Desktop, a UNC share, a path with .., or a junction inside the
output area that leads out of it reads exactly like a legitimate export. Only
the run itself sees the value the filter is really handed. So for a MODEL
script (nx_policy "Two kinds of script"):

  * every filter's execute (and IFilter.execute2, and Pipeline.execute) is
    wrapped. Before the real call, each OUTPUT path parameter (path_role:
    found from the installed filter's own signature, not a hand list) is read
    ONCE into a plain string — a PathLike that answers differently the second
    time gets no second time — resolved (path_contain.canonical: .., 8.3
    names, junctions, symlinks, \\\\?\\ and UNC spellings) and must:
      - lie under a write root (the vault's data_out, the run's staging and
        working folders),
      - not be an input (read-only roots),
      - not name an NTFS stream,
      - not replace a file or fill a folder this run did not make (the run's
        own folders, the files it was told it hands on, and what it created
        itself are its own);
    and the file-name pieces a directory writer builds names from (prefixes,
    extensions, array names) may not hold a separator, a colon or "..".
  * a Python audit hook (PEP 578 — it cannot be removed once added) holds
    every file change the script itself makes — open for writing, remove,
    rename, rmtree, mkdir, chmod, links — to the same rule, refuses deleting
    anything the run did not make, and refuses processes, native code
    (ctypes), sockets and the registry.

The binding's own execute functions are held only inside the wrappers. A
script that rebinds a class attribute (nx.WriteDREAM3DFilter.execute = ...)
replaces the wrapper with something that cannot reach the binding either —
it can at most call the wrapper again. (Measured: the audit hook does NOT
see such a rebinding — CPython raises object.__setattr__ only for a type's
__module__/__doc__/__name__ — so this rests on the wrapper, not the hook.)

A refusal raises PermissionError at the call that tried it, is printed to
stderr, and is written to the report file; the run exits non-zero even if
the script caught the exception, and workflow_runner fails the step with the
refusal as its error.

For a USER script only the capability rule is enforced here: a denied filter
(Execute Process, Python codegen) refuses to execute however it was reached —
by name, through get_filters(), or as a step of a saved pipeline. The user's
own file operations are theirs.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import nx_introspect   # noqa: E402  (same directory; pure stdlib)
import nx_policy       # noqa: E402  (same directory; pure stdlib)
import path_contain    # noqa: E402  (same directory; pure stdlib)

NX_MODULES = ("simplnx", "orientationanalysis", "itkimageprocessing")
EXIT_REFUSED = 3


# ============================================================
# Which path parameters a filter WRITES
# ============================================================

# Compound parameter values that carry a path: every one is a reader's.
_INPUT_COMPOUNDS = ("ImportData", "GeneratedFileListParameter",
                    "ReadCSVDataParameter", "ReadHDF5DatasetParameter",
                    "ReadH5EbsdFileParameter", "OEMEbsdScanSelectionParameter")
# os.PathLike parameters that are READ although neither their name nor their
# filter says so (checked against the installed catalog, 289 filters).
_KNOWN_INPUTS = frozenset({("CombineStlFilesFilter", "stl_files_path")})


def path_role(filter_name: str, param: str, type_str: Optional[str]
              ) -> Optional[str]:
    """'output', 'input', or None when the parameter is not a file path.

    Fail-safe: an os.PathLike parameter is an OUTPUT unless it is plainly an
    input — its name says input/import, or it belongs to a reader (Read*,
    ITKImport*, *ReaderFilter) and does not say output/export. On the
    installed build that makes these outputs: every Write* filter's path,
    ITKImageWriterFilter.file_name (a writer's, though the same name is
    ITKImageReaderFilter's input), ExtractPipelineToFile.output_file_path,
    ConvertHexGridToSquareGrid.output_path, EbsdToH5Ebsd.output_file_path and
    the four *_output_file of ComputeGBCD/GBPDMetricBased — filters whose
    names do not say they write (tests/test_nx_containment.py pins the list).
    """
    t = type_str or ""
    if "os.PathLike" not in t:
        return "input" if any(c in t for c in _INPUT_COMPOUNDS) else None
    if (filter_name, param) in _KNOWN_INPUTS:
        return "input"
    p = param.lower()
    if "output" in p or "export" in p:
        return "output"
    if "input" in p or "import" in p:
        return "input"
    reader = (filter_name.startswith(("Read", "ITKImport", "Import"))
              or filter_name.endswith("ReaderFilter"))
    if reader and "Write" not in filter_name:
        return "input"
    return "output"


def _default_path(default: Optional[str]) -> Optional[str]:
    """"WindowsPath('Untitled.dream3d')" -> 'Untitled.dream3d'."""
    m = re.search(r"Path\((['\"])(.*)\1\)", default or "")
    return m.group(2) if m else None


# Names a directory writer turns into FILE names inside its folder.
_NAME_PIECE = re.compile(r"(prefix|suffix|extension)$")


# ============================================================
# The guard
# ============================================================

class Refused(PermissionError):
    """The containment refused an operation."""


def _plain(value: Any) -> str:
    """``value`` as an exact str, read once. A str subclass, bytes or a
    PathLike becomes a plain str here, and the plain str is what the filter
    is handed — so the value checked is the value used."""
    if isinstance(value, str):
        return "".join((value,))        # an exact str, whatever the subclass
    if isinstance(value, (bytes, bytearray)):
        return os.fsdecode(bytes(value))
    v = os.fspath(value)                # TypeError for a non-path
    return _plain(v)


class Guard:
    def __init__(self, policy: Dict[str, Any]):
        self.trust = policy.get("trust") or nx_policy.MODEL
        self.model = self.trust == nx_policy.MODEL

        def canon(paths) -> List[str]:
            out = []
            for p in paths or []:
                try:
                    out.append(path_contain.canonical(p))
                except (OSError, ValueError, TypeError):
                    pass
            return out
        self.write_roots = canon(policy.get("write_roots"))
        self.own_roots = canon(policy.get("own_roots"))
        self.read_only = canon(policy.get("read_only"))
        self.own_files = set(canon(policy.get("own_files")))
        self.shown_roots = [str(p) for p in policy.get("write_roots") or []]
        # A vault on a network share makes its output area one too.
        self._network_roots = any(path_contain.network_or_device(p)
                                  for p in self.shown_roots)
        self.created: set = set()
        self.refusals: List[str] = []
        self.report = None
        self.patched: set = set()
        self._params: Dict[type, list] = {}
        self._uuid: Dict[type, Optional[str]] = {}
        self._depth = 0

    # ---- refusing ---------------------------------------------------------
    def refuse(self, what: str, why: str):
        msg = f"{what}: {why}"
        self.refusals.append(msg)
        try:
            sys.stderr.write(f"[nx_guard] refused - {msg}\n")
            sys.stderr.flush()
        except Exception:                                 # noqa: BLE001
            pass
        if self.report is not None:
            try:
                self.report.write(json.dumps({"refused": msg}) + "\n")
                self.report.flush()
            except Exception:                             # noqa: BLE001
                pass
        raise Refused(f"refused by the Council's containment - {msg}")

    # ---- the path rule ----------------------------------------------------
    @staticmethod
    def _inside(c: str, roots: Sequence[str]) -> bool:
        for r in roots:
            if c == r or c.startswith(r if r.endswith(os.sep) else r + os.sep):
                return True
        return False

    def _own(self, c: str) -> bool:
        return (c in self.own_files or c in self.created
                or self._inside(c, self.own_roots)
                or any(c.startswith(d + os.sep) for d in self.created))

    def check_write(self, value: Any, what: str, *,
                    mkdir: bool = False) -> str:
        """The plain path ``value`` names, if this run may write there;
        refuses otherwise. See the module docstring for the rule. With
        ``mkdir``, an existing folder is fine: making it again changes
        nothing (Path.mkdir(exist_ok=True) asks the OS first)."""
        try:
            s = _plain(value)
        except TypeError:
            self.refuse(what, f"a {type(value).__name__} is not a path this "
                              f"check can read")
        if not s.strip():
            self.refuse(what, "an empty path")
        if path_contain.has_stream_name(s):
            self.refuse(what, f"{s} names a stream inside a file")
        if path_contain.network_or_device(s) and not self._network_roots:
            # Refused from the spelling: resolving it would ask the network
            # for the host first.
            self.refuse(what, f"{s} is a network share or a device, not a "
                              f"folder in the output area")
        try:
            c = path_contain.canonical(s)
        except (OSError, ValueError) as exc:
            self.refuse(what, f"{s} cannot be resolved ({exc})")
        if not self._inside(c, self.write_roots):
            self.refuse(what, f"{s} is outside the output area. A pipeline "
                              f"the Council runs writes only under: "
                              f"{'; '.join(self.shown_roots) or '(none)'}")
        if self._inside(c, self.read_only):
            self.refuse(what, f"{s} is an input, and inputs are read-only")
        if os.path.isdir(c):
            if not mkdir and not self._own(c) and _has_entries(c):
                self.refuse(what, f"{s} is a folder that already holds files "
                                  f"this run did not make; a writer could "
                                  f"replace them. Write into a new folder.")
        elif os.path.lexists(c):
            if not self._own(c):
                self.refuse(what, f"{s} already exists and this run did not "
                                  f"make it; it is never overwritten")
        else:
            self.created.add(c)
        return s

    def check_delete(self, value: Any, what: str) -> None:
        """Deleting (or changing in place) is allowed only for what this run
        made itself."""
        if isinstance(value, int):
            return                      # an fd: opened, so already checked
        try:
            s = _plain(value)
        except TypeError:
            self.refuse(what, f"{value!r} is not a path this check can read")
        if path_contain.network_or_device(s) and not self._network_roots:
            self.refuse(what, f"{s} is a network share or a device")
        try:
            c = path_contain.canonical(s)
        except (OSError, ValueError):
            self.refuse(what, f"{s} cannot be resolved")
        if not self._own(c):
            self.refuse(what, f"{value} was not made by this run, and a "
                              f"pipeline run never deletes or changes what "
                              f"it did not make")

    # ---- filters ----------------------------------------------------------
    def params(self, cls: type) -> list:
        if cls not in self._params:
            doc = None
            try:
                doc = cls.execute.__doc__   # the wrapper keeps the binding's
            except Exception:                             # noqa: BLE001
                pass
            self._params[cls] = nx_introspect.parse_execute_signature(
                doc).get("params", [])
        return self._params[cls]

    def uuid(self, cls: type) -> Optional[str]:
        if cls not in self._uuid:
            try:
                self._uuid[cls] = str(cls().uuid()).lower()
            except Exception:                             # noqa: BLE001
                self._uuid[cls] = None
        return self._uuid[cls]

    def check_capability(self, cls: type, label: str) -> None:
        name = getattr(cls, "__name__", "")
        u = self.uuid(cls)
        if name in nx_policy.DENIED_CLASS_NAMES or (u and nx_policy.is_denied(u)):
            why = (nx_policy.reason(u) if u and nx_policy.is_denied(u)
                   else nx_policy.reason_for_class(name))
            self.refuse(f"running {label}", why)

    def check_call(self, cls: type, args: tuple, kwargs: dict,
                   positional: bool) -> Tuple[tuple, dict, list]:
        """Capability, then (MODEL) every output path. Returns the call's
        arguments with each output path replaced by the plain string that
        was checked, and [(canonical path, existed before)] for after()."""
        name = getattr(cls, "__name__", "?")
        self.check_capability(cls, name)
        if not self.model:
            return args, kwargs, []
        params = self.params(cls)
        args = list(args)
        pos = {p["name"]: i for i, p in enumerate(params)} if positional \
            else {}
        bound: Dict[str, Any] = {}
        for pname, i in pos.items():
            if i < len(args):
                bound[pname] = args[i]
        bound.update(kwargs)
        has_output = any(path_role(name, p["name"], p.get("type")) == "output"
                         for p in params)
        touched = []
        for p in params:
            pname, ptype = p["name"], p.get("type")
            role = path_role(name, pname, ptype)
            if role == "output":
                if pname in bound:
                    value = bound[pname]
                else:
                    value = _default_path(p.get("default"))
                    if value is None:
                        continue
                what = f"{name}.{pname}"
                s = self.check_write(value, what)
                touched.append(path_contain.canonical(s))
                if pname in bound:
                    if pname in pos and pos[pname] < len(args) \
                            and pname not in kwargs:
                        args[pos[pname]] = s
                    else:
                        kwargs[pname] = s
            elif has_output and pname in bound:
                self._check_name_pieces(name, pname, ptype, bound[pname])
        if name == "WriteDREAM3DFilter" and touched:
            xdmf = bound.get("write_xdmf_file")
            if xdmf is None:
                xdmf = next((p.get("default") == "True" for p in params
                             if p["name"] == "write_xdmf_file"), False)
            if xdmf:
                companion = os.path.splitext(touched[0])[0] + ".xdmf"
                self.check_write(companion, f"{name} (its .xdmf)")
        return tuple(args), kwargs, touched

    def _check_name_pieces(self, name: str, pname: str, ptype: Optional[str],
                           value: Any) -> None:
        """A directory writer builds file names from these: a prefix of
        "..\\..\\x" would put its files outside the folder that was checked."""
        t = ptype or ""
        pieces: List[str] = []
        if t == "str" and _NAME_PIECE.search(pname):
            pieces = [value] if isinstance(value, str) else []
        elif t.startswith("list[") and "DataPath" in t:
            # The arrays WriteASCIIData / WriteBinaryData write one file per,
            # named after the array.
            for v in (value if isinstance(value, (list, tuple)) else []):
                try:
                    pieces.extend(str(x) for x in v.parts())
                except Exception:                         # noqa: BLE001
                    continue
        for piece in pieces:
            if any(ch in piece for ch in "/\\:") or ".." in piece:
                self.refuse(f"{name}.{pname}",
                            f"{piece!r} would become part of a file name, "
                            f"and it holds a separator, a colon or '..'")

    def after(self, touched: list) -> None:
        for c in touched:
            if os.path.lexists(c):
                self.created.add(c)

    def check_pipeline(self, pipeline: Any) -> None:
        """Every step of a saved pipeline, before Pipeline.execute runs it."""
        for i in range(pipeline.size()):
            pf = pipeline[i]
            try:
                filt = pf.get_filter()
            except Exception as exc:                      # noqa: BLE001
                self.refuse(f"pipeline step {i}", f"its filter cannot be "
                                                 f"checked ({exc})")
            cls = type(filt)
            label = f"pipeline step {i} ({getattr(cls, '__name__', '?')})"
            u = None
            try:
                u = str(filt.uuid()).lower()
            except Exception:                             # noqa: BLE001
                pass
            if u and nx_policy.is_denied(u):
                self.refuse(f"running {label}", nx_policy.reason(u))
            self.check_capability(cls, label)
            if not self.model:
                continue
            args = pf.get_args()
            for p in self.params(cls):
                if path_role(cls.__name__, p["name"], p.get("type")) \
                        == "output" and p["name"] in args:
                    self.check_write(args[p["name"]], f"{label}.{p['name']}")

    # ---- wrapping simplnx ---------------------------------------------------
    def patch(self, modules) -> None:
        nx = modules.get("simplnx")
        if nx is None:
            return
        base = vars(nx).get("IFilter")
        classes = []
        for mod in modules.values():
            for obj in list(vars(mod).values()):
                if isinstance(obj, type) and base is not None \
                        and issubclass(obj, base) and obj is not base \
                        and "execute" in vars(obj):
                    classes.append(obj)
        try:
            classes.extend(c for c in nx.get_filters() if c not in classes)
        except Exception:                                 # noqa: BLE001
            pass
        for cls in classes:
            self._wrap_execute(cls)
        if base is not None and "execute2" in vars(base):
            self._wrap_execute2(base)
        pipe = vars(nx).get("Pipeline")
        if pipe is not None and "execute" in vars(pipe):
            self._wrap_pipeline(pipe)

    def _wrap_execute(self, cls: type) -> None:
        if cls in self.patched:
            return                      # a class two modules both export
        real = cls.execute
        doc = getattr(real, "__doc__", None)
        guard = self

        def execute(*args, **kwargs):
            args, kwargs, touched = guard.check_call(cls, args, kwargs, True)
            result = real(*args, **kwargs)
            guard.after(touched)
            return result
        execute.__doc__ = doc
        execute.__name__ = "execute"
        setattr(cls, "execute", staticmethod(execute))
        self.patched.add(cls)

    def _wrap_execute2(self, base: type) -> None:
        real = base.execute2
        guard = self

        def execute2(self_, data_structure, **kwargs):
            _a, kwargs, touched = guard.check_call(type(self_), (), kwargs,
                                                   False)
            result = real(self_, data_structure, **kwargs)
            guard.after(touched)
            return result
        execute2.__doc__ = getattr(real, "__doc__", None)
        setattr(base, "execute2", execute2)
        self.patched.add(base)

    def _wrap_pipeline(self, pipe: type) -> None:
        real = pipe.execute
        guard = self

        def execute(self_, *args, **kwargs):
            guard.check_pipeline(self_)
            return real(self_, *args, **kwargs)
        execute.__doc__ = getattr(real, "__doc__", None)
        setattr(pipe, "execute", execute)
        self.patched.add(pipe)

    # ---- the audit hook (MODEL scripts) -------------------------------------
    _WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT
                    | os.O_TRUNC)
    _NEVER = frozenset({
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        "os.spawn", "os.startfile", "os.kill", "os.killpg", "os.fork",
        "os.forkpty", "pty.spawn", "_winapi.CreateProcess",
        "_winapi.CreateJunction", "_winapi.OpenProcess",
        "_winapi.TerminateProcess", "ctypes.dlopen", "ctypes.dlsym",
        "ctypes.call_function", "ctypes.cdata", "socket.connect",
        "socket.bind", "socket.sendto", "socket.sendmsg", "winreg.CreateKey",
        "winreg.DeleteKey", "winreg.DeleteValue", "winreg.SetValue",
        "winreg.SaveKey", "winreg.LoadKey", "winreg.ConnectRegistry",
        "shutil.make_archive", "shutil.unpack_archive", "os.link",
        "os.symlink",
    })

    def audit(self, event: str, args: tuple) -> None:
        if self._depth:
            return                      # the guard's own work
        self._depth += 1
        try:
            self._audit(event, args)
        finally:
            self._depth -= 1

    def _audit(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            if isinstance(mode, str):
                writing = any(ch in mode for ch in "wax+")
            else:
                writing = bool((flags or 0) & self._WRITE_FLAGS)
            if writing:
                self.check_write(path, f"opening {path} for writing")
        elif event in ("os.remove", "os.rmdir", "shutil.rmtree"):
            self.check_delete(args[0], f"deleting {args[0]}")
        elif event in ("os.rename", "shutil.move"):
            self.check_delete(args[0], f"moving {args[0]}")
            self.check_write(args[1], f"moving onto {args[1]}")
        elif event in ("shutil.copyfile", "shutil.copytree"):
            self.check_write(args[1], f"copying onto {args[1]}")
        elif event == "os.mkdir":
            self.check_write(args[0], f"making the folder {args[0]}",
                             mkdir=True)
        elif event in ("os.chmod", "os.chown", "os.utime", "os.truncate",
                       "os.chflags", "os.lchflags", "os.setxattr",
                       "os.removexattr", "shutil.copymode",
                       "shutil.copystat", "shutil.chown"):
            target = args[1] if event in ("shutil.copymode",
                                          "shutil.copystat") else args[0]
            self.check_delete(target, f"changing {target}")
        elif event == "_winapi.CreateFile":
            name, access = args[0], args[1] if len(args) > 1 else 0
            if access & 0x40000000:     # GENERIC_WRITE
                self.check_write(name, f"opening {name} for writing")
        elif event in self._NEVER:
            self.refuse(event, "a pipeline script the Council runs may not "
                               "start processes, load native code, open "
                               "sockets, touch the registry or make links")

    # ---- the report ----------------------------------------------------------
    def open_report(self, path: Optional[str]) -> None:
        if path:
            self.report = open(path, "w", encoding="utf-8")

    def close_report(self, exit_code: int) -> None:
        if self.report is None:
            return
        try:
            self.report.write(json.dumps({"done": True,
                                          "refusals": len(self.refusals),
                                          "exit": exit_code}) + "\n")
            self.report.close()
        except Exception:                                 # noqa: BLE001
            pass


def _has_entries(folder: str) -> bool:
    try:
        with os.scandir(folder) as it:
            return any(True for _ in it)
    except OSError:
        return True                     # cannot look: assume it holds files


def read_report(path) -> Tuple[List[str], bool]:
    """(refusals, finished) from a report file nx_guard wrote."""
    refusals, done = [], False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if "refused" in rec:
                    refusals.append(str(rec["refused"]))
                if rec.get("done"):
                    done = True
    except OSError:
        pass
    return refusals, done


# ============================================================
# Running the script
# ============================================================

def _import_modules(want_nx: bool, model: bool) -> dict:
    """simplnx and its plugins (to wrap them), and — for a model script —
    everything nx_policy lets it import, imported BEFORE the audit hook so
    library start-up (numpy's DLL directories, simplnx's preferences) is not
    mistaken for the script's doing."""
    import importlib
    loaded = {}
    names = list(NX_MODULES) if want_nx else []
    if model:
        names += sorted(nx_policy.ALLOWED_IMPORT_ROOTS - set(NX_MODULES))
    for name in names:
        try:
            loaded[name] = importlib.import_module(name)
        except Exception:                                 # noqa: BLE001
            continue
    return {k: v for k, v in loaded.items() if k in NX_MODULES}


def _script_traceback(exc: BaseException, script: str) -> None:
    """The traceback from the script's own first frame (not runpy's or this
    file's), as `python script.py` would print it."""
    import traceback
    tb = exc.__traceback__
    want = os.path.normcase(os.path.abspath(script))
    t = tb
    while t is not None and os.path.normcase(
            os.path.abspath(t.tb_frame.f_code.co_filename)) != want:
        t = t.tb_next
    traceback.print_exception(type(exc), exc, t or tb)


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: nx_guard.py <policy.json> <script.py>\n")
        return 2
    sys.dont_write_bytecode = True      # an import must not write a .pyc
    with open(argv[1], encoding="utf-8") as fh:
        policy = json.load(fh)
    script = os.path.abspath(argv[2])
    guard = Guard(policy)
    modules = _import_modules(bool(policy.get("nx")), guard.model)
    guard.patch(modules)
    guard.open_report(policy.get("report"))
    if guard.model:
        sys.addaudithook(guard.audit)
    # As `python script.py` would have it: its folder first on the path, its
    # name as argv[0], and nothing of this file's folder in between.
    if sys.path and os.path.normcase(os.path.abspath(sys.path[0])) == \
            os.path.normcase(_HERE):
        sys.path[0] = os.path.dirname(script)
    else:
        sys.path.insert(0, os.path.dirname(script))
    sys.argv = [script] + list(argv[3:])
    import runpy
    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
        else:
            sys.stderr.write(f"{exc.code}\n")
            code = 1
    except BaseException as exc:                          # noqa: BLE001
        _script_traceback(exc, script)
        code = 1
    if guard.refusals and code == 0:
        code = EXIT_REFUSED
    guard.close_report(code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
