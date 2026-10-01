"""
council_core.bench_ports — stand-in ports for running ONE model-written handler
against a hidden test, with no toolkit.

WHY A FAKE AND NOT THE GENERATED APP
A handler is a method of HandlerMixin that talks to the window only through
``self.ports.<name>`` and two helpers (``report_error``, ``clear_ports``).
Building a real Qt window to test one method costs a QApplication, a display
plugin and seconds per case, and then the test still has to poke widgets to
set an entry's text. So this module rebuilds the PORT SURFACE the generated
app exposes (gui_emit_qt.PORTS_RUNTIME), kind for kind:

    .get()   the typed value; entry "abc" with type float reads as None,
             exactly like _coerce — a handler that does float(x) on it fails
             here the way it fails in the app
    .set(v)  writes; a label stores str(v), a listbox/treeview a list
    .clear() .enable(b) .on_change(f)
    listbox .items(), treeview .rows(), button .on_fire(f) / .fire()
    log_pane / status_bar / image_canvas are write-only: .get() raises
    TypeError, as the real proxy ports do

Anything a real port does not have is NOT here either, so a handler calling
``self.ports.table.add_row`` fails the same way it would in the app.

The test side gets a little more: ``port.value`` is the raw widget state (what
a user typed), ``port.selected`` a listbox's selection, ``port.lines`` what a
log pane received, and ``app.errors`` every report_error call.

Stdlib only. This file is COPIED next to the handler under test and run as a
script in a fresh interpreter (see llm_bench.run_hidden_test), so it must not
import anything from the Council.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# What each port kind exposes to a handler. llm_bench's static port check
# reads this too, so the gate and the fake can never disagree.
COMMON = ("clear", "enable", "name", "type", "direction", "widget")
API: Dict[str, tuple] = {
    "entry": COMMON + ("get", "set", "on_change"),
    "label": COMMON + ("get", "set", "on_change"),
    "spinbox": COMMON + ("get", "set", "on_change"),
    "scale": COMMON + ("get", "set", "on_change"),
    "checkbutton": COMMON + ("get", "set", "on_change"),
    "radiobutton": COMMON + ("get", "set", "on_change"),
    "combobox": COMMON + ("get", "set", "on_change"),
    "progressbar": COMMON + ("get", "set", "on_change"),
    "file_picker": COMMON + ("get", "set", "on_change"),
    "scrubber": COMMON + ("get", "set", "on_change"),
    "text": COMMON + ("get", "set", "on_change"),
    "listbox": COMMON + ("get", "set", "on_change", "items"),
    "treeview": COMMON + ("get", "set", "on_change", "rows"),
    "log_pane": COMMON + ("set",),
    "status_bar": COMMON + ("set",),
    "image_canvas": COMMON + ("set",),
    "chart_panel": COMMON + ("set",),
    # The real _EventPort inherits _Port.clear(), a no-op.
    "button": ("enable", "clear", "on_fire", "fire", "name", "type",
               "direction", "widget"),
    "notebook": COMMON + ("get", "set", "on_change"),
}

#: Methods a handler may call on self besides its own and the ports.
UI_HELPERS = ("ports", "report_error", "clear_ports", "on_close")


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def coerce(v: Any, t: str) -> Any:
    """gui_emit_qt.PORTS_RUNTIME._coerce, verbatim in behaviour."""
    if t in ("int", "float"):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v) if t == "int" else float(v)
        s = "" if v is None else str(v).strip()
        try:
            return int(s) if t == "int" else float(s)
        except ValueError:
            pass
        try:
            f = float(s)
            return int(f) if t == "int" else f
        except (ValueError, OverflowError):
            return None
    if t == "bool":
        return bool(v)
    if t == "path":
        return "" if v is None else str(v)
    return v


class Port:
    """One port. ``kind`` picks the behaviour; the API table gates access."""

    def __init__(self, name: str, kind: str, type: str = "str",
                 value: Any = None, items: Optional[List[Any]] = None,
                 values: Optional[List[str]] = None, ui: Any = None,
                 handler: str = ""):
        self.name, self.kind, self.type = name, kind, type
        self.direction = {"label": "o", "log_pane": "o", "status_bar": "o",
                          "image_canvas": "o", "chart_panel": "o",
                          "progressbar": "o", "button": "e"}.get(kind, "io")
        self.widget = None
        self.enabled = True
        self.choices = list(values or [])     # a combobox's dropdown
        self.selected: List[Any] = []          # a listbox/treeview selection
        self.lines: List[str] = []             # what a log pane received
        self.shown: Any = None                 # what an image canvas shows
        self._subs: List[Callable] = []
        self._ui, self._handler = ui, handler
        if kind in ("listbox", "treeview"):
            self.value: Any = list(items or [])
        elif kind == "checkbutton":
            self.value = bool(value)
        elif kind in ("spinbox", "scale", "progressbar", "scrubber",
                      "notebook"):
            self.value = value if value is not None else 0
        else:
            self.value = "" if value is None else value

    # The API gate: a method the real port kind lacks raises AttributeError
    # here, the way it would on the generated Ports object.
    def __getattribute__(self, attr: str) -> Any:
        if not attr.startswith("_") and attr in _GATED:
            kind = object.__getattribute__(self, "kind")
            if attr not in API.get(kind, ()):
                raise AttributeError(
                    f"port {object.__getattribute__(self, 'name')!r} "
                    f"({kind}) has no .{attr}()")
        return object.__getattribute__(self, attr)

    # -- reads ---------------------------------------------------------
    def get(self) -> Any:
        if self.kind == "listbox":
            return [str(v) for v in self.selected]
        if self.kind == "treeview":
            return [tuple(_s(c) for c in r) for r in self.selected]
        if self.kind in ("label", "text", "combobox"):
            return _s(self.value)
        return coerce(self.value, self.type)

    def items(self) -> List[str]:
        return [_s(v) for v in self.value]

    def rows(self) -> List[tuple]:
        return [tuple(_s(c) for c in r) for r in self.value]

    # -- writes --------------------------------------------------------
    def set(self, value: Any) -> None:
        k = self.kind
        if k == "listbox":
            self.value = [_s(v) for v in (value or [])]
            self.selected = []
        elif k == "treeview":
            self.value = [[_s(c) for c in r] for r in (value or [])]
            self.selected = []
        elif k == "log_pane":
            self.lines.append(_s(value))
        elif k == "image_canvas":
            self.shown = value
        elif k == "checkbutton":
            self.value = bool(value)
        elif k == "spinbox":
            self.value = int(coerce(value, "int") or 0)
        elif k in ("scale", "progressbar", "scrubber"):
            self.value = int(coerce(value, "float") or 0)
        elif k == "combobox":
            text = _s(value)
            if text and text not in self.choices:
                self.choices.append(text)
            self.value = text
        else:                                   # entry, label, text, picker
            self.value = _s(value)
        self._fire()

    def clear(self) -> None:
        k = self.kind
        if k == "button":
            return
        if k in ("listbox", "treeview"):
            self.set([])
        elif k in ("checkbutton", "spinbox", "scale", "scrubber",
                   "radiobutton"):
            return                       # no honest empty state; kept
        elif k == "image_canvas":
            self.set(None)
        elif k == "log_pane":
            return                       # a log is never wiped
        elif k == "progressbar":
            self.set(0)
        else:
            self.set("")

    def enable(self, on: bool = True) -> None:
        self.enabled = bool(on)

    def on_change(self, fn: Callable) -> None:
        self._subs.append(fn)

    def _fire(self) -> None:
        for fn in list(self._subs):
            fn(self.get() if "get" in API.get(self.kind, ()) else None)

    # -- events --------------------------------------------------------
    def on_fire(self, fn: Callable) -> None:
        self._subs.append(fn)

    def fire(self) -> None:
        for fn in list(self._subs):
            fn()
        m = getattr(self._ui, self._handler, None)
        if callable(m):
            m()


_GATED = frozenset({"get", "set", "items", "rows", "on_change", "on_fire",
                    "fire", "clear", "enable"})


class Ports:
    """Attribute and item access, like the generated Ports."""

    def __init__(self, ports: Dict[str, Port]):
        object.__setattr__(self, "_ports", dict(ports))

    def __getattr__(self, name: str) -> Port:
        try:
            return self._ports[name]
        except KeyError:
            raise AttributeError(f"this app has no port {name!r}") from None

    def __getitem__(self, name: str) -> Port:
        return self._ports[name]

    def __contains__(self, name: str) -> bool:
        return name in self._ports

    def __iter__(self):
        return iter(self._ports.values())

    def read(self) -> Dict[str, Any]:
        return {n: p.get() for n, p in self._ports.items()
                if "get" in API.get(p.kind, ()) and p.kind != "button"}


class FakeUi:
    """What MainUi gives a handler: ports, report_error, clear_ports."""

    def __init__(self, spec: List[Dict[str, Any]]):
        ports = {}
        for row in spec:
            name = row["name"]
            handler = row.get("handler") or (f"on_{name}"
                                              if row["kind"] == "button"
                                              else "")
            ports[name] = Port(name, row["kind"], row.get("type", "str"),
                               value=row.get("value"), items=row.get("items"),
                               values=row.get("values"), ui=self,
                               handler=handler)
        self.ports = Ports(ports)
        self.errors: List[tuple] = []

    def report_error(self, what: Any, exc: Any) -> None:
        self.errors.append((str(what), str(exc)))

    def clear_ports(self, *names: str) -> None:
        for name in names:
            try:
                self.ports[name].clear()
            except Exception:
                pass

    def on_close(self) -> None:
        pass


class FakeClock:
    """time.monotonic / time.time / time.perf_counter, under test control."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def install(self) -> None:
        time.monotonic = self            # type: ignore[assignment]
        time.time = self                 # type: ignore[assignment]
        time.perf_counter = self         # type: ignore[assignment]


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def num(text: Any) -> Optional[float]:
    """The first number in a label's text, or None. "Sum: 6.50" -> 6.5."""
    m = _NUM.search(_s(text).replace(",", ""))
    return float(m.group(0)) if m else None


def norm(text: Any) -> str:
    return " ".join(_s(text).split()).lower()


# ============================================================
# The subprocess entry point
# ============================================================

def run_case(case_path: str) -> int:
    """Import handlers.py from the CWD, build the app, run the hidden test.

    Prints exactly one verdict line: BENCH_OK, or BENCH_FAIL <stage>: <why>.
    """
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    here = os.getcwd()
    sys.path.insert(0, here)
    for extra in case.get("sys_path") or []:
        sys.path.insert(1, extra)
    clock = FakeClock(1000.0)
    if case.get("fake_clock"):
        clock.install()
    tmp = Path(here) / "scratch"
    tmp.mkdir(exist_ok=True)
    try:
        import handlers                                   # noqa: F401
        mixin = handlers.HandlerMixin
    except Exception as exc:
        print(f"BENCH_FAIL import: {exc!r}", flush=True)
        traceback.print_exc()
        return 0

    class App(mixin, FakeUi):                             # type: ignore[misc]
        pass

    try:
        app = App(case["ports"])
    except Exception as exc:
        print(f"BENCH_FAIL construct: {exc!r}", flush=True)
        return 0
    env = {"app": app, "P": app.ports, "clock": clock, "tmp": tmp,
           "Path": Path, "json": json, "num": num, "norm": norm}
    try:
        exec(compile(case["test"], "<hidden test>", "exec"), env)
    except AssertionError as exc:
        print(f"BENCH_FAIL assert: {exc}", flush=True)
        return 0
    except Exception as exc:                              # noqa: BLE001
        tb = traceback.extract_tb(exc.__traceback__)
        where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                      if Path(f.filename).name == "handlers.py"), "")
        print(f"BENCH_FAIL raised: {type(exc).__name__}: {exc}"
              + (f" (at {where})" if where else ""), flush=True)
        return 0
    print("BENCH_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_case(sys.argv[1]))
