"""
council_core.ledger_diagnose — "the Ledger stalls out and breaks consistently
on one Excel file; add an output so we can diagnose the cause and I can report
back on it" (user, 2026-10-22).

The Ledger tab's "Diagnose a file…" reads ONE file the way Extract does, in a
separate Python process, and writes a plain-text report the user can send:

  * the file's anatomy, read from the zip's own directory without opening the
    workbook: its sheets and their used range, pictures, charts, embedded
    objects, external links, pivot caches, macros, and the biggest parts;
  * each step of the read, one at a time, with the time and memory it took:
    the table reader (ledger_records.read_book), the sample reader with its
    pictures (source_extract.load_workbook_grids), then a whole Extract of
    this file into the current parent CSV's schema (the review it writes goes
    in the diagnosis folder, never over the user's own);
  * every thread's stack each time nothing has moved for STALL_S seconds,
    written by faulthandler's own C thread, so it is there even while a C
    call holds the GIL; and the stack of a crash (faulthandler.enable);
  * how the process ended: finished, crashed (with the exit code's name),
    killed after the time limit, or stopped by Cancel; the last step it was
    in; and the tail of the app's own stall log (logs/ledger_stalls.txt).

Because the read runs in its own process, a file that freezes or crashes it
cannot take the app down, and the child's notes are written line by line
(flushed), so they survive a crash. The child never touches the skip list
(council_core.file_guard): it does not read it, add to it or leave a
"reading now" note, so diagnosing a file changes nothing about later runs.
The report says whether the file is on the list now.

Run as a child: python -m council_core.ledger_diagnose <args.json>.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: The repository root: the child runs from it so its imports resolve.
APP_ROOT = Path(__file__).resolve().parent.parent

#: Seconds with no new line before every thread's stack is written again.
STALL_S = 30.0

#: The whole diagnosis is killed after this many seconds
#: (COUNCIL_DIAGNOSE_TIME_LIMIT_S; 0 = no limit).
DEFAULT_TIME_LIMIT_S = 20 * 60

#: How the child marks the start of a step: the report's "last step".
STEP = "== step: "
#: ...and the end of the last one.
DONE = "== done"

#: Windows exit codes a crash ends with, by name.
CRASH_CODES: Dict[int, str] = {
    0xC0000005: "access violation (a native library crashed)",
    0xC00000FD: "stack overflow",
    0xC0000409: "stack buffer overrun / fail-fast (a native library aborted)",
    0xC0000374: "heap corruption",
    0xC0000017: "out of memory",
    0xC000012D: "commit limit reached (out of virtual memory)",
    0xC0000142: "a DLL could not initialise (often out of memory)",
    0xC000001D: "illegal instruction",
    0xC0000094: "integer divide by zero",
    0xE06D7363: "unhandled C++ exception",
    0x80000003: "breakpoint",
    0x40010004: "killed (debugger / taskkill)",
}


def time_limit() -> float:
    raw = os.environ.get("COUNCIL_DIAGNOSE_TIME_LIMIT_S", "").strip()
    try:
        return max(0.0, float(raw)) if raw else float(DEFAULT_TIME_LIMIT_S)
    except ValueError:
        return float(DEFAULT_TIME_LIMIT_S)


def diagnostics_dir() -> Path:
    """<app dir>/logs/diagnostics: one folder per diagnosis."""
    from council_core import paths
    return paths.app_dir() / "logs" / "diagnostics"


def exit_meaning(code: Optional[int]) -> str:
    """A process's exit code in words."""
    if code is None:
        return "no exit code"
    if code == 0:
        return "finished normally"
    if -128 < code < 0:                     # POSIX: killed by a signal
        try:
            import signal
            return f"killed by signal {signal.Signals(-code).name}"
        except (ValueError, AttributeError):
            return f"killed by signal {-code}"
    u = code & 0xFFFFFFFF
    if u in CRASH_CODES:
        return f"crashed: 0x{u:08X} {CRASH_CODES[u]}"
    if u >= 0xC0000000:
        return f"crashed: 0x{u:08X}"
    return f"ended with exit code {code}"


# ── the file's anatomy (no workbook opened) ────────────────────────────
_DIM_RE = re.compile(rb'<(?:\w+:)?dimension\s+ref="([^"]+)"')


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):,.1f} MB"


def anatomy(path) -> List[str]:
    """What is inside an Office file, from its zip directory and the first
    few KB of each sheet: never the whole workbook. Lines of plain text."""
    p = Path(path)
    out: List[str] = []
    try:
        st = p.stat()
    except OSError as e:
        return [f"cannot stat the file: {e}"]
    out.append(f"size on disk: {_mb(st.st_size)} ({st.st_size:,} bytes)")
    out.append(f"modified: {datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}")
    out.append(f"path length: {len(str(p.resolve()))} characters")
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
    except OSError as e:
        return out + [f"cannot open it to read: {e}"]
    if head.startswith(b"\xD0\xCF\x11\xE0"):
        out.append("format: an OLE compound file (an old .xls, or a rights-protected / "
                   "encrypted Office file): read through the installed Excel")
        return out
    if not head.startswith(b"PK"):
        out.append(f"format: not a zip (first bytes {head!r})")
        return out
    try:
        zf = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError) as e:
        return out + [f"format: a damaged zip ({e})"]
    with zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        out.append(f"zip parts: {len(infos):,}, unpacked {_mb(total)}")
        names = [i.filename for i in infos]

        def count(pattern: str) -> List[zipfile.ZipInfo]:
            rx = re.compile(pattern, re.IGNORECASE)
            return [i for i in infos if rx.search(i.filename)]

        groups = [
            ("worksheets", r"^xl/worksheets/sheet\d+\.xml$"),
            ("chart sheets", r"^xl/chartsheets/"),
            ("drawings", r"^xl/drawings/drawing\d+\.xml$"),
            ("vml drawings (comments, form controls)", r"^xl/drawings/vmlDrawing"),
            ("pictures / media", r"/media/"),
            ("charts", r"/charts/chart\d+\.xml$"),
            ("embedded objects (OLE)", r"/embeddings/"),
            ("external links", r"^xl/externalLinks/externalLink"),
            ("pivot caches", r"^xl/pivotCache/pivotCacheDefinition"),
            ("pivot tables", r"^xl/pivotTables/"),
            ("tables", r"^xl/tables/"),
            ("comments", r"^xl/comments\d*\.xml$|threadedComments"),
            ("ActiveX controls", r"activeX"),
            ("custom XML", r"^customXml/"),
            ("macros (vbaProject)", r"vbaProject\.bin$"),
            ("slides", r"^ppt/slides/slide\d+\.xml$"),
        ]
        for label, pattern in groups:
            hit = count(pattern)
            if hit:
                out.append(f"{label}: {len(hit):,} ({_mb(sum(i.file_size for i in hit))})")
        media = count(r"/media/")
        if media:
            kinds: Dict[str, int] = {}
            for i in media:
                ext = Path(i.filename).suffix.lower() or "(none)"
                kinds[ext] = kinds.get(ext, 0) + 1
            out.append("  picture types: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
            big = max(media, key=lambda i: i.file_size)
            out.append(f"  largest picture: {big.filename} {_mb(big.file_size)}")
        for name in ("xl/sharedStrings.xml", "xl/styles.xml", "xl/calcChain.xml"):
            if name in names:
                out.append(f"{name}: {_mb(zf.getinfo(name).file_size)}")
        if "xl/styles.xml" in names:
            try:
                with zf.open("xl/styles.xml") as fh:
                    styles = fh.read(min(zf.getinfo("xl/styles.xml").file_size, 64 * 1024 * 1024))
                for tag in ("cellXfs", "fonts", "fills", "dxfs"):
                    m = re.search(rb"<" + tag.encode() + rb'\s+count="(\d+)"', styles)
                    if m:
                        out.append(f"  {tag}: {int(m.group(1)):,}")
            except Exception as e:                        # noqa: BLE001
                out.append(f"  styles.xml could not be read: {e}")
        sheet_names = _sheet_names(zf, names)
        sheets = count(r"^xl/worksheets/sheet\d+\.xml$")
        if sheets:
            out.append("sheets (name: used range, unpacked size):")
            for i in sorted(sheets, key=lambda i: int(re.search(r"(\d+)", i.filename.rsplit("/", 1)[1]).group(1))):
                ref = "?"
                try:
                    with zf.open(i) as fh:
                        m = _DIM_RE.search(fh.read(4096))
                    if m:
                        ref = m.group(1).decode("ascii", "replace")
                except Exception as e:                    # noqa: BLE001
                    ref = f"unreadable ({e})"
                shown = sheet_names.get(i.filename, i.filename.rsplit("/", 1)[1])
                out.append(f"  {shown}: {ref}, {_mb(i.file_size)}")
        top = sorted(infos, key=lambda i: i.file_size, reverse=True)[:8]
        out.append("biggest parts:")
        out += [f"  {i.filename}: {_mb(i.file_size)} (compressed {_mb(i.compress_size)})"
                for i in top]
    return out


def _sheet_names(zf: zipfile.ZipFile, names: List[str]) -> Dict[str, str]:
    """xl/worksheets/sheetN.xml -> the sheet's name in Excel, or {}."""
    if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        return {}
    try:
        wb = zf.read("xl/workbook.xml")
        rels = zf.read("xl/_rels/workbook.xml.rels")
    except Exception:                                     # noqa: BLE001
        return {}
    target = {m.group(1).decode(): m.group(2).decode()
              for m in re.finditer(rb'<Relationship[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"', rels)}
    target.update({m.group(2).decode(): m.group(1).decode()
                   for m in re.finditer(rb'<Relationship[^>]*\bTarget="([^"]+)"[^>]*\bId="([^"]+)"', rels)})
    out: Dict[str, str] = {}
    for m in re.finditer(rb'<(?:\w+:)?sheet\b[^>]*>', wb):
        tag = m.group(0)
        n = re.search(rb'\bname="([^"]*)"', tag)
        r = re.search(rb'\br:id="([^"]*)"', tag) or re.search(rb'\bid="([^"]*)"', tag)
        if not n or not r:
            continue
        t = target.get(r.group(1).decode(), "")
        t = t.lstrip("/")
        t = t if t.startswith("xl/") else f"xl/{t}"
        out[t] = n.group(1).decode("utf-8", "replace")
    return out


# ── the parent: run the child, write the report ────────────────────────
@dataclass
class Diagnosis:
    file: str
    folder: str
    report: str
    outcome: str
    last_step: str = ""
    seconds: float = 0.0
    lines: List[str] = field(default_factory=list)

    def describe(self) -> str:
        step = f"; last step: {self.last_step}" if self.last_step else ""
        return (f"Diagnosis of {Path(self.file).name}: {self.outcome}{step} "
                f"({self.seconds:.0f} s).\nReport: {self.report}\n"
                "Send that file back when reporting the problem.")


def run(file, csv: str = "", *, use_images: bool = True,
        vision_model: Optional[str] = None, assist: Any = False,
        on_line: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        timeout: Optional[float] = None, runner=None) -> Diagnosis:
    """Diagnose ``file`` in a child process; return where the report is.
    ``csv`` (optional): the parent CSV whose Extract is tried too.
    ``runner``: council_core.child_proc.run, or a stand-in (tests)."""
    from council_core import child_proc
    runner = runner or child_proc.run
    src = Path(file)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.-]+", "_", src.stem)[:40] or "file"
    folder = diagnostics_dir() / f"{stamp}_{safe}"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "steps.log"
    args = {"file": str(src), "csv": csv or "", "use_images": bool(use_images),
            "vision_model": vision_model or "", "assist": assist or False,
            "log": str(log), "folder": str(folder)}
    args_file = folder / "args.json"
    args_file.write_text(json.dumps(args, indent=1), encoding="utf-8")
    limit = time_limit() if timeout is None else timeout
    lines: List[str] = []

    def line(text: str) -> None:
        lines.append(text)
        if on_line is not None:
            on_line(text)

    started = time.monotonic()
    res = runner([sys.executable, "-m", "council_core.ledger_diagnose", str(args_file)],
                 cwd=str(APP_ROOT), env=child_proc.child_env(), timeout=limit or 10 ** 9,
                 retries=0, should_stop=should_stop, on_line=line)
    seconds = time.monotonic() - started
    if getattr(res, "stopped", False):
        outcome = "stopped by Cancel before it finished"
    elif getattr(res, "timed_out", False):
        outcome = (f"did not finish in {limit:g} s: killed (the stacks in the steps "
                   "show where it was)")
    elif getattr(res, "infra", ""):
        outcome = f"the diagnosis itself could not run: {res.infra}"
    elif getattr(res, "error", ""):
        outcome = f"the diagnosis itself could not start: {res.error}"
    else:
        outcome = exit_meaning(getattr(res, "returncode", None))
        if getattr(res, "returncode", None) == 0 and any(
                ln.startswith("   FAILED") for ln in lines):
            outcome = "finished, with a step that failed (see below)"
    try:
        steps = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        steps = "\n".join(lines)
    last = ""
    for ln in steps.splitlines():
        if STEP in ln:
            last = ln.split(STEP, 1)[1].strip()
        elif ln.rstrip().endswith("] " + DONE):
            last = "all steps finished"
    report = folder / "report.txt"
    report.write_text(_report_text(src, csv, outcome, last, seconds, steps, res,
                                   use_images, vision_model, assist),
                      encoding="utf-8")
    return Diagnosis(str(src), str(folder), str(report), outcome, last, seconds, lines)


def _report_text(src: Path, csv: str, outcome: str, last: str, seconds: float,
                 steps: str, res, use_images, vision_model, assist) -> str:
    from council_core import file_guard, stall_watchdog
    out = ["Council - Ledger file diagnosis",
           "=" * 60,
           f"file: {src}",
           f"when: {datetime.now().isoformat(timespec='seconds')}",
           f"outcome: {outcome}",
           f"last step reached: {last or '(none)'}",
           f"took: {seconds:.1f} s",
           f"exit code: {getattr(res, 'returncode', None)}"
           + (f", peak memory {res.peak_mb} MB" if getattr(res, "peak_mb", None) else ""),
           f"parent CSV tried: {csv or '(none: Extract step skipped)'}",
           f"read pictures: {bool(use_images)}; vision model: {vision_model or '(default)'}; "
           f"Council's model asked: {assist or 'no'}",
           f"council version: {_version()}"]
    try:
        entry = file_guard.skipped(src)
        out.append("on the skip list now: " + (f"yes - {entry.reason}" if entry else "no"))
    except Exception as e:                                # noqa: BLE001
        out.append(f"on the skip list now: unknown ({e})")
    out += ["", "System", "-" * 60]
    try:
        out.append(stall_watchdog.system_info())
    except Exception as e:                                # noqa: BLE001
        out.append(f"(system info failed: {e})")
    out += ["", "Steps (from the separate process, line by line)", "-" * 60,
            steps.rstrip() or "(nothing was written: the process did not start)"]
    err = (getattr(res, "stderr", "") or "").strip()
    if err:
        out += ["", "Error output", "-" * 60, err[-20000:]]
    tail = _stall_log_tail()
    if tail:
        out += ["", "The app's own stall log (last lines of logs/ledger_stalls.txt)",
                "-" * 60, tail]
    out.append("")
    return "\n".join(out)


def _version() -> str:
    for name in ("VERSION", "version.txt"):
        p = APP_ROOT / name
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace").strip()[:80]
    head = APP_ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            branch = ref[5:]
            sha = (APP_ROOT / ".git" / branch).read_text(encoding="utf-8").strip()
            return f"{branch.rsplit('/', 1)[-1]} {sha[:10]}"
        return ref[:10]
    except OSError:
        return "unknown"


def _stall_log_tail(lines: int = 150, max_age_days: float = 14) -> str:
    try:
        from council_core import stall_watchdog
        p = Path(stall_watchdog.default_dump_file("ledger_stalls.txt") or "")
        if not p.is_file() or time.time() - p.stat().st_mtime > max_age_days * 86400:
            return ""
        with open(p, "rb") as fh:
            fh.seek(max(0, p.stat().st_size - 256 * 1024))
            text = fh.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:                                     # noqa: BLE001
        return ""


# ── the child ──────────────────────────────────────────────────────────
class _Notes:
    """Lines to stdout (the app shows them) and to the log file, flushed
    one by one so a crash keeps them; each one re-arms the stall dump."""

    def __init__(self, log_path: str):
        self.fh = open(log_path, "a", encoding="utf-8", buffering=1)
        self.t0 = time.monotonic()

    def arm(self) -> None:
        import faulthandler
        try:
            faulthandler.dump_traceback_later(STALL_S, repeat=True, file=self.fh)
        except Exception:                                 # noqa: BLE001
            pass

    def __call__(self, text: str) -> None:
        from council_core import file_guard
        mem = file_guard.process_memory_mb()
        stamp = f"[{time.monotonic() - self.t0:7.1f}s" + (f" {mem:,.0f} MB]" if mem else "]")
        for i, ln in enumerate(str(text).splitlines() or [""]):
            row = f"{stamp} {ln}" if i == 0 else f"{' ' * len(stamp)} {ln}"
            self.fh.write(row + "\n")
            self.fh.flush()
            try:
                print(row, flush=True)
            except Exception:                             # noqa: BLE001
                pass
        self.arm()


def _quiet_file_guard() -> None:
    """The child reads past the skip list and leaves it as it is."""
    from council_core import file_guard
    file_guard.skipped = lambda path: None
    file_guard.add = lambda path, reason, tab="": None
    file_guard._note = lambda token, path, tab: None
    file_guard._unnote = lambda token: None
    file_guard.recover = lambda: []
    os.environ["COUNCIL_FILE_TIME_LIMIT_S"] = "0"
    os.environ["COUNCIL_FILE_MEMORY_LIMIT_MB"] = "0"
    os.environ["COUNCIL_FILE_UNPACKED_LIMIT_MB"] = "0"


def _step(say: _Notes, title: str, fn: Callable[[], Optional[str]]) -> None:
    say(STEP + title)
    t = time.monotonic()
    try:
        got = fn()
        say(f"   ok in {time.monotonic() - t:.1f} s" + (f": {got}" if got else ""))
    except BaseException as e:                            # noqa: BLE001
        say(f"   FAILED after {time.monotonic() - t:.1f} s: {e!r}")
        say(traceback.format_exc().rstrip())
        if isinstance(e, KeyboardInterrupt):
            raise


def child_main(args_file: str) -> int:
    import faulthandler
    args = json.loads(Path(args_file).read_text(encoding="utf-8"))
    say = _Notes(args["log"])
    faulthandler.enable(file=say.fh, all_threads=True)
    say.arm()
    src = Path(args["file"])
    say(f"diagnosing {src}")
    say(f"python {sys.version.split()[0]}, pid {os.getpid()}")
    _step(say, "the file's anatomy (zip directory only)",
          lambda: "\n" + "\n".join(anatomy(src)))
    _quiet_file_guard()
    notes: List[str] = []

    def table_reader() -> str:
        from council_core import ledger_records
        sheets = ledger_records.read_book(src, notes)
        cells = sum(len(getattr(s, "cells", {}) or {}) for s in sheets)
        return f"{len(sheets)} sheet(s), {cells:,} cells"

    def sample_reader() -> str:
        import source_extract
        grids, images = source_extract.load_workbook_grids(src, notes)
        return f"{len(grids)} grid(s), {len(images)} picture(s)"

    _step(say, "table reader (ledger_records.read_book)", table_reader)
    _step(say, "sample reader with pictures (source_extract.load_workbook_grids)",
          sample_reader)
    if notes:
        say("reader notes:\n" + "\n".join(f"  {n}" for n in notes[-40:]))
    csv = args.get("csv") or ""
    if csv and Path(csv).is_file():
        def whole_extract() -> str:
            from council_core import ledger_ingest
            reader = None
            if args.get("use_images") and args.get("vision_model"):
                from source_extract import ollama_vision_reader
                reader = ollama_vision_reader(args["vision_model"])
            s = ledger_ingest.run_extract(
                csv, [str(src)], str(Path(args["folder"]) / "review.xlsx"),
                use_images=bool(args.get("use_images")), image_reader=reader,
                progress=lambda msg, frac=None: say(f"   · {msg}"), log=say,
                assist=args.get("assist") or False)
            problems = "\n".join(f"  {p}" for p in list(s.problems)[:40])
            return (f"{s.values} values, {s.to_review} to review, images "
                    f"{s.images_read}/{s.images}, {len(s.problems)} problem(s)"
                    + (f"\n{problems}" if problems else ""))
        _step(say, f"whole Extract into {Path(csv).name} (review in the diagnosis folder)",
              whole_extract)
    else:
        say("no parent CSV picked: the whole-Extract step is skipped")
    say(DONE)
    faulthandler.cancel_dump_traceback_later()
    return 0


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1]))
