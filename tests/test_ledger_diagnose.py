"""
The Ledger's "Diagnose a file…" (council_core.ledger_diagnose; user
2026-10-22: "the ledger stalls out and breaks consistently on one excel file.
I'd like there to maybe add an output so we can diagnose the cause"): one
file read in a separate process, a report the user can send back, and the
skip list left as it was.

Run:  python -m pytest tests/test_ledger_diagnose.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from council_core import file_guard as fg  # noqa: E402
from council_core import ledger_diagnose as ld  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from ledger_fixtures import make_laser_log, make_template  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(fg, "_recovered", False)
    return tmp_path / "app"


def test_the_anatomy_names_sheets_pictures_and_sizes(tmp_path):
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    from PIL import Image
    png = tmp_path / "dot.png"
    Image.new("RGB", (4, 4), "red").save(png)
    wb = openpyxl.Workbook()
    wb.active.title = "Readings"
    wb.active["B3"] = 1
    wb.active["D9"] = 2
    wb.active.add_image(XLImage(str(png)), "F2")
    wb.create_sheet("Notes")["A1"] = "x"
    wb.save(tmp_path / "book.xlsx")
    text = "\n".join(ld.anatomy(tmp_path / "book.xlsx"))
    assert "worksheets: 2" in text
    assert "Readings: B3:D9" in text
    assert "Notes: A1" in text
    assert "pictures / media: 1" in text
    assert "picture types: .png 1" in text


def test_the_anatomy_of_files_that_are_not_zips(tmp_path):
    old = tmp_path / "old.xls"
    old.write_bytes(b"\xD0\xCF\x11\xE0" + b"\0" * 100)
    assert any("OLE compound file" in ln for ln in ld.anatomy(old))
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"PK\x03\x04garbage")
    assert any("damaged zip" in ln for ln in ld.anatomy(bad))


def test_a_whole_diagnosis_writes_a_report_and_leaves_the_skip_list(app, tmp_path):
    csv = Path(li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv)
    src = make_laser_log(tmp_path / "laser log.xlsx")
    fg.add(src, "took longer than 180 s to read", "Ledger")
    seen = []
    d = ld.run(src, str(csv), use_images=False, on_line=seen.append, timeout=300)
    report = Path(d.report).read_text(encoding="utf-8")
    assert d.outcome == "finished normally", report
    assert d.last_step == "all steps finished"
    for step in ("the file's anatomy", "table reader", "sample reader",
                 "whole Extract into l.csv"):
        assert f"== step: {step}" in report
    assert "FAILED" not in report
    assert "on the skip list now: yes - took longer than 180 s" in report
    # read past the list, and the list is as it was: still one entry, no note left
    assert [Path(e.path).name for e in fg.entries()] == ["laser log.xlsx"]
    assert not fg.reading_file().exists() or "laser log" not in fg.reading_file().read_text()
    assert any("== step: table reader" in ln for ln in seen)        # shown as it ran
    assert (Path(d.folder) / "review.xlsx").exists()                 # never the user's own
    assert Path(d.folder).parent == app / "logs" / "diagnostics"


def test_a_diagnosis_killed_after_its_time_limit_says_where_it_was(app, tmp_path):
    src = make_laser_log(tmp_path / "book.xlsx")

    def runner(argv, **kw):
        log = Path(argv[-1]).with_name("steps.log")
        log.write_text("[    0.0s] == step: the file's anatomy\n[    0.1s]    ok\n"
                       "[    0.1s] == step: sample reader with pictures\n", encoding="utf-8")
        return SimpleNamespace(returncode=None, timed_out=True, stopped=False, infra="",
                               error="", stderr="", peak_mb=None)
    d = ld.run(src, timeout=5, runner=runner)
    assert d.outcome.startswith("did not finish in 5 s")
    assert d.last_step == "sample reader with pictures"
    assert "last step reached: sample reader with pictures" in Path(d.report).read_text()


def test_a_crash_is_named():
    assert "access violation" in ld.exit_meaning(0xC0000005)
    assert "access violation" in ld.exit_meaning(-1073741819)       # as Windows reports it
    assert "stack overflow" in ld.exit_meaning(0xC00000FD)
    assert ld.exit_meaning(0) == "finished normally"


def test_a_stall_writes_every_threads_stack_to_the_log(tmp_path):
    log = tmp_path / "steps.log"
    code = textwrap.dedent(f"""
        import time
        from council_core import ledger_diagnose as ld
        ld.STALL_S = 0.5
        say = ld._Notes({str(log)!r})
        say("== step: stuck")
        def stuck_here():
            time.sleep(2.0)
        stuck_here()
    """)
    subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), timeout=60, check=True,
                   env={**os.environ, "COUNCIL_APP_DIR": str(tmp_path / "app")})
    text = log.read_text(encoding="utf-8")
    assert "== step: stuck" in text
    assert "stuck_here" in text             # the stack names the function it is stuck in
