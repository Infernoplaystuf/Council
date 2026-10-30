"""
council_core.ledger_workers — the Ledger's Extract reads and matches several
workbooks at once, in helper processes.

User 2026-10-30: "Does any of the app have multithreading" and "write a
multithreading proposal"; agreed: results come back as JSON, half the CPU
cores and at most 4 helpers, files Excel opens stay one at a time.

Measured first (83 synthetic workbooks, 57 s): reading the files was 4 s and
matching their values to the template 23 s, all in one Python thread. Python
runs one thread's code at a time, so threads would not have helped (16 logs
of 3,000 rows: 12.6 s in one thread, 15.1 s in 4, 4.4 s in 4 processes).

What a helper does is exactly what the Ledger does in its own process,
``source_extract._extract_one``, on a fresh ``Extraction`` of that one file;
the Ledger puts the files' results into the run in the order of the files
(``put``), so a review is the same with helpers or without. A file is done in
the Ledger's own process, as before, when:

  * Excel opens it (.xls, .xlsb, rights-protected, anything openpyxl cannot
    open): one Excel, one file at a time;
  * its values have no part and Council's model is to be asked (the model is
    loaded in the Ledger's process);
  * its helper stopped while it was being read (the file is put on the skip
    list first, so it is not read again here: council_core.file_guard).

Messages between the two are JSON (send_bytes over a local pipe of
multiprocessing.connection: never pickle). The helpers are plain Python
started as ``python -c`` (``Council.exe --ledger-worker`` in the built app:
council_qt.py), and stop when the Ledger closes its end.

COUNCIL_LEDGER_WORKERS: how many (0 or 1: none). Default: half the CPU
cores, at most 4, and only for a run of at least MIN_FILES files a helper
can take.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]

#: a run of fewer files a helper can take is read in the Ledger's process:
#: starting a helper (Python and openpyxl, ~1-2 s on Windows) is about one
#: small workbook's read
MIN_FILES = 4
MAX_WORKERS = 4
#: how many files past the one the Ledger waits for may be read meanwhile
AHEAD = 64
#: the workbook kinds openpyxl reads; any other is Excel's (or no workbook)
_ZIP_BOOKS = (".xlsx", ".xlsm", ".xltx", ".xltm")
_EXCEL_ONLY = (".xls", ".xlsb")


def worker_count(n_files: int) -> int:
    """How many helpers for a run of ``n_files`` they can take: 0 = none."""
    raw = os.environ.get("COUNCIL_LEDGER_WORKERS", "").strip()
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = -1
    else:
        n = -1
    if n < 0:
        n = min(MAX_WORKERS, (os.cpu_count() or 2) // 2)
    if n < 2 or n_files < MIN_FILES:
        return 0
    return min(n, n_files)


def suits(src) -> bool:
    """Whether a helper can read ``src`` (Excel's files stay in the Ledger's
    process, one at a time)."""
    p = Path(src)
    suf = p.suffix.lower()
    if suf in _EXCEL_ONLY:
        return False
    if suf in _ZIP_BOOKS:
        try:
            import excel_open
            return p.exists() and excel_open.kind_of(p) == "zip"
        except Exception:                                   # noqa: BLE001
            return False
    return True


# ── JSON for what one file gave ─────────────────────────────────────────
def _obs_json(o) -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(o)


def _obs(d: Dict[str, Any]):
    from source_extract import Obs
    d = dict(d)
    d["guesses"] = [tuple(g) for g in d.get("guesses") or []]
    return Obs(**d)


def run_json(run) -> Dict[str, Any]:
    """One file's Extraction (a fresh one, only that file's) as JSON."""
    from dataclasses import asdict
    return {
        "obs": [_obs_json(o) for o in run.obs],
        "unmatched": [_obs_json(o) for o in run.unmatched],
        "images": [asdict(n) for n in run.images],
        "problems": list(run.problems),
        "open_failures": [list(x) for x in run.open_failures],
        "timings": [list(x) for x in run.timings],
        "read": list(run.read or []),
        "filled_copies": [[rows, where] for rows, where in run.filled_copies],
        "other_words": dict(run.other_words),
        "unrelated": list(run.unrelated),
        "index_parts": [[k, list(v)] for k, v in run.index_parts.items()],
    }


def put(run, part: Dict[str, Any]) -> None:
    """One file's result, from ``run_json``, into the run: as
    _extract_one would have put it there itself."""
    from source_extract import ImageNote
    run.obs.extend(_obs(d) for d in part["obs"])
    run.unmatched.extend(_obs(d) for d in part["unmatched"])
    run.images.extend(ImageNote(**d) for d in part["images"])
    run.problems.extend(part["problems"])
    run.open_failures.extend(tuple(x) for x in part["open_failures"])
    run.timings.extend((str(a), float(b)) for a, b in part["timings"])
    if run.read is not None:
        run.read.extend(part["read"])
    run.filled_copies.extend((rows, where) for rows, where in part["filled_copies"])
    run.other_words.update(part["other_words"])
    run.unrelated.extend(part["unrelated"])
    for k, v in part["index_parts"]:
        run.index_parts.setdefault(k, tuple(v))


def config_json(schema, aliases, ignore, taught, explanations, pattern_mask, part,
                pattern_strict, image_dir, images: str) -> Dict[str, Any]:
    """What a helper needs to read a file as extract() would: the Matcher's
    makings, not the Matcher (it is built again from them)."""
    from dataclasses import asdict
    return {"schema": asdict(schema), "aliases": dict(aliases or {}),
            "ignore": list(ignore or ()), "taught": asdict(taught) if taught else None,
            "explanations": [asdict(e) for e in explanations or ()],
            "pattern_mask": pattern_mask or "", "part": part or "",
            "pattern_strict": bool(pattern_strict),
            "image_dir": str(image_dir) if image_dir else "", "images": images}


# ── the helper ──────────────────────────────────────────────────────────
class NeedsLedger(BaseException):
    """This file is the Ledger's own to read (Excel, or Council's model).
    A BaseException: the reads' own ``except Exception`` must let it by."""


class _Setup:
    def __init__(self, cfg: Dict[str, Any]):
        import re as _re  # noqa: F401  (source_extract imports it too)
        import source_extract as se
        from sample_ledger import TemplateField, TemplateSchema
        sd = dict(cfg["schema"])
        sd["fields"] = [TemplateField(**f) for f in sd["fields"]]
        self.schema = TemplateSchema(**sd)
        taught = se.Taught(**cfg["taught"]) if cfg.get("taught") else None
        self.m = se.Matcher(self.schema, cfg["aliases"], cfg["ignore"], taught=taught)
        if cfg.get("explanations"):
            import docs_open
            self.m.use_explanations([docs_open.Explanation(**e)
                                     for e in cfg["explanations"]])
        from council_core import ledger_examples
        try:
            self.m.layout = ledger_examples.template_layout(
                getattr(self.schema, "template_path", ""))
        except Exception:                                   # noqa: BLE001
            self.m.layout = {}
        self.codes = se.ItemCodes(self.schema.items)
        mask = cfg.get("pattern_mask") or ""
        self.mask_re = se.mask_to_regex(mask) if mask else None
        self.part = cfg.get("part") or ""
        self.strict = bool(cfg.get("pattern_strict", True))
        self.image_dir = cfg.get("image_dir") or None
        self.reader = se.default_image_reader() if cfg.get("images") == "default" else None
        self.asks_model = False


class _Steps:
    """The progress a file's read reports: what it advanced, said again by
    the Ledger as the file's result is put in."""

    def __init__(self, beat: Callable[[], None]):
        self.done: List[str] = []
        self._beat = beat

    def stage(self, *a, **k) -> None:
        self._beat()

    def update(self, *a, **k) -> None:
        self._beat()

    def advance(self, n: int = 1, what: str = "") -> None:
        self.done.append(what)


class _Beat:
    def __init__(self, beat):
        self.beat = beat


def _job(setup: _Setup, src: str, label: str, send: Callable[[Dict[str, Any]], None],
         job: int) -> Dict[str, Any]:
    import source_extract as se
    last = [0.0]

    def beat() -> None:
        now = time.monotonic()
        if now - last[0] >= 1.0:
            last[0] = now
            send({"beat": job})
    steps = _Steps(beat)
    run = se.Extraction([src], read=[], pattern_given=setup.mask_re is not None)
    here = {"what": ""}

    def ask_model(_name, _grids):
        raise NeedsLedger("Council's model is asked which part it is")
    se._extract_one(Path(src), run, steps, _Beat(beat), beat, here, None,
                    setup.image_dir, setup.reader, setup.m, setup.codes, setup.mask_re,
                    setup.part, setup.schema, time, label,
                    ask_model if setup.asks_model else None, mask_strict=setup.strict)
    return {"done": job, "part": run_json(run), "steps": steps.done}


def serve(conn) -> None:
    """A helper's loop: the config, then one file per message, until the
    Ledger closes its end."""
    import source_extract as se

    def no_excel(path, *_a, **_k):
        raise NeedsLedger(f"{Path(path).name} is opened through Excel")
    se._grids_from_excel = no_excel             # one Excel, in the Ledger's process

    def send(msg: Dict[str, Any]) -> None:
        conn.send_bytes(json.dumps(msg).encode("utf-8"))
    cfg = json.loads(conn.recv_bytes().decode("utf-8"))
    setup = _Setup(cfg["config"])
    setup.asks_model = bool(cfg.get("asks_model"))
    send({"ready": os.getpid()})
    threads = threading.active_count()
    while True:
        try:
            msg = json.loads(conn.recv_bytes().decode("utf-8"))
        except (EOFError, OSError):
            return
        job = msg["job"]
        try:
            reply = _job(setup, msg["src"], msg["label"], send, job)
        except NeedsLedger as e:
            reply = {"ledger": job, "why": str(e)}
        except Exception as e:                              # noqa: BLE001
            import traceback
            reply = {"ledger": job, "why": f"the helper failed: {e!r}",
                     "trace": traceback.format_exc()[-4000:]}
        # a read file_guard left behind still runs on its thread: this helper
        # is replaced, so it does not go on taking a core
        reply["spent"] = threading.active_count() > threads
        send(reply)


def main() -> int:
    """The helper's entry: ``python -c`` (start_command) or
    ``Council.exe --ledger-worker``. The pipe's address and key come in the
    environment, never on the command line."""
    from multiprocessing.connection import Client
    address = os.environ.get("COUNCIL_LEDGER_WORKER_ADDRESS", "")
    key = bytes.fromhex(os.environ.get("COUNCIL_LEDGER_WORKER_KEY", ""))
    if not address or not key:
        return 2
    try:
        conn = Client(address, authkey=key)
    except OSError:
        return 3
    try:
        serve(conn)
    finally:
        conn.close()
    return 0


def start_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ledger-worker"]
    return [sys.executable, "-c",
            "import sys; sys.path.insert(0, %r); "
            "from council_core.ledger_workers import main; sys.exit(main())" % str(ROOT)]


# ── the Ledger's side ───────────────────────────────────────────────────
class _Helper:
    def __init__(self, proc: subprocess.Popen, conn, inbox: "queue.Queue"):
        self.proc, self.conn = proc, conn
        self.job: Optional[int] = None
        self.pid = proc.pid
        self.alive = True
        self._t = threading.Thread(target=self._listen, args=(inbox,), daemon=True,
                                   name=f"ledger-helper-{proc.pid}")
        self._t.start()

    def _listen(self, inbox: "queue.Queue") -> None:
        while True:
            try:
                raw = self.conn.recv_bytes()
            except (EOFError, OSError):
                inbox.put((self, None))
                return
            try:
                inbox.put((self, json.loads(raw.decode("utf-8"))))
            except ValueError:
                inbox.put((self, None))
                return

    def send(self, msg: Dict[str, Any]) -> bool:
        try:
            self.conn.send_bytes(json.dumps(msg).encode("utf-8"))
            return True
        except (OSError, ValueError):
            return False

    def stop(self) -> None:
        self.alive = False
        try:
            self.conn.close()
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            pass


class Pool:
    """``n`` helpers reading ``jobs`` ((path, label) each) ahead, in order.
    ``result(i)`` waits for job i: its JSON result, or None when the Ledger
    is to read that file itself."""

    def __init__(self, config: Dict[str, Any], jobs: Sequence[Tuple[str, str]], n: int,
                 asks_model: bool = False, start_timeout: float = 60.0):
        from multiprocessing.connection import Listener
        self.jobs = list(jobs)
        self.results: Dict[int, Optional[Dict[str, Any]]] = {}
        self.steps: Dict[int, List[str]] = {}
        self._next = 0
        self._want = 0
        self._inbox: "queue.Queue" = queue.Queue()
        self._key = secrets.token_bytes(32)
        self._listener = Listener(authkey=self._key)           # a local pipe
        self._hello = {"config": config, "asks_model": bool(asks_model)}
        self._start_timeout = start_timeout
        self.helpers: List[_Helper] = []
        self.started = 0
        for _ in range(n):
            if not self._start_one():
                break

    def _start_one(self) -> bool:
        env = dict(os.environ)
        env["COUNCIL_LEDGER_WORKER_ADDRESS"] = str(self._listener.address)
        env["COUNCIL_LEDGER_WORKER_KEY"] = self._key.hex()
        env.pop("COUNCIL_LEDGER_WORKERS", None)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(start_command(), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags)
        except OSError:
            return False
        conn = self._accept(proc)
        if conn is None:
            try:
                proc.kill()
            except OSError:
                pass
            return False
        try:
            conn.send_bytes(json.dumps(self._hello).encode("utf-8"))
        except (OSError, ValueError):
            conn.close()
            proc.kill()
            return False
        h = _Helper(proc, conn, self._inbox)
        self.helpers.append(h)
        self.started += 1
        return True

    def _accept(self, proc: subprocess.Popen):
        """The helper's connection, or None when it did not come (it could
        not start, or failed before connecting)."""
        box: Dict[str, Any] = {}

        def accept() -> None:
            try:
                box["conn"] = self._listener.accept()
            except Exception as e:                          # noqa: BLE001
                box["error"] = e
        t = threading.Thread(target=accept, daemon=True, name="ledger-helper-accept")
        t.start()
        end = time.monotonic() + self._start_timeout
        while t.is_alive() and time.monotonic() < end:
            if proc.poll() is not None:
                break
            t.join(0.1)
        if "conn" in box:
            return box["conn"]
        if t.is_alive():
            # unblock the accept: a connection of our own, closed at once
            try:
                from multiprocessing.connection import Client
                Client(self._listener.address, authkey=self._key).close()
            except Exception:                               # noqa: BLE001
                pass
            t.join(2)
            c = box.get("conn")
            if c is not None:
                c.close()
        return None

    def _live(self) -> List[_Helper]:
        return [h for h in self.helpers if h.alive]

    def _dispatch(self) -> None:
        # read ahead of the file the Ledger waits for, not without end: the
        # results wait in memory until their turn. Far enough that a big
        # file further on starts while one before it is still read (3 per
        # helper kept a 6 s log waiting behind a 10 s one)
        ahead = self._want + AHEAD
        for h in self._live():
            if h.job is None and self._next < len(self.jobs) and self._next <= ahead:
                i = self._next
                self._next += 1
                src, label = self.jobs[i]
                h.job = i
                if not h.send({"job": i, "src": src, "label": label}):
                    self._lost(h)

    def _lost(self, h: _Helper) -> None:
        """A helper that stopped: the file it held is the Ledger's (on the
        skip list first when it stopped while reading it), and a new helper
        takes its place while files are left."""
        i = h.job
        h.job = None
        if h.alive:
            h.stop()
        if i is not None and i not in self.results:
            src = self.jobs[i][0]
            try:
                from council_core import file_guard
                file_guard.add(src, "the helper reading it stopped (it crashed or ran out "
                                    "of memory)", "Ledger")
                file_guard.forget_pid(h.pid)
            except Exception:                               # noqa: BLE001
                pass
            self.results[i] = None
        if self._next < len(self.jobs) and not self._live():
            self._start_one()

    def result(self, i: int, beat: Callable[[], None] = lambda: None,
               should_stop: Callable[[], bool] = lambda: False) -> Optional[Dict[str, Any]]:
        """Job i's result (None: the Ledger reads it). Raises ``Stopped``
        when ``should_stop()`` says so while it waits."""
        self._want = i
        self._dispatch()
        while i not in self.results:
            if should_stop():
                raise Stopped()
            if not self._live():
                # every helper gone: the files left are the Ledger's
                for j in range(len(self.jobs)):
                    self.results.setdefault(j, None)
                break
            try:
                h, msg = self._inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg is None:
                if h.alive or h.job is not None:
                    self._lost(h)
                self._dispatch()
                continue
            beat()
            if "beat" in msg or "ready" in msg:
                continue
            j = msg.get("done", msg.get("ledger"))
            if j is None:
                continue
            self.results[j] = msg.get("part") if "done" in msg else None
            self.steps[j] = list(msg.get("steps") or [])
            h.job = None
            if msg.get("spent"):
                h.stop()
                if self._next < len(self.jobs):
                    self._start_one()
            self._dispatch()
        return self.results.pop(i)

    def close(self) -> None:
        """Every helper stopped; what they were reading is crossed out (a
        Cancel: those reads did not stall anything)."""
        for h in self.helpers:
            if h.alive:
                h.stop()
            try:
                from council_core import file_guard
                file_guard.forget_pid(h.pid)
            except Exception:                               # noqa: BLE001
                pass
        try:
            self._listener.close()
        except OSError:
            pass


class Stopped(Exception):
    """A Cancel while the Ledger waited for a helper."""


def start(config: Dict[str, Any], jobs: Sequence[Tuple[str, str]],
          asks_model: bool = False) -> Tuple[Optional[Pool], Dict[str, int]]:
    """A pool for the jobs ((path, label) each, in order) a helper can take,
    and each one's job number by path; (None, {}) when helpers are off, too
    few files suit them, or none started."""
    mine: List[Tuple[str, str]] = []
    index: Dict[str, int] = {}
    for src, label in jobs:
        if suits(src):
            index[str(src)] = len(mine)
            mine.append((str(src), label))
    n = worker_count(len(mine))
    if not n:
        return None, {}
    try:
        pool = Pool(config, mine, n, asks_model=asks_model)
    except Exception:                                       # noqa: BLE001
        return None, {}
    if not pool.started:
        pool.close()
        return None, {}
    return pool, index
