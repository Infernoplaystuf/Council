"""
The Ledger's Extract reads and matches several workbooks at once, in helper
processes (council_core.ledger_workers) - user, 2026-10-30: "write a
multithreading proposal"; JSON between the processes, half the CPU cores
and at most 4 helpers, files Excel opens one at a time in the Ledger's own
process. A review is the same with helpers or without.

Run:  python -m pytest tests/test_ledger_workers.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import asdict
from pathlib import Path

import openpyxl
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import file_guard  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_workers as lw  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.delenv("COUNCIL_LEDGER_WORKERS", raising=False)
    return tmp_path / "app"


@pytest.fixture()
def ledger(tmp_path):
    tpl = lf.make_template(tmp_path / "template.xlsx")
    ps = li.create_parent(tpl)
    return Path(ps.csv), ParentCSV(ps.csv).schema


def _partless(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"], ws["B1"] = "Frequency (kHz)", 55
    ws["A2"], ws["B2"] = "Zorp rate", 7
    wb.save(path)
    return path


def _sources(folder: Path) -> list:
    """Nine workbooks of three kinds, some parts in more than one, and a CSV."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in range(3):
        d = folder / f"sample {n}"
        d.mkdir()
        out += [lf.make_laser_log(d / "laser_log.xlsx"),
                lf.make_nde_book(d / "NDE results.xlsx"),
                lf.make_summary(d / f"summary_{n}.xlsx")]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws["A1"], ws["B1"] = "P/N:", 103
    ws["A2"], ws["B2"] = "Zorp rate", 7                 # a label no rule places
    ws["A3"], ws["B3"] = "Quux depth", 2.5
    wb.save(folder / "notes_103.xlsx")
    out.append(folder / "notes_103.xlsx")
    csv = folder / "settings.csv"
    csv.write_text("Part Number,Power (W)\n101,288\n102,301\n", encoding="utf-8")
    return out + [csv]


def _same(a, b) -> None:
    assert [asdict(o) for o in a.obs] == [asdict(o) for o in b.obs]
    assert [asdict(o) for o in a.unmatched] == [asdict(o) for o in b.unmatched]
    assert [asdict(n) for n in a.images] == [asdict(n) for n in b.images]
    assert a.problems == b.problems
    assert a.read == b.read
    assert a.open_failures == b.open_failures
    assert a.other_words == b.other_words
    assert a.unrelated == b.unrelated
    assert a.index_parts == b.index_parts
    assert a.filled_copies == b.filled_copies
    assert a.model_parts == b.model_parts
    assert [t[0] for t in a.timings] == [t[0] for t in b.timings]
    assert a.cancelled == b.cancelled


# ---------------------------------------------- how many, and which files

def test_half_the_cores_at_most_four_and_only_for_enough_files(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert lw.worker_count(50) == 4
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    assert lw.worker_count(50) == 3
    assert lw.worker_count(lw.MIN_FILES - 1) == 0          # not worth starting them
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert lw.worker_count(50) == 0                         # one helper is no help
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "0")
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert lw.worker_count(50) == 0
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "6")
    assert lw.worker_count(50) == 6
    assert lw.worker_count(5) == 5


def test_files_excel_opens_stay_in_the_ledgers_process(tmp_path):
    book = lf.make_summary(tmp_path / "a.xlsx")
    assert lw.suits(book)
    (tmp_path / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    (tmp_path / "b.xlsb").write_bytes(b"PK\x03\x04")
    (tmp_path / "locked.xlsx").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)  # not a zip
    assert not lw.suits(tmp_path / "old.xls")
    assert not lw.suits(tmp_path / "b.xlsb")
    assert not lw.suits(tmp_path / "locked.xlsx")
    (tmp_path / "n.csv").write_text("a,b\n", encoding="utf-8")
    assert lw.suits(tmp_path / "n.csv")


# ---------------------------------------------- the same review

def test_a_run_with_helpers_is_the_run_without(tmp_path, ledger, monkeypatch):
    _csv, schema = ledger
    srcs = _sources(tmp_path / "src")
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "0")
    alone = se.extract(schema, srcs, use_images=False)
    started = []
    real = lw.Pool.__init__

    def counted(self, *a, **k):
        real(self, *a, **k)
        started.append(self.started)
    monkeypatch.setattr(lw.Pool, "__init__", counted)
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "3")
    helped = se.extract(schema, srcs, use_images=False)
    assert started == [3]
    assert alone.obs and alone.unmatched
    _same(alone, helped)


def test_the_review_workbook_is_the_same(tmp_path, ledger, monkeypatch):
    csv, _schema = ledger
    srcs = _sources(tmp_path / "src")

    def review(n: str):
        monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", n)
        out = tmp_path / f"run_{n}" / "review.xlsx"
        out.parent.mkdir()
        s = li.run_extract(csv, srcs, review_path=out, use_images=False)
        wb = openpyxl.load_workbook(out, read_only=True, data_only=True)
        try:
            here = str(out.parent)
            sheets = {name: [tuple(v.replace(here, "<run>") if isinstance(v, str) else v
                                   for v in r) for r in wb[name].iter_rows(values_only=True)]
                      for name in wb.sheetnames if name not in ("_run", "Read me")}
        finally:
            wb.close()
        return s, sheets
    s0, a = review("0")
    s2, b = review("2")
    assert a == b
    assert (s0.files, s0.values, s0.pre_accepted, s0.to_review) == \
           (s2.files, s2.values, s2.pre_accepted, s2.to_review)


def test_a_workbook_with_no_part_asks_councils_model_in_the_ledgers_process(
        tmp_path, ledger, monkeypatch):
    """The model is loaded in the Ledger's process: a file whose values have
    no part comes back to it, and the model is asked as before."""
    _csv, schema = ledger
    srcs = _sources(tmp_path / "src")
    srcs.insert(4, _partless(tmp_path / "src" / "b_nopart.xlsx"))
    asked = []

    def model(name, grids):
        asked.append((name, threading.get_ident(), len(grids)))
        return "777", "its title"
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "0")
    alone = se.extract(schema, srcs, use_images=False, part_finder=model)
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "2")
    helped = se.extract(schema, srcs, use_images=False, part_finder=model)
    assert [a[0] for a in asked] == ["b_nopart.xlsx", "b_nopart.xlsx"]
    assert asked[0][2] == asked[1][2]
    _same(alone, helped)
    assert helped.model_parts["b_nopart.xlsx"] == ("777", "its title")


# ---------------------------------------------- cancel, and a helper that stops

def test_cancel_while_waiting_for_a_helper_stops_them_and_skips_nothing(
        tmp_path, ledger, monkeypatch):
    _csv, schema = ledger
    srcs = _sources(tmp_path / "src")
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "2")
    cancel = threading.Event()
    real = lw.put
    put = []

    def put_then_cancel(run, part):
        real(run, part)
        put.append(1)
        if len(put) == 2:
            cancel.set()
    monkeypatch.setattr(lw, "put", put_then_cancel)
    run = se.extract(schema, srcs, use_images=False, cancel=cancel)
    assert run.cancelled
    assert len(run.read) == 2                   # the two before it, whole
    assert any(p.startswith("Cancelled by user before") for p in run.problems)
    # what the helpers were reading when they were stopped did not stall
    # anything: it is not put on the skip list by the next read
    file_guard._recovered = False
    file_guard.recover()
    assert file_guard.entries() == []
    assert file_guard._load(file_guard.reading_file()) == {}


def test_a_helper_that_stops_puts_its_file_on_the_skip_list_and_the_run_goes_on(
        tmp_path, ledger, monkeypatch):
    """A helper that crashed reading a file: the file is skipped (said in the
    review, as a file that stalled), a new helper takes the files left."""
    _csv, schema = ledger
    srcs = _sources(tmp_path / "src")
    bad = srcs[3]
    script = tmp_path / "crashing_helper.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(lw.ROOT)!r})
        from council_core import ledger_workers as lw
        real = lw._job
        def job(setup, src, label, send, n):
            if src == {str(bad)!r}:
                os._exit(3)                       # a crash in the middle of it
            return real(setup, src, label, send, n)
        lw._job = job
        sys.exit(lw.main())
    """), encoding="utf-8")
    monkeypatch.setattr(lw, "start_command", lambda: [sys.executable, str(script)])
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "2")
    run = se.extract(schema, srcs, use_images=False)
    assert not run.cancelled
    assert str(bad) not in run.read
    assert len(run.read) == len(srcs) - 1
    assert any(bad.name in p and "the helper reading it stopped" in p for p in run.problems)
    assert (bad.parent.name and file_guard.skipped(bad) is not None)


def test_helpers_that_cannot_start_leave_the_run_to_the_ledger(tmp_path, ledger, monkeypatch):
    _csv, schema = ledger
    srcs = _sources(tmp_path / "src")
    monkeypatch.setattr(lw, "start_command",
                        lambda: [sys.executable, "-c", "import sys; sys.exit(1)"])
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "2")
    run = se.extract(schema, srcs, use_images=False)
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", "0")
    _same(se.extract(schema, srcs, use_images=False), run)


# ---------------------------------------------- JSON, never pickle

def test_what_a_file_gave_goes_as_json_and_comes_back_the_same(tmp_path, ledger):
    _csv, schema = ledger
    src = lf.make_nde_book(tmp_path / "NDE results.xlsx")
    run = se.extract(schema, [src], use_images=False)
    text = json.dumps(lw.run_json(run))
    back = se.Extraction([str(src)], read=[])
    lw.put(back, json.loads(text))
    _same(run, back)


def test_the_messages_are_json_not_pickle():
    import inspect
    src = inspect.getsource(lw)
    assert "pickle" not in src.replace("never pickle", "").replace("not pickle", "")
    assert ".send(" not in src.replace("h.send(", "").replace("def send(", "")
    assert ".recv(" not in src


# ---------------------------------------------- the skip list across processes

def test_two_processes_writing_the_skip_list_at_once_lose_nothing(tmp_path):
    files = []
    for n in range(2):
        d = tmp_path / f"p{n}"
        d.mkdir()
        files.append([d / f"f{i}.txt" for i in range(40)])
        for f in files[-1]:
            f.write_text("x", encoding="utf-8")
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(lw.ROOT)!r})
        from council_core import file_guard
        for p in sys.argv[1:]:
            file_guard.add(p, "test", "Ledger")
            file_guard._note("t-" + p, p, "Ledger")
            file_guard._unnote("t-" + p)
    """)
    env = dict(os.environ)
    procs = [subprocess.Popen([sys.executable, "-c", code, *map(str, fs)], env=env)
             for fs in files]
    for p in procs:
        assert p.wait(timeout=120) == 0
    keys = {e.path for e in file_guard.entries()}
    assert keys == {file_guard._key(f) for fs in files for f in fs}
    assert file_guard._load(file_guard.reading_file()) == {}


def test_forget_pid_crosses_out_only_that_process(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    file_guard._note("one", a, "Ledger")
    data = file_guard._load(file_guard.reading_file())
    data["two"] = {"path": file_guard._key(b), "tab": "Ledger", "session": "other",
                   "pid": 999999, "started": time.time()}
    file_guard._save(file_guard.reading_file(), data)
    file_guard.forget_pid(999999)
    assert set(file_guard._load(file_guard.reading_file())) == {"one"}
    file_guard._unnote("one")


def test_the_built_app_starts_a_helper_without_qt(monkeypatch):
    """The .exe has no python to start: it starts itself with --ledger-worker,
    which reads and nothing else (no window, no Qt). Without the pipe's
    address it stops at once."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COUNCIL_LEDGER_WORKER_")}
    code = ("import sys, runpy; sys.argv = ['council_qt.py', '--ledger-worker']; "
            "ns = runpy.run_path('council_qt.py', run_name='entry'); rc = ns['main'](); "
            "assert 'PySide6' not in sys.modules; sys.exit(rc)")
    p = subprocess.run([sys.executable, "-c", code], cwd=str(lw.ROOT), env=env,
                       timeout=60, capture_output=True, text=True)
    assert p.returncode == 2, p.stderr
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert lw.start_command() == [sys.executable, "--ledger-worker"]
